#!/usr/bin/env python3
"""Create a local-only X Agent account-screening report from explicit snapshots."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MARKET_REFERENCE = re.compile(
    r"(?:\$(?:btc|eth|sol|zec|xrp|bnb|doge|ada|avax|link|sui|hype|near|ton|tao|ltc|dot|atom|uni|aave|"
    r"mkr|arb|op|fil|inj|apt|sei|pepe|shib|spy|qqq|dia|iwm|mstr|coin|tsla|nvda|aapl|msft|amzn|meta|googl|"
    r"gme|amd|pltr)\b|\b(?:btc|bitcoin|eth|ethereum|sol|solana|zec|xrp|bnb|doge|ada|avax|link|sui|hype|near|"
    r"ton|tao|ltc|dot|atom|uni|aave|mkr|arb|op|fil|inj|apt|sei|etf|index|nasdaq|dow|s&p|美股|纳指|标普)\b)",
    re.IGNORECASE,
)
TICKER = re.compile(r"\$[A-Za-z][A-Za-z0-9_]{1,14}\b")
MARKET_BROAD_REFERENCE = re.compile(r"(?:\b(?:crypto|stock|stocks|equity|macro|treasury|fed|rates)\b|联储|利率)", re.IGNORECASE)
MARKET_CONTEXT = re.compile(
    r"(?:long|short|bullish|bearish|buy|sell|position|conviction|trade|trading|price|chart|market|perp|"
    r"futures|option|看多|看空|仓位|买入|卖出|价格|行情|交易|建仓|清仓|期货|期权|杠杆|目标)",
    re.IGNORECASE,
)
CEX_CONTEXT = re.compile(r"(?:\b(?:cex|binance|okx|coinbase|bybit|kraken|spot|perp|futures|option)\b|中心化交易所|现货|永续|期货|期权)", re.IGNORECASE)
ONCHAIN_MARKET_CONTEXT = re.compile(
    r"(?:\b(?:on[ -]?chain|tokenized|dex|amm|liquidity pool|launchpad)\b|链上|代币化|去中心化交易所|流动性池)",
    re.IGNORECASE,
)
MAJOR_CRYPTO_REFERENCE = re.compile(
    r"(?:\$(?:btc|eth|sol|zec|xrp|bnb|doge|ada|avax|link|sui|hype|near|ton|tao|ltc|dot|atom|uni|aave|mkr|arb|op|fil|inj|apt|sei)\b|"
    r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|zec|xrp|bnb|doge|ada|avax|link|sui|hype|near|ton|tao|ltc|dot|atom|uni|aave|mkr|arb|op|fil|inj|apt|sei)\b)",
    re.IGNORECASE,
)
PROJECT_CRYPTO_SIGNAL = re.compile(
    r"(?:\b(?:token|airdrop|protocol|mainnet|testnet|points|liquidity|contract|tge|meme|degen|fdv|tvl|"
    r"launchpad|staking|bridge|wallet|defi|nft|dao|dapp)\b|0x[a-f0-9]{8,}|"
    r"空投|合约|发射|链上|新币|土狗|代币|去中心化|钱包|跨链|质押)",
    re.IGNORECASE,
)
ATTITUDE = re.compile(
    r"(?:long|short|bullish|bearish|buy|sell|position|conviction|看多|看空|买入|卖出|仓位|恐慌|狂热|预期|目标)",
    re.IGNORECASE,
)
MARKET_VIEWPOINT = re.compile(
    r"(?:\b(?:long|short|bullish|bearish|buy|sell|position|conviction|accumulate|reduce|trim|hold|"
    r"overweight|underweight|outlook|forecast|target|expect|thesis|valuation|undervalued|overvalued|"
    r"risk[- ]?on|risk[- ]?off)\b|看多|看空|买入|卖出|仓位|建仓|清仓|减仓|止盈|止损|目标|预期|"
    r"高估|低估|估值|乐观|悲观|恐慌|狂热|机会|风险)",
    re.IGNORECASE,
)
PROJECT_INCLUDE_CRYPTO_EVIDENCE = re.compile(
    r"(?:\b(?:tge|airdrop|defi|dex|nft|dao|dapp|meme|degen|fdv|tvl|launchpad|staking|bridge|wallet|"
    r"tokenomics|on[ -]?chain|mainnet|testnet)\b|0x[a-f0-9]{8,}|空投|链上|新币|土狗|代币|去中心化|钱包|跨链|质押|"
    r"合约地址|主网|测试网)",
    re.IGNORECASE,
)
PROJECT_LOGIC = re.compile(
    r"(?:\b(?:thesis|because|why|catalyst|utility|use case|adoption|revenue|growth|valuation|market cap|"
    r"tokenomics|fees|yield|incentive|partnership|roadmap|buy|hold|accumulate|undervalued|upside)\b|"
    r"逻辑|理由|看好|买入|持有|低估|估值|市值|催化|用途|产品|增长|收益|机制|激励|合作|叙事|机会)",
    re.IGNORECASE,
)
NOISE = re.compile(r"(?:giveaway|抽奖|retweet|转发|新闻|公告)", re.IGNORECASE)
PROMPT_VERSION = "x-agent-screen-v8-attitude-and-project-logic"


def has_market_signal(text: str) -> bool:
    """Keep generic chain tickers out unless the author names a CEX trade context."""
    candidate = bool(
        MARKET_REFERENCE.search(text)
        or (MARKET_BROAD_REFERENCE.search(text) and MARKET_CONTEXT.search(text))
        or (TICKER.search(text) and CEX_CONTEXT.search(text) and MARKET_CONTEXT.search(text))
    )
    if ONCHAIN_MARKET_CONTEXT.search(text) and not CEX_CONTEXT.search(text) and not MAJOR_CRYPTO_REFERENCE.search(text):
        return False
    return candidate


def has_market_include_evidence(text: str) -> bool:
    """A market include needs the author's actual view or trade, not market mention alone."""
    return has_market_signal(text) and bool(MARKET_VIEWPOINT.search(text))


def has_project_signal(text: str) -> bool:
    """Require a crypto/onchain indicator so generic product launches cannot qualify."""
    return bool(PROJECT_CRYPTO_SIGNAL.search(text))


def has_project_include_evidence(text: str) -> bool:
    """Generic protocol vocabulary is recall-only; include evidence must be unambiguously crypto/onchain."""
    return bool(PROJECT_INCLUDE_CRYPTO_EVIDENCE.search(text))


def has_project_logic(text: str) -> bool:
    return bool(PROJECT_LOGIC.search(text))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def write_json_atomically(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_accounts(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            str(row.get("screen_name") or row.get("username") or "").lower(): row
            for row in csv.DictReader(handle)
            if str(row.get("screen_name") or row.get("username") or "").strip()
        }


def parse_time(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def load_posts(paths: list[Path], *, cutoff: datetime, as_of: datetime) -> dict[str, list[dict[str, Any]]]:
    by_account: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            tweet_id = str(row.get("tweet_id") or "")
            account = str(row.get("account_screen_name") or "").lower()
            created_at = parse_time(row.get("created_at_iso"))
            if not tweet_id or not account or tweet_id in seen or created_at is None:
                continue
            if created_at < cutoff or created_at > as_of:
                continue
            # A quote's expanded text contains somebody else's opinion.  The
            # account screen is based only on the tracked author's own words.
            author_text = " ".join(str(row.get("text") or "").split())
            if not author_text:
                continue
            seen.add(tweet_id)
            item = dict(row)
            item["text"] = author_text
            by_account.setdefault(account, []).append(item)
    for rows in by_account.values():
        rows.sort(key=lambda row: str(row.get("created_at_iso") or ""), reverse=True)
    return by_account


def rough_screen(rows: list[dict[str, Any]]) -> dict[str, Any]:
    market_hits = sum(has_market_signal(str(row["text"])) for row in rows)
    project_hits = sum(has_project_signal(str(row["text"])) for row in rows)
    attitude_hits = sum(has_market_signal(str(row["text"])) and bool(ATTITUDE.search(str(row["text"]))) for row in rows)
    noise_hits = sum(bool(NOISE.search(str(row["text"]))) for row in rows)
    if not rows:
        tier = "no_sample"
    elif max(market_hits, project_hits) >= 3:
        tier = "strong_candidate"
    elif market_hits or project_hits or attitude_hits:
        tier = "weak_candidate"
    else:
        tier = "obvious_exclude"
    ranked = sorted(
        rows,
        key=lambda row: (
            has_market_signal(str(row["text"]))
            + has_project_signal(str(row["text"]))
            + bool(ATTITUDE.search(str(row["text"]))),
            len(str(row["text"])),
        ),
        reverse=True,
    )[:5]
    return {
        "tier": tier,
        "market_hits": market_hits,
        "project_hits": project_hits,
        "market_attitude_hits": attitude_hits,
        "noise_hits": noise_hits,
        "first_post_at": str(rows[-1].get("created_at_iso") or "") if rows else None,
        "last_post_at": str(rows[0].get("created_at_iso") or "") if rows else None,
        "posts": ranked,
    }


def fingerprint(row: dict[str, Any], *, cutoff: datetime, as_of: datetime, model: str, fallback_model: str) -> str:
    evidence = [
        {
            "tweet_id": str(post.get("tweet_id") or ""),
            "text_hash": hashlib.sha256(str(post.get("text") or "").encode("utf-8")).hexdigest(),
        }
        for post in row["representative_posts"]
    ]
    payload = {
        "account": row["username"],
        "window_start": cutoff.isoformat(),
        "window_end": as_of.isoformat(),
        "posts": evidence,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "fallback_model": fallback_model,
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def prompt(row: dict[str, Any]) -> str:
    return (
        "只根据原帖判断两个订阅，不做画像。市场情绪仅纳入持续讨论主流 CEX Crypto、美股、指数、ETF 或二级交易；"
        "链上小币/degen 归项目推介。项目推介只包括 Crypto 链上项目，必须有项目主体和逻辑；"
        "传统美股、航天、科学、AI/SaaS 或普通产品发布绝不属于项目推介。少于 3 条证据不能 include。"
        "市场情绪 include 的每个 evidence_post_id 都必须是不同帖子，且同时含主流/CEX 标的与作者自己的交易或态度观点；"
        "纯价格、纯新闻、上所公告不算证据。项目推介 include 的每个 evidence_post_id 都必须是不同帖子且有明确 Crypto/链上证据，"
        "并至少有一条证据直接给出作者的项目逻辑；普通产品发布、抽奖、公告不算证据。只返回 JSON。"
        + json.dumps(
            {
                "account": row["username"],
                "posts": [{"id": post["tweet_id"], "text": post["text"][:900]} for post in row["representative_posts"]],
                "format": {
                    "market_sentiment": {"decision": "include|exclude|review", "reason": "短理由", "evidence_post_ids": []},
                    "project_promotion": {"decision": "include|exclude|review", "reason": "短理由", "evidence_post_ids": []},
                },
            },
            ensure_ascii=False,
        )
    )


@dataclass
class RequestStats:
    requests: int = 0
    prompt_characters: int = 0
    response_characters: int = 0


class ModelCallError(RuntimeError):
    def __init__(
        self,
        message: str,
        stats: RequestStats,
        *,
        primary_stats: RequestStats | None = None,
        fallback_stats: RequestStats | None = None,
    ) -> None:
        super().__init__(message)
        self.stats = stats
        self.primary_stats = primary_stats or stats
        self.fallback_stats = fallback_stats or RequestStats()


def call_model(
    base_url: str,
    api_key: str,
    model: str,
    prompt_text: str,
    *,
    retries: int,
    timeout: float,
) -> tuple[dict[str, Any], RequestStats]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt_text}],
            "temperature": 0.1,
            "reasoning_effort": "none",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    last_error: Exception | None = None
    stats = RequestStats()
    for attempt in range(retries + 1):
        try:
            stats.requests += 1
            stats.prompt_characters += len(prompt_text)
            request = Request(
                f"{base_url.rstrip('/')}/chat/completions",
                data=body,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            content = str(payload["choices"][0]["message"]["content"])
            stats.response_characters += len(content)
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise ValueError("model returned no JSON object")
            parsed = json.loads(match.group(0))
            if not isinstance(parsed, dict):
                raise ValueError("model returned non-object JSON")
            return parsed, stats
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(min(8.0, 2.0**attempt))
    assert last_error is not None
    raise ModelCallError(f"{type(last_error).__name__}: {last_error}", stats) from last_error


def call_with_fallback(
    base_url: str,
    api_key: str,
    primary_model: str,
    fallback_model: str,
    row: dict[str, Any],
    *,
    retries: int,
    timeout: float,
) -> tuple[dict[str, Any], str, str | None, RequestStats, RequestStats]:
    prompt_text = prompt(row)
    empty = RequestStats()
    try:
        raw, primary_stats = call_model(
            base_url, api_key, primary_model, prompt_text, retries=retries, timeout=timeout
        )
        return raw, primary_model, None, primary_stats, empty
    except ModelCallError as primary_error:
        if not fallback_model or fallback_model == primary_model:
            raise
        try:
            raw, fallback_stats = call_model(
                base_url, api_key, fallback_model, prompt_text, retries=retries, timeout=timeout
            )
            return (
                raw,
                fallback_model,
                f"{type(primary_error).__name__}: {primary_error}",
                primary_error.stats,
                fallback_stats,
            )
        except ModelCallError as fallback_error:
            combined = RequestStats(
                requests=primary_error.stats.requests + fallback_error.stats.requests,
                prompt_characters=primary_error.stats.prompt_characters + fallback_error.stats.prompt_characters,
                response_characters=primary_error.stats.response_characters + fallback_error.stats.response_characters,
            )
            raise ModelCallError(
                f"primary={type(primary_error).__name__}: {primary_error}; "
                f"fallback={type(fallback_error).__name__}: {fallback_error}",
                combined,
                primary_stats=primary_error.stats,
                fallback_stats=fallback_error.stats,
            ) from fallback_error


def validate(raw: dict[str, Any], row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    post_by_id = {str(post["tweet_id"]): post for post in row["representative_posts"]}
    result: dict[str, dict[str, Any]] = {}
    for module in ("market_sentiment", "project_promotion"):
        value = raw.get(module, {}) if isinstance(raw, dict) else {}
        value = value if isinstance(value, dict) else {}
        decision = value.get("decision") if value.get("decision") in {"include", "exclude", "review"} else "review"
        evidence_ids: list[str] = []
        for item in value.get("evidence_post_ids", []):
            evidence_id = str(item)
            if evidence_id in post_by_id and evidence_id not in evidence_ids:
                evidence_ids.append(evidence_id)
        reason = str(value.get("reason") or "").strip()[:300]
        signal = has_market_include_evidence if module == "market_sentiment" else has_project_include_evidence
        signal_evidence = [evidence_id for evidence_id in evidence_ids if signal(str(post_by_id[evidence_id].get("text") or ""))]
        has_logic = any(has_project_logic(str(post_by_id[evidence_id].get("text") or "")) for evidence_id in evidence_ids)
        if decision == "include" and (
            len(evidence_ids) < 3
            or len(signal_evidence) < 3
            or (module == "project_promotion" and not has_logic)
            or not reason
        ):
            decision = "review"
            validation_reason = (
                "自动复核：市场 include 需要 3 条不同的主流标的加作者交易/态度原帖；"
                if module == "market_sentiment"
                else "自动复核：项目 include 需要 3 条不同的链上 Crypto 原帖证据，并包含可见项目逻辑。"
            )
            reason = f"{reason} {validation_reason}".strip()[:300]
        result[module] = {"decision": decision, "reason": reason, "evidence_post_ids": evidence_ids}
    return result


def rule_judgment(tier: str) -> tuple[dict[str, dict[str, Any]], str]:
    if tier == "no_sample":
        reason = "本地快照窗口内没有原帖样本，未调用模型。"
    else:
        reason = "确定性规则未发现持续的市场或项目高信号，未调用模型。"
    judgment = {
        module: {"decision": "review" if tier == "no_sample" else "exclude", "reason": reason, "evidence_post_ids": []}
        for module in ("market_sentiment", "project_promotion")
    }
    return judgment, reason


def report_markdown(report: dict[str, Any]) -> str:
    rows = report["accounts"]

    def decision(row: dict[str, Any], module: str) -> str:
        return str((row.get("judgment") or {}).get(module, {}).get("decision") or "")

    groups = [
        ("建议打开市场情绪", "market_sentiment", lambda row: decision(row, "market_sentiment") == "include"),
        ("建议打开项目推介", "project_promotion", lambda row: decision(row, "project_promotion") == "include"),
        ("两个方向都建议打开", "market_sentiment", lambda row: decision(row, "market_sentiment") == decision(row, "project_promotion") == "include"),
        ("待人工审阅", "market_sentiment", lambda row: row.get("status") == "failed" or any(decision(row, module) == "review" for module in ("market_sentiment", "project_promotion"))),
        ("确定性排除", "market_sentiment", lambda row: row["rough_screen"]["tier"] == "obvious_exclude"),
        ("无样本", "market_sentiment", lambda row: row["rough_screen"]["tier"] == "no_sample"),
    ]
    window = report["sample_window"]
    sample_kind = report.get("sample_kind") or "explicit_snapshot"
    kind_text = "历史抽样窗口" if sample_kind == "historical_sample" else "本地快照窗口"
    coverage_note = (
        "这些本地文件来自多个历史采集批次，时间窗口并不连续，也不构成每个账号的完整 30 天时间线。"
        if sample_kind == "historical_sample"
        else ""
    )
    lines = [
        "# X Agent 任务 1 账号初筛报告",
        "",
        "初始化建议，不是账号画像；不写订阅开关，不修改 X 快讯信源，不进入发布链路。",
        *([coverage_note] if coverage_note else []),
        "",
        f"- 样本窗口：{window['cutoff']} 至 {window['as_of']}（{kind_text}；以提供的本地快照最新帖子为准）。",
        f"- 输入账号文件：`{report['accounts_file']}`。",
        f"- 输入帖子快照：{len(report['post_files'])} 个；快照中可用帖子时间范围：{window.get('earliest_post') or '-'} 至 {window.get('latest_post') or '-'}。",
        f"- 总账号：{report['total_accounts']}；模型候选：{report['candidate_accounts']}；缓存命中：{report.get('cache_hits', 0)}；实际 HTTP 请求：{report['model_calls']}（Luna {report.get('primary_model_calls', 0)}，Terra {report.get('fallback_calls', 0)}）；失败：{report['failures']}。",
        f"- 估算 token：输入 {report.get('estimated_input_tokens', 0)}，输出 {report.get('estimated_output_tokens', 0)}；成本估算：{report.get('estimated_cost_note') or '未提供兼容路由定价，未伪造美元金额。'}",
        "",
    ]
    for title, module, predicate in groups:
        chosen = [row for row in rows if predicate(row)]
        lines.extend([f"## {title}（{len(chosen)}）", ""])
        lines.extend(
            f"- @{row['username']}：{(row.get('judgment') or {}).get(module, {}).get('reason') or row['rough_screen']['tier']}"
            for row in chosen
        )
        lines.append("")
    lines.extend(["## 代表性帖子", ""])
    for row in rows:
        if row.get("representative_posts"):
            lines.append(f"### @{row['username']}")
            lines.extend(
                f"- [{post['tweet_id']}]({post.get('url', '')}): {post['text'][:240]}"
                for post in row["representative_posts"]
            )
            lines.append("")
    return "\n".join(lines)


def make_review_judgment(reason: str) -> dict[str, dict[str, Any]]:
    return {
        module: {"decision": "review", "reason": reason, "evidence_post_ids": []}
        for module in ("market_sentiment", "project_promotion")
    }


def api_settings() -> tuple[str, str]:
    base_url = (
        os.getenv("X_AGENT_OPENAI_BASE_URL")
        or os.getenv("ODAILY_LLM_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or ""
    )
    explicit_key = os.getenv("X_AGENT_OPENAI_API_KEY") or ""
    if explicit_key:
        return base_url, explicit_key
    if base_url.startswith(("http://127.0.0.1:", "http://localhost:", "https://127.0.0.1:", "https://localhost:")):
        api_key = os.getenv("LITELLM_MASTER_KEY") or os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    else:
        api_key = os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    return base_url, api_key


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen explicit local X-account snapshots for X Agent subscriptions.")
    parser.add_argument("--accounts", required=True, type=Path, help="CSV of existing X Agent handles.")
    parser.add_argument("--posts", required=True, action="append", type=Path, help="One local content_items.json snapshot; repeat as needed.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="Local file containing X_AGENT_* or generic LLM settings.")
    parser.add_argument("--output", type=Path, default=Path("data/exports/x_agent_local_screening.json"))
    parser.add_argument("--report", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--progress", type=Path, help="Optional local progress JSON; defaults beside --output.")
    parser.add_argument("--days", type=int, default=30, help="Sample window ending at --as-of or the latest supplied post.")
    parser.add_argument("--as-of", help="UTC ISO timestamp for a reproducible snapshot window.")
    parser.add_argument(
        "--sample-kind",
        choices=("current_snapshot", "historical_sample"),
        default="current_snapshot",
        help="Describe whether supplied files form a current snapshot or a historical sample.",
    )
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--model-tier",
        choices=("all_candidates", "strong_only"),
        default="all_candidates",
        help="Send every rule candidate, or only strong candidates, to the model. Weak candidates stay review in strong_only mode.",
    )
    parser.add_argument("--model", help="Primary model. Defaults to X_AGENT_MODEL or gpt-5.6-luna.")
    parser.add_argument("--fallback-model", help="Fallback model. Defaults to X_AGENT_FALLBACK_MODEL or gpt-5.6-terra.")
    parser.add_argument("--max-accounts", type=int, default=0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--input-cost-per-million", type=float, default=0.0, help="Optional USD input-token price for this route.")
    parser.add_argument("--output-cost-per-million", type=float, default=0.0, help="Optional USD output-token price for this route.")
    args = parser.parse_args()

    load_env(args.env_file.resolve())
    args.model = args.model or os.getenv("X_AGENT_MODEL") or "gpt-5.6-luna"
    args.fallback_model = args.fallback_model or os.getenv("X_AGENT_FALLBACK_MODEL") or "gpt-5.6-terra"
    supplied_posts = [path.resolve() for path in args.posts]
    raw_post_times: list[datetime] = []
    for path in supplied_posts:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        raw_post_times.extend(
            created for row in raw if isinstance(row, dict) for created in [parse_time(row.get("created_at_iso"))] if created
        )
    if not raw_post_times:
        raise SystemExit("提供的帖子快照没有可用 created_at_iso 时间")
    as_of = parse_time(args.as_of) if args.as_of else max(raw_post_times)
    if as_of is None:
        raise SystemExit("--as-of 必须是有效 ISO 时间")
    if args.days <= 0:
        raise SystemExit("--days 必须大于 0")
    cutoff = as_of - timedelta(days=args.days)
    account_rows = load_accounts(args.accounts.resolve())
    posts_by_account = load_posts(supplied_posts, cutoff=cutoff, as_of=as_of)
    available_post_times = [
        parse_time(post.get("created_at_iso"))
        for posts in posts_by_account.values()
        for post in posts
        if parse_time(post.get("created_at_iso"))
    ]
    rows: list[dict[str, Any]] = []
    for username, info in account_rows.items():
        rough = rough_screen(posts_by_account.get(username, []))
        row = {
            "username": username,
            "display_name": info.get("name") or info.get("display_name") or "",
            "profile_url": info.get("url") or f"https://x.com/{username}",
            "post_count": len(posts_by_account.get(username, [])),
            "rough_screen": {key: value for key, value in rough.items() if key != "posts"},
            "representative_posts": rough["posts"],
        }
        row["input_fingerprint"] = fingerprint(row, cutoff=cutoff, as_of=as_of, model=args.model, fallback_model=args.fallback_model)
        if rough["tier"] in {"no_sample", "obvious_exclude"}:
            row["judgment"], _ = rule_judgment(rough["tier"])
            row["status"] = "no_sample" if rough["tier"] == "no_sample" else "rule_excluded"
        elif len(row["representative_posts"]) < 3:
            row["judgment"] = {
                module: {
                    "decision": "review",
                    "reason": "相关信号样本少于 3 条，未调用模型。",
                    "evidence_post_ids": [],
                }
                for module in ("market_sentiment", "project_promotion")
            }
            row["status"] = "insufficient_sample"
        elif args.model_tier == "strong_only" and rough["tier"] == "weak_candidate":
            row["judgment"] = {
                module: {
                    "decision": "review",
                    "reason": "仅出现弱相关信号，未调用模型；等待人工审阅或更完整本地快照。",
                    "evidence_post_ids": [],
                }
                for module in ("market_sentiment", "project_promotion")
            }
            row["status"] = "weak_candidate_review"
        rows.append(row)
    if args.max_accounts:
        rows = rows[: args.max_accounts]

    candidates = [
        row for row in rows
        if row["rough_screen"]["tier"] == "strong_candidate"
        or (args.model_tier == "all_candidates" and row["rough_screen"]["tier"] == "weak_candidate")
        and len(row["representative_posts"]) >= 3
    ]
    cache_path = args.cache or args.output.with_suffix(".cache.json")
    progress_path = args.progress or args.output.with_suffix(".progress.json")
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cache = {}
    if not isinstance(cache, dict):
        cache = {}
    base_url, api_key = api_settings()

    def run(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
        outcome = {
            "cache_hits": 0,
            "model_accounts": 0,
            "primary_model_calls": 0,
            "fallback_calls": 0,
            "prompt_characters": 0,
            "response_characters": 0,
            "fallback_accounts": 0,
        }
        cached = cache.get(row["input_fingerprint"])
        if isinstance(cached, dict) and isinstance(cached.get("judgment"), dict):
            row.update(cached)
            row["status"] = "cached"
            outcome["cache_hits"] = 1
            return row, outcome
        if not base_url or not api_key:
            row["status"] = "failed"
            row["error"] = "missing X Agent model configuration"
            row["judgment"] = make_review_judgment("模型配置缺失，待人工审阅。")
            return row, outcome
        try:
            raw, actual_model, fallback_reason, primary_stats, fallback_stats = call_with_fallback(
                base_url, api_key, args.model, args.fallback_model, row,
                retries=max(0, args.retries), timeout=max(1.0, args.timeout),
            )
            outcome.update(
                {
                    "model_accounts": 1,
                    "primary_model_calls": primary_stats.requests,
                    "fallback_calls": fallback_stats.requests,
                    "prompt_characters": primary_stats.prompt_characters + fallback_stats.prompt_characters,
                    "response_characters": primary_stats.response_characters + fallback_stats.response_characters,
                    "fallback_accounts": int(bool(fallback_reason)),
                }
            )
            row["judgment"] = validate(raw, row)
            row["model"] = actual_model
            row["fallback_reason"] = fallback_reason
            row["status"] = "success"
            return row, outcome
        except ModelCallError as exc:
            outcome.update(
                {
                    "model_accounts": 1,
                    "primary_model_calls": exc.primary_stats.requests,
                    "fallback_calls": exc.fallback_stats.requests,
                    "prompt_characters": exc.stats.prompt_characters,
                    "response_characters": exc.stats.response_characters,
                    "fallback_accounts": int(bool(exc.fallback_stats.requests)),
                }
            )
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["judgment"] = make_review_judgment("模型请求失败，待人工审阅。")
            return row, outcome
        except Exception as exc:
            row["status"] = "failed"
            row["error"] = f"{type(exc).__name__}: {exc}"
            row["judgment"] = make_review_judgment("模型请求失败，待人工审阅。")
            return row, outcome

    screened = {row["username"]: row for row in rows}
    cache_hits = model_accounts = primary_model_calls = fallback_calls = fallback_accounts = 0
    prompt_characters = response_characters = failures = 0
    completed_candidates = 0

    def write_progress() -> None:
        write_json_atomically(
            progress_path,
            {
                "status": "running",
                "sample_kind": args.sample_kind,
                "model_tier": args.model_tier,
                "candidate_accounts": len(candidates),
                "completed_candidates": completed_candidates,
                "cache_hits": cache_hits,
                "model_accounts": model_accounts,
                "model_calls": primary_model_calls + fallback_calls,
                "primary_model_calls": primary_model_calls,
                "fallback_calls": fallback_calls,
                "failures": failures,
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )

    write_progress()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(run, row) for row in candidates]
        for future in as_completed(futures):
            row, outcome = future.result()
            screened[row["username"]] = row
            cache_hits += outcome["cache_hits"]
            model_accounts += outcome["model_accounts"]
            primary_model_calls += outcome["primary_model_calls"]
            fallback_calls += outcome["fallback_calls"]
            fallback_accounts += outcome["fallback_accounts"]
            prompt_characters += outcome["prompt_characters"]
            response_characters += outcome["response_characters"]
            failures += int(row["status"] == "failed")
            completed_candidates += 1
            if row["status"] in {"success", "cached"}:
                cache[row["input_fingerprint"]] = {
                    "judgment": row["judgment"],
                    "model": row.get("model"),
                    "fallback_reason": row.get("fallback_reason"),
                }
                write_json_atomically(cache_path, cache)
            if completed_candidates % 20 == 0 or completed_candidates == len(candidates):
                write_progress()
    final_rows = [screened[row["username"]] for row in rows]
    estimated_input_tokens = math.ceil(prompt_characters / 4)
    estimated_output_tokens = math.ceil(response_characters / 4)
    estimated_cost = None
    if args.input_cost_per_million or args.output_cost_per_million:
        estimated_cost = (
            estimated_input_tokens * max(0.0, args.input_cost_per_million)
            + estimated_output_tokens * max(0.0, args.output_cost_per_million)
        ) / 1_000_000
    output = {
        "source": "explicit local snapshots",
        "sample_kind": args.sample_kind,
        "accounts_file": str(args.accounts.resolve()),
        "post_files": [str(path) for path in supplied_posts],
        "model": args.model,
        "fallback_model": args.fallback_model,
        "sample_window": {
            "cutoff": cutoff.isoformat(),
            "as_of": as_of.isoformat(),
            "earliest_post": min(available_post_times).isoformat() if available_post_times else None,
            "latest_post": max(available_post_times).isoformat() if available_post_times else None,
        },
        "total_accounts": len(final_rows),
        "candidate_accounts": len(candidates),
        "cache_hits": cache_hits,
        "model_accounts": model_accounts,
        "model_calls": primary_model_calls + fallback_calls,
        "primary_model_calls": primary_model_calls,
        "fallback_calls": fallback_calls,
        "fallback_accounts": fallback_accounts,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_cost_usd": estimated_cost,
        "estimated_cost_note": (
            f"${estimated_cost:.6f}，基于命令行提供的每百万 token 路由价格。"
            if estimated_cost is not None
            else "未提供兼容路由定价，按字符估算 token，不提供美元金额。"
        ),
        "failures": failures,
        "accounts": final_rows,
    }
    write_json_atomically(args.output, output)
    write_json_atomically(cache_path, cache)
    write_json_atomically(
        progress_path,
        {
            "status": "complete",
            "candidate_accounts": len(candidates),
            "completed_candidates": completed_candidates,
            "cache_hits": cache_hits,
            "model_accounts": model_accounts,
            "model_calls": primary_model_calls + fallback_calls,
            "primary_model_calls": primary_model_calls,
            "fallback_calls": fallback_calls,
            "failures": failures,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    (args.report or args.output.with_suffix(".md")).write_text(report_markdown(output), encoding="utf-8")
    print(
        json.dumps(
            {key: output[key] for key in ("total_accounts", "candidate_accounts", "cache_hits", "model_calls", "fallback_calls", "failures")},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
