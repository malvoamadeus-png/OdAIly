from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from packages.common.postgres import build_psycopg_connect_kwargs
from packages.common.pipeline_schema import COMPETITOR_FILTER_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL, WRITER3_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url

from .models import ContextResult, OdailyReference, Writer3Candidate, Writer3Task


WRITER3_CURRENT_SOURCE = "odaily_reference"


class Writer3Repository(Protocol):
    def init_schema(self) -> None: ...
    def claim_task(
        self,
        *,
        worker_id: str,
        start_after: datetime,
        freshness_window_seconds: int,
        lock_seconds: int = 300,
    ) -> Writer3Task | None: ...
    def list_odaily_references(self, *, since: datetime) -> list[OdailyReference]: ...
    def upsert_odaily_references(self, references: list[OdailyReference]) -> int: ...
    def complete_skipped(self, task: Writer3Task, *, reason: str, metadata: dict[str, Any] | None = None) -> None: ...
    def complete_sent(
        self,
        task: Writer3Task,
        *,
        analysis: dict[str, Any],
        candidates: list[Writer3Candidate],
        context: ContextResult,
        telegram_text: str,
        telegram_result: dict[str, Any],
        analysis_model: str,
        writer_model: str,
        writer_reasoning_effort: str,
    ) -> None: ...
    def complete_failed(self, task: Writer3Task, *, error: str) -> None: ...
    def reset_task(self, task_id: int) -> bool: ...
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


class PostgresWriter3Repository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()
        self.application_name = "odaily-writer3"

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
            conn.execute(PIPELINE_MONITORING_SCHEMA_SQL + COMPETITOR_FILTER_SCHEMA_SQL + WRITER3_SCHEMA_SQL)
            conn.commit()

    def claim_task(
        self,
        *,
        worker_id: str,
        start_after: datetime,
        freshness_window_seconds: int,
        lock_seconds: int = 300,
    ) -> Writer3Task | None:
        current_cutoff = datetime.now(UTC) - timedelta(seconds=freshness_window_seconds)
        lock_until = datetime.now(UTC) + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    SELECT
                        NULL::bigint AS task_id,
                        %(current_source)s AS source,
                        r.source_item_id,
                        r.source_url,
                        r.title,
                        r.content,
                        r.published_at,
                        r.updated_at,
                        r.metadata,
                        CASE
                            WHEN r.content ~* '^\\s*Odaily\\s*星球日报讯'
                                THEN r.content
                            ELSE 'Odaily星球日报讯 ' || r.content
                        END AS final_content,
                        w.id AS context_id
                    FROM odaily_reference_items r
                    LEFT JOIN writer3_contexts w
                        ON w.current_source = %(current_source)s
                       AND w.current_source_item_id = r.source_item_id
                    WHERE r.published_at IS NOT NULL
                      AND r.content IS NOT NULL
                      AND r.content <> ''
                      AND r.published_at >= %(start_after)s
                      AND r.published_at >= %(current_cutoff)s
                      AND r.published_at <= now()
                      AND (
                          w.id IS NULL
                          OR (
                              w.status IN ('pending', 'processing')
                              AND (w.locked_until IS NULL OR w.locked_until < now())
                          )
                      )
                    ORDER BY r.published_at ASC, r.source_item_id ASC
                    FOR UPDATE OF r SKIP LOCKED
                    LIMIT 1
                    """,
                    {
                        "current_source": WRITER3_CURRENT_SOURCE,
                        "start_after": start_after,
                        "current_cutoff": current_cutoff,
                    },
                ).fetchone()
                if row is None:
                    return None
                context_row = conn.execute(
                    """
                    INSERT INTO writer3_contexts (
                        task_id, current_source, current_source_item_id, current_source_url,
                        current_title, current_content, current_published_at,
                        status, locked_by, locked_until, attempt_count, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'processing', %s, %s, 1, now(), now())
                    ON CONFLICT (current_source, current_source_item_id) DO UPDATE SET
                        status = 'processing',
                        task_id = COALESCE(EXCLUDED.task_id, writer3_contexts.task_id),
                        current_source_url = EXCLUDED.current_source_url,
                        current_title = EXCLUDED.current_title,
                        current_content = EXCLUDED.current_content,
                        current_published_at = EXCLUDED.current_published_at,
                        locked_by = EXCLUDED.locked_by,
                        locked_until = EXCLUDED.locked_until,
                        attempt_count = writer3_contexts.attempt_count + 1,
                        last_error = NULL,
                        updated_at = now()
                    RETURNING id
                    """,
                    (
                        row["task_id"],
                        row["source"],
                        row["source_item_id"],
                        row["source_url"],
                        row["title"],
                        row["final_content"],
                        row["published_at"],
                        worker_id,
                        lock_until,
                    ),
                ).fetchone()
            conn.commit()
        task = _row_to_task(row)
        return _replace_task_context_id(task, int(context_row["id"]) if context_row else task.context_id)

    def list_odaily_references(self, *, since: datetime) -> list[OdailyReference]:
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT source_item_id, source_url, title, content, published_at, metadata, raw_payload
                FROM odaily_reference_items
                WHERE published_at >= %s
                ORDER BY published_at DESC NULLS LAST, updated_at DESC
                """,
                (since,),
            ).fetchall()
        return [_row_to_reference(row) for row in rows]

    def upsert_odaily_references(self, references: list[OdailyReference]) -> int:
        if not references:
            return 0
        count = 0
        with self._connect() as conn:
            for item in references:
                conn.execute(
                    """
                    INSERT INTO odaily_reference_items (
                        source_item_id, source_url, title, content, published_at, raw_payload, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source_item_id) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        published_at = EXCLUDED.published_at,
                        raw_payload = EXCLUDED.raw_payload,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    (
                        item.source_item_id,
                        item.source_url,
                        item.title,
                        item.content,
                        item.published_at,
                        self._Jsonb(item.raw_payload),
                        self._Jsonb(item.metadata),
                    ),
                )
                count += 1
            conn.commit()
        return count

    def complete_skipped(self, task: Writer3Task, *, reason: str, metadata: dict[str, Any] | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE writer3_contexts
                SET status = 'skipped',
                    skip_reason = %s,
                    metadata = %s,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (reason, self._Jsonb(metadata or {}), _context_id(task)),
            )
            conn.commit()

    def complete_sent(
        self,
        task: Writer3Task,
        *,
        analysis: dict[str, Any],
        candidates: list[Writer3Candidate],
        context: ContextResult,
        telegram_text: str,
        telegram_result: dict[str, Any],
        analysis_model: str,
        writer_model: str,
        writer_reasoning_effort: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE writer3_contexts
                SET status = 'sent',
                    analysis_model = %s,
                    writer_model = %s,
                    writer_reasoning_effort = %s,
                    analysis_result = %s,
                    candidates = %s,
                    context_text = %s,
                    evidence_source_item_ids = %s,
                    telegram_text = %s,
                    telegram_result = %s,
                    sent_at = now(),
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    analysis_model,
                    writer_model,
                    writer_reasoning_effort,
                    self._Jsonb(analysis),
                    self._Jsonb([_candidate_to_dict(item) for item in candidates]),
                    context.context_text,
                    context.evidence_source_item_ids,
                    telegram_text,
                    self._Jsonb(telegram_result),
                    _context_id(task),
                ),
            )
            conn.commit()

    def complete_failed(self, task: Writer3Task, *, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE writer3_contexts
                SET status = 'failed',
                    last_error = %s,
                    locked_by = NULL,
                    locked_until = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (error[:2000], _context_id(task)),
            )
            conn.commit()

    def reset_task(self, task_id: int) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE writer3_contexts
                SET status = 'pending',
                    locked_by = NULL,
                    locked_until = NULL,
                    last_error = NULL,
                    skip_reason = NULL,
                    updated_at = now()
                WHERE task_id = %s
                  AND status IN ('failed', 'skipped', 'processing')
                """,
                (task_id,),
            )
            conn.commit()
            return bool(cursor.rowcount)

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


def _row_to_task(row: dict[str, Any]) -> Writer3Task:
    return Writer3Task(
        task_id=int(row["task_id"]) if row.get("task_id") is not None else None,
        source=str(row["source"]),
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        final_content=str(row["final_content"]),
        published_at=row.get("published_at"),
        updated_at=row.get("updated_at"),
        metadata=row.get("metadata") or {},
        context_id=int(row["context_id"]) if row.get("context_id") is not None else None,
    )


def _replace_task_context_id(task: Writer3Task, context_id: int | None) -> Writer3Task:
    return Writer3Task(
        task_id=task.task_id,
        source=task.source,
        source_item_id=task.source_item_id,
        source_url=task.source_url,
        title=task.title,
        content=task.content,
        final_content=task.final_content,
        published_at=task.published_at,
        updated_at=task.updated_at,
        metadata=task.metadata,
        context_id=context_id,
    )


def _context_id(task: Writer3Task) -> int:
    if task.context_id is None:
        raise ValueError("missing writer3 context_id")
    return task.context_id


def _row_to_reference(row: dict[str, Any]) -> OdailyReference:
    return OdailyReference(
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        published_at=row.get("published_at"),
        metadata=row.get("metadata") or {},
        raw_payload=row.get("raw_payload") or {},
    )


def _candidate_to_dict(item: Writer3Candidate) -> dict[str, Any]:
    return {
        "source_item_id": item.source_item_id,
        "source_url": item.source_url,
        "title": item.title,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "score": item.score,
        "matched_aliases": item.matched_aliases,
        "matched_prior_types": item.matched_prior_types,
    }
