from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from packages.x_processing.repository import SCHEMA_SQL, _import_psycopg, get_database_url

from .fetchers import NewsflashItem


class CompetitorMonitorRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_enabled_filter_keywords(self) -> list[str]: ...
    def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]: ...
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


class PostgresCompetitorMonitorRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def list_enabled_filter_keywords(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT term
                FROM competitor_filter_keywords
                WHERE enabled = true
                ORDER BY length(term) DESC, term ASC
                """
            ).fetchall()
        return [str(row["term"]) for row in rows if str(row["term"]).strip()]

    def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]:
        task_count = 0
        reference_count = 0
        with self._connect() as conn:
            for item in items:
                published_at = parse_datetime(item.published_at)
                if item.source == "odaily":
                    previous = conn.execute(
                        "SELECT 1 FROM odaily_reference_items WHERE source_item_id = %s",
                        (item.source_item_id,),
                    ).fetchone()
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
                            published_at,
                            self._Jsonb(item.raw_payload),
                            self._Jsonb(item.metadata),
                        ),
                    )
                    if previous is None:
                        reference_count += 1
                    continue
                row = conn.execute(
                    """
                    INSERT INTO tasks (
                        source, source_item_id, source_url, title, content, published_at, raw_payload, metadata, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (source, source_item_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        item.source,
                        item.source_item_id,
                        item.source_url,
                        item.title,
                        item.content,
                        published_at,
                        self._Jsonb(item.raw_payload),
                        self._Jsonb({**item.metadata, "source_kind": "competitor"}),
                    ),
                ).fetchone()
                if row:
                    task_count += 1
            conn.commit()
        return task_count, reference_count

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


def parse_datetime(value: str | None):
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
