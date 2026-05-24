from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from packages.common.pipeline_schema import PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.repository import _import_psycopg, get_database_url
from packages.x_processing.searcher import SearchDocument

from .models import (
    ALERT_PROMPT_KEY,
    ALERT_TASK_SOURCE,
    AlertStage,
    DOMAIN_WORKER_TASK_SOURCES,
    DomainRoute,
    ExternalMediaAlertPipelineRecord,
    MAINSTREAM_MEDIA_TASK_SOURCE,
    MediaNewsflashItem,
    STAGE_SPECS,
)


TASK_NOTIFY_CHANNEL = "external_media_alert_task_queue_changed"
PROMPT_NOTIFY_CHANNEL = "prompt_config_changed"


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_media_title_key(value: str | None) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    compact = "".join(ch for ch in text if ch.isalnum())
    return compact or " ".join(text.split())


def build_mainstream_media_task_source_item_id(*, source: str, source_url: str | None, title: str) -> str:
    normalized_url = normalize_compare_url(source_url)
    if normalized_url:
        return f"{source}:{normalized_url}"
    title_key = normalize_media_title_key(title)
    return f"{source}:title:{title_key or 'untitled'}"


def normalize_compare_url(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = text.split("#", 1)[0]
    text = text.split("?", 1)[0]
    return text.rstrip("/")


class ExternalMediaAlertRepository(Protocol):
    def init_schema(self) -> None: ...
    def claim_task(self, stage: AlertStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None: ...
    def get_pipeline(self, task_id: int) -> ExternalMediaAlertPipelineRecord: ...
    def save_media_newsflash_items(self, items: list[MediaNewsflashItem]) -> tuple[int, int]: ...
    def get_active_prompt(self, template_key: str = ALERT_PROMPT_KEY) -> PromptTemplateVersion: ...
    def complete_domain(
        self,
        task_id: int,
        *,
        route: DomainRoute,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
    ) -> None: ...
    def complete_domain_discard(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
        discard_reason: str = "non_crypto",
    ) -> None: ...
    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None: ...
    def complete_search_ready(self, task_id: int, *, result: dict[str, Any]) -> None: ...
    def complete_notify(self, task_id: int, *, telegram_result: dict[str, Any]) -> None: ...
    def fail_task(self, task_id: int, *, stage: AlertStage, error: str, status: str | None = None) -> None: ...
    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]: ...
    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]: ...
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


def _row_to_task(row: dict[str, Any]) -> TaskRecord:
    return TaskRecord(
        id=int(row["id"]),
        source=str(row["source"]),
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        published_at=row.get("published_at"),
        raw_payload=row.get("raw_payload") or {},
        metadata=row.get("metadata") or {},
        status=str(row["status"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_prompt(row: dict[str, Any]) -> PromptTemplateVersion:
    return PromptTemplateVersion(
        id=int(row["id"]),
        template_key=str(row["template_key"]),
        version_number=int(row["version_number"]),
        content=str(row["content"]),
        feature_mode_enabled=bool(row.get("feature_mode_enabled") or False),
        note=row.get("note"),
        created_at=row.get("created_at"),
        published_at=row.get("published_at"),
    )


def _row_to_pipeline(row: dict[str, Any]) -> ExternalMediaAlertPipelineRecord:
    domain_route = row.get("domain_route")
    if domain_route != "crypto":
        domain_route = None
    return ExternalMediaAlertPipelineRecord(
        task_id=int(row["task_id"]),
        domain_route=domain_route,
        discard_reason=row.get("discard_reason"),
        prompt_template_key=row.get("prompt_template_key"),
        prompt_version_id=row.get("prompt_version_id"),
        domain_model=row.get("domain_model"),
        domain_output=row.get("domain_output") or {},
        search_result=row.get("search_result") or {},
        telegram_result=row.get("telegram_result") or {},
        last_error=row.get("last_error"),
    )


class PostgresExternalMediaAlertRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row, autocommit=autocommit)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def claim_task(self, stage: AlertStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        spec = STAGE_SPECS[stage]
        sources = [ALERT_TASK_SOURCE] if stage == "notify" else list(DOMAIN_WORKER_TASK_SOURCES)
        with self._connect() as conn:
            row = conn.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM tasks
                    WHERE source = ANY(%(sources)s)
                      AND status IN (%(claim_status)s, %(processing_status)s)
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
                    "sources": sources,
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
                "INSERT INTO external_media_alert_pipeline (task_id) VALUES (%s) ON CONFLICT (task_id) DO NOTHING",
                (row["id"],),
            )
            conn.commit()
            return _row_to_task(row)

    def get_pipeline(self, task_id: int) -> ExternalMediaAlertPipelineRecord:
        with self._connect(autocommit=True) as conn:
            row = conn.execute(
                "SELECT * FROM external_media_alert_pipeline WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"external media alert pipeline row not found for task {task_id}")
        return _row_to_pipeline(row)

    def save_media_newsflash_items(self, items: list[MediaNewsflashItem]) -> tuple[int, int]:
        if not items:
            return 0, 0
        saved = 0
        duplicate = 0
        with self._connect() as conn:
            for item in items:
                title_key = normalize_media_title_key(item.title)
                if not title_key:
                    duplicate += 1
                    continue
                effective_published_at = item.published_at or utc_now()
                task_source_item_id = build_mainstream_media_task_source_item_id(
                    source=item.source,
                    source_url=item.source_url,
                    title=item.title,
                )
                row = conn.execute(
                    """
                    INSERT INTO media_newsflash (source, title, content, source_url, title_key, published_at)
                    SELECT %s, %s, %s, %s, %s, %s
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM media_newsflash
                        WHERE source = %s
                          AND (
                              title_key = %s
                              OR (%s IS NOT NULL AND source_url = %s)
                          )
                    )
                    RETURNING id
                    """,
                    (
                        item.source,
                        item.title,
                        item.content,
                        item.source_url,
                        title_key,
                        effective_published_at,
                        item.source,
                        title_key,
                        item.source_url,
                        item.source_url,
                    ),
                ).fetchone()
                if row is None:
                    duplicate += 1
                    continue
                conn.execute(
                    """
                    INSERT INTO tasks (
                        source, source_item_id, source_url, title, content, published_at, raw_payload, metadata, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (source, source_item_id) DO NOTHING
                    """,
                    (
                        MAINSTREAM_MEDIA_TASK_SOURCE,
                        task_source_item_id,
                        item.source_url,
                        item.title,
                        item.content,
                        effective_published_at,
                        self._Jsonb(item.raw_payload),
                        self._Jsonb(
                            {
                                **item.metadata,
                                "source_kind": MAINSTREAM_MEDIA_TASK_SOURCE,
                                "origin_source": item.source,
                                "media_source_item_id": task_source_item_id,
                                "original_title": item.title,
                                "excerpt": item.content,
                            }
                        ),
                    ),
                )
                saved += 1
            conn.commit()
        return saved, duplicate

    def get_active_prompt(self, template_key: str = ALERT_PROMPT_KEY) -> PromptTemplateVersion:
        with self._connect(autocommit=True) as conn:
            row = conn.execute(
                """
                SELECT v.*, t.feature_mode_enabled
                FROM prompt_templates t
                JOIN prompt_template_versions v ON v.id = t.active_version_id
                WHERE t.template_key = %s
                  AND v.deleted_at IS NULL
                """,
                (template_key,),
            ).fetchone()
        if row is None:
            raise ValueError(f"active prompt not found: {template_key}")
        return _row_to_prompt(row)

    def complete_domain(
        self,
        task_id: int,
        *,
        route: DomainRoute,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET domain_route = %s,
                    discard_reason = NULL,
                    prompt_template_key = %s,
                    prompt_version_id = %s,
                    domain_model = %s,
                    domain_output = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    route,
                    prompt.template_key if prompt else None,
                    prompt.id if prompt else None,
                    model,
                    self._Jsonb({"route": route, "raw_output": raw_output}),
                    task_id,
                ),
            )
            self._set_task_status(conn, task_id, "classified")
            conn.commit()

    def complete_domain_discard(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
        discard_reason: str = "non_crypto",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET domain_route = NULL,
                    discard_reason = %s,
                    prompt_template_key = %s,
                    prompt_version_id = %s,
                    domain_model = %s,
                    domain_output = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (
                    discard_reason,
                    prompt.template_key if prompt else None,
                    prompt.id if prompt else None,
                    model,
                    self._Jsonb({"route": "discard", "discard_reason": discard_reason, "raw_output": raw_output}),
                    task_id,
                ),
            )
            self._set_task_status(conn, task_id, "discarded")
            conn.commit()

    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET search_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (self._Jsonb(result), task_id),
            )
            self._set_task_status(conn, task_id, "duplicate")
            conn.commit()

    def complete_search_ready(self, task_id: int, *, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET search_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (self._Jsonb(result), task_id),
            )
            self._set_task_status(conn, task_id, "deduped")
            conn.commit()

    def complete_notify(self, task_id: int, *, telegram_result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET telegram_result = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (self._Jsonb(telegram_result), task_id),
            )
            self._set_task_status(conn, task_id, "notified")
            conn.commit()

    def fail_task(self, task_id: int, *, stage: AlertStage, error: str, status: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE external_media_alert_pipeline
                SET last_error = %s,
                    updated_at = now()
                WHERE task_id = %s
                """,
                (error[:2000], task_id),
            )
            self._set_task_status(conn, task_id, status or STAGE_SPECS[stage].failure_status)
            conn.commit()

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                """
                SELECT source_item_id, source_url, title, content, published_at, metadata
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
                source="odaily",
                source_url=row.get("source_url"),
                published_at=row.get("published_at"),
                metadata=row.get("metadata") or {},
            )
            for row in rows
        ]

    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]:
        where = """
            (
                (t.source = %s AND t.status = 'notified')
                OR (t.source = %s AND t.status IN ('ready_review', 'notified'))
            )
        """
        params: list[Any] = [ALERT_TASK_SOURCE, MAINSTREAM_MEDIA_TASK_SOURCE]
        if since is not None:
            where += " AND t.created_at >= %s"
            params.append(since)
        with self._connect(autocommit=True) as conn:
            rows = conn.execute(
                f"""
                SELECT t.id, t.source_item_id, t.source_url, t.title, t.content, t.published_at, t.metadata
                FROM tasks t
                WHERE {where}
                ORDER BY t.created_at DESC, t.id DESC
                """,
                params,
            ).fetchall()
        documents: list[SearchDocument] = []
        for row in rows:
            metadata = row.get("metadata") or {}
            documents.append(
                SearchDocument(
                    doc_type="external_media_alert_history",
                    doc_id=str(row["source_item_id"]),
                    title=row.get("title"),
                    content=str(row["content"]),
                    source=str(metadata.get("source_kind") or row.get("source") or ALERT_TASK_SOURCE),
                    source_url=row.get("source_url"),
                    task_id=int(row["id"]),
                    published_at=row.get("published_at"),
                    metadata=metadata,
                )
            )
        return documents

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


class InMemoryExternalMediaAlertRepository:
    def __init__(self) -> None:
        self.tasks: dict[int, TaskRecord] = {}
        self.pipelines: dict[int, ExternalMediaAlertPipelineRecord] = {}
        self.media_newsflashes: list[dict[str, Any]] = []
        self.odaily_references: list[SearchDocument] = []
        self.prompts: dict[str, PromptTemplateVersion] = {
            ALERT_PROMPT_KEY: PromptTemplateVersion(
                id=1,
                template_key=ALERT_PROMPT_KEY,
                version_number=1,
                content=f"prompt {ALERT_PROMPT_KEY}",
            )
        }
        self.heartbeats: list[dict[str, Any]] = []
        self._locks: set[int] = set()
        self._next_task_id = 1

    def init_schema(self) -> None:
        return None

    def add_task(self, task: TaskRecord) -> None:
        self.tasks[task.id] = task
        self._next_task_id = max(self._next_task_id, task.id + 1)

    def save_media_newsflash_items(self, items: list[MediaNewsflashItem]) -> tuple[int, int]:
        saved = 0
        duplicate = 0
        for item in items:
            title_key = normalize_media_title_key(item.title)
            if not title_key:
                duplicate += 1
                continue
            effective_published_at = item.published_at or utc_now()
            task_source_item_id = build_mainstream_media_task_source_item_id(
                source=item.source,
                source_url=item.source_url,
                title=item.title,
            )
            exists = any(
                row["source"] == item.source
                and (
                    row["title_key"] == title_key
                    or (item.source_url and row.get("source_url") == item.source_url)
                )
                for row in self.media_newsflashes
            )
            if exists:
                duplicate += 1
                continue
            if not any(
                task.source == MAINSTREAM_MEDIA_TASK_SOURCE and task.source_item_id == task_source_item_id
                for task in self.tasks.values()
            ):
                now = utc_now()
                self.tasks[self._next_task_id] = TaskRecord(
                    id=self._next_task_id,
                    source=MAINSTREAM_MEDIA_TASK_SOURCE,
                    source_item_id=task_source_item_id,
                    source_url=item.source_url,
                    title=item.title,
                    content=item.content,
                    published_at=effective_published_at,
                    raw_payload=item.raw_payload,
                    metadata={
                        **item.metadata,
                        "source_kind": MAINSTREAM_MEDIA_TASK_SOURCE,
                        "origin_source": item.source,
                        "media_source_item_id": task_source_item_id,
                        "original_title": item.title,
                        "excerpt": item.content,
                    },
                    status="pending",
                    created_at=now,
                    updated_at=now,
                )
                self._next_task_id += 1
            self.media_newsflashes.append(
                {
                    "source": item.source,
                    "title": item.title,
                    "content": item.content,
                    "source_url": item.source_url,
                    "title_key": title_key,
                    "published_at": effective_published_at,
                }
            )
            saved += 1
        return saved, duplicate

    def claim_task(self, stage: AlertStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        del worker_id, lock_seconds
        spec = STAGE_SPECS[stage]
        claim_sources = {ALERT_TASK_SOURCE} if stage == "notify" else set(DOMAIN_WORKER_TASK_SOURCES)
        for task in sorted(self.tasks.values(), key=lambda item: item.id):
            if task.source not in claim_sources:
                continue
            if task.status not in {spec.claim_status, spec.processing_status} or task.id in self._locks:
                continue
            self._locks.add(task.id)
            updated = TaskRecord(**{**asdict(task), "status": spec.processing_status, "updated_at": utc_now()})
            self.tasks[task.id] = updated
            self.pipelines.setdefault(task.id, ExternalMediaAlertPipelineRecord(task_id=task.id))
            return updated
        return None

    def get_pipeline(self, task_id: int) -> ExternalMediaAlertPipelineRecord:
        return self.pipelines[task_id]

    def get_active_prompt(self, template_key: str = ALERT_PROMPT_KEY) -> PromptTemplateVersion:
        return self.prompts[template_key]

    def complete_domain(
        self,
        task_id: int,
        *,
        route: DomainRoute,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
    ) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(
            **{
                **asdict(current),
                "domain_route": route,
                "discard_reason": None,
                "prompt_template_key": prompt.template_key if prompt else None,
                "prompt_version_id": prompt.id if prompt else None,
                "domain_model": model,
                "domain_output": {"route": route, "raw_output": raw_output},
                "last_error": None,
            }
        )
        self._set_status(task_id, "classified")

    def complete_domain_discard(
        self,
        task_id: int,
        *,
        prompt: PromptTemplateVersion | None,
        model: str,
        raw_output: str,
        discard_reason: str = "non_crypto",
    ) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(
            **{
                **asdict(current),
                "domain_route": None,
                "discard_reason": discard_reason,
                "prompt_template_key": prompt.template_key if prompt else None,
                "prompt_version_id": prompt.id if prompt else None,
                "domain_model": model,
                "domain_output": {"route": "discard", "discard_reason": discard_reason, "raw_output": raw_output},
                "last_error": None,
            }
        )
        self._set_status(task_id, "discarded")

    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(
            **{**asdict(current), "search_result": result, "last_error": None}
        )
        self._set_status(task_id, "duplicate")

    def complete_search_ready(self, task_id: int, *, result: dict[str, Any]) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(
            **{**asdict(current), "search_result": result, "last_error": None}
        )
        self._set_status(task_id, "deduped")

    def complete_notify(self, task_id: int, *, telegram_result: dict[str, Any]) -> None:
        current = self.pipelines[task_id]
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(
            **{**asdict(current), "telegram_result": telegram_result, "last_error": None}
        )
        self._set_status(task_id, "notified")

    def fail_task(self, task_id: int, *, stage: AlertStage, error: str, status: str | None = None) -> None:
        current = self.pipelines.get(task_id, ExternalMediaAlertPipelineRecord(task_id=task_id))
        self.pipelines[task_id] = ExternalMediaAlertPipelineRecord(**{**asdict(current), "last_error": error})
        self._set_status(task_id, status or STAGE_SPECS[stage].failure_status)

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        return [
            item
            for item in self.odaily_references
            if item.published_at is None or item.published_at >= since
        ]

    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]:
        results: list[SearchDocument] = []
        for task in self.tasks.values():
            is_alert_history = task.source == ALERT_TASK_SOURCE and task.status == "notified"
            is_mainstream_history = task.source == MAINSTREAM_MEDIA_TASK_SOURCE and task.status in {"ready_review", "notified"}
            if not is_alert_history and not is_mainstream_history:
                continue
            if since is not None and task.created_at is not None and task.created_at < since:
                continue
            results.append(
                SearchDocument(
                    doc_type="external_media_alert_history",
                    doc_id=task.source_item_id,
                    title=task.title,
                    content=task.content,
                    source=str(task.metadata.get("source_kind") or task.source),
                    source_url=task.source_url,
                    task_id=task.id,
                    published_at=task.published_at,
                    metadata=task.metadata,
                )
            )
        return results

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
        self.heartbeats.append(
            {
                "component": component,
                "worker_id": worker_id,
                "status": status,
                "success": success,
                "error": error,
                "metadata": metadata or {},
            }
        )

    def _set_status(self, task_id: int, status: str) -> None:
        task = self.tasks[task_id]
        self.tasks[task_id] = TaskRecord(**{**asdict(task), "status": status, "updated_at": utc_now()})
        self._locks.discard(task_id)


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS tasks (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    published_at timestamptz,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_item_id)
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_until timestamptz;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_tasks_external_media_alert_status_lock
ON tasks(source, status, locked_until, created_at ASC);

CREATE TABLE IF NOT EXISTS external_media_alert_pipeline (
    task_id bigint PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    domain_route text CHECK (domain_route IS NULL OR domain_route IN ('crypto')),
    discard_reason text CHECK (discard_reason IS NULL OR discard_reason IN ('non_crypto', 'market_analysis')),
    prompt_template_key text REFERENCES prompt_templates(template_key),
    prompt_version_id bigint REFERENCES prompt_template_versions(id),
    domain_model text,
    domain_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE external_media_alert_pipeline
    DROP CONSTRAINT IF EXISTS external_media_alert_pipeline_domain_route_check;
ALTER TABLE external_media_alert_pipeline
    DROP CONSTRAINT IF EXISTS external_media_alert_pipeline_discard_reason_check;
ALTER TABLE external_media_alert_pipeline
    ADD CONSTRAINT external_media_alert_pipeline_domain_route_check
    CHECK (domain_route IS NULL OR domain_route IN ('crypto'));
ALTER TABLE external_media_alert_pipeline
    ADD CONSTRAINT external_media_alert_pipeline_discard_reason_check
    CHECK (discard_reason IS NULL OR discard_reason IN ('non_crypto', 'market_analysis'));

CREATE INDEX IF NOT EXISTS idx_external_media_alert_pipeline_route
ON external_media_alert_pipeline(domain_route);

CREATE OR REPLACE FUNCTION notify_external_media_alert_task_queue_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'external_media_alert_task_queue_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'status', NEW.status)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_external_media_alert_queue_notify ON tasks;
CREATE TRIGGER trg_tasks_external_media_alert_queue_notify
AFTER INSERT OR UPDATE OF status ON tasks
FOR EACH ROW
WHEN (NEW.source IN ('external_media_alert', 'mainstream_media'))
EXECUTE FUNCTION notify_external_media_alert_task_queue_changed();

CREATE TABLE IF NOT EXISTS media_newsflash (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    title text NOT NULL,
    content text NOT NULL,
    source_url text,
    title_key text,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE media_newsflash ADD COLUMN IF NOT EXISTS source_url text;
ALTER TABLE media_newsflash ADD COLUMN IF NOT EXISTS title_key text;
ALTER TABLE media_newsflash ADD COLUMN IF NOT EXISTS published_at timestamptz;
ALTER TABLE media_newsflash ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE media_newsflash ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_media_newsflash_source_title_key
ON media_newsflash(source, title_key);

CREATE INDEX IF NOT EXISTS idx_media_newsflash_source_url
ON media_newsflash(source, source_url);
"""
