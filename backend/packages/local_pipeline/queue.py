from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal


LocalPipelineJobType = Literal["write_flow", "alert_only"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def encode_dt(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat()


def decode_payload(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class LocalPipelineJob:
    id: int
    job_type: LocalPipelineJobType
    task_id: int
    source: str
    source_item_id: str
    status: str
    attempt_count: int
    payload: dict[str, Any]


class LocalPipelineQueue:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS local_pipeline_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL CHECK (job_type IN ('write_flow', 'alert_only')),
                    task_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    source_item_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    payload TEXT NOT NULL DEFAULT '{}',
                    locked_by TEXT,
                    locked_at TEXT,
                    next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source, source_item_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_local_pipeline_jobs_status_next
                ON local_pipeline_jobs(status, next_attempt_at, created_at)
                """
            )

    def enqueue(
        self,
        *,
        job_type: LocalPipelineJobType,
        task_id: int,
        source: str,
        source_item_id: str,
        payload: dict[str, Any] | None = None,
    ) -> LocalPipelineJob:
        encoded_payload = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO local_pipeline_jobs (
                    job_type, task_id, source, source_item_id, payload, status, next_attempt_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(source, source_item_id) DO UPDATE SET
                    job_type = excluded.job_type,
                    task_id = excluded.task_id,
                    payload = excluded.payload,
                    status = CASE
                        WHEN local_pipeline_jobs.status = 'succeeded' THEN local_pipeline_jobs.status
                        ELSE 'pending'
                    END,
                    next_attempt_at = CASE
                        WHEN local_pipeline_jobs.status = 'succeeded' THEN local_pipeline_jobs.next_attempt_at
                        ELSE excluded.next_attempt_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (job_type, task_id, source, source_item_id, encoded_payload, now, now),
            )
            row = conn.execute(
                """
                SELECT *
                FROM local_pipeline_jobs
                WHERE source = ? AND source_item_id = ?
                """,
                (source, source_item_id),
            ).fetchone()
            return self._row_to_job(row)

    def claim_next(self, *, worker_id: str) -> LocalPipelineJob | None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT *
                FROM local_pipeline_jobs
                WHERE status IN ('pending', 'failed')
                  AND next_attempt_at <= ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                """
                UPDATE local_pipeline_jobs
                SET status = 'running',
                    attempt_count = attempt_count + 1,
                    locked_by = ?,
                    locked_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (worker_id, now, now, int(row["id"])),
            )
            row = conn.execute("SELECT * FROM local_pipeline_jobs WHERE id = ?", (int(row["id"]),)).fetchone()
            conn.commit()
            return self._row_to_job(row)

    def requeue_stale_running_jobs(self, *, stale_before: datetime) -> int:
        now = encode_dt()
        cutoff = encode_dt(stale_before)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE local_pipeline_jobs
                SET status = 'failed',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = CASE
                        WHEN coalesce(last_error, '') = '' THEN 'stale running job requeued after worker restart'
                        ELSE substr(last_error, 1, 1800) || '\n[stale running job requeued after worker restart]'
                    END,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE status = 'running'
                  AND locked_at IS NOT NULL
                  AND locked_at < ?
                """,
                (now, now, cutoff),
            )
            return int(cursor.rowcount or 0)

    def mark_succeeded(self, job_id: int) -> None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE local_pipeline_jobs
                SET status = 'succeeded',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, job_id),
            )

    def mark_failed(self, job_id: int, *, error: str, attempt_count: int) -> None:
        delay = min(300, max(5, 2 ** min(attempt_count, 8)))
        next_attempt_at = encode_dt(utc_now() + timedelta(seconds=delay))
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE local_pipeline_jobs
                SET status = 'failed',
                    locked_by = NULL,
                    locked_at = NULL,
                    last_error = ?,
                    next_attempt_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], next_attempt_at, now, job_id),
            )

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, count(*) AS count
                FROM local_pipeline_jobs
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> LocalPipelineJob:
        return LocalPipelineJob(
            id=int(row["id"]),
            job_type=str(row["job_type"]),  # type: ignore[arg-type]
            task_id=int(row["task_id"]),
            source=str(row["source"]),
            source_item_id=str(row["source_item_id"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            payload=decode_payload(row["payload"]),
        )
