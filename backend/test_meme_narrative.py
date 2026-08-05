from __future__ import annotations

import argparse
import asyncio
import json
from unittest.mock import AsyncMock, patch

from packages.meme_scanner import narrative, narrative_v2


ADDRESS = "0x1111111111111111111111111111111111111111"


def narrative_args(output):
    return argparse.Namespace(
        chain="bsc",
        contract=ADDRESS,
        output=str(output),
        output_dir=str(output.parent),
        gpt_model="gpt-test",
        grok_model="grok-test",
        gpt_timeout=1,
        grok_timeout=1,
        telegram_config="telegram.txt",
        telegram_session="telegram.session",
        allowed_chats="whitelist.txt",
        dialogs_limit=1,
        proxy="auto",
        telegram_timeout=1,
        connection_retries=1,
    )


def test_run_async_returns_json_safe_output_path(tmp_path):
    output = tmp_path / "narrative.json"
    telegram = {
        "contexts": [{
            "chat_title": "A",
            "context": [{"message_id": 1, "text": "A concrete claim", "sender_username": "user"}],
        }],
    }
    grok = {
        "raw": {},
        "source_actions": [],
        "narrative_materials": [{"id": "grok:narrative:1", "statement": "A concrete claim"}],
        "supplemental_information": [],
        "type_hypothesis": "pure_meme",
        "diagnostic": {"stage": "ca_research", "http_status": 200},
    }
    x_source = {"posts": [], "raw": {}, "diagnostic": {"stage": "x_ca_collection", "http_status": 200}}
    writer_results = [
        ({"entity_candidates": []}, {}),
        ({
            "primary_type": "pure_meme",
            "angle_material_ids": ["grok:narrative:1"],
            "reader_text": "A concrete claim",
            "used_material_ids": ["grok:narrative:1"],
        }, {}),
    ]
    with patch.object(narrative_v2, "collect_telegram_contexts", new=AsyncMock(return_value=telegram)), patch.object(
        narrative_v2, "collect_x_posts", return_value=x_source
    ), patch.object(narrative_v2, "collect_grok_material", return_value=grok), patch.object(
        narrative_v2, "chat_completion_json_with_metrics", side_effect=writer_results
    ):
        result = asyncio.run(narrative_v2.run_async(narrative_args(output)))

    assert result["output_path"] == str(output)
    json.dumps(result)


def test_generate_reader_text_does_not_turn_tg_volume_into_a_reader_angle(tmp_path) -> None:
    result_payload = {
        "reader_text": "",
        "telegram_contexts": [],
        "telegram_messages": [
            {"id": "tg:1", "text": f"call {ADDRESS}"},
        ],
        "x_posts": [],
        "grok_research": {},
        "grok_diagnostics": [],
        "performance": {},
    }
    with patch.object(narrative, "_run", return_value=result_payload):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="tg_burst",
            database_path=tmp_path / "meme.sqlite3",
            evidence=None,
            timeout=1,
        )

    assert result["reader_text"] == ""
    assert result["telegram_messages"] == result_payload["telegram_messages"]


def test_generate_reader_text_returns_empty_without_source_material(tmp_path) -> None:
    with patch.object(
        narrative,
        "_run",
        return_value={
            "reader_text": "",
            "telegram_contexts": [],
            "telegram_messages": [],
            "x_posts": [],
            "grok_research": {},
            "grok_diagnostics": [],
            "performance": {},
        },
    ):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="market_cap_milestone",
            database_path=tmp_path / "missing.sqlite3",
            evidence=None,
            timeout=1,
        )

    assert result["reader_text"] == ""
    assert result["telegram_messages"] == []


def test_generate_reader_text_preserves_v2_material_buckets(tmp_path) -> None:
    payload = {
        "reader_text": "白宫账号发文提到 Golden Age。",
        "telegram_contexts": [{"chat_title": "Golden", "context": []}],
        "telegram_messages": [{"id": "tg:1", "text": "白宫奶黄金时代了"}],
        "x_posts": [{"id": "x:1", "author": "@WhiteHouse", "text": "Golden Age", "url": "https://x.com/WhiteHouse/status/1"}],
        "grok_research": {
            "source_actions": [],
            "narrative_materials": [{"id": "grok:narrative:1", "statement": "Golden Age 与白宫发文有关"}],
            "supplemental_information": [],
        },
        "grok_diagnostics": [],
        "performance": {},
    }
    with patch.object(narrative, "_run", return_value=payload):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="tg_burst",
            database_path=tmp_path / "meme.sqlite3",
            evidence=None,
            timeout=1,
        )

    assert result["telegram_contexts"] == payload["telegram_contexts"]
    assert result["x_posts"] == payload["x_posts"]
    assert "Golden Age" in result["grok_text"]


def test_generate_reader_text_persists_stage_error_as_transient_diagnostic(tmp_path) -> None:
    error = narrative_v2.NarrativeStageError("grok_ca_research", RuntimeError("HTTP 502"))
    with patch.object(narrative, "_run", side_effect=error):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="tg_burst",
            database_path=tmp_path / "meme.sqlite3",
            evidence=None,
            timeout=1,
        )

    assert result["status"] == "error"
    assert result["failure_stage"] == "grok_ca_research"
    assert result["failure_code"] == "stage_failed"
    assert result["transient_error"] == "narrative_grok_ca_research_failed"
    assert result["decision_code"] == "narrative_error"


def test_generate_reader_text_retries_grok_http_diagnostic(tmp_path) -> None:
    with patch.object(
        narrative,
        "_run",
        return_value={
            "status": "error",
            "failure_stage": "grok_ca_research",
            "failure_code": "http_502",
            "failure_message": "ca_research 返回 HTTP 502。",
            "reader_text": "",
            "telegram_contexts": [],
            "telegram_messages": [],
            "x_posts": [],
            "grok_research": {},
            "grok_diagnostics": [{"stage": "ca_research", "http_status": 502}],
            "performance": {},
        },
    ):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="tg_burst",
            database_path=tmp_path / "meme.sqlite3",
            evidence=None,
            timeout=1,
        )

    assert result["transient_error"] == "narrative_grok_ca_research_failed"


def test_diagnostic_failure_maps_grok_stage_and_http_code() -> None:
    assert narrative_v2._diagnostic_failure([{"stage": "ca_research", "http_status": 503}]) == (
        "grok_ca_research",
        "http_503",
        "ca_research 返回 HTTP 503。",
    )


def test_decision_metadata_distinguishes_empty_angle_from_empty_writer() -> None:
    status, code, reason = narrative_v2._decision_metadata(
        result={
            "primary_type": "pure_meme",
            "source_materials": [{"id": "x:1"}],
            "angle_materials": [],
            "supplemental_information": [],
            "reader_text": "A usable narrative.",
        },
        counts={"total_materials": 1},
        type_hypothesis="pure_meme",
    )
    assert (status, code) == ("success", "no_usable_angle")
    assert "叙事角度" in reason

    status, code, _ = narrative_v2._decision_metadata(
        result={
            "primary_type": "",
            "source_materials": [{"id": "x:1"}],
            "angle_materials": [],
            "supplemental_information": [],
            "reader_text": "",
        },
        counts={"total_materials": 1},
        type_hypothesis="",
    )
    assert (status, code) == ("empty", "writer_returned_empty")
    assert narrative_v2._failure_stage_for_empty("no_materials", {"telegram_contexts": 0, "telegram_messages": 0}) == "telegram_collection"
    assert narrative_v2._failure_stage_for_empty("writer_returned_empty", {"telegram_contexts": 1}) == "final_writer"
