from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .formatter import parse_draft_output
from .models import DraftBrief, TaskRecord


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

KNOWN_TITLE_SUBJECTS: tuple[dict[str, Any], ...] = (
    {
        "key": "rune",
        "name": "Rune",
        "type": "kol",
        "importance": "high",
        "aliases": ("Rune", "RuneCrypto_", "@RuneCrypto_"),
        "title_instruction": "观点、批评或争议内容优先保留 Rune 为发言主体，可使用“Rune：……”或“Rune称……”。",
    },
    {
        "key": "cobie",
        "name": "Cobie",
        "type": "kol",
        "importance": "high",
        "aliases": ("Cobie", "@cobie"),
        "title_instruction": "观点或判断内容优先保留 Cobie 为发言主体。",
    },
    {
        "key": "vitalik",
        "name": "Vitalik",
        "type": "founder",
        "importance": "high",
        "aliases": ("Vitalik", "Vitalik Buterin", "@VitalikButerin"),
        "title_instruction": "观点、路线或以太坊相关判断优先保留 Vitalik 为发言主体。",
    },
    {
        "key": "cz",
        "name": "CZ",
        "type": "founder",
        "importance": "high",
        "aliases": ("CZ", "Changpeng Zhao", "赵长鹏", "@cz_binance"),
        "title_instruction": "观点、回应或行业判断优先保留 CZ 为发言主体。",
    },
)


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


@dataclass(frozen=True, slots=True)
class StructuredWriterResult:
    draft: DraftBrief
    trace: dict[str, Any]


def match_known_title_subjects(task: TaskRecord) -> list[dict[str, str]]:
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
    for subject in KNOWN_TITLE_SUBJECTS:
        matched_alias = next(
            (alias for alias in subject["aliases"] if _alias_matches(text, str(alias))),
            None,
        )
        if matched_alias is None:
            continue
        matches.append(
            {
                "key": str(subject["key"]),
                "name": str(subject["name"]),
                "type": str(subject["type"]),
                "importance": str(subject["importance"]),
                "matched_alias": str(matched_alias),
            }
        )
    return matches


def build_known_subject_prompt(matches: list[dict[str, str]]) -> str:
    if not matches:
        return ""
    instructions: list[str] = []
    matched_keys = {match["key"] for match in matches}
    for subject in KNOWN_TITLE_SUBJECTS:
        if str(subject["key"]) in matched_keys:
            instructions.append(f"- {subject['name']}：{subject['title_instruction']}")
    return "【当前材料命中的知名主体】\n" + "\n".join(instructions)


def parse_structured_writer_output(
    raw_output: str,
    *,
    known_subjects: list[dict[str, str]],
    feature_mode_enabled: bool,
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
    if strategy not in TITLE_STRATEGIES:
        raise ValueError("writer output contains unsupported title_strategy")
    if not isinstance(strategy_reason, str):
        raise ValueError("writer output title_strategy_reason must be a string")
    if not isinstance(matched_rules, list) or any(rule not in TITLE_RULES for rule in matched_rules):
        raise ValueError("writer output contains unsupported matched_title_rules")
    if not isinstance(feature_applied, bool) or not isinstance(feature_reason, str):
        raise ValueError("writer output feature mode fields are invalid")

    draft = parse_draft_output(f"{title}\n\n{content}")
    applied = feature_applied if feature_mode_enabled else False
    trace = {
        "schema_version": 1,
        "title_strategy": strategy,
        "title_strategy_reason": strategy_reason.strip(),
        "matched_known_subjects": known_subjects,
        "matched_title_rules": list(dict.fromkeys(str(rule) for rule in matched_rules)),
        "feature_mode_applied": applied,
        "feature_mode_reason": feature_reason.strip() if applied else "",
    }
    return StructuredWriterResult(draft=draft, trace=trace)


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
    escaped = re.escape(alias.casefold())
    return re.search(rf"(?<![\w]){escaped}(?![\w])", text.casefold()) is not None

