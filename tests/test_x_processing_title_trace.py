from __future__ import annotations

import json

import pytest

from packages.x_processing.models import TaskRecord
from packages.x_processing.title_trace import (
    build_known_subject_prompt,
    match_known_title_subjects,
    parse_structured_writer_output,
)


def _task(*, title: str, content: str, metadata: dict | None = None) -> TaskRecord:
    return TaskRecord(
        id=1,
        source="x",
        source_item_id="item-1",
        source_url="https://x.com/example/status/1",
        title=title,
        content=content,
        metadata=metadata or {},
    )


def test_known_subject_prompt_only_injects_subjects_matched_by_current_task() -> None:
    matches = match_known_title_subjects(
        _task(
            title="Base users report losses",
            content="Rune says trust in the Base community has nearly disappeared.",
            metadata={"author_username": "RuneCrypto_"},
        )
    )
    prompt = build_known_subject_prompt(matches)

    assert [match["key"] for match in matches] == ["rune"]
    assert "Rune" in prompt
    assert "Cobie" not in prompt
    assert "Vitalik" not in prompt


def test_structured_writer_trace_keeps_deterministic_subjects() -> None:
    known_subjects = [{"key": "rune", "name": "Rune", "matched_alias": "Rune"}]
    raw = json.dumps(
        {
            "title": "Rune says Base community trust has nearly disappeared",
            "content": "More than 10,000 Base users reportedly lost about 99% of their assets.",
            "title_strategy": "speaker_anchor",
            "title_strategy_reason": "The speaker is central to the claim.",
            "matched_title_rules": ["known_speaker_anchor"],
            "feature_mode_applied": True,
            "feature_mode_reason": "The subject anchor was emphasized.",
        }
    )

    result = parse_structured_writer_output(
        raw,
        known_subjects=known_subjects,
        feature_mode_enabled=True,
    )

    assert result.trace["title_strategy"] == "speaker_anchor"
    assert result.trace["matched_known_subjects"] == known_subjects
    assert result.trace["feature_mode_applied"] is True


def test_disabled_feature_mode_forces_trace_to_false() -> None:
    raw = json.dumps(
        {
            "title": "Numerai completes NMR buyback",
            "content": "Numerai repurchased NMR over the past year.",
            "title_strategy": "action_first",
            "title_strategy_reason": "The buyback is the main action.",
            "matched_title_rules": ["action_first"],
            "feature_mode_applied": True,
            "feature_mode_reason": "Requested by the model.",
        }
    )

    result = parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)

    assert result.trace["feature_mode_applied"] is False
    assert result.trace["feature_mode_reason"] == ""


def test_structured_writer_rejects_title_trace_label_in_content() -> None:
    raw = json.dumps(
        {
            "title": "Michael Saylor\uff1a\u6bd4\u7279\u5e01\u8981\u6210\u4e3a\u5168\u7403\u8d27\u5e01\u7f51\u7edc\u9700\u8981\u4f01\u4e1a\u91c7\u7528",
            "content": "**\u6807\u9898\uff1a\u53d1\u8a00\u4eba\u524d\u7f6e**\nOdaily\u661f\u7403\u65e5\u62a5\u8baf Michael Saylor \u8868\u793a\uff0c\u4f01\u4e1a\u91c7\u7528\u662f\u5fc5\u8981\u7684\u3002",
            "title_strategy": "speaker_anchor",
            "title_strategy_reason": "The speaker is central to the statement.",
            "matched_title_rules": ["known_speaker_anchor"],
            "feature_mode_applied": False,
            "feature_mode_reason": "",
        }
    )

    with pytest.raises(ValueError, match="structured field label"):
        parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps({"title": "Only title"}),
        json.dumps(
            {
                "title": "Title",
                "content": "Content.",
                "title_strategy": "invented_strategy",
                "title_strategy_reason": "",
                "matched_title_rules": [],
                "feature_mode_applied": False,
                "feature_mode_reason": "",
            }
        ),
    ],
)
def test_invalid_writer_json_is_rejected(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)
