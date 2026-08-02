from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from packages.pipeline_supervisor.sqlite_repository import SQLitePipelineSupervisorRepository


def create_tasks_table(path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE tasks (
                id integer PRIMARY KEY,
                source text NOT NULL,
                status text NOT NULL,
                locked_until text,
                created_at text NOT NULL,
                updated_at text NOT NULL
            )
            """
        )


def insert_task(
    path,
    *,
    task_id: int,
    status: str,
    created_at: str,
    updated_at: str,
    locked_until: str | None = None,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO tasks(id, source, status, locked_until, created_at, updated_at)
            VALUES (?, 'ai_source', ?, ?, ?, ?)
            """,
            (task_id, status, locked_until, created_at, updated_at),
        )


def test_fresh_pending_task_with_sqlite_timestamp_is_not_stale(tmp_path) -> None:
    path = tmp_path / "odaily.sqlite"
    create_tasks_table(path)
    insert_task(
        path,
        task_id=1,
        status="pending",
        created_at="2026-08-02 03:10:21",
        updated_at="2026-08-02 03:10:21",
    )

    repository = SQLitePipelineSupervisorRepository(path)
    rows = repository.list_old_claimable_tasks(
        cutoff=datetime(2026, 8, 2, 3, 0, tzinfo=UTC),
    )

    assert rows == []


def test_old_pending_task_is_reported_across_timestamp_formats(tmp_path) -> None:
    path = tmp_path / "odaily.sqlite"
    create_tasks_table(path)
    insert_task(
        path,
        task_id=1,
        status="pending",
        created_at="2026-08-02 02:40:00",
        updated_at="2026-08-02 02:50:00",
    )
    insert_task(
        path,
        task_id=2,
        status="pending",
        created_at="2026-08-02T02:45:00+00:00",
        updated_at="2026-08-02T02:55:00+00:00",
    )

    repository = SQLitePipelineSupervisorRepository(path)
    rows = repository.list_old_claimable_tasks(
        cutoff=datetime(2026, 8, 2, 3, 0, tzinfo=UTC),
    )

    assert rows == [
        {
            "source": "ai_source",
            "status": "pending",
            "count": 2,
            "oldest_updated_at": "2026-08-02 02:50:00",
            "oldest_created_at": "2026-08-02 02:40:00",
        }
    ]


def test_running_task_uses_normalized_lock_and_update_times(tmp_path) -> None:
    path = tmp_path / "odaily.sqlite"
    create_tasks_table(path)
    insert_task(
        path,
        task_id=1,
        status="running",
        created_at="2026-08-02 02:59:00",
        updated_at="2026-08-02 03:09:00",
        locked_until="2099-08-02T03:20:00+00:00",
    )

    repository = SQLitePipelineSupervisorRepository(path)
    rows = repository.list_stuck_processing_tasks(
        cutoff=datetime(2026, 8, 2, 3, 0, tzinfo=UTC),
    )

    assert rows == []
