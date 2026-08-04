from __future__ import annotations

from unittest.mock import patch

from packages.meme_scanner import narrative


ADDRESS = "0x1111111111111111111111111111111111111111"


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
