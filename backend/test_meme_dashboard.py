from __future__ import annotations

import json
import sqlite3

from packages.meme_dashboard import MemeDashboardStore


def test_meme_dashboard_reads_generated_text_and_tg_counts(tmp_path) -> None:
    path = tmp_path / "meme.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,
              payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,
              title TEXT,content TEXT,updated_at TEXT
            );
            CREATE TABLE tg_candidates (
              id INTEGER PRIMARY KEY,mention_count INTEGER,chat_count INTEGER,sender_count INTEGER
            );
            """
        )
        connection.execute("INSERT INTO tg_candidates VALUES (7,5,2,3)")
        connection.execute(
            "INSERT INTO jobs VALUES (1,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "0x1111111111111111111111111111111111111111",
                "tg_burst:7",
                300000,
                json.dumps(
                    {
                        "launchpad_platform": "telegram",
                        "name": "Kids",
                        "symbol": "KIDS",
                        "market_cap": 360000,
                        "volume_24h": 220000,
                    }
                ),
                "tg_burst",
                "2026-08-03T06:00:00+00:00",
                "publisher_pending",
                None,
                "Meme速递：BSC上KIDS社群热议中，市值36万美元",
                "BSC上KIDS社群热议中，当前市值36万美元。",
                "2026-08-03T06:01:00+00:00",
            ),
        )

    dashboard = MemeDashboardStore(path).dashboard()

    assert dashboard["available"] is True
    assert dashboard["last_error"] is None
    assert dashboard["items"][0]["market_cap"] == 360000
    assert dashboard["items"][0]["mention_count"] == 5
    assert dashboard["items"][0]["title"].endswith("市值36万美元")


def test_meme_dashboard_reports_missing_database(tmp_path) -> None:
    dashboard = MemeDashboardStore(tmp_path / "missing.sqlite3").dashboard()
    assert dashboard["available"] is False
    assert dashboard["items"] == []
