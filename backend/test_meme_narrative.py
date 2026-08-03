from __future__ import annotations

import sqlite3
from unittest.mock import patch

from packages.meme_scanner import narrative


ADDRESS = "0x1111111111111111111111111111111111111111"


def test_generate_reader_text_uses_tg_fallback_without_llm(tmp_path) -> None:
    path = tmp_path / "meme.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE ca_mentions (
              address TEXT, chat_id INTEGER, message_id INTEGER, sender_key TEXT,
              sent_at TEXT, text_excerpt TEXT
            )"""
        )
        connection.execute(
            "INSERT INTO ca_mentions VALUES (?,?,?,?,?,?)",
            (ADDRESS, 1, 10, "sender-1", "2026-08-03T00:00:00+00:00", f"call {ADDRESS}"),
        )

    settings = {"api_key": None, "base_url": "http://localhost", "api_style": "chat_completions", "model": "test"}
    with patch.object(narrative, "_llm_settings", return_value=settings), patch.object(
        narrative, "_grok_material", return_value=""
    ):
        result = narrative.generate_reader_text(
            address=ADDRESS,
            symbol="KIDS",
            trigger_kind="tg_burst",
            database_path=path,
            evidence=None,
            timeout=1,
        )

    assert result["reader_text"] == "Telegram 白名单社群中，多名用户正在围绕该代币展开讨论。"
    assert len(result["telegram_messages"]) == 1


def test_generate_reader_text_returns_empty_without_source_material(tmp_path) -> None:
    with patch.object(narrative, "_grok_material", return_value=""):
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
