from __future__ import annotations

import json
import re
from typing import Any

from .models import AuditorIssue, AuditorResult, AuditorTask


AUDITOR_PROMPT_VERSION = "auditor_zh_quality_v9"


FIXED_TRAILING_SLOGANS = ("在定价之前，看见变化",)
HEADLINE_QUANTIFIER_PREFIXES = ("一个", "一名", "一位", "一家", "一则", "一笔", "一处", "一项")
CHAIN_TRANSFER_ACTION_WORDS = ("提出", "转出", "转入", "提取", "存入")
TRADING_ACTION_WORDS = ("购入", "买入", "加仓")
ENTITY_CORRECTION_KEYWORDS = (
    "人名",
    "姓名",
    "名称",
    "称呼",
    "机构",
    "职位",
    "头衔",
    "地名",
    "项目名",
    "项目名称",
    "代币名",
    "币种",
    "链名",
    "公司名",
    "组织名",
    "实体",
    "主席",
    "总统",
    "总理",
    "首相",
    "部长",
    "议长",
    "ceo",
    "首席执行官",
    "创始人",
    "美联储",
    "联储",
    "交易所",
    "基金",
    "协议",
    "网络",
)


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
                        "evidence": {"type": "string"},
                    },
                    "required": ["type", "location", "original", "suggested", "reason", "evidence"],
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
- 语法错误：主谓宾残缺、语序明显不通、重复粘贴、断句异常。
- 错别字或同音误用：只在上下文非常明确时提示。
- 明确格式异常：例如重复粘贴导致的结构损坏，但不要把纯标点样式问题当成格式异常。

不要检查：
- 标点符号问题本身，包括连续标点、英文标点混用、括号或引号不配对、明显句末标点缺失、半角/全角标点切换。
- 中英文之间、数字与中文单位之间、数字与币种之间、英文缩写与括号之间、中文与括号之间的空格风格。
- 千分位逗号、钱包地址展示、省略号、半角/全角括号、数量表达是否统一。
- Odaily 固定前缀、媒体称谓、常见术语格式是否统一。
- 正文里已经存在的分行、段落换行或列表换行；不要仅因上一行末尾没有句号、下一行以“交易开放时间：”“杠杆倍数：”等标签开头就报警。
- 正文尾部固定标语，例如“在定价之前，看见变化”。
- 可省略的结构助词“的”，例如“14.25%消费税”“应 Shielded Labs 请求”这类表达不要提示补“的”。
- 新闻标题中的常见省略式表达，例如“一男子”“一女子”“一地址”“一新创建地址”等标题体量词省略，不按语法残缺处理。
- 链上、钱包、地址、交易所资金流转语境中的动作词误判；对“提出 / 转出 / 转入 / 提取 / 存入”等表述，若上下文可解释为 transfer、withdraw、deposit 等链上动作，不要擅自改成“买入 / 购入 / 加仓”。
- 风格润色、标题吸引力、表达是否更优雅。
- 事实真伪、价格数据、链上数据、来源可靠性。
- 加密行业项目名、交易所名、代币名、英文缩写或链名是否应翻译。
- 人名、机构、职位、地名、项目名、代币名等实体替换；除非同一条标题或正文内部存在可直接引用的矛盾，并且证据片段里直接出现建议替换后的实体名称，否则不要报警。
- 不要仅凭常识猜测数字、金额、日期、比例、数量级是否写错；如果想指出这类问题，必须能在同一条标题或正文中引用另一处直接构成矛盾或校验关系的依据片段。
- 不要根据外部背景知识、历史事件印象、常识时间线去猜测日期或年份是否写错；只有同一条标题或正文内部出现可直接引用的日期矛盾或明确时间关系冲突时，才允许提示。

报警标准：
- 只有明确错误或高置信异常才设置 has_issue=true。
- 可改可不改的表达，has_issue=false。
- has_issue=false 时 issues 必须为空数组，summary 为空字符串。
- issue.original 必须是标题或正文中真实存在的短片段。
- issue.suggested 只给最小必要改法，不整段重写。
- issue.reason 必须直接说明错因，不能只说“疑似有误”。
- issue.evidence 仅在涉及数字、金额、日期、比例、数量级等数据类指正时填写；必须引用同一条标题或正文中真实存在、且能支持该指正的另一处片段。其他问题填空字符串。
- 如果 issue.suggested 改动了日期或年份，issue.evidence 必须包含同一条标题或正文中的另一处完整日期，或能直接证明时间关系冲突的原文片段；做不到时必须输出 has_issue=false。
- 如果 issue.suggested 改动了人名、机构、职位、地名、项目名、代币名等实体，issue.evidence 必须引用同一条标题或正文中直接出现建议实体的原文片段；做不到时必须输出 has_issue=false。
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
        if issue_type == "punctuation":
            continue
        if issue_type not in {"punctuation", "grammar", "typo", "format", "other"}:
            issue_type = "other"
        location = str(raw_issue.get("location") or "content")
        if location not in {"title", "content"}:
            location = "content"
        original = _clean_excerpt(str(raw_issue.get("original") or ""))
        suggested = _clean_excerpt(str(raw_issue.get("suggested") or ""))
        reason = _clean(str(raw_issue.get("reason") or ""))
        evidence = _clean_excerpt(str(raw_issue.get("evidence") or ""))
        source_text = task.title if location == "title" else task.content
        if not original or original not in (source_text or ""):
            continue
        if not suggested or suggested == original:
            continue
        if _should_ignore_issue(
            location=location,
            original=original,
            suggested=suggested,
            reason=reason,
            evidence=evidence,
            task=task,
        ):
            continue
        issues.append(
            AuditorIssue(
                issue_type=issue_type,  # type: ignore[arg-type]
                location=location,
                original=original,
                suggested=suggested,
                reason=reason,
                evidence=evidence,
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
                "evidence": issue.evidence,
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


def _clean_excerpt(value: str) -> str:
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in value.strip().splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _should_ignore_issue(*, location: str, original: str, suggested: str, reason: str, evidence: str, task: AuditorTask) -> bool:
    if _is_punctuation_only_issue(original=original, suggested=suggested, reason=reason):
        return True
    if _is_fixed_trailing_slogan_issue(location=location, original=original, task=task):
        return True
    if _is_line_break_boundary_punctuation_issue(location=location, original=original, suggested=suggested):
        return True
    if _is_missing_de_issue(original=original, suggested=suggested, reason=reason):
        return True
    if _is_headline_quantifier_expansion_issue(location=location, original=original, suggested=suggested, task=task):
        return True
    if _is_chain_transfer_action_issue(original=original, suggested=suggested):
        return True
    if _is_unsupported_entity_correction_issue(
        original=original,
        suggested=suggested,
        reason=reason,
        evidence=evidence,
        task=task,
    ):
        return True
    if _is_unsupported_fact_correction_issue(
        location=location,
        original=original,
        suggested=suggested,
        reason=reason,
        evidence=evidence,
        task=task,
    ):
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


def _is_punctuation_only_issue(*, original: str, suggested: str, reason: str) -> bool:
    normalized_original = _normalize_for_punctuation_check(original)
    normalized_suggested = _normalize_for_punctuation_check(suggested)
    if normalized_original != normalized_suggested:
        return False
    return _contains_punctuation_keyword(reason) or original != suggested


def _normalize_for_punctuation_check(value: str) -> str:
    punctuation_map = str.maketrans(
        {
            "（": "(",
            "）": ")",
            "【": "[",
            "】": "]",
            "“": '"',
            "”": '"',
            "‘": "'",
            "’": "'",
            "，": ",",
            "。": ".",
            "：": ":",
            "；": ";",
            "！": "!",
            "？": "?",
        }
    )
    translated = value.translate(punctuation_map)
    translated = re.sub(r"[\s()\[\]\"'.,:;!?]", "", translated)
    return translated


def _contains_punctuation_keyword(value: str) -> bool:
    normalized = _normalize_for_fact_check(value).lower()
    return any(
        keyword in normalized
        for keyword in (
            "标点",
            "括号",
            "引号",
            "句号",
            "逗号",
            "分号",
            "冒号",
            "问号",
            "叹号",
            "顿号",
            "punctuation",
            "parenth",
            "quote",
        )
    )


def _is_line_break_boundary_punctuation_issue(*, location: str, original: str, suggested: str) -> bool:
    if location != "content" or "\n" not in original:
        return False
    normalized_suggested = re.sub(r"[。！？；]+\n", "\n", suggested)
    return normalized_suggested == original


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


def _is_unsupported_entity_correction_issue(
    *,
    original: str,
    suggested: str,
    reason: str,
    evidence: str,
    task: AuditorTask,
) -> bool:
    if not _looks_like_entity_correction_issue(original=original, suggested=suggested, reason=reason):
        return False
    source_text = _build_evidence_source_text(task)
    if not evidence:
        return True
    if evidence == original:
        return True
    if evidence not in source_text:
        return True
    if suggested not in evidence:
        return True
    return False


def _is_unsupported_fact_correction_issue(
    *,
    location: str,
    original: str,
    suggested: str,
    reason: str,
    evidence: str,
    task: AuditorTask,
) -> bool:
    if not _looks_like_fact_correction_issue(original=original, suggested=suggested, reason=reason):
        return False
    source_text = _build_evidence_source_text(task)
    if not evidence:
        return True
    if evidence == original:
        return True
    if evidence not in (source_text or ""):
        return True
    if _changes_date_like_value(original=original, suggested=suggested) and not _has_date_like_evidence(evidence):
        return True
    return False


def _looks_like_entity_correction_issue(*, original: str, suggested: str, reason: str) -> bool:
    normalized_original = _normalize_for_fact_check(original)
    normalized_suggested = _normalize_for_fact_check(suggested)
    if normalized_original == normalized_suggested:
        return False
    combined = "\n".join((normalized_original, normalized_suggested, _normalize_for_fact_check(reason).lower()))
    if any(keyword in combined for keyword in ENTITY_CORRECTION_KEYWORDS):
        return True
    return _looks_like_ticker_replacement(original=normalized_original, suggested=normalized_suggested)


def _looks_like_fact_correction_issue(*, original: str, suggested: str, reason: str) -> bool:
    normalized_original = _normalize_for_fact_check(original)
    normalized_suggested = _normalize_for_fact_check(suggested)
    if normalized_original == normalized_suggested:
        return False
    if _contains_fact_keyword(reason):
        return True
    original_numbers = _extract_fact_tokens(normalized_original)
    suggested_numbers = _extract_fact_tokens(normalized_suggested)
    if original_numbers != suggested_numbers:
        return bool(original_numbers or suggested_numbers)
    return False


def _normalize_for_fact_check(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")


def _contains_fact_keyword(value: str) -> bool:
    normalized = _normalize_for_fact_check(value).lower()
    return any(
        keyword in normalized
        for keyword in (
            "数据",
            "数字",
            "金额",
            "日期",
            "比例",
            "数量级",
            "数值",
            "单位",
            "韩元",
            "美元",
            "usdt",
            "btc",
            "eth",
            "%",
        )
    )


def _extract_fact_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"\d+(?:\.\d+)?(?:[%％]|万亿|亿|万|千|百|美元|美金|韩元|人民币|元|枚|股|年|月|日|天|倍|x)?",
            value,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_ticker_replacement(*, original: str, suggested: str) -> bool:
    ticker_pattern = re.compile(r"^[A-Z0-9]{2,12}$")
    return bool(ticker_pattern.fullmatch(original) and ticker_pattern.fullmatch(suggested))


def _changes_date_like_value(*, original: str, suggested: str) -> bool:
    return _extract_date_like_tokens(_normalize_for_fact_check(original)) != _extract_date_like_tokens(
        _normalize_for_fact_check(suggested)
    )


def _has_date_like_evidence(value: str) -> bool:
    return bool(_extract_date_like_tokens(_normalize_for_fact_check(value)))


def _extract_date_like_tokens(value: str) -> tuple[str, ...]:
    return tuple(
        re.findall(
            r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月|\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}|\d{4}/\d{1,2}/\d{1,2}",
            value,
            flags=re.IGNORECASE,
        )
    )


def _build_evidence_source_text(task: AuditorTask) -> str:
    return "\n".join(part for part in (task.title or "", task.content or "") if part)
