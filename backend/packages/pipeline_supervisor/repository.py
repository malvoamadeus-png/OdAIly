from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Protocol

from packages.common.pipeline_schema import PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url


def to_json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): to_json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(
            (to_json_safe(item) for item in value),
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
        )
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


EXPECTED_HEARTBEAT_COMPONENTS = [
    "x_capture",
    "non_mainstream_media",
    "competitor_monitor",
    "external_media_alert_domain_judge",
    "external_media_alert_search",
    "external_media_alert_notify",
    "x_process_judge",
    "x_process_search",
    "x_process_write",
    "x_process_format_publish",
]

MONITORED_TASK_SOURCES = ["x", "blockbeats", "panews", "jinse", "non_mainstream_media", "mainstream_media", "external_media_alert"]


class PipelineSupervisorRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_stale_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]: ...
    def list_stale_success_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]: ...
    def list_old_claimable_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]: ...
    def list_stuck_processing_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]: ...
    def list_recent_failed_tasks(self, *, since: datetime, threshold: int) -> list[dict[str, Any]]: ...
    def count_recent_x_success_attempts(self, *, since: datetime) -> int: ...
    def count_recent_x_capture_success_heartbeats(self, *, since: datetime) -> int: ...
    def claim_alert(self, *, alert_key: str, message: str, dedup_cutoff: datetime, metadata: dict[str, Any] | None = None) -> bool: ...


class PostgresPipelineSupervisorRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(PIPELINE_MONITORING_SCHEMA_SQL)
            conn.commit()

    def list_stale_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH expected(component) AS (
                    SELECT unnest(%s::text[])
                ),
                latest AS (
                    SELECT DISTINCT ON (component)
                        component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                    FROM pipeline_worker_heartbeats
                    ORDER BY component, last_seen_at DESC
                )
                SELECT
                    expected.component,
                    latest.worker_id,
                    latest.status,
                    latest.last_seen_at,
                    latest.last_success_at,
                    latest.last_error,
                    latest.metadata
                FROM expected
                LEFT JOIN latest ON latest.component = expected.component
                WHERE latest.last_seen_at IS NULL OR latest.last_seen_at < %s
                ORDER BY expected.component ASC
                """,
                (EXPECTED_HEARTBEAT_COMPONENTS, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_stale_success_heartbeats(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (component)
                        component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                    FROM pipeline_worker_heartbeats
                    WHERE component = ANY(%s::text[])
                    ORDER BY component, last_seen_at DESC
                )
                SELECT component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                FROM latest
                WHERE last_seen_at >= %s
                  AND (last_success_at IS NULL OR last_success_at < %s)
                ORDER BY component ASC
                """,
                (EXPECTED_HEARTBEAT_COMPONENTS, cutoff, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_old_claimable_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, status, count(*)::int AS count, min(created_at) AS oldest_created_at, min(updated_at) AS oldest_updated_at
                FROM tasks
                WHERE source = ANY(%s::text[])
                  AND status = ANY(%s::text[])
                  AND updated_at < %s
                GROUP BY source, status
                ORDER BY oldest_updated_at ASC
                """,
                (
                    MONITORED_TASK_SOURCES,
                    ["pending", "judged", "searched", "classified", "deduped", "written"],
                    cutoff,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_stuck_processing_tasks(self, *, cutoff: datetime) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, status, count(*)::int AS count, min(updated_at) AS oldest_updated_at, min(locked_until) AS oldest_locked_until
                FROM tasks
                WHERE source = ANY(%s::text[])
                  AND status = ANY(%s::text[])
                  AND (
                      updated_at < %s
                      OR locked_until < now()
                  )
                GROUP BY source, status
                ORDER BY oldest_updated_at ASC
                """,
                (
                    MONITORED_TASK_SOURCES,
                    ["judging", "classifying", "deduping", "writing", "formatting", "notifying"],
                    cutoff,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_recent_failed_tasks(self, *, since: datetime, threshold: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source, status, count(*)::int AS count, max(updated_at) AS latest_updated_at
                FROM tasks
                WHERE source = ANY(%s::text[])
                  AND status = ANY(%s::text[])
                  AND updated_at >= %s
                GROUP BY source, status
                HAVING count(*) >= %s
                ORDER BY count(*) DESC, latest_updated_at DESC
                """,
                (
                    MONITORED_TASK_SOURCES,
                    ["judge_failed", "domain_failed", "search_failed", "write_failed", "format_failed", "publish_failed", "notify_failed"],
                    since,
                    threshold,
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def count_recent_x_success_attempts(self, *, since: datetime) -> int:
        with self._connect() as conn:
            exists = conn.execute("SELECT to_regclass('public.x_capture_attempts') AS table_name").fetchone()
            if not exists or not exists.get("table_name"):
                return 0
            row = conn.execute(
                """
                SELECT count(*)::int AS count
                FROM x_capture_attempts
                WHERE status = 'success'
                  AND finished_at >= %s
                """,
                (since,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def count_recent_x_capture_success_heartbeats(self, *, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT count(*)::int AS count
                FROM pipeline_worker_heartbeats
                WHERE component = 'x_capture'
                  AND last_success_at >= %s
                """,
                (since,),
            ).fetchone()
        return int(row["count"]) if row else 0

    def claim_alert(self, *, alert_key: str, message: str, dedup_cutoff: datetime, metadata: dict[str, Any] | None = None) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO pipeline_alerts (alert_key, last_sent_at, last_message, metadata)
                VALUES (%s, now(), %s, %s)
                ON CONFLICT (alert_key) DO UPDATE SET
                    last_sent_at = EXCLUDED.last_sent_at,
                    last_message = EXCLUDED.last_message,
                    send_count = pipeline_alerts.send_count + 1,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                WHERE pipeline_alerts.last_sent_at < %s
                RETURNING alert_key
                """,
                (alert_key, message, self._Jsonb(to_json_safe(metadata or {})), dedup_cutoff),
            ).fetchone()
            conn.commit()
            return row is not None
