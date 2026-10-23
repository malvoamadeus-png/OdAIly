from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .formatter import parse_draft_output
from .models import DraftBrief, TaskRecord
from packages.newsflash_priority import parse_newsflash_priority


TITLE_STRATEGIES = (
    "plain",
    "speaker_anchor",
    "entity_front",
    "action_first",
    "result_front",
    "amount_front",
    "time_window_front",
)

TITLE_RULES = (
    "known_speaker_anchor",
    "entity_front",
    "action_first",
    "result_change_front",
    "amount_front",
    "time_window_front",
    "plain_direct",
    "feature_subject_amplification",
)

DEFAULT_KNOWN_TITLE_SUBJECT_NAMES: tuple[str, ...] = ("Rune", "Cobie", "Vitalik", "CZ")
KNOWN_TITLE_SUBJECTS: tuple[str, ...] = DEFAULT_KNOWN_TITLE_SUBJECT_NAMES
KNOWN_TITLE_SUBJECT_SPLIT_PATTERN = re.compile(r"[、,\n\r;；，]+")


WRITER_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "odaily_writer_with_title_trace",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "title_strategy": {"type": "string", "enum": list(TITLE_STRATEGIES)},
            "title_strategy_reason": {"type": "string"},
            "matched_title_rules": {
                "type": "array",
                "items": {"type": "string", "enum": list(TITLE_RULES)},
            },
            "feature_mode_applied": {"type": "boolean"},
            "feature_mode_reason": {"type": "string"},
        },
        "required": [
            "title",
            "content",
            "title_strategy",
            "title_strategy_reason",
            "matched_title_rules",
            "feature_mode_applied",
            "feature_mode_reason",
        ],
    },
    "strict": True,
}

EVENT_TRACKING_WRITER_JSON_SCHEMA = {
    **WRITER_JSON_SCHEMA,
    "name": "odaily_event_tracking_writer_with_priority",
    "schema": {
        **WRITER_JSON_SCHEMA["schema"],
        "properties": {
            **WRITER_JSON_SCHEMA["schema"]["properties"],
            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
            "priority_reason": {"type": "string"},
        },
    },
    "strict": False,
}


@dataclass(frozen=True, slots=True)
class StructuredWriterResult:
    draft: DraftBrief
    trace: dict[str, Any]
    priority_level: str | None = None
    priority_reason: str | None = None
    priority_status: str = "not_applicable"
    priority_error: str | None = None


def normalize_known_title_subject_names(value: str | list[Any] | tuple[Any, ...] | None) -> list[str]:
    if value is None:
        raw_items: list[Any] = []
    elif isinstance(value, str):
        raw_items = KNOWN_TITLE_SUBJECT_SPLIT_PATTERN.split(value)
    else:
        raw_items = list(value)
    names: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        if item is None:
            continue
        name = re.sub(r"\s+", " ", str(item)).strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def match_known_title_subjects(
    task: TaskRecord,
    *,
    subject_names: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    names = normalize_known_title_subject_names(
        list(DEFAULT_KNOWN_TITLE_SUBJECT_NAMES) if subject_names is None else subject_names
    )
    metadata_values = [
        task.metadata.get("effective_author_name"),
        task.metadata.get("author_display_name"),
        task.metadata.get("author_username"),
        task.metadata.get("account_username"),
    ]
    text = "\n".join(
        [task.title or "", task.content, *(str(value) for value in metadata_values if value)]
    )
    matches: list[dict[str, str]] = []
    for name in names:
        if not _alias_matches(text, name):
            continue
        matches.append(
            {
                "key": name.casefold(),
                "name": name,
                "matched_alias": name,
            }
        )
    return matches


def build_known_subject_prompt(matches: list[dict[str, str]]) -> str:
    if not matches:
        return ""
    names = normalize_known_title_subject_names([match["name"] for match in matches if match.get("name")])
    if not names:
        return ""
    return (
        f"当前材料命中知名人物：{'、'.join(names)}。"
        "遇到这些人物观点时可使用“人名：观点”标题。"
    )


def parse_structured_writer_output(
    raw_output: str,
    *,
    known_subjects: list[dict[str, str]],
    feature_mode_enabled: bool,
    include_newsflash_priority: bool = False,
) -> StructuredWriterResult:
    try:
        payload = json.loads(_strip_json_fence(raw_output))
    except Exception as exc:
        raise ValueError("writer output must be a valid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("writer output must be a JSON object")

    title = payload.get("title")
    content = payload.get("content")
    strategy = payload.get("title_strategy")
    strategy_reason = payload.get("title_strategy_reason")
    matched_rules = payload.get("matched_title_rules")
    feature_applied = payload.get("feature_mode_applied")
    feature_reason = payload.get("feature_mode_reason")
    if not isinstance(title, str) or not isinstance(content, str):
        raise ValueError("writer output title and content must be strings")
    strategy_valid = isinstance(strategy, str) and strategy in TITLE_STRATEGIES
    strategy_reason_valid = isinstance(strategy_reason, str)
    matched_rules_valid = isinstance(matched_rules, list) and all(
        isinstance(rule, str) and rule in TITLE_RULES for rule in matched_rules
    )
    valid_matched_rules = (
        [rule for rule in matched_rules if isinstance(rule, str) and rule in TITLE_RULES]
        if isinstance(matched_rules, list)
        else []
    )
    feature_trace_complete = isinstance(feature_applied, bool) and isinstance(feature_reason, str)
    title_trace_complete = strategy_valid and strategy_reason_valid and matched_rules_valid

    draft = parse_draft_output(f"{title}\n\n{content}")
    applied = feature_applied if feature_mode_enabled and isinstance(feature_applied, bool) else False
    trace = {
        "schema_version": 1,
        "matched_known_subjects": known_subjects,
        "matched_title_rules": list(dict.fromkeys(valid_matched_rules)),
        "feature_mode_applied": applied,
        "feature_mode_reason": feature_reason.strip() if applied and isinstance(feature_reason, str) else "",
    }
    if strategy_valid:
        trace["title_strategy"] = strategy
    if strategy_reason_valid:
        trace["title_strategy_reason"] = strategy_reason.strip()
    if not title_trace_complete:
        trace["title_trace_status"] = "incomplete"
    if not feature_trace_complete:
        trace["feature_mode_trace_status"] = "incomplete"
    priority_level = None
    priority_reason = None
    priority_status = "not_applicable"
    priority_error = None
    if include_newsflash_priority:
        priority_status = "failed"
        try:
            priority_level, priority_reason = parse_newsflash_priority(payload)
            priority_status = "evaluated"
        except Exception as exc:
            priority_error = str(exc)
        trace["newsflash_priority"] = {
            "status": priority_status,
            "level": priority_level,
            "reason": priority_reason,
            "error": priority_error,
        }
    return StructuredWriterResult(
        draft=draft,
        trace=trace,
        priority_level=priority_level,
        priority_reason=priority_reason,
        priority_status=priority_status,
        priority_error=priority_error,
    )


def _strip_json_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _alias_matches(text: str, alias: str) -> bool:
    if re.search(r"[\u3400-\u9fff]", alias):
        return alias.casefold() in text.casefold()
    escaped = re.escape(alias.casefold())
    return re.search(rf"(?<![\w]){escaped}(?![\w])", text.casefold()) is not None
