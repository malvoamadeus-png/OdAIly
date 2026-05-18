from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from packages.common.pipeline_schema import (
    COMPETITOR_FILTER_SCHEMA_SQL,
    NEWSFLASH_EVENT_SCHEMA_SQL,
    PIPELINE_MONITORING_SCHEMA_SQL,
)

from .models import (
    COMPETITOR_SOURCES,
    NEWS_TYPES,
    NON_MAINSTREAM_MEDIA_SOURCE,
    ODAILY_REFERENCE_SOURCE,
    PROMPT_KEY_BY_NEWS_TYPE,
    PROCESSING_SOURCES,
    SEARCH_FIRST_SOURCES,
    STAGE_SPECS,
    NewsType,
    PipelineRecord,
    ProcessingStage,
    PromptTemplateVersion,
    TaskRecord,
)
from .searcher import SearchDocument, content_hash


TASK_NOTIFY_CHANNEL = "x_task_queue_changed"
PROMPT_NOTIFY_CHANNEL = "prompt_config_changed"


PROMPT_SEEDS: dict[str, tuple[str, str, str]] = {
    "x_regular_writer": ("X 常规快讯", "docs/常规快讯模板.txt", "initial regular writer template"),
    "x_onchain_writer": ("X 链上快讯", "docs/链上快讯模板.txt", "initial onchain writer template"),
    "x_funding_writer": ("X 融资快讯", "docs/融资快讯模板.txt", "initial funding writer template"),
    "non_mainstream_media_writer": (
        "非主流外媒快讯",
        "docs/外媒模板.txt",
        "initial non-mainstream media writer template",
    ),
}


class XProcessingRepository(Protocol):
    def claim_task(self, stage: ProcessingStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None: ...
    def get_pipeline(self, task_id: int) -> PipelineRecord: ...
    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion: ...
    def complete_judge(
        self,
        task_id: int,
        *,
        news_type: NewsType,
        model: str,
        raw_output: str,
    ) -> None: ...
    def complete_judge_discard(
        self,
        task_id: int,
        *,
        discard_type: str,
        model: str,
        raw_output: str,
    ) -> None: ...
    def complete_search(self, task_id: int) -> None: ...
    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None: ...
    def complete_search_ready(self, task_id: int, *, candidate_id: int, result: dict[str, Any]) -> None: ...
    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]: ...
    def list_active_candidate_documents(self) -> list[SearchDocument]: ...
    def create_candidate_for_task(self, task: TaskRecord, *, search_result: dict[str, Any]) -> tuple[int, bool]: ...
    def link_task_to_candidate(self, task: TaskRecord, *, candidate_id: int, search_result: dict[str, Any]) -> None: ...
    def complete_write(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion,
        model: str,
        draft_title: str,
        draft_content: str,
        raw_output: str,
    ) -> None: ...
    def complete_format_publish(
        self,
        task_id: int,
        *,
        final_title: str,
        final_content: str,
        push_result: dict[str, Any],
        telegram_result: dict[str, Any],
    ) -> None: ...
    def fail_task(self, task_id: int, *, stage: ProcessingStage, error: str, status: str | None = None) -> None: ...
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


def utc_now() -> datetime:
    return datetime.now(UTC)


def get_database_url() -> str:
    load_dotenv()
    value = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Missing SUPABASE_DB_URL or DATABASE_URL")
    return value


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except Exception as exc:  # pragma: no cover - exercised only when dependency is absent.
        raise RuntimeError("psycopg is required for Supabase/Postgres access") from exc
    return psycopg, dict_row, Jsonb


def _row_to_task(row: dict[str, Any]) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        source=str(row["source"]),
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        raw_payload=row.get("raw_payload") or {},
        metadata=row.get("metadata") or {},
        status=str(row["status"]),
        published_at=row.get("published_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_pipeline(row: dict[str, Any]) -> PipelineRecord:
    news_type = row.get("news_type")
    if news_type not in NEWS_TYPES:
        news_type = None
    return PipelineRecord(
        task_id=int(row["task_id"]),
        news_type=news_type,
        candidate_id=row.get("candidate_id"),
        prompt_template_key=row.get("prompt_template_key"),
        prompt_version_id=row.get("prompt_version_id"),
        draft_title=row.get("draft_title"),
        draft_content=row.get("draft_content"),
        final_title=row.get("final_title"),
        final_content=row.get("final_content"),
        push_result=row.get("push_result") or {},
        telegram_result=row.get("telegram_result") or {},
        last_error=row.get("last_error"),
    )


def _row_to_prompt(row: dict[str, Any]) -> PromptTemplateVersion:
    return PromptTemplateVersion(
        id=int(row["id"]),
        template_key=str(row["template_key"]),
        version_number=int(row["version_number"]),
        content=str(row["content"]),
        note=row.get("note"),
        created_at=row.get("created_at"),
        published_at=row.get("published_at"),
    )


class PostgresXProcessingRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row, autocommit=autocommit)

    def init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def clear_old_pending_x_tasks(self) -> int:
        with self._connect() as conn:
            row = conn.execute("DELETE FROM tasks WHERE source = 'x' AND status = 'pending' RETURNING id").fetchall()
            conn.commit()
            return len(row)

    def seed_prompt_templates(self, *, root_dir: Path) -> None:
        with self._connect() as conn:
            for template_key, (display_name, relative_path, note) in PROMPT_SEEDS.items():
                content = (root_dir / relative_path).read_text(encoding="utf-8")
                conn.execute(
                    """
                    INSERT INTO prompt_templates (template_key, display_name)
                    VALUES (%s, %s)
                    ON CONFLICT (template_key) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        updated_at = now()
                    """,
                    (template_key, display_name),
                )
                existing = conn.execute(
                    "SELECT active_version_id FROM prompt_templates WHERE template_key = %s",
                    (template_key,),
                ).fetchone()
                if existing and existing.get("active_version_id"):
                    continue
                version = conn.execute(
                    """
                    INSERT INTO prompt_template_versions (template_key, version_number, content, note, published_at)
                    VALUES (%s, 1, %s, %s, now())
                    ON CONFLICT (template_key, version_number) DO UPDATE
                    SET content = EXCLUDED.content,
                        note = EXCLUDED.note,
                        published_at = COALESCE(prompt_template_versions.published_at, now())
                    RETURNING id
                    """,
                    (template_key, content, note),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE prompt_templates
                    SET active_version_id = %s,
                        updated_at = now()
                    WHERE template_key = %s
                    """,
                    (version["id"], template_key),
                )
            conn.commit()

    def claim_task(self, stage: ProcessingStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        spec = STAGE_SPECS[stage]
        sources = tuple(PROCESSING_SOURCES)
        source_filter = "t.source = ANY(%(sources)s)"
        if stage == "judge":
            source_filter = """
                (
                    (t.source = 'x' AND t.status = %(claim_status)s)
                    OR (t.source = ANY(%(search_first_sources)s) AND t.status = 'searched')
                    OR (t.source = ANY(%(sources)s) AND t.status = %(processing_status)s)
                )
            """
        elif stage == "search":
            source_filter = """
                (
                    (t.source = 'x' AND t.status = %(claim_status)s)
                    OR (t.source = ANY(%(search_first_sources)s) AND t.status = 'pending')
                    OR (t.source = ANY(%(sources)s) AND t.status = %(processing_status)s)
                )
            """
        else:
            source_filter = "t.source = ANY(%(sources)s) AND t.status IN (%(claim_status)s, %(processing_status)s)"
        with self._connect() as conn:
            row = conn.execute(
                f"""
                WITH candidate AS (
                    SELECT id
                    FROM tasks t
                    WHERE {source_filter}
                      AND (locked_until IS NULL OR locked_until < now())
                    ORDER BY created_at ASC, id ASC
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE tasks t
                SET status = %(processing_status)s,
                    locked_by = %(worker_id)s,
                    locked_until = now() + (%(lock_seconds)s || ' seconds')::interval,
                    attempt_count = attempt_count + 1,
                    updated_at = now()
                FROM candidate
                WHERE t.id = candidate.id
                RETURNING t.*
                """,
                {
                    "claim_status": spec.claim_status,
                    "processing_status": spec.processing_status,
                    "worker_id": worker_id,
                    "lock_seconds": lock_seconds,
                    "sources": list(sources),
                    "search_first_sources": list(SEARCH_FIRST_SOURCES),
                },
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "INSERT INTO x_task_pipeline (task_id) VALUES (%s) ON CONFLICT (task_id) DO NOTHING",
                (row["id"],),
            )
            conn.commit()
            return _row_to_task(row)

    def get_pipeline(self, task_id: int) -> PipelineRecord:
        with self._connect(autocommit=True) as conn:
            row = conn.execute("SELECT * FROM x_task_pipeline WHERE task_id = %s", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"pipeline row not found for task {task_id}")
        return _row_to_pipeline(row)

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        with self._connect(autocommit=True) as conn:
            row = conn.execute(
                """
                SELECT v.*
                FROM prompt_templates t
                JOIN prompt_template_versions v ON v.id = t.active_version_id
                WHERE t.template_key = %s
                """,
                (template_key,),
            ).fetchone()
        if row is None:
            raise ValueError(f"active prompt not found: {template_key}")
        return _row_to_prompt(row)

    def complete_judge(self, task_id: int, *, news_type: NewsType, model: str, raw_output: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET news_type = %s,
                    judge_model = %s,
                    judge_output = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    news_type,
                    model,
                    self._Jsonb({"route": news_type, "discard_type": "none", "raw_output": raw_output}),
                    task_id,
                ),
            )
            task = conn.execute("SELECT source FROM tasks WHERE id = %s", (task_id,)).fetchone()
            next_status = "deduped" if task and task.get("source") in SEARCH_FIRST_SOURCES else "judged"
            self._set_task_status(conn, task_id, next_status)
            conn.commit()

    def complete_judge_discard(self, task_id: int, *, discard_type: str, model: str, raw_output: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET news_type = NULL,
                    judge_model = %s,
                    judge_output = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    model,
                    self._Jsonb({"route": "discard", "discard_type": discard_type, "raw_output": raw_output}),
                    task_id,
                ),
            )
            self._set_task_status(conn, task_id, "discarded")
            conn.commit()

    def complete_search(self, task_id: int) -> None:
        self.complete_search_ready(task_id, candidate_id=0, result={"skipped": True, "reason": "searcher is no-op"})

    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            candidate_id = result.get("candidate_id")
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET candidate_id = COALESCE(%s, candidate_id),
                    search_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (candidate_id, self._Jsonb(result), task_id),
            )
            self._set_task_status(conn, task_id, "duplicate")
            conn.commit()

    def complete_search_ready(self, task_id: int, *, candidate_id: int, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET candidate_id = NULLIF(%s, 0),
                    search_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (candidate_id, self._Jsonb(result), task_id),
            )
            task = conn.execute("SELECT source FROM tasks WHERE id = %s", (task_id,)).fetchone()
            next_status = "searched" if task and task.get("source") in SEARCH_FIRST_SOURCES else "deduped"
            self._set_task_status(conn, task_id, next_status)
            conn.commit()

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT source_item_id, source_url, title, content, published_at, raw_payload, metadata
                FROM odaily_reference_items
                WHERE published_at IS NULL OR published_at >= %s
                ORDER BY published_at DESC NULLS LAST, updated_at DESC
                """,
                (since,),
            ).fetchall()
        return [
            SearchDocument(
                doc_type="odaily_reference",
                doc_id=str(row["source_item_id"]),
                title=row.get("title"),
                content=str(row["content"]),
                source=ODAILY_REFERENCE_SOURCE,
                source_url=row.get("source_url"),
                published_at=row.get("published_at"),
                metadata=row.get("metadata") or {},
            )
            for row in rows
        ]

    def list_active_candidate_documents(self) -> list[SearchDocument]:
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT id, primary_task_id, title, content, status, metadata, updated_at
                FROM search_event_candidates
                WHERE status = 'active'
                  AND (expires_at IS NULL OR expires_at > now())
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [
            SearchDocument(
                doc_type="candidate",
                doc_id=str(row["id"]),
                title=row.get("title"),
                content=str(row["content"]),
                source="candidate",
                task_id=row.get("primary_task_id"),
                candidate_id=int(row["id"]),
                metadata=row.get("metadata") or {},
            )
            for row in rows
        ]

    def create_candidate_for_task(self, task: TaskRecord, *, search_result: dict[str, Any]) -> tuple[int, bool]:
        text = f"{task.title or ''}\n{task.content}".strip()
        digest = content_hash(text)
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (digest,))
                existing = conn.execute(
                    """
                    SELECT id, primary_task_id
                    FROM search_event_candidates
                    WHERE content_hash = %s
                      AND status = 'active'
                      AND (expires_at IS NULL OR expires_at > now())
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (digest,),
                ).fetchone()
                if existing is not None:
                    candidate_id = int(existing["id"])
                    if int(existing["primary_task_id"]) == task.id:
                        self._insert_event_source(conn, candidate_id=candidate_id, task=task, role="primary", search_result=search_result)
                        return candidate_id, True
                    self._insert_event_source(conn, candidate_id=candidate_id, task=task, role="supporting", search_result=search_result)
                    return candidate_id, False
                row = conn.execute(
                    """
                    INSERT INTO search_event_candidates (
                        primary_task_id, status, title, content, content_hash, metadata, expires_at
                    )
                    VALUES (%s, 'active', %s, %s, %s, %s, now() + interval '2 days')
                    RETURNING id
                    """,
                    (
                        task.id,
                        task.title,
                        task.content,
                        digest,
                        self._Jsonb({"source": task.source, "source_item_id": task.source_item_id}),
                    ),
                ).fetchone()
                candidate_id = int(row["id"])
                self._insert_event_source(conn, candidate_id=candidate_id, task=task, role="primary", search_result=search_result)
            conn.commit()
            return candidate_id, True

    def link_task_to_candidate(self, task: TaskRecord, *, candidate_id: int, search_result: dict[str, Any]) -> None:
        with self._connect() as conn:
            with conn.transaction():
                self._insert_event_source(conn, candidate_id=candidate_id, task=task, role="supporting", search_result=search_result)
            conn.commit()

    def _insert_event_source(self, conn, *, candidate_id: int, task: TaskRecord, role: str, search_result: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO search_event_sources (
                candidate_id, task_id, source, source_item_id, source_url, title, content, role, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidate_id, task_id) DO UPDATE SET
                role = EXCLUDED.role,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            """,
            (
                candidate_id,
                task.id,
                task.source,
                task.source_item_id,
                task.source_url,
                task.title,
                task.content,
                role,
                self._Jsonb({"search_result": search_result}),
            ),
        )

    def complete_write(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion,
        model: str,
        draft_title: str,
        draft_content: str,
        raw_output: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET prompt_template_key = %s,
                    prompt_version_id = %s,
                    writer_model = %s,
                    writer_output = %s,
                    draft_title = %s,
                    draft_content = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    prompt.template_key,
                    prompt.id,
                    model,
                    self._Jsonb({"raw_output": raw_output}),
                    draft_title,
                    draft_content,
                    task_id,
                ),
            )
            self._set_task_status(conn, task_id, "written")
            conn.commit()

    def complete_format_publish(
        self,
        task_id: int,
        *,
        final_title: str,
        final_content: str,
        push_result: dict[str, Any],
        telegram_result: dict[str, Any],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET final_title = %(final_title)s,
                    final_content = %(final_content)s,
                    push_result = %(push_result)s,
                    telegram_result = %(telegram_result)s,
                    last_error = CASE WHEN %(telegram_ok)s THEN NULL ELSE %(telegram_error)s END,
                    updated_at = now()
                WHERE task_id = %(task_id)s
                """,
                {
                    "final_title": final_title,
                    "final_content": final_content,
                    "push_result": self._Jsonb(push_result),
                    "telegram_result": self._Jsonb(telegram_result),
                    "telegram_ok": bool(telegram_result.get("ok")),
                    "telegram_error": telegram_result.get("error"),
                    "task_id": task_id,
                },
            )
            self._set_task_status(conn, task_id, "ready_review")
            conn.commit()

    def fail_task(self, task_id: int, *, stage: ProcessingStage, error: str, status: str | None = None) -> None:
        status = status or STAGE_SPECS[stage].failure_status
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET last_error = %s,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (error[:2000], task_id),
            )
            self._set_task_status(conn, task_id, status)
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
                    status = EXCLUDED.status,
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_success_at = CASE
                        WHEN %s THEN EXCLUDED.last_success_at
                        ELSE pipeline_worker_heartbeats.last_success_at
                    END,
                    last_error = EXCLUDED.last_error,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    component,
                    worker_id,
                    status,
                    success,
                    (error or "")[:2000] if error else None,
                    self._Jsonb(metadata or {}),
                    success,
                ),
            )
            conn.commit()

    def _set_task_status(self, conn, task_id: int, status: str) -> None:
        conn.execute(
            """
            UPDATE tasks
            SET status = %s,
                locked_by = NULL,
                locked_until = NULL,
                updated_at = now()
            WHERE id = %s
            """,
            (status, task_id),
        )


class InMemoryXProcessingRepository:
    def __init__(self) -> None:
        self.tasks: dict[int, TaskRecord] = {}
        self.pipelines: dict[int, PipelineRecord] = {}
        self.odaily_references: list[SearchDocument] = []
        self.candidates: dict[int, SearchDocument] = {}
        self.event_sources: list[dict[str, Any]] = []
        self._next_candidate_id = 1
        self.prompts: dict[str, PromptTemplateVersion] = {
            key: PromptTemplateVersion(id=index, template_key=key, version_number=1, content=f"prompt {key}")
            for index, key in enumerate(PROMPT_KEY_BY_NEWS_TYPE.values(), start=1)
        }
        self._locks: set[int] = set()

    def add_task(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task

    def claim_task(self, stage: ProcessingStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        spec = STAGE_SPECS[stage]
        for task in sorted(self.tasks.values(), key=lambda item: item.id):
            claim_status = spec.claim_status
            if stage == "search" and task.source in SEARCH_FIRST_SOURCES:
                claim_status = "pending"
            elif stage == "judge" and task.source in SEARCH_FIRST_SOURCES:
                claim_status = "searched"
            if task.status not in {claim_status, spec.processing_status} or task.id in self._locks:
                continue
            self._locks.add(task.id)
            updated = TaskRecord(**{**asdict(task), "status": spec.processing_status, "updated_at": utc_now()})
            self.tasks[task.id] = updated
            self.pipelines.setdefault(task.id, PipelineRecord(task_id=task.id))
            return updated
        return None

    def get_pipeline(self, task_id: int) -> PipelineRecord:
        return self.pipelines[task_id]

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        return self.prompts[template_key]

    def complete_judge(self, task_id: int, *, news_type: NewsType, model: str, raw_output: str) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(**{**asdict(current), "news_type": news_type, "last_error": None})
        task = self.tasks[task_id]
        self._set_status(task_id, "deduped" if task.source in SEARCH_FIRST_SOURCES else "judged")

    def complete_judge_discard(self, task_id: int, *, discard_type: str, model: str, raw_output: str) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(**{**asdict(current), "news_type": None, "last_error": None})
        self._set_status(task_id, "discarded")

    def complete_search(self, task_id: int) -> None:
        self.complete_search_ready(task_id, candidate_id=0, result={"skipped": True, "reason": "searcher is no-op"})

    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(**{**asdict(current), "last_error": None})
        self._set_status(task_id, "duplicate")

    def complete_search_ready(self, task_id: int, *, candidate_id: int, result: dict[str, Any]) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(**{**asdict(current), "candidate_id": candidate_id or None, "last_error": None})
        task = self.tasks[task_id]
        self._set_status(task_id, "searched" if task.source in SEARCH_FIRST_SOURCES else "deduped")

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        return [
            item
            for item in self.odaily_references
            if item.published_at is None or item.published_at >= since
        ]

    def list_active_candidate_documents(self) -> list[SearchDocument]:
        return list(self.candidates.values())

    def create_candidate_for_task(self, task: TaskRecord, *, search_result: dict[str, Any]) -> tuple[int, bool]:
        for candidate_id, document in self.candidates.items():
            if document.task_id == task.id:
                self.event_sources.append(
                    {"candidate_id": candidate_id, "task_id": task.id, "role": "primary", "search_result": search_result}
                )
                return candidate_id, True
        candidate_id = self._next_candidate_id
        self._next_candidate_id += 1
        document = SearchDocument(
            doc_type="candidate",
            doc_id=str(candidate_id),
            title=task.title,
            content=task.content,
            source="candidate",
            task_id=task.id,
            candidate_id=candidate_id,
            metadata={"source": task.source, "source_item_id": task.source_item_id},
        )
        self.candidates[candidate_id] = document
        self.event_sources.append({"candidate_id": candidate_id, "task_id": task.id, "role": "primary", "search_result": search_result})
        return candidate_id, True

    def link_task_to_candidate(self, task: TaskRecord, *, candidate_id: int, search_result: dict[str, Any]) -> None:
        self.event_sources.append({"candidate_id": candidate_id, "task_id": task.id, "role": "supporting", "search_result": search_result})

    def complete_write(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion,
        model: str,
        draft_title: str,
        draft_content: str,
        raw_output: str,
    ) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(
            **{
                **asdict(current),
                "prompt_template_key": prompt.template_key,
                "prompt_version_id": prompt.id,
                "draft_title": draft_title,
                "draft_content": draft_content,
                "last_error": None,
            }
        )
        self._set_status(task_id, "written")

    def complete_format_publish(
        self,
        task_id: int,
        *,
        final_title: str,
        final_content: str,
        push_result: dict[str, Any],
        telegram_result: dict[str, Any],
    ) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = PipelineRecord(
            **{
                **asdict(current),
                "final_title": final_title,
                "final_content": final_content,
                "push_result": push_result,
                "telegram_result": telegram_result,
                "last_error": None if telegram_result.get("ok") else telegram_result.get("error"),
            }
        )
        self._set_status(task_id, "ready_review")

    def fail_task(self, task_id: int, *, stage: ProcessingStage, error: str, status: str | None = None) -> None:
        current = self.pipelines.get(task_id, PipelineRecord(task_id=task_id))
        self.pipelines[task_id] = PipelineRecord(**{**asdict(current), "last_error": error})
        self._set_status(task_id, status or STAGE_SPECS[stage].failure_status)

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
        return None

    def _set_status(self, task_id: int, status: str) -> None:
        task = self.tasks[task_id]
        self.tasks[task_id] = TaskRecord(**{**asdict(task), "status": status, "updated_at": utc_now()})
        self._locks.discard(task_id)


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + COMPETITOR_FILTER_SCHEMA_SQL + NEWSFLASH_EVENT_SCHEMA_SQL + """
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_until timestamptz;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_tasks_x_status_lock
ON tasks(source, status, locked_until, created_at ASC);

CREATE TABLE IF NOT EXISTS odaily_reference_items (
    source_item_id text PRIMARY KEY,
    source_url text,
    title text,
    content text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    template_key text PRIMARY KEY,
    display_name text NOT NULL,
    active_version_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_template_versions (
    id bigserial PRIMARY KEY,
    template_key text NOT NULL REFERENCES prompt_templates(template_key) ON DELETE CASCADE,
    version_number integer NOT NULL,
    content text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    UNIQUE (template_key, version_number)
);

CREATE TABLE IF NOT EXISTS x_task_pipeline (
    task_id bigint PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    news_type text CHECK (news_type IS NULL OR news_type IN ('regular', 'onchain', 'funding', 'non_mainstream_media')),
    candidate_id bigint,
    judge_model text,
    judge_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt_template_key text REFERENCES prompt_templates(template_key),
    prompt_version_id bigint REFERENCES prompt_template_versions(id),
    writer_model text,
    writer_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    draft_title text,
    draft_content text,
    final_title text,
    final_content text,
    push_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS candidate_id bigint;

CREATE TABLE IF NOT EXISTS search_event_candidates (
    id bigserial PRIMARY KEY,
    primary_task_id bigint REFERENCES tasks(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active',
    title text,
    content text NOT NULL,
    content_hash text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_event_sources (
    id bigserial PRIMARY KEY,
    candidate_id bigint NOT NULL REFERENCES search_event_candidates(id) ON DELETE CASCADE,
    task_id bigint REFERENCES tasks(id) ON DELETE SET NULL,
    source text NOT NULL,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    role text NOT NULL CHECK (role IN ('primary', 'supporting')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, task_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'x_task_pipeline_candidate_fk'
    ) THEN
        ALTER TABLE x_task_pipeline
        ADD CONSTRAINT x_task_pipeline_candidate_fk
        FOREIGN KEY (candidate_id) REFERENCES search_event_candidates(id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_news_type ON x_task_pipeline(news_type);
CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_candidate ON x_task_pipeline(candidate_id);
CREATE INDEX IF NOT EXISTS idx_odaily_reference_published ON odaily_reference_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_candidates_status_expires ON search_event_candidates(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_search_candidates_hash ON search_event_candidates(content_hash);
CREATE INDEX IF NOT EXISTS idx_search_sources_candidate ON search_event_sources(candidate_id);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_template ON prompt_template_versions(template_key, version_number DESC);

CREATE OR REPLACE FUNCTION notify_x_task_queue_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'x_task_queue_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'status', NEW.status)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_x_queue_notify ON tasks;
CREATE TRIGGER trg_tasks_x_queue_notify
AFTER INSERT OR UPDATE OF status ON tasks
FOR EACH ROW
WHEN (NEW.source IN ('x', 'blockbeats', 'panews', 'jinse', 'non_mainstream_media'))
EXECUTE FUNCTION notify_x_task_queue_changed();

CREATE OR REPLACE FUNCTION notify_prompt_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'prompt_config_changed',
        json_build_object('template_key', NEW.template_key, 'active_version_id', NEW.active_version_id)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prompt_templates_notify ON prompt_templates;
CREATE TRIGGER trg_prompt_templates_notify
AFTER UPDATE OF active_version_id ON prompt_templates
FOR EACH ROW
WHEN (OLD.active_version_id IS DISTINCT FROM NEW.active_version_id)
EXECUTE FUNCTION notify_prompt_config_changed();

ALTER TABLE prompt_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_template_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_task_pipeline ENABLE ROW LEVEL SECURITY;
ALTER TABLE odaily_reference_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_sources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS prompt_templates_anon_all ON prompt_templates;
DROP POLICY IF EXISTS prompt_template_versions_anon_all ON prompt_template_versions;
DROP POLICY IF EXISTS x_task_pipeline_anon_select ON x_task_pipeline;
DROP POLICY IF EXISTS odaily_reference_items_anon_select ON odaily_reference_items;
DROP POLICY IF EXISTS search_event_candidates_anon_select ON search_event_candidates;
DROP POLICY IF EXISTS search_event_sources_anon_select ON search_event_sources;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        GRANT USAGE ON SCHEMA public TO anon;
        GRANT SELECT, INSERT, UPDATE ON prompt_templates TO anon;
        GRANT SELECT, INSERT, UPDATE ON prompt_template_versions TO anon;
        GRANT SELECT ON x_task_pipeline, odaily_reference_items, search_event_candidates, search_event_sources TO anon;
        GRANT USAGE, SELECT ON SEQUENCE prompt_template_versions_id_seq TO anon;

        EXECUTE 'CREATE POLICY prompt_templates_anon_all ON prompt_templates FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY prompt_template_versions_anon_all ON prompt_template_versions FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY x_task_pipeline_anon_select ON x_task_pipeline FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY odaily_reference_items_anon_select ON odaily_reference_items FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY search_event_candidates_anon_select ON search_event_candidates FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY search_event_sources_anon_select ON search_event_sources FOR SELECT TO anon USING (true)';
    END IF;
END
$$;
"""
