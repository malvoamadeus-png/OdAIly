from __future__ import annotations

import json

import pytest

from packages.x_processing.models import TaskRecord
from packages.x_processing.title_trace import (
    build_known_subject_prompt,
    match_known_title_subjects,
    normalize_known_title_subject_names,
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
        ),
        subject_names=["Rune", "Cobie", "Vitalik"],
    )
    prompt = build_known_subject_prompt(matches)

    assert [match["key"] for match in matches] == ["rune"]
    assert prompt == "当前材料命中知名人物：Rune。遇到这些人物观点时可使用“人名：观点”标题。"
    assert "Cobie" not in prompt
    assert "Vitalik" not in prompt
    assert "title_instruction" not in prompt
    assert "观点、批评或争议内容" not in prompt


def test_known_subject_names_normalize_operator_input() -> None:
    assert normalize_known_title_subject_names("CZ、Vitalik\nCZ, Rune； Cobie") == [
        "CZ",
        "Vitalik",
        "Rune",
        "Cobie",
    ]
    assert normalize_known_title_subject_names(["CZ", None, " ", "cz", "赵长鹏"]) == ["CZ", "赵长鹏"]


def test_known_subject_matching_supports_cjk_names_inside_sentence() -> None:
    matches = match_known_title_subjects(
        _task(title="赵长鹏回应行业监管", content="币安联合创始人赵长鹏表示，监管框架仍需完善。"),
        subject_names=["赵长鹏"],
    )

    assert matches == [{"key": "赵长鹏", "name": "赵长鹏", "matched_alias": "赵长鹏"}]


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


def test_structured_writer_rejects_repeated_title_in_content() -> None:
    title = "Michael Saylor\uff1a\u6bd4\u7279\u5e01\u8981\u6210\u4e3a\u5168\u7403\u8d27\u5e01\u7f51\u7edc\u9700\u8981\u4f01\u4e1a\u91c7\u7528"
    raw = json.dumps(
        {
            "title": title,
            "content": f"{title}\n\nMichael Saylor \u5728 X \u5e73\u53f0\u53d1\u6587\u8868\u793a\uff0c\u4f01\u4e1a\u91c7\u7528\u662f\u5fc5\u8981\u7684\u3002",
            "title_strategy": "speaker_anchor",
            "title_strategy_reason": "The speaker is central to the statement.",
            "matched_title_rules": ["known_speaker_anchor"],
            "feature_mode_applied": False,
            "feature_mode_reason": "",
        }
    )

    with pytest.raises(ValueError, match="repeats title"):
        parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)


def test_structured_writer_rejects_title_label_inside_content() -> None:
    title = "损失2375万美元USDC，Ostium价格数据遭攻击"
    raw = json.dumps(
        {
            "title": title,
            "content": f"标题为：{title}\n\n据Ostium监测，Ostium发布攻击事件更新。",
            "title_strategy": "result_front",
            "title_strategy_reason": "The loss is placed first.",
            "matched_title_rules": ["result_change_front", "entity_front"],
            "feature_mode_applied": True,
            "feature_mode_reason": "The result is front-loaded.",
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="structured field label"):
        parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=True)


def test_structured_writer_rejects_title_repeated_as_first_content_sentence() -> None:
    title = "Mike Novogratz\u547c\u5401\u4e24\u515a\u59a5\u534f\u63a8\u8fdb\u300a\u6e05\u6670\u5ea6\u6cd5\u6848\u300b"
    raw = json.dumps(
        {
            "title": title,
            "content": (
                f"Odaily\u661f\u7403\u65e5\u62a5\u8baf {title}\u3002 "
                "Galaxy Digital \u9996\u5e2d\u6267\u884c\u5b98 Mike Novogratz \u8868\u793a\uff0c"
                "\u8be5\u6cd5\u6848\u5173\u4e4e\u7f8e\u56fd\u672a\u6765\u3002"
            ),
            "title_strategy": "speaker_anchor",
            "title_strategy_reason": "The speaker is central to the statement.",
            "matched_title_rules": ["known_speaker_anchor"],
            "feature_mode_applied": False,
            "feature_mode_reason": "",
        }
    )

    with pytest.raises(ValueError, match="repeats title"):
        parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)


def test_structured_writer_allows_same_subject_with_different_first_sentence() -> None:
    title = "Mike Novogratz\u547c\u5401\u4e24\u515a\u59a5\u534f\u63a8\u8fdb\u300a\u6e05\u6670\u5ea6\u6cd5\u6848\u300b"
    raw = json.dumps(
        {
            "title": title,
            "content": (
                "Mike Novogratz \u8868\u793a\uff0c\u300a\u6e05\u6670\u5ea6\u6cd5\u6848\u300b"
                "\u5173\u4e4e\u7f8e\u56fd\u672a\u6765\uff0c\u76ee\u524d\u4ec5\u5269\u4f26\u7406\u76f8\u5173\u6761\u6b3e\u7684\u6587\u5b57\u6253\u78e8\u5de5\u4f5c\u3002"
            ),
            "title_strategy": "speaker_anchor",
            "title_strategy_reason": "The speaker is central to the statement.",
            "matched_title_rules": ["known_speaker_anchor"],
            "feature_mode_applied": False,
            "feature_mode_reason": "",
        }
    )

    result = parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=False)

    assert result.draft.title == title


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
