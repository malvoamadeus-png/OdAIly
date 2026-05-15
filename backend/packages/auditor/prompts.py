from __future__ import annotations

import json
import re
from typing import Any

from .models import AuditorIssue, AuditorResult, AuditorTask


AUDITOR_PROMPT_VERSION = "auditor_zh_quality_v1"


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
                        "type": {"type": "string", "enum": ["punctuation", "grammar", "typo", "spacing", "format", "other"]},
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
- 中英文和数字排版异常：数字、币种、英文缩写、百分号、括号附近的明显空格异常。

不要检查：
- Odaily 固定前缀、媒体称谓、常见术语格式是否统一。
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
        if issue_type not in {"punctuation", "grammar", "typo", "spacing", "format", "other"}:
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

