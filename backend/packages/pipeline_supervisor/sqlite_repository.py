from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings

from .repository import EXPECTED_HEARTBEAT_COMPONENTS, MONITORED_TASK_SOURCES, to_json_safe


FAILURE_STATUSES = (
    "judge_failed", "domain_failed", "search_failed", "write_failed", "format_failed",
    "publish_failed", "publisher_failed", "notify_failed",
)


def _rows(rows) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        item = dict(row)
        if "metadata" in item and isinstance(item["metadata"], str):
            item["metadata"] = json.loads(item["metadata"] or "{}")
        result.append(item)
    return result


class SQLitePipelineSupervisorRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL, worker_id text NOT NULL, status text NOT NULL,
                    last_seen_at text NOT NULL, last_success_at text, last_error text, metadata text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component, worker_id));
                CREATE TABLE IF NOT EXISTS pipeline_alerts (
                    alert_key text PRIMARY KEY, last_sent_at text NOT NULL, last_message text NOT NULL,
                    send_count integer NOT NULL DEFAULT 1, metadata text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.commit()

    def list_stale_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            latest = {r["component"]: dict(r) for r in conn.execute(
                "SELECT h.* FROM pipeline_worker_heartbeats h JOIN (SELECT component,MAX(last_seen_at) seen FROM pipeline_worker_heartbeats GROUP BY component) l ON l.component=h.component AND l.seen=h.last_seen_at"
            ).fetchall()}
        cutoff_text = cutoff.astimezone(UTC).isoformat()
        return [latest.get(component, {"component": component, "worker_id": None, "status": None, "last_seen_at": None,
                                        "last_success_at": None, "last_error": None, "metadata": {}})
                for component in EXPECTED_HEARTBEAT_COMPONENTS
                if component not in latest or str(latest[component]["last_seen_at"]) < cutoff_text]

    def get_latest_heartbeat(self, *, component: str) -> dict[str, Any] | None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            row = conn.execute(
                """
                SELECT component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                FROM pipeline_worker_heartbeats
                WHERE component = ?
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (component,),
            ).fetchone()
        return _rows([row])[0] if row else None

    def list_stale_success_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        cutoff_text = cutoff.astimezone(UTC).isoformat()
        self.init_schema()
        placeholders = ",".join("?" for _ in EXPECTED_HEARTBEAT_COMPONENTS)
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f"""SELECT h.* FROM pipeline_worker_heartbeats h
                JOIN (SELECT component,MAX(last_seen_at) seen FROM pipeline_worker_heartbeats GROUP BY component) l
                  ON l.component=h.component AND l.seen=h.last_seen_at
                WHERE h.component IN ({placeholders}) AND h.last_seen_at>=?
                  AND (h.last_success_at IS NULL OR h.last_success_at<?) ORDER BY h.component""",
                (*EXPECTED_HEARTBEAT_COMPONENTS, cutoff_text, cutoff_text)).fetchall()
        return _rows(rows)

    def list_old_claimable_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        return self._task_groups(
            "status='pending' AND julianday(updated_at) < julianday(?)",
            (cutoff.astimezone(UTC).isoformat(),),
            False,
        )

    def list_stuck_processing_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        now = datetime.now(UTC).isoformat()
        return self._task_groups(
            "status='running' AND (julianday(updated_at) < julianday(?) OR julianday(locked_until) < julianday(?))",
            (cutoff.astimezone(UTC).isoformat(), now),
            True,
        )

    def _task_groups(self, where: str, args: tuple[Any, ...], include_lock: bool) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in MONITORED_TASK_SOURCES)
        lock_column = (
            ", MIN(datetime(locked_until)) AS oldest_locked_until"
            if include_lock
            else ", MIN(datetime(created_at)) AS oldest_created_at"
        )
        with connect_sqlite(self.path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
                return []
            rows = conn.execute(
                f"SELECT source,status,COUNT(*) count,MIN(datetime(updated_at)) oldest_updated_at{lock_column} "
                f"FROM tasks WHERE source IN ({placeholders}) AND {where} "
                "GROUP BY source,status ORDER BY julianday(oldest_updated_at)",
                (*MONITORED_TASK_SOURCES, *args),
            ).fetchall()
        return _rows(rows)

    def list_recent_failed_tasks(self, *, since: datetime, threshold: int) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in MONITORED_TASK_SOURCES)
        statuses = ",".join("?" for _ in FAILURE_STATUSES)
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f"""SELECT t.source,t.status,COUNT(*) count,MAX(t.updated_at) latest_updated_at,
                SUBSTR(MAX(COALESCE(x.last_error,e.last_error,'')),1,500) sample_error FROM tasks t
                LEFT JOIN x_task_pipeline x ON x.task_id=t.id LEFT JOIN external_media_alert_pipeline e ON e.task_id=t.id
                WHERE t.source IN ({placeholders}) AND t.status IN ({statuses}) AND t.updated_at>=?
                GROUP BY t.source,t.status HAVING COUNT(*)>=? ORDER BY count DESC,latest_updated_at DESC""",
                (*MONITORED_TASK_SOURCES, *FAILURE_STATUSES, since.astimezone(UTC).isoformat(), threshold)).fetchall()
        return _rows(rows)

    def list_recent_dashscope_arrearage_failures(self, *, since: datetime) -> list[dict[str, Any]]:
        rows = self.list_recent_failed_tasks(since=since, threshold=1)
        return [r for r in rows if r["status"] == "search_failed" and "dashscope" in (r.get("sample_error") or "").lower() and "arrearage" in (r.get("sample_error") or "").lower()]

    def count_recent_x_success_attempts(self, *, since: datetime) -> int:
        with connect_sqlite(self.path) as conn:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_capture_attempts'").fetchone(): return 0
            row = conn.execute("SELECT 1 FROM x_capture_attempts WHERE status='success' AND finished_at>=? LIMIT 1", (since.astimezone(UTC).isoformat(),)).fetchone()
        return 1 if row else 0

    def count_recent_x_capture_success_heartbeats(self, *, since: datetime) -> int:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            row = conn.execute("SELECT 1 FROM pipeline_worker_heartbeats WHERE component='x_capture' AND last_success_at>=? LIMIT 1", (since.astimezone(UTC).isoformat(),)).fetchone()
        return 1 if row else 0

    def claim_alert(self, *, alert_key: str, message: str, dedup_cutoff: datetime, metadata: dict[str, Any] | None = None) -> bool:
        self.init_schema()
        now = datetime.now(UTC).isoformat()
        with connect_sqlite(self.path) as conn:
            existing = conn.execute("SELECT last_sent_at FROM pipeline_alerts WHERE alert_key=?", (alert_key,)).fetchone()
            if existing and str(existing["last_sent_at"]) >= dedup_cutoff.astimezone(UTC).isoformat(): return False
            conn.execute("""INSERT INTO pipeline_alerts(alert_key,last_sent_at,last_message,metadata) VALUES(?,?,?,?)
                ON CONFLICT(alert_key) DO UPDATE SET last_sent_at=excluded.last_sent_at,last_message=excluded.last_message,
                send_count=pipeline_alerts.send_count+1,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP""",
                (alert_key, now, message, json.dumps(to_json_safe(metadata or {}), ensure_ascii=False)))
            conn.commit()
        return True


def create_pipeline_supervisor_repository(database_url: str | None = None) -> SQLitePipelineSupervisorRepository:
    del database_url
    return SQLitePipelineSupervisorRepository(load_storage_settings().sqlite_path)
