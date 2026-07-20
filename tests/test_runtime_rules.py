from __future__ import annotations

import json

from packages.runtime_rules import build_runtime_rules_payload


def test_runtime_rules_are_versioned_complete_and_secret_free() -> None:
    payload = build_runtime_rules_payload()
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    section_keys = {section["key"] for section in payload["sections"]}
    entry_keys = {
        entry["key"]
        for section in payload["sections"]
        for entry in section["entries"]
    }

    assert payload["schema_version"] == 1
    assert {"judge", "exclusions", "writer", "formatter", "publisher"} <= section_keys
    assert {
        "judge-competitor",
        "judge-jin10",
        "source-exclusion-semantics",
        "writer-title-trace-schema",
        "writer-known-subjects",
        "formatter-replacements",
        "publisher-default-rules",
        "publisher-source-fallbacks",
    } <= entry_keys
    assert "database_url" not in serialized
    assert "service_role_key" not in serialized
    assert "openai_api_key" not in serialized
    assert ".env" not in serialized


def test_runtime_rules_known_subjects_are_displayed_as_names() -> None:
    payload = build_runtime_rules_payload(known_title_subject_names=["CZ", "Vitalik"])
    entry = next(
        entry
        for section in payload["sections"]
        for entry in section["entries"]
        if entry["key"] == "writer-known-subjects"
    )

    assert entry["content"] == "CZ、Vitalik"
    assert entry["editable"] is True
    assert "title_instruction" not in entry["content"]
