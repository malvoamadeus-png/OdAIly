from __future__ import annotations

import json
import re
from typing import Any

from .matching import clean_alias, clean_text, dedupe_aliases
from .models import AnalysisResult, ContextResult, FocusSubject, Writer3Candidate, Writer3Task


ANALYSIS_SCHEMA = {
    "type": "json_schema",
    "name": "writer3_focus_analysis",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "should_run_writer3": {"type": "boolean"},
            "current_event_type": {
                "type": "string",
                "enum": [
                    "financing",
                    "mainnet_launch",
                    "testnet_launch",
                    "airdrop",
                    "tokenomics",
                    "regulation",
                    "bill",
                    "lawsuit",
                    "security",
                    "none",
                ],
            },
            "focus_subject": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "type", "aliases"],
            },
            "context_entities": {"type": "array", "items": {"type": "string"}},
            "matter_key": {"type": "string"},
            "matter_aliases": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "should_run_writer3",
            "current_event_type",
            "focus_subject",
            "context_entities",
            "matter_key",
            "matter_aliases",
        ],
    },
    "strict": True,
}


CONTEXT_SCHEMA = {
    "type": "json_schema",
    "name": "writer3_context_text",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "should_write": {"type": "boolean"},
            "context_text": {"type": "string"},
            "evidence_source_item_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["should_write", "context_text", "evidence_source_item_ids"],
    },
    "strict": True,
}


def build_analysis_prompt(task: Writer3Task) -> str:
    return f"""你是 Odaily 快讯编写者3的生产解析器。请判断当前已发布快讯是否进入“此前消息”候选检索，并抽取焦点主体。

只允许以下正向触发类型进入：
- financing: 项目或协议融资。
- mainnet_launch: 项目、协议或网络主网上线。
- testnet_launch: 项目、协议或网络测试网上线。
- airdrop: 空投、申领、发放等重要空投事实。
- tokenomics: 代币经济学、代币分配、TGE 等关键事实。
- regulation: 监管政策或监管机构动作。
- bill: 法案阶段变化。
- lawsuit: 诉讼、法院裁决、和解等案件进展。
- security: 黑客攻击、漏洞、损失、追踪、赔付或恢复进展。

以下一律不进入，即使文本里出现“上线”“融资”等词：
- 报价、价格结果、涨跌幅、突破、跌破等价格交易类快讯。
- 交易所上币、上线交易对、Launchpool、Alpha、现货或合约上线等 listing 类快讯。
- 普通合作、活动、社区动态、泛公告。
- 仅因同一主体近期有其他新闻而可能相关的内容。

焦点主体是当前事件真正发生在其身上的对象，不是所有被提到的实体。交易所、监管机构、法院、媒体、投资机构等实体只有在其本身就是当前事件对象时，才可作为焦点主体；否则只放入 context_entities。

输出 JSON：
- should_run_writer3: boolean
- current_event_type: financing/mainnet_launch/testnet_launch/airdrop/tokenomics/regulation/bill/lawsuit/security/none
- focus_subject: name/type/aliases
- context_entities: string[]
- matter_key: 法案、诉讼、监管类的同一事项检索锚点；其他类型可为空字符串
- matter_aliases: matter_key 的别名、简称、案名、法案名、核心当事方+争议主题组合；其他类型可为空数组

法案、诉讼、监管类必须输出 matter_key 和 matter_aliases。不要把 SEC、CFTC、司法部、法院、参议院、众议院、委员会、美联储等高频机构单独作为 matter_key。matter_key 应该指向同一法案、同一案件或同一监管事项，例如“CLARITY Act”“Kalshi预测市场管辖权争议”“Bitcoin Fog上诉案”。

【标题】
{task.title or ""}

【正文】
{task.final_content or task.content}
"""


def build_context_prompt(task: Writer3Task, analysis: AnalysisResult, candidates: list[Writer3Candidate]) -> str:
    candidate_lines: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        candidate_lines.append(
            f"""候选 {index}
source_item_id: {candidate.source_item_id}
候选分数: {candidate.score:.2f}
时间: {candidate.published_at.isoformat() if candidate.published_at else ""}
标题: {candidate.title or ""}
命中别名: {"、".join(candidate.matched_aliases)}
命中前序类型: {"、".join(candidate.matched_prior_types)}
正文: {preview(candidate.content, 700)}
"""
        )
    return f"""你是 Odaily 快讯编写者3。请根据当前快讯和此前候选，判断候选中是否存在可作为“此前消息”的前序事实，并在可用时写出此前消息。

要求：
- 可以使用一条或多条候选，但只能使用候选中明确支持的事实，不补充候选没有写明的信息。
- 被使用的候选必须与当前快讯存在明确前序关系，不只是同主体弱相关、同机构弱相关或同赛道弱相关。
- context_text 是可直接发布的新闻正文，只陈述候选中的前序事实，不写你的判断过程、选择理由或关联分析。
- context_text 必须以“此前消息，”开头。不要把“为什么相关”“如何衔接当前事件”写进正文；这些是判断过程，不是可发布事实。
- 不要写虚性描述、抽象评价或候选没有明确说出的延伸判断，例如“引发持续讨论”“受到关注”“延续布局”“形成前后衔接”“提供前情”“可视为下一步”。如果候选只提供了具体动作、文件、金额、条款或进展，就只写这些具体事实。
- 不要硬套“主体+于+时间+发生/宣布/完成/进入”的句式，不要输出 ISO 时间戳；除非候选原文中的自然日期对理解前因后果必要，通常不要写时间。
- 如果没有候选真正可用，should_write=false，context_text 为空字符串，evidence_source_item_ids 为空数组。

输出 JSON：
- should_write: boolean
- context_text: string
- evidence_source_item_ids: string[]

【当前快讯】
时间: {task.published_at.isoformat() if task.published_at else ""}
事件类型: {analysis.current_event_type}
焦点主体: {analysis.focus_subject.name}
事项锚点: {analysis.matter_key}
标题: {task.title or ""}
正文: {preview(task.final_content, 900)}

【此前候选】
{''.join(candidate_lines)}
"""


def parse_analysis_output(raw_output: str) -> AnalysisResult:
    payload = _loads_json_object(raw_output)
    event_type = str(payload.get("current_event_type") or "none")
    if event_type not in ANALYSIS_SCHEMA["schema"]["properties"]["current_event_type"]["enum"]:
        event_type = "none"
    focus = payload.get("focus_subject") if isinstance(payload.get("focus_subject"), dict) else {}
    name = clean_alias(str(focus.get("name") or ""))
    subject_type = clean_alias(str(focus.get("type") or "none")) or "none"
    aliases = dedupe_aliases([str(item) for item in focus.get("aliases", []) if str(item).strip()])
    context_entities = dedupe_aliases([str(item) for item in payload.get("context_entities", []) if str(item).strip()])
    matter_key = clean_alias(str(payload.get("matter_key") or ""))
    matter_aliases = dedupe_aliases([str(item) for item in payload.get("matter_aliases", []) if str(item).strip()])
    should_run = bool(payload.get("should_run_writer3")) and event_type != "none"
    if event_type in {"regulation", "bill", "lawsuit"} and not matter_aliases and not matter_key:
        should_run = False
    if should_run and event_type not in {"regulation", "bill", "lawsuit"} and not name:
        should_run = False
    if not should_run:
        event_type = "none"
        name = ""
        subject_type = "none"
        aliases = []
        matter_key = ""
        matter_aliases = []
    return AnalysisResult(
        should_run_writer3=should_run,
        current_event_type=event_type,  # type: ignore[arg-type]
        focus_subject=FocusSubject(name=name, subject_type=subject_type, aliases=aliases),
        context_entities=context_entities,
        matter_key=matter_key,
        matter_aliases=matter_aliases,
    )


def parse_context_output(raw_output: str) -> ContextResult:
    payload = _loads_json_object(raw_output)
    should_write = bool(payload.get("should_write"))
    context_text = clean_text(str(payload.get("context_text") or ""))
    evidence_ids = [clean_alias(str(item)) for item in payload.get("evidence_source_item_ids", []) if clean_alias(str(item))]
    if should_write and not context_text.startswith("此前消息，"):
        context_text = f"此前消息，{context_text.removeprefix('此前消息').lstrip('，, ')}"
    if not should_write:
        context_text = ""
        evidence_ids = []
    return ContextResult(should_write=should_write, context_text=context_text, evidence_source_item_ids=evidence_ids)


def preview(value: str | None, max_chars: int) -> str:
    text = clean_text(value)
    return text if len(text) <= max_chars else text[:max_chars] + "..."


def _loads_json_object(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("model output is not a JSON object")
    return payload
