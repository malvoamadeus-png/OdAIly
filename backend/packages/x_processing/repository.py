from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from dotenv import load_dotenv

from .models import (
    NEWS_TYPES,
    PROMPT_KEY_BY_NEWS_TYPE,
    STAGE_SPECS,
    NewsType,
    PipelineRecord,
    ProcessingStage,
    PromptTemplateVersion,
    TaskRecord,
)


TASK_NOTIFY_CHANNEL = "x_task_queue_changed"
PROMPT_NOTIFY_CHANNEL = "prompt_config_changed"


PROMPT_SEEDS: dict[str, tuple[str, str, str]] = {
    "x_regular_writer": ("X 常规快讯", "docs/常规快讯模板.txt", "initial regular writer template"),
    "x_onchain_writer": ("X 链上快讯", "docs/链上快讯模板.txt", "initial onchain writer template"),
    "x_funding_writer": ("X 融资快讯", "docs/融资快讯模板.txt", "initial funding writer template"),
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
    def complete_search(self, task_id: int) -> None: ...
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

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

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
        with self._connect() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM tasks
                    WHERE source = 'x'
                      AND status = %(claim_status)s
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
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM x_task_pipeline WHERE task_id = %s", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"pipeline row not found for task {task_id}")
        return _row_to_pipeline(row)

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        with self._connect() as conn:
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
                (news_type, model, self._Jsonb({"raw_output": raw_output}), task_id),
            )
            self._set_task_status(conn, task_id, "judged")
            conn.commit()

    def complete_search(self, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_task_pipeline
                SET search_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (self._Jsonb({"skipped": True, "reason": "searcher is no-op in v1"}), task_id),
            )
            self._set_task_status(conn, task_id, "deduped")
            conn.commit()

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
            if task.status != spec.claim_status or task.id in self._locks:
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
        self._set_status(task_id, "judged")

    def complete_search(self, task_id: int) -> None:
        self._set_status(task_id, "deduped")

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

    def _set_status(self, task_id: int, status: str) -> None:
        task = self.tasks[task_id]
        self.tasks[task_id] = TaskRecord(**{**asdict(task), "status": status, "updated_at": utc_now()})
        self._locks.discard(task_id)


SCHEMA_SQL = """
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_until timestamptz;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_tasks_x_status_lock
ON tasks(source, status, locked_until, created_at ASC);

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
    news_type text CHECK (news_type IS NULL OR news_type IN ('regular', 'onchain', 'funding')),
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

CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_news_type ON x_task_pipeline(news_type);
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
WHEN (NEW.source = 'x')
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

DROP POLICY IF EXISTS prompt_templates_anon_all ON prompt_templates;
DROP POLICY IF EXISTS prompt_template_versions_anon_all ON prompt_template_versions;
DROP POLICY IF EXISTS x_task_pipeline_anon_select ON x_task_pipeline;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        GRANT USAGE ON SCHEMA public TO anon;
        GRANT SELECT, INSERT, UPDATE ON prompt_templates TO anon;
        GRANT SELECT, INSERT, UPDATE ON prompt_template_versions TO anon;
        GRANT SELECT ON x_task_pipeline TO anon;
        GRANT USAGE, SELECT ON SEQUENCE prompt_template_versions_id_seq TO anon;

        EXECUTE 'CREATE POLICY prompt_templates_anon_all ON prompt_templates FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY prompt_template_versions_anon_all ON prompt_template_versions FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY x_task_pipeline_anon_select ON x_task_pipeline FOR SELECT TO anon USING (true)';
    END IF;
END
$$;
"""
