from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from packages.common.postgres import build_psycopg_connect_kwargs
from packages.common.pipeline_schema import AUDITOR_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url

from .models import AuditorTask


class AuditorRepository(Protocol):
    def init_schema(self) -> None: ...
    def claim_task(
        self,
        *,
        worker_id: str,
        prompt_version: str,
        lookback_minutes: int,
        lock_seconds: int = 300,
    ) -> AuditorTask | None: ...
    def complete_passed(self, task: AuditorTask, *, model: str, prompt_version: str, raw_output: str, result: dict[str, Any]) -> None: ...
    def complete_flagged(
        self,
        task: AuditorTask,
        *,
        model: str,
        prompt_version: str,
        raw_output: str,
        result: dict[str, Any],
        telegram_text: str,
        telegram_result: dict[str, Any],
    ) -> None: ...
    def complete_failed(self, task: AuditorTask, *, error: str) -> None: ...
    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class PostgresAuditorRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()
        self.application_name = "odaily-auditor"

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=self._dict_row,
                autocommit=autocommit,
                application_name=self.application_name,
            ),
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(PIPELINE_MONITORING_SCHEMA_SQL + AUDITOR_SCHEMA_SQL)
            conn.commit()

    def claim_task(
        self,
        *,
        worker_id: str,
        prompt_version: str,
        lookback_minutes: int,
        lock_seconds: int = 300,
    ) -> AuditorTask | None:
        cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
        lock_until = datetime.now(UTC) + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT
                        r.source_item_id,
                        r.source_url,
                        r.title,
                        r.content,
                        r.published_at,
                        r.metadata,
                        md5(coalesce(r.title, '') || E'\n' || r.content) AS content_hash
                    FROM odaily_reference_items r
                    LEFT JOIN auditor_checks a
                        ON a.source_item_id = r.source_item_id
                       AND a.content_hash = md5(coalesce(r.title, '') || E'\n' || r.content)
                       AND a.prompt_version = %(prompt_version)s
                    WHERE r.content IS NOT NULL
                      AND r.content <> ''
                      AND r.published_at IS NOT NULL
                      AND r.published_at >= %(cutoff)s
                      AND r.published_at <= now()
                      AND (
                          a.id IS NULL
                          OR (
                              a.status IN ('pending', 'processing', 'failed')
                              AND (a.locked_until IS NULL OR a.locked_until < now())
                          )
                      )
                    ORDER BY r.published_at ASC, r.source_item_id ASC
                    FOR UPDATE OF r SKIP LOCKED
                    LIMIT 1
                    """,
                    {"prompt_version": prompt_version, "cutoff": cutoff},
                ).fetchone()
                if row is None:
                    return None
                check_row = conn.execute(
                    """
                    INSERT INTO auditor_checks (
                        source_item_id, source_url, title, content, content_hash, published_at,
                        prompt_version, status, locked_by, locked_until, attempt_count,
                        metadata, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'processing', %s, %s, 1, %s, now(), now())
                    ON CONFLICT (source_item_id, content_hash, prompt_version) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        published_at = EXCLUDED.published_at,
                        status = 'processing',
                        locked_by = EXCLUDED.locked_by,
                        locked_until = EXCLUDED.locked_until,
                        attempt_count = auditor_checks.attempt_count + 1,
                        last_error = NULL,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        row["source_item_id"],
                        row["source_url"],
                        row["title"],
                        row["content"],
                        row["content_hash"],
                        row["published_at"],
                        prompt_version,
                        worker_id,
                        lock_until,
                        self._Jsonb(row.get("metadata") or {}),
                    ),
                ).fetchone()
            conn.commit()
        return _row_to_task({**dict(row), "id": check_row["id"]})

    def complete_passed(self, task: AuditorTask, *, model: str, prompt_version: str, raw_output: str, result: dict[str, Any]) -> None:
        self._complete(
            task,
            status="passed",
            model=model,
            prompt_version=prompt_version,
            raw_output=raw_output,
            result=result,
            telegram_text=None,
            telegram_result={},
        )

    def complete_flagged(
        self,
        task: AuditorTask,
        *,
        model: str,
        prompt_version: str,
        raw_output: str,
        result: dict[str, Any],
        telegram_text: str,
        telegram_result: dict[str, Any],
    ) -> None:
        self._complete(
            task,
            status="flagged",
            model=model,
            prompt_version=prompt_version,
            raw_output=raw_output,
            result=result,
            telegram_text=telegram_text,
            telegram_result=telegram_result,
        )

    def _complete(
        self,
        task: AuditorTask,
        *,
        status: str,
        model: str,
        prompt_version: str,
        raw_output: str,
        result: dict[str, Any],
        telegram_text: str | None,
        telegram_result: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE auditor_checks
                SET status = %s,
                    model = %s,
                    prompt_version = %s,
                    raw_output = %s,
                    audit_result = %s,
                    telegram_text = %s,
                    telegram_result = %s,
                    alerted_at = CASE WHEN %s = 'flagged' THEN now() ELSE alerted_at END,
                    locked_by = NULL,
                    locked_until = NULL,
                    last_error = CASE WHEN %s THEN NULL ELSE %s END,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    status,
                    model,
                    prompt_version,
                    raw_output,
                    self._Jsonb(result),
                    telegram_text,
                    self._Jsonb(telegram_result),
                    status,
                    bool(telegram_result.get("ok", True)),
                    telegram_result.get("error"),
                    task.id,
                ),
            )
            conn.commit()

    def complete_failed(self, task: AuditorTask, *, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE auditor_checks
                SET status = 'failed',
                    last_error = %s,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (error[:2000], task.id),
            )
            conn.commit()

    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_worker_heartbeats (
                    component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                )
                VALUES (%s, %s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END, %s, %s)
                ON CONFLICT (component, worker_id) DO UPDATE SET
                    status = excluded.status,
                    last_seen_at = excluded.last_seen_at,
                    last_success_at = CASE
                        WHEN %s THEN excluded.last_seen_at
                        ELSE pipeline_worker_heartbeats.last_success_at
                    END,
                    last_error = excluded.last_error,
                    metadata = excluded.metadata,
                    updated_at = now()
                """,
                (component, worker_id, status, success, error, self._Jsonb(metadata or {}), success),
            )
            conn.commit()


def calculate_content_hash(title: str | None, content: str) -> str:
    import hashlib

    return hashlib.md5(f"{title or ''}\n{content}".encode("utf-8"), usedforsecurity=False).hexdigest()


def _row_to_task(row: dict[str, Any]) -> AuditorTask:
    return AuditorTask(
        id=int(row["id"]),
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        published_at=row.get("published_at"),
        metadata=row.get("metadata") or {},
    )
