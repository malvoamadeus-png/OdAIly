from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from packages.maintenance.sqlite_repository import SQLiteMaintenanceRepository


def test_five_day_cleanup_trims_only_unreferenced_terminal_business_payloads(tmp_path):
    database = tmp_path / "main.sqlite"
    feedback_database = tmp_path / "editor_plugin_local.sqlite"
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks(
                id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT,raw_payload TEXT,metadata TEXT,
                title TEXT,content TEXT
            );
            CREATE TABLE x_task_pipeline(
                task_id INTEGER PRIMARY KEY,judge_output TEXT,search_result TEXT,writer_output TEXT,
                draft_title TEXT,draft_content TEXT,publisher_output TEXT,final_title TEXT,final_content TEXT
            );
            CREATE TABLE newsflash_production_metadata(delivery_key TEXT,odaily_id TEXT);
            CREATE TABLE auditor_checks(id INTEGER PRIMARY KEY,source_item_id TEXT,status TEXT);
            CREATE TABLE fact_checks(source_item_id TEXT,status TEXT);
            CREATE TABLE search_event_candidates(primary_task_id INTEGER,status TEXT,expires_at TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
            [
                (1, "published", old, '{"raw":"large"}', '{"site_key":"x","junk":"remove"}', "Keep title", "Keep body"),
                (2, "published", old, '{"raw":"protected"}', '{"site_key":"x"}', "Audit title", "Audit body"),
                (3, "published", old, '{"raw":"protected"}', '{"site_key":"x"}', "Event title", "Event body"),
                (4, "processing", old, '{"raw":"active"}', '{"site_key":"x"}', "Active title", "Active body"),
                (5, "ready_review", old, '{"raw":"flagged-old"}', '{"site_key":"x"}', "Old flagged title", "Old flagged body"),
                (6, "write_failed", old, '{"raw":"failed"}', '{"site_key":"x"}', "Failed title", "Failed body"),
                (7, "published", old, '{"raw":"outbox-protected"}', '{"site_key":"x"}', "Rejected title", "Rejected body"),
            ],
        )
        conn.executemany(
            "INSERT INTO x_task_pipeline VALUES(?,?,?,?,?,?,?,?,?)",
            [(task_id, '{"judge":1}', '{"search":1}', '{"writer":1}', "Draft", "Draft body", '{"publish":1}', "Final", "Final body")
             for task_id in (1, 2, 3, 4, 5, 6, 7)],
        )
        conn.executemany(
            "INSERT INTO newsflash_production_metadata VALUES(?,?)",
            [(f"odaily-task-{task_id}", f"item-{task_id}") for task_id in (2, 3, 5, 7)],
        )
        conn.execute("INSERT INTO auditor_checks VALUES(40,'item-2','pending')")
        conn.execute("INSERT INTO auditor_checks VALUES(42,'item-5','flagged')")
        conn.execute("INSERT INTO auditor_checks VALUES(41,'item-7','flagged')")
        conn.execute("INSERT INTO search_event_candidates VALUES(3,'active',NULL)")
    with sqlite3.connect(feedback_database) as conn:
        conn.execute("CREATE TABLE editor_plugin_local_feedbacks(feed_item_id TEXT,feed_kind TEXT,feedback TEXT,manual_rejection_status TEXT)")
        conn.execute("INSERT INTO editor_plugin_local_feedbacks VALUES('auditor:41','auditor_alert','reject','pending')")

    repository = SQLiteMaintenanceRepository(database, feedback_path=feedback_database)
    dry = repository.cleanup(dry_run=True, completed_field_retention_days=5)
    assert dry.cleared["tasks_payloads"] == 2
    assert dry.cleared["x_task_pipeline_outputs"] == 2
    with sqlite3.connect(database) as conn:
        before = conn.execute("SELECT raw_payload FROM tasks WHERE id=1").fetchone()[0]
        assert json.loads(before) == {"raw": "large"}

    applied = repository.cleanup(dry_run=False, completed_field_retention_days=5)
    assert applied.cleared["tasks_payloads"] == 2
    assert applied.cleared["x_task_pipeline_outputs"] == 2
    assert applied.has_more is False
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        task = conn.execute("SELECT * FROM tasks WHERE id=1").fetchone()
        assert task["raw_payload"] == "{}"
        assert json.loads(task["metadata"]) == {"site_key": "x"}
        assert (task["title"], task["content"]) == ("Keep title", "Keep body")
        for task_id in (2, 3, 4, 7):
            row = conn.execute("SELECT raw_payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            assert row["raw_payload"] != "{}"
        for task_id in (5,):
            row = conn.execute("SELECT raw_payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            assert row["raw_payload"] != "{}"
        for task_id in (6,):
            row = conn.execute("SELECT raw_payload FROM tasks WHERE id=?", (task_id,)).fetchone()
            assert row["raw_payload"] == "{}"
        pipeline = conn.execute("SELECT judge_output,draft_title,draft_content,final_content FROM x_task_pipeline WHERE task_id=1").fetchone()
        assert pipeline["judge_output"] == "{}"
        assert pipeline["draft_title"] is None and pipeline["draft_content"] is None
        assert pipeline["final_content"] == "Final body"
    repeated = repository.cleanup(dry_run=False, completed_field_retention_days=5)
    assert repeated.cleared["tasks_payloads"] == 0
    assert repeated.cleared["x_task_pipeline_outputs"] == 0


def test_maintenance_processes_at_most_500_rows_per_table_per_run(tmp_path):
    database = tmp_path / "main.sqlite"
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks(id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT,raw_payload TEXT,metadata TEXT,title TEXT,content TEXT);
            CREATE TABLE pipeline_alerts(alert_key TEXT PRIMARY KEY,created_at TEXT);
            """
        )
        conn.executemany(
            "INSERT INTO tasks VALUES(?,?,?,?,?,?,?)",
            [(task_id, "published", old, '{"raw":1}', "{}", f"Title {task_id}", f"Body {task_id}") for task_id in range(505)],
        )
        conn.executemany(
            "INSERT INTO pipeline_alerts VALUES(?,?)",
            [(f"alert-{task_id}", old) for task_id in range(505)],
        )

    result = SQLiteMaintenanceRepository(database).cleanup(
        dry_run=False,
        completed_field_retention_days=5,
        retention_days=7,
    )

    assert result.cleared["tasks_payloads"] == 500
    assert result.deleted["pipeline_alerts"] == 500
    assert result.has_more is True
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE raw_payload<>'{}'").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM pipeline_alerts").fetchone()[0] == 5

    next_batch = SQLiteMaintenanceRepository(database).cleanup(
        dry_run=False,
        completed_field_retention_days=5,
        retention_days=7,
    )
    assert next_batch.cleared["tasks_payloads"] == 5
    assert next_batch.deleted["pipeline_alerts"] == 5
    assert next_batch.has_more is False


def test_main_pipeline_retention_does_not_delete_other_module_data(tmp_path):
    database = tmp_path / "main.sqlite"
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks(id INTEGER PRIMARY KEY,status TEXT,updated_at TEXT,raw_payload TEXT,metadata TEXT,title TEXT,content TEXT);
            CREATE TABLE x_task_pipeline(task_id INTEGER PRIMARY KEY,judge_output TEXT,search_result TEXT,writer_output TEXT,draft_title TEXT,draft_content TEXT);
            CREATE TABLE whale_watch_activities(id INTEGER PRIMARY KEY,created_at TEXT);
            CREATE TABLE whale_watch_hyperliquid_activities(id INTEGER PRIMARY KEY,created_at TEXT);
            CREATE TABLE editor_plugin_feedbacks(id INTEGER PRIMARY KEY,created_at TEXT);
            CREATE TABLE pipeline_worker_heartbeats(worker_id TEXT PRIMARY KEY,updated_at TEXT);
            CREATE TABLE pipeline_alerts(alert_key TEXT PRIMARY KEY,created_at TEXT);
            """
        )
        conn.execute("INSERT INTO tasks VALUES(1,'published',?,'{\"payload\":1}','{\"site_key\":\"x\"}','Title','Body')", (old,))
        conn.execute("INSERT INTO x_task_pipeline VALUES(1,'{\"judge\":1}','{\"search\":1}','{\"writer\":1}','Draft','Draft body')")
        conn.execute("INSERT INTO whale_watch_activities VALUES(1,?)", (old,))
        conn.execute("INSERT INTO whale_watch_hyperliquid_activities VALUES(1,?)", (old,))
        conn.execute("INSERT INTO editor_plugin_feedbacks VALUES(1,?)", (old,))
        conn.execute("INSERT INTO pipeline_worker_heartbeats VALUES('worker-1',?)", (old,))
        conn.execute("INSERT INTO pipeline_alerts VALUES('alert-1',?)", (old,))

    result = SQLiteMaintenanceRepository(database).cleanup_main_pipeline_fields()

    assert result.deleted == {}
    assert result.cleared["tasks_payloads"] == 1
    assert result.cleared["x_task_pipeline_outputs"] == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT raw_payload,title,content FROM tasks WHERE id=1").fetchone() == ("{}", "Title", "Body")
        for table in (
            "whale_watch_activities", "whale_watch_hyperliquid_activities", "editor_plugin_feedbacks",
            "pipeline_worker_heartbeats", "pipeline_alerts",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
