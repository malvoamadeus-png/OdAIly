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
    assert dashboard["items"][0]["narrative_available"] is False

    detail = MemeDashboardStore(path).narrative_detail(1)
    assert detail is not None
    assert detail["available"] is False
    assert detail["narrative"] is None


def test_meme_dashboard_reports_missing_database(tmp_path) -> None:
    dashboard = MemeDashboardStore(tmp_path / "missing.sqlite3").dashboard()
    assert dashboard["available"] is False
    assert dashboard["items"] == []


def test_meme_dashboard_hides_gate_failed_jobs(tmp_path) -> None:
    path = tmp_path / "meme.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,title TEXT,content TEXT,updated_at TEXT)")
        connection.execute("INSERT INTO jobs VALUES (1,'0xbad','market_cap:0xbad:1000000',1000000,'{}','market_cap_milestone','2026-08-13T00:00:00Z','discarded','volume_gate_failed','','','2026-08-13T00:00:01Z')")
    assert MemeDashboardStore(path).dashboard()["items"] == []


def test_meme_dashboard_exposes_narrative_summary_and_lazy_detail(tmp_path) -> None:
    path = tmp_path / "meme.sqlite3"
    narrative = {
        "status": "empty",
        "failure_stage": None,
        "failure_code": None,
        "decision_code": "materials_but_no_type",
        "decision_reason": "no valid type",
        "grok_research": {"type_hypothesis": ""},
        "telegram_contexts": [{"chat_title": "Alpha", "message_id": 9, "context": []}],
        "x_posts": [{"id": "x:1", "text": "source"}],
        "reader_text": "",
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,
              payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,
              narrative_json TEXT,title TEXT,content TEXT,updated_at TEXT
            );
            CREATE TABLE tg_candidates (
              id INTEGER PRIMARY KEY,mention_count INTEGER,chat_count INTEGER,sender_count INTEGER
            );
            """
        )
        connection.execute(
            "INSERT INTO jobs VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "0x2222222222222222222222222222222222222222",
                "tg_burst:1",
                0,
                "{}",
                "tg_burst",
                "2026-08-05T00:00:00+00:00",
                "discarded",
                "no_usable_narrative",
                json.dumps(narrative),
                "",
                "",
                "2026-08-05T00:01:00+00:00",
            ),
        )

    dashboard = MemeDashboardStore(path).dashboard()
    item = dashboard["items"][0]
    assert item["narrative_available"] is True
    assert item["narrative_status"] == "empty"
    assert item["failure_code"] == "materials_but_no_type"
    assert item["type_hypothesis"] is None

    detail = MemeDashboardStore(path).narrative_detail(1)
    assert detail is not None
    assert detail["available"] is True
    assert detail["narrative"]["x_posts"][0]["id"] == "x:1"


def test_meme_dashboard_reconstructs_lifecycle_and_narrative_durations(tmp_path) -> None:
    path = tmp_path / "meme.sqlite3"
    narrative = {"performance": {"total_duration_ms": 2500}}
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,
              payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,
              narrative_json TEXT,title TEXT,content TEXT,updated_at TEXT,
              processing_started_at TEXT,publishing_started_at TEXT,completed_at TEXT
            )"""
        )
        connection.execute(
            "CREATE TABLE tg_candidates (id INTEGER PRIMARY KEY, mention_count INTEGER, chat_count INTEGER, sender_count INTEGER)"
        )
        connection.execute(
            "INSERT INTO jobs VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "0x333", "market_cap:0x333:500000", 500000, "{}", "market_cap_milestone",
                "2026-08-14T00:00:00+00:00", "publisher_pending", None, json.dumps(narrative),
                "title", "content", "2026-08-14T00:05:00+00:00",
                "2026-08-14T00:00:30+00:00", "2026-08-14T00:02:30+00:00", "2026-08-14T00:03:00+00:00",
            ),
        )
    timing = MemeDashboardStore(path).dashboard()["items"][0]["timing"]
    assert timing["queue_duration_ms"] == 30000
    assert timing["narrative_duration_ms"] == 2500
    assert timing["publishing_duration_ms"] == 30000
    assert timing["total_duration_ms"] == 180000
