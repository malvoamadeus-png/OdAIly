from __future__ import annotations

import json
import re
from typing import Any

from .models import AuditorIssue, AuditorResult, AuditorTask


AUDITOR_PROMPT_VERSION = "auditor_zh_quality_v4"


FIXED_TRAILING_SLOGANS = ("在定价之前，看见变化",)
HEADLINE_QUANTIFIER_PREFIXES = ("一个", "一名", "一位", "一家", "一则", "一笔", "一处", "一项")
CHAIN_TRANSFER_ACTION_WORDS = ("提出", "转出", "转入", "提取", "存入")
TRADING_ACTION_WORDS = ("购入", "买入", "加仓")


AUDITOR_SCHEMA = {
    "type": "json_schema",
    "name": "odaily_auditor_zh_quality",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "has_issue": {"type": "boolean"},
            "severity": {"type": "string", "enum": ["low", "medium", "high"]},
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "enum": ["punctuation", "grammar", "typo", "format", "other"]},
                        "location": {"type": "string", "enum": ["title", "content"]},
                        "original": {"type": "string"},
                        "suggested": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["type", "location", "original", "suggested", "reason"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["has_issue", "severity", "issues", "summary"],
    },
    "strict": True,
}


def build_auditor_prompt(task: AuditorTask) -> str:
    return f"""你是 Odaily 已发布快讯的中文质量审核者。请只检查明确的中文文本质量问题，并输出结构化 JSON。

只检查：
- 标点错误：连续标点、英文标点混用、括号或引号不配对、明显句末标点缺失。
- 语法错误：主谓宾残缺、语序明显不通、重复粘贴、断句异常。
- 错别字或同音误用：只在上下文非常明确时提示。

不要检查：
- 中英文之间、数字与中文单位之间、数字与币种之间、英文缩写与括号之间、中文与括号之间的空格风格。
- 千分位逗号、钱包地址展示、省略号、半角/全角括号、数量表达是否统一。
- Odaily 固定前缀、媒体称谓、常见术语格式是否统一。
- 正文尾部固定标语，例如“在定价之前，看见变化”。
- 可省略的结构助词“的”，例如“14.25%消费税”“应 Shielded Labs 请求”这类表达不要提示补“的”。
- 新闻标题中的常见省略式表达，例如“一男子”“一女子”“一地址”“一新创建地址”等标题体量词省略，不按语法残缺处理。
- 链上、钱包、地址、交易所资金流转语境中的动作词误判；对“提出 / 转出 / 转入 / 提取 / 存入”等表述，若上下文可解释为 transfer、withdraw、deposit 等链上动作，不要擅自改成“买入 / 购入 / 加仓”。
- 风格润色、标题吸引力、表达是否更优雅。
- 事实真伪、价格数据、链上数据、来源可靠性。
- 加密行业项目名、交易所名、代币名、英文缩写或链名是否应翻译。

报警标准：
- 只有明确错误或高置信异常才设置 has_issue=true。
- 可改可不改的表达，has_issue=false。
- has_issue=false 时 issues 必须为空数组，summary 为空字符串。
- issue.original 必须是标题或正文中真实存在的短片段。
- issue.suggested 只给最小必要改法，不整段重写。
- 不输出推理过程。

【标题】
{task.title or ""}

【正文】
{task.content}
"""


def parse_auditor_output(raw_output: str, task: AuditorTask) -> AuditorResult:
    payload = _loads_json_object(raw_output)
    severity = str(payload.get("severity") or "low")
    if severity not in {"low", "medium", "high"}:
        severity = "low"
    issues: list[AuditorIssue] = []
    for raw_issue in payload.get("issues") or []:
        if not isinstance(raw_issue, dict):
            continue
        issue_type = str(raw_issue.get("type") or "other")
        if issue_type == "spacing":
            continue
        if issue_type not in {"punctuation", "grammar", "typo", "format", "other"}:
            issue_type = "other"
        location = str(raw_issue.get("location") or "content")
        if location not in {"title", "content"}:
            location = "content"
        original = _clean(str(raw_issue.get("original") or ""))
        suggested = _clean(str(raw_issue.get("suggested") or ""))
        reason = _clean(str(raw_issue.get("reason") or ""))
        source_text = task.title if location == "title" else task.content
        if not original or original not in (source_text or ""):
            continue
        if not suggested or suggested == original:
            continue
        if _should_ignore_issue(location=location, original=original, suggested=suggested, reason=reason, task=task):
            continue
        issues.append(
            AuditorIssue(
                issue_type=issue_type,  # type: ignore[arg-type]
                location=location,
                original=original,
                suggested=suggested,
                reason=reason,
            )
        )
    has_issue = bool(payload.get("has_issue")) and bool(issues)
    return AuditorResult(
        has_issue=has_issue,
        severity=severity if has_issue else "low",  # type: ignore[arg-type]
        issues=issues if has_issue else [],
        summary=_clean(str(payload.get("summary") or "")) if has_issue else "",
    )


def auditor_result_to_dict(result: AuditorResult) -> dict[str, Any]:
    return {
        "has_issue": result.has_issue,
        "severity": result.severity,
        "issues": [
            {
                "type": issue.issue_type,
                "location": issue.location,
                "original": issue.original,
                "suggested": issue.suggested,
                "reason": issue.reason,
            }
            for issue in result.issues
        ],
        "summary": result.summary,
    }


def _loads_json_object(raw_output: str) -> dict[str, Any]:
    text = raw_output.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"```$", "", text).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("auditor model output is not a JSON object")
    return payload


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _should_ignore_issue(*, location: str, original: str, suggested: str, reason: str, task: AuditorTask) -> bool:
    if _is_fixed_trailing_slogan_issue(location=location, original=original, task=task):
        return True
    if _is_missing_de_issue(original=original, suggested=suggested, reason=reason):
        return True
    if _is_headline_quantifier_expansion_issue(location=location, original=original, suggested=suggested, task=task):
        return True
    if _is_chain_transfer_action_issue(original=original, suggested=suggested):
        return True
    return False


def _is_fixed_trailing_slogan_issue(*, location: str, original: str, task: AuditorTask) -> bool:
    if location != "content":
        return False
    content = (task.content or "").rstrip()
    for slogan in FIXED_TRAILING_SLOGANS:
        if original in slogan and content.endswith(slogan):
            return True
    return False


def _is_missing_de_issue(*, original: str, suggested: str, reason: str) -> bool:
    reason_text = _normalize_for_de_check(reason)
    if "结构助词的" in reason_text:
        return True
    if any(phrase in reason_text for phrase in ("缺少的", "补的", "补充的", "补上的", "补加的")):
        return True
    original_text = _normalize_for_de_check(original)
    suggested_text = _normalize_for_de_check(suggested)
    return suggested_text.replace("的", "") == original_text


def _normalize_for_de_check(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("“", "").replace("”", "").replace('"', "")


def _is_headline_quantifier_expansion_issue(*, location: str, original: str, suggested: str, task: AuditorTask) -> bool:
    if location != "title":
        return False
    title = _normalize_for_de_check(task.title or "")
    if not title or _normalize_for_de_check(original) not in title:
        return False
    original_text = _normalize_for_de_check(original)
    suggested_text = _normalize_for_de_check(suggested)
    if not original_text.startswith("一") or len(original_text) < 2:
        return False
    original_tail = original_text[1:].replace("的", "")
    for prefix in HEADLINE_QUANTIFIER_PREFIXES:
        if not suggested_text.startswith(prefix):
            continue
        suggested_tail = suggested_text[len(prefix) :].replace("的", "")
        if suggested_tail == original_tail:
            return True
    return False


def _is_chain_transfer_action_issue(*, original: str, suggested: str) -> bool:
    original_text = _normalize_for_de_check(original)
    suggested_text = _normalize_for_de_check(suggested)
    if not any(original_text.startswith(word) for word in CHAIN_TRANSFER_ACTION_WORDS):
        return False
    return any(suggested_text.startswith(word) for word in TRADING_ACTION_WORDS)
