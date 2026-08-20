from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from dotenv import load_dotenv

from packages.common.config import DEFAULT_GPT_WRITER_MODEL, DEFAULT_OPENAI_BASE_URL
from packages.common.paths import get_paths
from packages.meme_scanner import context_search
from packages.meme_scanner import fxtwitter_search
from packages.meme_scanner import gmgn_narrative
from packages.meme_scanner import grok_search as grok_x_search

PATHS = get_paths()
CONFIG_DIR = PATHS.config_dir
EXPORTS_DATA_DIR = PATHS.exports_dir


def load_project_env() -> None:
    load_dotenv()


def post_json(path: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    base_url = os.environ.get("ODAILY_LLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_OPENAI_BASE_URL
    api_key = os.environ.get("ODAILY_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing OdAIly LLM API key")
    response = requests.post(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


DEFAULT_GPT_MODEL = DEFAULT_GPT_WRITER_MODEL
DEFAULT_GROK_MODEL = grok_x_search.DEFAULT_MODEL
DEFAULT_OUTPUT_DIR = EXPORTS_DATA_DIR / "narrative"
GROK_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
READER_OPENING = "据Odaily Meme速递监测，"
READER_DISCLAIMER = "「Meme 速递」由 Odaily 独家 AI 模型筛选社区热议潜力标的。内容基于公开信息整理，不构成投资建议，请自行甄别并注意 Meme 币高波动风险。"
LEGACY_READER_DISCLAIMERS = (
    "以上内容均基于公开内容整理，真实性仍需读者自行鉴别，Meme 币价格波动较大，请注意资产保护。",
    "以上内容均根据公开渠道整理，真实性仍需读者自行鉴别，Meme 币价格波动较大，请注意资产保护。",
)
GROK_SUPPLEMENT_MARKER = "Grok补充："
GMGN_SUPPLEMENT_MARKER = "GMGN补充："
# A production probe accepted two simultaneous x_search requests. Keep the
# cap local to this worker so a proxy failure cannot fan out without bound.
GROK_REQUEST_SLOTS = threading.BoundedSemaphore(2)

# These are the numeric senders behind the bot examples supplied for this pipeline.
# They are used only in memory to remove the messages before any source material is saved.
EXCLUDED_TELEGRAM_BOT_SENDER_IDS = frozenset({6126376117, 7178305557, 7913738110})
READER_TEXT_FORBIDDEN = re.compile(
    r"这里不能写成|不应延伸为|不应写成|只能写为|只能视作|"
    r"项目页面(?:将|称)|项目页(?:将|称)|材料(?:里|中)(?:显示|表明)|"
    r"Grok 找到|Grok\s*(?:还称|又称)|X Search 找到|当前材料(?:显示|表明)|"
    r"有人把它说成|X\s*上常见词|围绕几个说法|几条线|这个角度|"
    r"把.{1,40}(?:连到|归到).{1,40}|拿.{1,40}来聊|"
    r"(?:把|将).{1,60}(?:与|和).{1,60}(?:联系|关联)(?:在一起|起来)?|"
    r"\b(?:fr\s+fr|for\s+real)\b|\bwallet\s+gang\b|something.?s\s+cooking|"
    r"有庄|庄家.{0,12}钱包|老外(?:庄|真?来(?:了)?)|聪明钱(?:来|进)|鲸鱼(?:来|进)",
    re.IGNORECASE,
)


class NarrativeStageError(RuntimeError):
    def __init__(self, stage: str, error: BaseException) -> None:
        self.stage = stage
        self.original_error = error
        super().__init__(str(error) or error.__class__.__name__)


def setup_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def extract_json_object(text: str, label: str) -> dict[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.IGNORECASE)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} did not return a JSON object: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{label} did not return a JSON object.")
    return parsed


def reported_token_usage(data: Any) -> dict[str, int]:
    """Keep only provider-reported token counters; absent counters stay absent."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return {}
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    result: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int):
                result[target] = value
                break
    return result


def performance_entry(stage: str, started_at: float, response_data: Any = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"stage": stage, "duration_ms": round((time.perf_counter() - started_at) * 1000)}
    tokens = reported_token_usage(response_data)
    if tokens:
        entry["tokens"] = tokens
    return entry


def chat_completion_json_with_metrics(prompt: str, *, model: str, timeout: int) -> tuple[dict[str, Any], dict[str, Any]]:
    started_at = time.perf_counter()
    load_project_env()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "Return only a valid JSON object. Do not use Markdown fences.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    data = post_json("chat/completions", payload, timeout)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("GPT response has no chat-completion content.") from exc
    if not isinstance(content, str):
        raise RuntimeError("GPT response content is not text.")
    return extract_json_object(content, "GPT"), performance_entry("", started_at, data)


def chat_completion_json(prompt: str, *, model: str, timeout: int) -> dict[str, Any]:
    return chat_completion_json_with_metrics(prompt, model=model, timeout=timeout)[0]


def post_grok_response(
    *,
    base_url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int,
) -> tuple[requests.Response, int]:
    """Retry a transient xAI failure once; callers retain a diagnostic on failure."""
    with GROK_REQUEST_SLOTS:
        response: requests.Response | None = None
        for attempt in range(1, 3):
            response = requests.post(
                f"{base_url}/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            if response.status_code not in GROK_TRANSIENT_STATUS_CODES or attempt == 2:
                return response, attempt
            time.sleep(2)
    raise AssertionError("unreachable")


def build_telegram_extraction_prompt(contract: str, contexts: list[dict[str, Any]]) -> str:
    return f"""你是消息素材提取器。目标代币的精确 CA 是 {contract}。

只从下方 Telegram 上下文提取“有人拿什么来源、字眼、事件、身份或具体说法谈这个代币”。
不要查询、补充或解释任何背景；不要判断真假、价值、走势或是否值得写。机器人消息已经在输入前
删除。只保留具体说法，不要保留单纯喊单、价格、市值、买卖诱导、自动数据卡片或重复 CA。

返回 JSON：
{{
  "telegram_claims":[{{"id":"tg:<chat_title>:<message_id>","chat_title":"","message_id":0,
  "speaker":"","quote":"原话短引","claim":"该消息明确提出的说法"}}],
  "entity_candidates":[{{"id":"entity:1","entity":"消息中出现的具名人物或账号",
  "action":"其明确动作","message_ids":[0]}}]
}}

规则：每条 claim 必须能对应一条输入消息；quote 必须是该消息的短引；没有可用素材就返回空数组。
claim 只改写消息实际说出的内容，不要写“把 A 连到 B”“把 A 归为 B”“拿 A 来聊”这类
提取者的解释；原话含糊时保留短引或不提取。这是素材提取，不是读者文案，不要去重或裁决冲突。

`entity_candidates` 只做机械提取：仅当消息出现了具名人物/账号，并且同时说了其明确动作
（关注、发帖、转发、回复、点赞等）时才返回。不要判断此人是谁、是否重要或动作是否利好；
不得把“dev”“him”“鲸鱼”或匿名群友列为候选。最多返回 3 个候选，每个候选的 entity 必须
在对应消息原文中出现。

Telegram 上下文：
{json.dumps(contexts, ensure_ascii=False)}"""


def build_grok_research_prompt(contract: str, chain: str) -> str:
    return f"""在 X 上研究链 {chain}、精确 CA 为 {contract} 的 Meme 代币。目标不是判断
真实性、价值或涨跌，而是找出可直接回答“炒作者拿什么来源、字眼、事件、身份或具体说法
去炒它”的材料。不要访问或引用项目官网、区块浏览器、行情站、媒体页。

返回且只返回 JSON：
{{
  "x_claims":[{{"id":"x:1","author":"@handle","text":"原帖原文","url":"https://x.com/...",
  "timestamp":"ISO 时间或空字符串","claim":"这条原帖实际说出的具体材料"}}],
  "grok_claims":[{{"id":"grok:1","claim":"Grok 的具体研究结论"}}]
}}

`x_claims` 只收录本次实际找到、且带可定位 X URL 的原帖；绝不编造作者、原文、URL 或时间。
没有这类原帖时，`x_claims` 为空。Grok 的可用结论放进 `grok_claims`，最终会明确归为 Grok。
一条材料必须是：直接来源、明确说明为什么会炒的角度，或解释人物/动作为何重要的背景。
排除机器人、扫描器、排行榜、自动广告、纯 CA、价格/市值、买卖建议、泛泛喊单、纯疑问、
市场状态、鲸鱼/新钱包状态，以及“community led”“ape culture”“viral ticker”这类没有具体
人物、动作或来源的泛词。不要输出读者文案、泛词清单或市场趋势概括。"""


def build_entity_lookup_prompt(contract: str, chain: str, candidates: list[dict[str, Any]]) -> str:
    return f"""只使用 X Search。链 {chain}、精确 CA {contract} 的 Telegram 素材提到以下
人物/账号及动作。逐一查找其身份，及其与相关项目或动作的直接关系，目的是解释该动作为什么
会被炒作。不要搜索其他人物，不要扩展成代币行情、社区趋势或泛词研究。

候选：
{json.dumps(candidates, ensure_ascii=False)}

返回且只返回 JSON：
{{"entity_supplements":[{{"candidate_id":"entity:1","id":"grok:entity:1",
"claim":"可直接写成‘Grok 指出，X 是 Y’的简短身份或关系结论"}}]}}

找不到可用结论的候选不要返回。结论即使没有 X 原帖链接也可返回，但不能编造不相关身份。
不要列 URL、原帖摘要、价格、市场趋势、泛词或读者文案。"""


def build_telegram_entity_prompt(contract: str, contexts: list[dict[str, Any]]) -> str:
    return f"""You only mechanically extract named-entity candidates from Telegram material for exact CA {contract}.
The original Telegram messages will be passed through unchanged to the final writer. Do not generate claims, summaries, role hints, or rewrites.
Return only JSON: {{"entity_candidates":[{{"id":"entity:1","entity":"named person or account exactly as written","action":"explicit action","message_ids":[0]}}]}}.
Return a candidate only when the same message contains a named person/account and a concrete action such as followed, posted, reposted, replied, or liked. Do not judge identity, importance, or whether the action matters. Never emit dev, him, whales, or anonymous chat members. At most three candidates; entity must appear verbatim in the referenced source message.
Telegram contexts:\n{json.dumps(contexts, ensure_ascii=False)}"""


def build_grok_research_prompt_v2(contract: str) -> str:
    return f"""Independently research the meme token identified only by this exact CA: {contract}
Use X Search. Do not read Telegram material and do not use a chain, token name, person, or any other extra lead as input. The task is to identify the concrete source, wording, event, identity, or claim people use to hype it. Do not assess truth, value, price, or tradeability. Do not use project pages, explorers, market sites, or media pages.
You may state a research conclusion even without an original-post link; it will be attributed to Grok and must never be presented as an X post.
Return only JSON: {{"source_actions":[{{"id":"grok:source:1","actor":"original named X account or person","action":"posted|reposted|quoted|replied|liked|mentioned","date":"YYYY-MM-DD or empty string","quote":"the exact short source wording","url":"https://x.com/... or empty string"}}],"narrative_materials":[{{"id":"grok:narrative:1","statement":"specific narrative statement"}}],"supplemental_information":[{{"id":"grok:supplement:1","statement":"specific identity, event, or relationship that helps explain an action"}}],"type_hypothesis":"pure_meme|celebrity_anchor|app_linked|"}}.
`source_actions` is only for an original X action. Do not turn a community repost or a Grok inference into a source action; leave it empty if the original action cannot be identified. Preserve actor, action, date, and short quote exactly; include a URL when X Search returned one, otherwise leave URL empty rather than inventing it.
Every item must be concrete. Use empty arrays and an empty string when unavailable. Do not produce reader copy, price/market-cap claims, trade advice, generic hype, pure questions, market state, whale/new-wallet activity, vague unnamed-role movement (“wallet gang”, dealers, foreigners, smart money, whales arrived), `fr fr`/`for real`, or unsupported buzzwords. Named-person actions such as “Frank arrived” may be retained."""


def normalize_grok_materials(value: Any, *, prefix: str, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    materials: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        statement = str(item.get("statement") if isinstance(item, dict) else "").strip()
        if statement and not re.search(r"no qualifying|no usable narrative", statement, re.IGNORECASE):
            materials.append({"id": str(item.get("id") if isinstance(item, dict) else "") or f"{prefix}:{index}", field: statement})
    return materials


def normalize_grok_source_actions(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    actions: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            continue
        actor = str(item.get("actor") or "").strip()
        action = str(item.get("action") or "").strip()
        quote = str(item.get("quote") or "").strip()
        url = str(item.get("url") or "").strip()
        if not (actor and action and quote):
            continue
        actions.append({
            "id": str(item.get("id") or f"grok:source:{index}"),
            "actor": actor,
            "action": action,
            "date": str(item.get("date") or "").strip(),
            "quote": quote,
            "url": url if re.match(r"https://(?:x|twitter)\.com/", url) else "",
        })
    return actions


def normalize_x_claims(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    claims: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        author = str(item.get("author") or "").strip()
        text = str(item.get("text") or "").strip()
        claim = str(item.get("claim") or "").strip()
        if not (re.match(r"https://(?:x|twitter)\.com/", url) and author and text and claim):
            continue
        claims.append(
            {
                "id": str(item.get("id") or f"x:{index}"),
                "author": author,
                "text": text,
                "url": url,
                "timestamp": str(item.get("timestamp") or "").strip(),
                "claim": claim,
            }
        )
    return claims


def normalize_grok_claims(value: Any, *, prefix: str = "grok:ca") -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    claims: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        claim = str(item.get("claim") if isinstance(item, dict) else "").strip()
        if claim and not re.search(r"no qualifying|no usable narrative|没有可用(?:素材|叙事|帖子)", claim, re.IGNORECASE):
            claims.append({"id": f"{prefix}:{index}", "claim": claim})
    return claims


def collect_x_posts(contract: str, *, model: str = "", timeout: int) -> dict[str, Any]:
    del model
    result = fxtwitter_search.search_ca(contract, pages_per_feed=2, count_per_page=20, timeout=timeout)
    raw = result["raw"]
    return {
        "posts": result["posts"],
        "excluded_posts": result["excluded_posts"],
        "raw": raw,
        "diagnostic": {
            "stage": "x_ca_collection",
            "source": "fxtwitter",
            "pages": len(raw.get("pages") or []),
            "unique_results": raw.get("unique_results", 0),
            "kept_results": len(result["posts"]),
            "excluded_results": len(result["excluded_posts"]),
        },
    }


def collect_x_posts_resilient(contract: str, *, timeout: int) -> dict[str, Any]:
    try:
        return collect_x_posts(contract, timeout=timeout)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return {
            "posts": [],
            "excluded_posts": [],
            "raw": {
                "source": "fxtwitter",
                "query": contract,
                "pages": [],
                "unique_results": 0,
            },
            "diagnostic": {
                "stage": "x_ca_collection",
                "source": "fxtwitter",
                "degraded": True,
                "error": message,
            },
        }


def collect_gmgn_narrative(chain: str, contract: str, *, timeout: int) -> dict[str, Any]:
    try:
        result = gmgn_narrative.collect(chain, contract, timeout=timeout)
    except gmgn_narrative.GmgnHTTPError as exc:
        status = exc.status_code
        return {
            "supplement": [],
            "raw": None,
            "diagnostic": {
                "stage": "gmgn_narrative",
                "source": "gmgn",
                "http_status": status,
                "page_http_status": exc.page_status,
                "browser_mode": "playwright_headed",
                "optional": True,
                "error": str(exc),
            },
        }
    except Exception as exc:
        return {
            "supplement": [],
            "raw": None,
            "diagnostic": {"stage": "gmgn_narrative", "source": "gmgn", "optional": True, "error": str(exc)},
        }
    narrative = str(result.get("narrative") or "").strip()
    supplement = [{"id": "gmgn:supplement:1", "statement": narrative}] if narrative else []
    return {
        "supplement": supplement,
        "raw": result.get("raw"),
        "diagnostic": result.get("diagnostic") or {"stage": "gmgn_narrative", "source": "gmgn"},
    }


def collect_grok_material(contract: str, *, model: str, timeout: int) -> dict[str, Any]:
    load_project_env()
    api_key = grok_x_search.resolve_api_key(None, grok_x_search.DEFAULT_API_KEY_FILE)
    base_url = grok_x_search.resolve_base_url(None)
    prompt = build_grok_research_prompt_v2(contract)
    response, attempts = post_grok_response(
        base_url=base_url,
        api_key=api_key,
        payload={
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "tools": [{"type": "x_search"}],
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        return {
            "raw": None,
            "source_actions": [],
            "narrative_materials": [],
            "supplemental_information": [],
            "type_hypothesis": "",
            "diagnostic": {"stage": "ca_research", "http_status": response.status_code, "attempts": attempts},
        }
    raw = response.json()
    output_text = grok_x_search.extract_output_text(raw).strip()
    if not output_text:
        return {
            "raw": raw,
            "source_actions": [],
            "narrative_materials": [],
            "supplemental_information": [],
            "type_hypothesis": "",
            "diagnostic": {
                "stage": "ca_research",
                "http_status": response.status_code,
                "attempts": attempts,
                "num_server_side_tools_used": raw.get("usage", {}).get("num_server_side_tools_used"),
                "num_sources_used": raw.get("usage", {}).get("num_sources_used"),
                "empty_output": True,
            },
        }
    parsed = extract_json_object(output_text, "Grok")
    return {
        "raw": raw,
        "source_actions": normalize_grok_source_actions(parsed.get("source_actions")),
        "narrative_materials": normalize_grok_materials(parsed.get("narrative_materials"), prefix="grok:narrative", field="statement"),
        "supplemental_information": normalize_grok_materials(parsed.get("supplemental_information"), prefix="grok:supplement", field="statement"),
        "type_hypothesis": str(parsed.get("type_hypothesis") or "").strip() if str(parsed.get("type_hypothesis") or "").strip() in {"pure_meme", "celebrity_anchor", "app_linked"} else "",
        "diagnostic": {
            "stage": "ca_research",
            "http_status": response.status_code,
            "attempts": attempts,
            "num_server_side_tools_used": raw.get("usage", {}).get("num_server_side_tools_used"),
            "num_sources_used": raw.get("usage", {}).get("num_sources_used"),
        },
    }


def normalize_entity_candidates(value: Any, contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    message_texts: dict[int, list[str]] = {}
    for context in contexts:
        for message in context.get("context", []):
            if isinstance(message, dict):
                message_id = int(message.get("message_id") or 0)
                message_texts.setdefault(message_id, []).append(str(message.get("text") or ""))
    if not isinstance(value, list):
        return []
    candidates: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or len(candidates) >= 3:
            continue
        entity = str(item.get("entity") or "").strip()
        action = str(item.get("action") or "").strip()
        message_ids: list[int] = []
        for message_id in item.get("message_ids", []):
            try:
                parsed_id = int(message_id)
            except (TypeError, ValueError):
                continue
            if parsed_id > 0:
                message_ids.append(parsed_id)
        texts = [text for message_id in message_ids for text in message_texts.get(message_id, [])]
        if not (entity and action and message_ids and any(entity.casefold() in text.casefold() for text in texts)):
            continue
        candidates.append(
            {
                "id": f"entity:{len(candidates) + 1}",
                "entity": entity,
                "action": action,
                "message_ids": list(dict.fromkeys(message_ids)),
            }
        )
    return candidates


def normalize_entity_supplements(value: Any, candidates: list[dict[str, Any]]) -> list[dict[str, str]]:
    candidate_ids = {candidate["id"] for candidate in candidates}
    if not isinstance(value, list):
        return []
    supplements: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict) or str(item.get("candidate_id") or "") not in candidate_ids:
            continue
        claim = str(item.get("claim") or "").strip()
        if claim:
            supplements.append(
                {
                    "id": f"grok:entity:{index}",
                    "candidate_id": str(item["candidate_id"]),
                    "claim": claim,
                }
            )
    return supplements


def collect_entity_supplements(
    contract: str,
    chain: str,
    candidates: list[dict[str, Any]],
    *,
    model: str,
    timeout: int,
) -> dict[str, Any]:
    if not candidates:
        return {"claims": [], "raw": None, "diagnostic": {"stage": "entity_lookup", "skipped": "no_candidates"}}
    load_project_env()
    api_key = grok_x_search.resolve_api_key(None, grok_x_search.DEFAULT_API_KEY_FILE)
    base_url = grok_x_search.resolve_base_url(None)
    response, attempts = post_grok_response(
        base_url=base_url,
        api_key=api_key,
        payload={
            "model": model,
            "input": [{"role": "user", "content": build_entity_lookup_prompt(contract, chain, candidates)}],
            "tools": [{"type": "x_search"}],
        },
        timeout=timeout,
    )
    if response.status_code >= 400:
        return {
            "claims": [],
            "raw": None,
            "diagnostic": {"stage": "entity_lookup", "http_status": response.status_code, "attempts": attempts},
        }
    raw = response.json()
    output_text = grok_x_search.extract_output_text(raw).strip()
    if not output_text:
        return {
            "claims": [],
            "raw": raw,
            "diagnostic": {"stage": "entity_lookup", "http_status": response.status_code, "attempts": attempts, "empty_output": True},
        }
    parsed = extract_json_object(output_text, "Grok entity lookup")
    return {
        "claims": normalize_entity_supplements(parsed.get("entity_supplements"), candidates),
        "raw": raw,
        "diagnostic": {"stage": "entity_lookup", "http_status": response.status_code, "attempts": attempts},
    }


async def collect_telegram_contexts(args: argparse.Namespace, output: Path) -> dict[str, Any]:
    search_args = argparse.Namespace(
        config=args.telegram_config,
        session=args.telegram_session,
        allowed_chats=args.allowed_chats,
        term=[args.contract],
        # Narrative collection must preserve the original CA discussion as well
        # as the latest one, so it deliberately has no date cutoff or hit cap.
        lookback_hours=None,
        dialogs_limit=args.dialogs_limit,
        search_backend="telegram",
        search_limit_per_term=None,
        max_contexts=20,
        oldest_contexts=20,
        per_chat_limit=1200,
        before=2,
        after=15,
        output=str(output),
        print_hits=0,
        print_text_len=260,
        proxy=args.proxy,
        timeout=args.telegram_timeout,
        connection_retries=args.connection_retries,
        exclude_sender_id=list(EXCLUDED_TELEGRAM_BOT_SENDER_IDS),
    )
    await context_search.run(search_args)
    return json.loads(output.read_text(encoding="utf-8"))


def normalize_telegram_claims(value: Any, contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed: dict[tuple[str, int], str] = {}
    for context in contexts:
        chat_title = str(context.get("chat_title") or "")
        for message in context.get("context", []):
            if isinstance(message, dict):
                allowed[(chat_title, int(message.get("message_id") or 0))] = str(message.get("text") or "")
    if not isinstance(value, list):
        return []
    claims: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        chat_title = str(item.get("chat_title") or "").strip()
        message_id = int(item.get("message_id") or 0)
        source_text = allowed.get((chat_title, message_id))
        quote = str(item.get("quote") or "").strip()
        claim = str(item.get("claim") or "").strip()
        if not (source_text and quote and claim):
            continue
        if " ".join(quote.split()) not in " ".join(source_text.split()):
            continue
        claims.append(
            {
                "id": str(item.get("id") or f"tg:{chat_title}:{message_id}"),
                "chat_title": chat_title,
                "message_id": message_id,
                "speaker": str(item.get("speaker") or "群里有人").strip(),
                "quote": quote,
                "claim": claim,
            }
        )
    return claims


def telegram_messages_from_contexts(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose human Telegram text verbatim; only exact chat/message duplicates collapse."""
    messages: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for context in contexts:
        chat_title = str(context.get("chat_title") or "").strip()
        for message in context.get("context", []):
            if not isinstance(message, dict):
                continue
            message_id = int(message.get("message_id") or 0)
            key = (chat_title, message_id)
            text = str(message.get("text") or "").strip()
            if not chat_title or not message_id or not text or key in seen:
                continue
            seen.add(key)
            messages.append(
                {
                    "id": f"tg:{len(messages) + 1}",
                    "chat_title": chat_title,
                    "message_id": message_id,
                    "sent_at": str(message.get("sent_at") or ""),
                    "speaker": str(message.get("sender_username") or message.get("sender_name") or ""),
                    "text": text,
                }
            )
    return messages


def build_final_writer_prompt(
    contract: str,
    chain: str,
    telegram_claims: list[dict[str, Any]],
    x_claims: list[dict[str, Any]],
    grok_claims: list[dict[str, Any]],
) -> str:
    material = {"telegram_claims": telegram_claims, "x_claims": x_claims, "grok_claims": grok_claims}
    return f"""你是消息总结者。为链 {chain}、精确 CA {contract} 写中文 `reader_text`。

唯一任务：让读者知道炒作者拿什么来源、字眼、事件、身份或具体说法去炒这个代币。只能用
下方素材；不得查询、补充背景、判断真假、价值、走势、传播或买卖。

在同一次调用内、输出前静默完成：
1. 为每条可用材料建立事实关系账本：它必须属于 `source`（直接文本或人物动作）、`angle`
   （明确说明为什么会炒的理由）或 `supplement`（只解释人物/动作为何重要的背景）。没有位置
   的材料删除。
2. 选择最能解释现有材料的 `primary_type`：`pure_meme`、`celebrity_anchor` 或 `app_linked`。
   它只决定自然段顺序，不显示标题；其他类型不另起重复段落。
3. 保护原话短引、人物、动作、时间与归属。不能把“群里说 Ozzy 关注”写成“Ozzy 看好”。
4. 删除没有炒作理由的纯疑问、质疑、鲸鱼/新钱包状态、价格、市值、买卖诱导、泛泛喊单、
   机器人内容、纯 CA、重复转述和泛词。无解释的“某人关注了”也删除。

段落骨架：`celebrity_anchor` 与 `pure_meme` 可按 来源→角度→补充信息；`app_linked` 可按
角度→补充信息。任何位置可为空：只有原始来源文本本身出现了被炒的精确字眼，或人物的
文本动作本身已直接把该字眼与代币连起来时，角度可以为空。单独“某账户买了”“某人关注了”
“又开一个”不满足这一条件，必须另有明确角度或身份补充，否则删除。没有可用材料时
reader_text 必须为空，绝不硬凑。

归属规则：X 素材写 `@作者说/发帖写`；Telegram 写 `群里有人说` 或素材中的账号；Grok 材料
只能写 `Grok 提到/表示/指出`，即使它没有 X 原帖。连续使用 Grok 可只点出一次；中间换源后
再使用 Grok 时重新点出。不要写“Grok 找到”“X Search 找到”“Grok 把 XX 归到 XX 这条线”。

文风：直接白话，只写具体主语和动作。删掉营销式强调、无源权威、翻译腔、AI/报告腔、空泛
总括、多余标题、emoji、作者评价、否定式对比和结论式旁白。禁止“有人把它说成”“拿 A 来聊”
“把 A 连到/归到 B”“这说明”“X 上常见词”“围绕几个说法”“几条线”“这个角度”。可以删句、
合并相邻事实和调整顺序；不得新增人物、事实、因果、身份或背景。

返回 JSON：
{{
  "primary_type":"pure_meme|celebrity_anchor|app_linked|",
  "source_material_ids":[],
  "angle_material_ids":[],
  "supplemental_information_ids":[],
  "reader_text":"",
  "used_material_ids":[],
  "discarded_material_ids":[]
}}
所有 id 数组只能引用输入素材 id。三个材料组均为空时 reader_text 必须为空；reader_text 中的
每一项内容都必须出现在 source/angle/supplement 三组之一。

素材：
{json.dumps(material, ensure_ascii=False)}"""


def build_final_writer_prompt_v2(
    contract: str,
    chain: str,
    telegram_messages: list[dict[str, Any]],
    x_posts: list[dict[str, Any]],
    grok_source_actions: list[dict[str, Any]],
    grok_narrative_materials: list[dict[str, Any]],
    grok_supplemental_information: list[dict[str, Any]],
    entity_supplements: list[dict[str, Any]],
    gmgn_supplement: list[dict[str, Any]],
    type_hypothesis: str,
) -> str:
    material = {
        "telegram_messages": telegram_messages,
        "x_posts": x_posts,
        "grok_source_actions": grok_source_actions,
        "grok_narrative_materials": grok_narrative_materials,
        "grok_supplemental_information": grok_supplemental_information,
        "entity_supplements": entity_supplements,
        "gmgn_supplement": gmgn_supplement,
        "grok_type_hypothesis_nonfinal": type_hypothesis,
    }
    return f"""You are a Chinese-language message summarizer. Write the final `reader_text` for chain {chain}, exact CA {contract}.
Your only job is to let a reader understand what concrete source, wording, event, identity, or assertion people use to hype this token. Use only the supplied material. Do not research, supply background, assess truth, price, value, upside, spread, or trading.

Telegram message text is verbatim material, not a pre-written claim. First silently cluster semantically repeated assertions across all sources. One group chat counts as one source regardless of how many people in it repeated the point. Do not show chat names, usernames, or ordinary X handles. Refer to one relevant chat as "群聊 A 表示…" and multiple relevant chats as "多个群聊表示…"; use stable A/B/C labels only when the distinction is needed. Refer to one X source as "X 上有人表示…" and multiple independent X sources as "X 上多名用户表示…". For a Grok narrative material used as a main assertion, say "Grok 指出/表示/提到…". Do not use "Grok 还称" or "Grok 又称" to connect separate material groups. Telegram and X must keep their ordinary source-subject attribution; never label them as "材料补充". Do not mechanically list every post.

The three final types are pure_meme, celebrity_anchor, app_linked. Decide the final type yourself; Grok's type hint is non-final. Source, angle, and supplemental information can each be empty. If a direct word anchor or a clearly meaningful action already explains the hype, do not invent an angle just to fill a section. A source or angle remains usable when supplemental information is absent: preserve what a chat or X source actually said about a named person or account, but do not infer that person's identity, importance, or causal impact. Remove pure questions and doubts, prices, market caps, buy/sell calls, bot content, repeated CA mentions, and vague unnamed-role movement such as “wallet gang shows up”, “有庄”, “庄家还有钱包在”, or “老外来了”. A concrete named-person action such as “Frank 来了” remains usable. `fr fr` and `for real` are agreement/emphasis, not an angle; delete them unless other concrete content remains, and then keep only the concrete content. Do not turn a question into a source claim or use it as corroboration.

Use the three buckets deliberately. For celebrity_anchor and pure_meme, the natural order is source, then angle, then supplemental information; for app_linked, use angle, then supplemental information. For celebrity_anchor specifically, a source must be an actual original action: a `grok_source_action` with actor, action, and quote (with date and URL when returned), or an `x_post` whose author is itself the actor and whose text is that actor's direct action. A third party writing “He Yi called it” is not an original source. The first paragraph must name that actor and write the action directly in this form: “XXX 于 X月X日发文提到‘……’”“XXX 于 X月X日转发/引用/评论……”. If the cited source actions span calendar days, include month and day for each action, never the year. A community repost, a Grok summary, or an ordinary X user's claim is not a replacement for this source paragraph. If no original action is available, leave source empty: retain a concrete community claim only as an angle, with its ordinary attribution, and do not invent an empty first paragraph or a named action. When both source and angle are nonempty, write source as the first paragraph and the angle as a separate second paragraph. The second paragraph states only new community wording or interpretation; do not repeat the source. A direct Binance/official/high-profile account mention of the word itself is already a complete reason and may have an empty angle.

When `grok_supplemental_information` or `entity_supplements` is used, write all such facts in a separate paragraph beginning exactly with `Grok补充：`; join concrete facts with `；` and do not repeat "Grok 提到/表示" for every item. Put the used IDs in `supplemental_information_ids`, including entity supplement IDs. Do not merge this paragraph into the main Telegram/X narrative, and do not use "Grok 还称" as a bridge. Normalize entity lookup silently: never write identity-resolution text such as "王大友是王大有". Use the canonical person as the subject with a verified role or action, such as "加密KOL王大有正在推动并建设 RTX 社区"; for a known public figure, use a compact appositive subject such as "英伟达CEO黄仁勋账号关注了 RTX 社区成员". If the material does not support a direct normalized fact, omit it. A Grok narrative material used as the main angle may still use the ordinary `Grok 指出/表示/提到` attribution in its own paragraph; it must remain separate from the `Grok补充：` paragraph.

When `gmgn_supplement` is non-empty, it must be used as a separate final paragraph beginning exactly with `GMGN补充：`; put its ID in `supplemental_information_ids`. Preserve the supplied Simplified Chinese narrative as faithfully as possible and do not present it as an X, Telegram, or Grok claim. Keep `GMGN补充：` and `Grok补充：` as separate paragraphs.

Supplemental information, when used, follows in its own paragraph with its exact source marker. This is an ordering rule, not a completeness gate for Grok materials; a non-empty `gmgn_supplement` is mandatory.

Use plain natural Chinese. The program adds the fixed opening and closing disclaimer after validation; do not add either one yourself. Lead with the direct assertion rather than a report of the collection process. No headings, bullet lists, AI/report language, generic catchphrases, or phrases such as “用……来解释……”, “把……说成……的例子”, “有人将其视为……”, “这被认为是……的体现”, “有人把它说成”, “把……标成……”, “叙事包含……”, “把 A 连到/归到 B”, “把/将 A 与 B 联系/关联起来”, “这说明”, “X 上常见词”, “几条线”, or “这个角度”. Delete this explanatory shell and write the source subject + verb + direct assertion instead: write “X 上有人称它为‘龙二金狗’”, not “X 上有人把该 CA 标成‘龙二金狗’”; write “该 CA 与 SK Hynix 的股票吉祥物有关”, not “帖文把该 CA 与 SK Hynix 的股票吉祥物联系在一起”. Do not invent facts or causal links. Return only JSON:
{{"primary_type":"pure_meme|celebrity_anchor|app_linked|","source_material_ids":[],"angle_material_ids":[],"supplemental_information_ids":[],"reader_text":"","used_material_ids":[],"discarded_material_ids":[]}}
All ID arrays may contain only supplied material IDs. If there is no usable material, reader_text must be empty.
Material:\n{json.dumps(material, ensure_ascii=False)}"""


def valid_ids(result: dict[str, Any], field: str, known_ids: set[str]) -> list[str]:
    value = result.get(field, [])
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item) for item in value if str(item) in known_ids))


def normalize_reader_text(value: Any) -> str:
    reader_text = str(value or "").strip()
    if not reader_text:
        return ""
    for legacy_disclaimer in LEGACY_READER_DISCLAIMERS:
        reader_text = reader_text.replace(legacy_disclaimer, READER_DISCLAIMER)
    reader_text = normalize_gmgn_supplement_lines(reader_text)
    reader_text = normalize_grok_supplement_punctuation(reader_text)
    if not reader_text.startswith(READER_OPENING):
        reader_text = f"{READER_OPENING}{reader_text}"
    if not reader_text.endswith(READER_DISCLAIMER):
        reader_text = f"{reader_text.rstrip()}\n\n{READER_DISCLAIMER}"
    return reader_text


def normalize_grok_supplement_punctuation(value: str) -> str:
    """Ensure the dedicated Grok supplement paragraph has sentence punctuation."""
    lines = value.splitlines()
    normalized: list[str] = []
    terminal_punctuation = "。！？!?；;"
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{GROK_SUPPLEMENT_MARKER}") and stripped and stripped[-1] not in terminal_punctuation:
            stripped = f"{stripped}。"
        normalized.append(stripped)
    return "\n".join(normalized)


def _last_unquoted_comma(value: str) -> int:
    quote_stack: list[str] = []
    symmetric_quotes = {'"', "'"}
    quote_pairs = {"“": "”", "‘": "’", "「": "」", "『": "』"}
    last_comma = -1
    for index, char in enumerate(value):
        if quote_stack:
            if char == quote_stack[-1]:
                quote_stack.pop()
            continue
        if char in quote_pairs:
            quote_stack.append(quote_pairs[char])
        elif char in symmetric_quotes:
            quote_stack.append(char)
        elif char in {"，", ","}:
            last_comma = index
    return last_comma


def clean_gmgn_supplement_line(line: str) -> str:
    """Remove the GMGN shield-logo sentence when it is not useful to readers."""
    line = line.strip()
    if not line.startswith(GMGN_SUPPLEMENT_MARKER) or "标志" not in line:
        return line

    statement = line[len(GMGN_SUPPLEMENT_MARKER) :]
    marker_index = statement.find("标志")
    comma_index = _last_unquoted_comma(statement[:marker_index])
    if comma_index < 0:
        return ""

    prefix = statement[:comma_index].rstrip(" \t，,")
    return f"{GMGN_SUPPLEMENT_MARKER}{prefix}。" if prefix else ""


def normalize_gmgn_supplement_lines(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        cleaned = clean_gmgn_supplement_line(line)
        if cleaned:
            lines.append(cleaned)
    return "\n".join(lines)


def ensure_gmgn_supplement(result: dict[str, Any], gmgn_supplement: list[dict[str, Any]]) -> None:
    if not gmgn_supplement:
        return
    item = gmgn_supplement[0]
    item_id = str(item.get("id") or "").strip()
    statement = str(item.get("statement") or "").strip()
    if not item_id or not statement:
        return
    supplement_ids = result.get("supplemental_information_ids")
    if not isinstance(supplement_ids, list):
        supplement_ids = []
        result["supplemental_information_ids"] = supplement_ids
    if item_id not in supplement_ids:
        supplement_ids.append(item_id)
    used_ids = result.get("used_material_ids")
    if not isinstance(used_ids, list):
        used_ids = []
        result["used_material_ids"] = used_ids
    if item_id not in used_ids:
        used_ids.append(item_id)
    reader_text = str(result.get("reader_text") or "").strip()
    reader_text = normalize_gmgn_supplement_lines(reader_text)
    if not re.search(r"(?m)^GMGN补充：", reader_text):
        section = clean_gmgn_supplement_line(f"{GMGN_SUPPLEMENT_MARKER}{statement}")
        if section:
            result["reader_text"] = f"{reader_text}\n\n{section}".strip()
        else:
            result["reader_text"] = reader_text
            result["supplemental_information_ids"] = [x for x in supplement_ids if str(x) != item_id]
            result["used_material_ids"] = [x for x in used_ids if str(x) != item_id]
    else:
        result["reader_text"] = reader_text


def validate_final_result(
    result: dict[str, Any],
    material_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    reader_text = normalize_reader_text(result.get("reader_text"))
    if READER_TEXT_FORBIDDEN.search(reader_text):
        raise RuntimeError("Final reader_text contains internal review language.")

    known_ids = set(material_by_id)
    source_ids = valid_ids(result, "source_material_ids", known_ids)
    angle_ids = valid_ids(result, "angle_material_ids", known_ids)
    supplement_ids = valid_ids(result, "supplemental_information_ids", known_ids)
    used_ids = valid_ids(result, "used_material_ids", known_ids)
    if not re.search(r"(?m)^GMGN补充：", reader_text):
        supplement_ids = [item_id for item_id in supplement_ids if not item_id.startswith("gmgn:")]
        used_ids = [item_id for item_id in used_ids if not item_id.startswith("gmgn:")]
    grouped_ids = set(source_ids + angle_ids + supplement_ids)
    if reader_text and not grouped_ids:
        raise RuntimeError("Final reader_text has no classified materials.")
    if reader_text and not used_ids:
        used_ids = list(grouped_ids)
    gmgn_ids = [item_id for item_id in supplement_ids if item_id.startswith("gmgn:")]
    grok_ids = [item_id for item_id in supplement_ids if item_id.startswith("grok:")]
    if gmgn_ids and not re.search(r"(?m)^GMGN补充：", reader_text):
        raise RuntimeError("Final reader_text must place GMGN supplemental material under its labeled paragraph.")
    if grok_ids and not re.search(r"(?m)^Grok补充：", reader_text):
        raise RuntimeError("Final reader_text must place Grok supplemental material under its labeled paragraph.")

    return {
        "primary_type": str(result.get("primary_type") or "").strip()
        if str(result.get("primary_type") or "").strip() in {"pure_meme", "celebrity_anchor", "app_linked"}
        else "",
        "source_materials": [material_by_id[item_id] for item_id in source_ids],
        "angle_materials": [material_by_id[item_id] for item_id in angle_ids],
        "supplemental_information": [material_by_id[item_id] for item_id in supplement_ids],
        "reader_text": reader_text,
        "used_material_ids": used_ids,
        "discarded_material_ids": valid_ids(result, "discarded_material_ids", known_ids),
    }


def _material_counts(
    *,
    contexts: list[dict[str, Any]],
    telegram_messages: list[dict[str, Any]],
    x_posts: list[dict[str, Any]],
    grok_source_actions: list[dict[str, Any]],
    grok_narrative_materials: list[dict[str, Any]],
    grok_supplemental_information: list[dict[str, Any]],
    entity_supplements: list[dict[str, Any]],
    gmgn_supplement: list[dict[str, Any]],
) -> dict[str, int]:
    counts = {
        "telegram_contexts": len(contexts),
        "telegram_messages": len(telegram_messages),
        "x_posts": len(x_posts),
        "grok_source_actions": len(grok_source_actions),
        "grok_narrative_materials": len(grok_narrative_materials),
        "grok_supplemental_information": len(grok_supplemental_information),
        "entity_supplements": len(entity_supplements),
        "gmgn_supplement": len(gmgn_supplement),
    }
    counts["total_materials"] = sum(
        value for key, value in counts.items() if key != "telegram_contexts"
    )
    return counts


def _decision_metadata(
    *,
    result: dict[str, Any],
    counts: dict[str, int],
    type_hypothesis: str,
) -> tuple[str, str, str]:
    reader_text = str(result.get("reader_text") or "").strip()
    primary_type = str(result.get("primary_type") or "").strip()
    grouped_materials = sum(
        len(result.get(field) or [])
        for field in ("source_materials", "angle_materials", "supplemental_information")
        if isinstance(result.get(field), list)
    )
    if counts["total_materials"] == 0:
        return "empty", "no_materials", "没有找到可供最终写作者使用的 Telegram、X 或 Grok 材料。"
    if not reader_text and primary_type:
        return "empty", "type_selected_but_empty_reader_text", "已选出叙事类型，但最终写作者没有生成可用正文。"
    if not reader_text:
        if grouped_materials:
            return "empty", "writer_returned_empty", "最终写作者已经分类了部分材料，但没有生成可读正文。"
        hypothesis_note = "Grok 类型假设也为空。" if not type_hypothesis else f"Grok 类型假设为 {type_hypothesis}。"
        return "empty", "materials_but_no_type", f"存在候选材料，但没有形成有效的最终类型或分类材料；{hypothesis_note}"
    if not primary_type:
        hypothesis_note = "Grok 类型假设为空。" if not type_hypothesis else f"Grok 类型假设为 {type_hypothesis}。"
        return "success", "materials_but_no_type", f"已生成正文，但最终写作者没有返回有效的叙事类型；{hypothesis_note}"
    if not result.get("angle_materials"):
        return "success", "no_usable_angle", "已经选出类型并生成正文，但没有形成可用的叙事角度材料。"
    return "success", "completed", "已生成正文并返回有效的叙事类型。"


def _failure_stage_for_empty(decision_code: str, counts: dict[str, int]) -> str | None:
    if decision_code == "no_materials":
        if counts.get("telegram_contexts", 0) == 0 and counts.get("telegram_messages", 0) == 0:
            return "telegram_collection"
        if counts.get("x_posts", 0) == 0:
            return "x_ca_collection"
        if counts.get("grok_source_actions", 0) == 0 and counts.get("grok_narrative_materials", 0) == 0:
            return "grok_ca_research"
    if decision_code in {"writer_returned_empty", "type_selected_but_empty_reader_text", "materials_but_no_type"}:
        return "final_writer"
    return None


def _diagnostic_failure(diagnostics: list[dict[str, Any]]) -> tuple[str, str, str] | None:
    stage_aliases = {
        "ca_research": "grok_ca_research",
        "entity_lookup": "grok_entity_lookup",
    }
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            continue
        try:
            http_status = int(diagnostic.get("http_status") or 0)
        except (TypeError, ValueError):
            http_status = 0
        if http_status < 400:
            continue
        raw_stage = str(diagnostic.get("stage") or "narrative_pipeline")
        stage = stage_aliases.get(raw_stage, raw_stage)
        code = f"http_{http_status}"
        return stage, code, f"{raw_stage} 返回 HTTP {http_status}。"
    return None


async def run_async(args: argparse.Namespace) -> dict[str, Any]:
    async def timed(stage: str, awaitable: Any) -> tuple[Any, dict[str, Any]]:
        started_at = time.perf_counter()
        try:
            value = await awaitable
        except NarrativeStageError:
            raise
        except Exception as exc:
            raise NarrativeStageError(stage, exc) from exc
        raw = value.get("raw") if isinstance(value, dict) else None
        return value, performance_entry(stage, started_at, raw)

    started_at = time.perf_counter()
    output_path = Path(args.output) if args.output else (
        Path(args.output_dir) / f"{args.chain}-{args.contract.lower()}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    telegram_path = output_path.with_suffix(".telegram.json")
    gmgn_timeout = int(getattr(args, "gmgn_timeout", min(int(args.grok_timeout), 20)))

    telegram_task = asyncio.create_task(timed("telegram_collection", collect_telegram_contexts(args, telegram_path)))
    x_posts_task = asyncio.create_task(
        timed("x_ca_collection", asyncio.to_thread(collect_x_posts_resilient, args.contract, timeout=args.grok_timeout))
    )
    # Research is CA-only and can run while Telegram material is being prepared.
    grok_task = asyncio.create_task(
        timed("grok_ca_research", asyncio.to_thread(collect_grok_material, args.contract, model=args.grok_model, timeout=args.grok_timeout))
    )
    gmgn_task = asyncio.create_task(
        timed("gmgn_narrative", asyncio.to_thread(collect_gmgn_narrative, args.chain, args.contract, timeout=gmgn_timeout))
    )
    (telegram_source, telegram_performance), (x_source, x_performance) = await asyncio.gather(
        telegram_task,
        x_posts_task,
    )
    contexts = list(telegram_source.get("contexts") or [])
    try:
        extraction, entity_extraction_performance = chat_completion_json_with_metrics(
            build_telegram_entity_prompt(args.contract, contexts),
            model=args.gpt_model,
            timeout=args.gpt_timeout,
        )
    except Exception as exc:
        raise NarrativeStageError("telegram_entity_extraction", exc) from exc
    entity_extraction_performance["stage"] = "telegram_entity_extraction"
    telegram_messages = telegram_messages_from_contexts(contexts)
    entity_candidates = normalize_entity_candidates(extraction.get("entity_candidates"), contexts)
    entity_lookup_task = asyncio.create_task(
        timed(
            "grok_entity_lookup",
            asyncio.to_thread(
                collect_entity_supplements,
                args.contract,
                args.chain,
                entity_candidates,
                model=args.grok_model,
                timeout=args.grok_timeout,
            ),
        )
    )
    (grok_source, grok_performance), (entity_lookup, entity_lookup_performance), (gmgn_source, gmgn_performance) = await asyncio.gather(
        grok_task,
        entity_lookup_task,
        gmgn_task,
    )
    x_posts = x_source["posts"]
    grok_source_actions = grok_source["source_actions"]
    grok_narrative_materials = grok_source["narrative_materials"]
    grok_supplemental_information = grok_source["supplemental_information"]
    entity_supplements = entity_lookup["claims"]
    gmgn_supplement = gmgn_source["supplement"]
    material_by_id = {
        item["id"]: item
        for item in telegram_messages + x_posts + grok_source_actions + grok_narrative_materials + grok_supplemental_information + entity_supplements + gmgn_supplement
    }
    try:
        final, final_writer_performance = chat_completion_json_with_metrics(
            build_final_writer_prompt_v2(
                args.contract,
                args.chain,
                telegram_messages,
                x_posts,
                grok_source_actions,
                grok_narrative_materials,
                grok_supplemental_information,
                entity_supplements,
                gmgn_supplement,
                grok_source["type_hypothesis"],
            ),
            model=args.gpt_model,
            timeout=args.gpt_timeout,
        )
    except Exception as exc:
        raise NarrativeStageError("final_writer", exc) from exc
    final_writer_performance["stage"] = "final_writer"
    ensure_gmgn_supplement(final, gmgn_supplement)
    try:
        result = validate_final_result(final, material_by_id)
    except Exception as exc:
        raise NarrativeStageError("final_validation", exc) from exc
    counts = _material_counts(
        contexts=contexts,
        telegram_messages=telegram_messages,
        x_posts=x_posts,
        grok_source_actions=grok_source_actions,
        grok_narrative_materials=grok_narrative_materials,
        grok_supplemental_information=grok_supplemental_information,
        entity_supplements=entity_supplements,
        gmgn_supplement=gmgn_supplement,
    )
    status, decision_code, decision_reason = _decision_metadata(
        result=result,
        counts=counts,
        type_hypothesis=str(grok_source["type_hypothesis"] or "").strip(),
    )
    diagnostics = [x_source["diagnostic"], grok_source["diagnostic"], entity_lookup["diagnostic"]]
    diagnostic_failure = _diagnostic_failure(diagnostics)
    if diagnostic_failure:
        failure_stage, failure_code, failure_message = diagnostic_failure
        status = "error"
        decision_code = "narrative_error"
        decision_reason = failure_message
    else:
        failure_stage = _failure_stage_for_empty(decision_code, counts) if status == "empty" else None
        failure_code = decision_code if status == "empty" else None
        failure_message = decision_reason if status == "empty" else None
    output = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chain": args.chain,
        "contract": args.contract,
        "status": status,
        "failure_stage": failure_stage,
        "failure_code": failure_code,
        "failure_message": failure_message,
        "material_counts": counts,
        "decision_code": decision_code,
        "decision_reason": decision_reason,
        "telegram_contexts": contexts,
        "telegram_messages": telegram_messages,
        "entity_candidates": entity_candidates,
        "x_posts": x_posts,
        "x_excluded_posts": x_source.get("excluded_posts") or [],
        "gmgn_supplement": gmgn_supplement,
        "gmgn_diagnostic": gmgn_source["diagnostic"],
        "type_hypothesis": grok_source["type_hypothesis"],
        "grok_research": {
            "source_actions": grok_source_actions,
            "narrative_materials": grok_narrative_materials,
            "supplemental_information": grok_supplemental_information,
            "type_hypothesis": grok_source["type_hypothesis"],
        },
        "entity_supplements": entity_supplements,
        "grok_diagnostics": diagnostics,
        "performance": {
            "total_duration_ms": round((time.perf_counter() - started_at) * 1000),
            "calls": [
                telegram_performance,
                x_performance,
                grok_performance,
                gmgn_performance,
                entity_extraction_performance,
                entity_lookup_performance,
                final_writer_performance,
            ],
        },
        **result,
    }
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    telegram_path.unlink(missing_ok=True)
    # The caller persists this return value as JSON; keep the path JSON-safe.
    return {"output_path": str(output_path), **output}


def run(args: argparse.Namespace) -> int:
    setup_stdout()
    result = asyncio.run(run_async(args))
    print(f"Saved: {result['output_path']}")
    if result["reader_text"]:
        print()
        print(result["reader_text"])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Telegram and Grok narrative material, then compose reader text.")
    parser.add_argument("--chain", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--output", help="Combined JSON output path.")
    parser.add_argument("--gpt-model", default=DEFAULT_GPT_MODEL)
    parser.add_argument("--grok-model", default=DEFAULT_GROK_MODEL)
    parser.add_argument("--gpt-timeout", type=int, default=180)
    parser.add_argument("--grok-timeout", type=int, default=180)
    parser.add_argument("--gmgn-timeout", type=int, default=20)
    parser.add_argument("--telegram-config", default=str(CONFIG_DIR / "telegram.txt"))
    parser.add_argument("--telegram-session", default=str(CONFIG_DIR.parent / "telegram_probe"))
    parser.add_argument("--allowed-chats", default=str(CONFIG_DIR / "whitelist.txt"))
    parser.add_argument("--dialogs-limit", type=int, default=250)
    parser.add_argument("--proxy", default="auto")
    parser.add_argument("--telegram-timeout", type=int, default=20)
    parser.add_argument("--connection-retries", type=int, default=3)
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
