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
