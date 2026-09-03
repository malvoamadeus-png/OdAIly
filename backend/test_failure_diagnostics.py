import sqlite3
from datetime import UTC, datetime

from packages.common.failure_diagnostics import classify_failure
from packages.failure_diagnostics import FailureDiagnosticsStore


def test_classify_failure_distinguishes_quota_timeout_and_upstream_5xx() -> None:
    quota = classify_failure("OpenAI error: insufficient_quota", status="search_failed")
    timeout = classify_failure(
        "OpenAI request failed: model=gpt-5.6-luna timeout_seconds=90 error=Read timed out",
        status="search_failed",
    )
    upstream = classify_failure(
        "OpenAI request failed: model=gpt-5.6-luna status_code=503 body_prefix=upstream",
        status="judge_failed",
    )

    assert quota.code == "ai_quota_exhausted"
    assert timeout.code == "ai_request_timeout"
    assert upstream.code == "upstream_http_5xx"


def test_store_joins_task_pipeline_queue_and_worker_evidence(tmp_path) -> None:
    database_path = tmp_path / "odaily.sqlite"
    queue_path = tmp_path / "local_pipeline.sqlite"
    now = datetime.now(UTC).isoformat()

    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, title TEXT,
                status TEXT, created_at TEXT, updated_at TEXT, attempt_count INTEGER,
                locked_by TEXT, locked_until TEXT
            );
            CREATE TABLE x_task_pipeline (
                task_id INTEGER PRIMARY KEY, writer_model TEXT, last_error TEXT
            );
            CREATE TABLE pipeline_worker_heartbeats (
                component TEXT, worker_id TEXT, status TEXT, last_seen_at TEXT,
                last_success_at TEXT, last_error TEXT, metadata TEXT
            );
            """,
        )
        connection.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (7, "ai_source", "source-7", "测试任务", "search_failed", now, now, 3, None, None),
        )
        connection.execute(
            "INSERT INTO x_task_pipeline VALUES (?, ?, ?)",
            (7, None, "OpenAI request failed: model=gpt-5.6-luna timeout_seconds=90 error=Read timed out"),
        )
        connection.execute(
            "INSERT INTO pipeline_worker_heartbeats VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("local_pipeline", "worker-1", "running", now, now, None, "{}"),
        )
        connection.commit()

    with sqlite3.connect(queue_path) as connection:
        connection.executescript(
            """
            CREATE TABLE local_pipeline_jobs (
                id INTEGER PRIMARY KEY, job_type TEXT, task_id INTEGER, source TEXT,
                source_item_id TEXT, status TEXT, attempt_count INTEGER,
                last_error TEXT, next_attempt_at TEXT, created_at TEXT, updated_at TEXT,
                storage_epoch TEXT
            );
            """,
        )
        connection.execute(
            "INSERT INTO local_pipeline_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (19, "write_flow", 7, "ai_source", "source-7", "exhausted", 3, None, now, now, now, "sqlite-primary"),
        )
        connection.commit()

    result = FailureDiagnosticsStore(
        database_path,
        queue_path,
        storage_epoch="sqlite-primary",
        task_stuck_minutes=10,
        heartbeat_stale_minutes=10,
    ).get_task(7)

    assert result is not None
    assert result["diagnosis"]["code"] == "ai_request_timeout"
    assert result["diagnosis"]["evidence"]["model"] == "gpt-5.6-luna"
    assert result["diagnosis"]["evidence"]["timeout_seconds"] == "90"
    assert result["queue"]["status"] == "exhausted"
    assert "job_id=19" in result["handoff_summary"]
