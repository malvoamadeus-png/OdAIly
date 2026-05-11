from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from packages.x_processing.repository import SCHEMA_SQL, _import_psycopg, get_database_url
from packages.x_processing.searcher import content_hash, normalize_for_embedding

from .events import EventAssignment, EventSourceRecord, NewsflashItemRecord, generate_event_id
from .fetchers import NewsflashItem


class CompetitorMonitorRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_enabled_filter_keywords(self) -> list[str]: ...
    def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]: ...
    def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]: ...
    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]: ...
    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]: ...
    def create_event_for_item(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str: ...
    def assign_item_to_event(self, assignment: EventAssignment) -> None: ...
    def update_event_summaries(self, event_ids: set[str]) -> None: ...
    def prune_excluded_event_sources(self, terms: list[str] | None = None) -> dict[str, int]: ...
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

    def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]:
        if not items:
            return []
        records: list[NewsflashItemRecord] = []
        with self._connect() as conn:
            for item in items:
                published_at = parse_datetime(item.published_at)
                digest = content_hash(normalize_for_embedding(title=item.title, content=item.content))
                row = conn.execute(
                    """
                    INSERT INTO newsflash_items (
                        source, source_item_id, source_url, title, content, content_hash,
                        published_at, raw_payload, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (source, source_item_id) DO UPDATE SET
                        source_url = EXCLUDED.source_url,
                        title = EXCLUDED.title,
                        content = EXCLUDED.content,
                        content_hash = EXCLUDED.content_hash,
                        published_at = EXCLUDED.published_at,
                        raw_payload = EXCLUDED.raw_payload,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    RETURNING id, source, source_item_id, source_url, title, content, published_at, first_seen_at, metadata
                    """,
                    (
                        item.source,
                        item.source_item_id,
                        item.source_url,
                        item.title,
                        item.content,
                        digest,
                        published_at,
                        self._Jsonb(item.raw_payload),
                        self._Jsonb(item.metadata),
                    ),
                ).fetchone()
                records.append(_row_to_newsflash_item(row))
            conn.commit()
        return records

    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]:
        if not item_ids:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    s.event_id,
                    i.id,
                    i.source,
                    i.source_item_id,
                    i.source_url,
                    i.title,
                    i.content,
                    i.published_at,
                    i.first_seen_at,
                    i.metadata
                FROM newsflash_event_sources s
                JOIN newsflash_items i ON i.id = s.item_id
                JOIN newsflash_events e ON e.event_id = s.event_id
                WHERE e.status = 'active'
                  AND i.id = ANY(%s)
                ORDER BY i.id ASC
                """,
                (list(item_ids),),
            ).fetchall()
        return [EventSourceRecord(event_id=str(row["event_id"]), item=_row_to_newsflash_item(row)) for row in rows]

    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]:
        params: dict[str, Any] = {"since": since}
        exclude_sql = ""
        if exclude_item_ids:
            exclude_sql = "AND i.id <> ALL(%(exclude_item_ids)s)"
            params["exclude_item_ids"] = list(exclude_item_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    s.event_id,
                    i.id,
                    i.source,
                    i.source_item_id,
                    i.source_url,
                    i.title,
                    i.content,
                    i.published_at,
                    i.first_seen_at,
                    i.metadata
                FROM newsflash_event_sources s
                JOIN newsflash_items i ON i.id = s.item_id
                JOIN newsflash_events e ON e.event_id = s.event_id
                WHERE e.status = 'active'
                  AND COALESCE(i.published_at, i.first_seen_at) >= %(since)s
                  {exclude_sql}
                ORDER BY COALESCE(i.published_at, i.first_seen_at) DESC, i.id DESC
                """,
                params,
            ).fetchall()
        return [EventSourceRecord(event_id=str(row["event_id"]), item=_row_to_newsflash_item(row)) for row in rows]

    def create_event_for_item(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str:
        event_id = generate_event_id()
        event_time = item.published_at or item.first_seen_at
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO newsflash_events (
                    event_id, representative_item_id, representative_title, event_time,
                    first_source, first_published_at, needs_review, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_id) DO NOTHING
                """,
                (
                    event_id,
                    item.id,
                    item.title,
                    event_time,
                    item.source,
                    event_time,
                    needs_review,
                    self._Jsonb({"created_from": {"source": item.source, "source_item_id": item.source_item_id}}),
                ),
            )
            conn.commit()
        return event_id

    def assign_item_to_event(self, assignment: EventAssignment) -> None:
        with self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO newsflash_event_sources (
                        event_id, item_id, source, source_item_id, role, match_method,
                        similarity, matched_item_id, ai_result, metadata
                    )
                    SELECT
                        %(event_id)s, i.id, i.source, i.source_item_id, %(role)s, %(match_method)s,
                        %(similarity)s, %(matched_item_id)s, %(ai_result)s, %(metadata)s
                    FROM newsflash_items i
                    WHERE i.id = %(item_id)s
                    ON CONFLICT (item_id) DO UPDATE SET
                        event_id = EXCLUDED.event_id,
                        role = EXCLUDED.role,
                        match_method = EXCLUDED.match_method,
                        similarity = EXCLUDED.similarity,
                        matched_item_id = EXCLUDED.matched_item_id,
                        ai_result = EXCLUDED.ai_result,
                        metadata = EXCLUDED.metadata,
                        updated_at = now()
                    """,
                    {
                        "event_id": assignment.event_id,
                        "item_id": assignment.item_id,
                        "role": assignment.role,
                        "match_method": assignment.match_method,
                        "similarity": assignment.similarity,
                        "matched_item_id": assignment.matched_item_id,
                        "ai_result": self._Jsonb(assignment.ai_result),
                        "metadata": self._Jsonb({"needs_review": assignment.needs_review}),
                    },
                )
                if assignment.needs_review:
                    conn.execute(
                        "UPDATE newsflash_events SET needs_review = true, updated_at = now() WHERE event_id = %s",
                        (assignment.event_id,),
                    )
            conn.commit()

    def update_event_summaries(self, event_ids: set[str]) -> None:
        if not event_ids:
            return
        with self._connect() as conn:
            self._update_event_summaries(conn, event_ids)
            conn.commit()

    def prune_excluded_event_sources(self, terms: list[str] | None = None) -> dict[str, int]:
        exclude_terms = terms if terms is not None else self.list_enabled_filter_keywords()
        normalized_terms = [normalized for term in exclude_terms if (normalized := normalize_exclude_term(term))]
        if not normalized_terms:
            return {"matched_items": 0, "removed_sources": 0, "deleted_events": 0, "updated_events": 0}

        with self._connect() as conn:
            with conn.transaction():
                matched_rows = conn.execute(
                    """
                    SELECT id
                    FROM newsflash_items
                    WHERE EXISTS (
                        SELECT 1
                        FROM unnest(%s::text[]) AS term
                        WHERE strpos(
                            lower(regexp_replace(coalesce(title, '') || E'\n' || coalesce(content, ''), '\\s+', ' ', 'g')),
                            term
                        ) > 0
                    )
                    """,
                    (normalized_terms,),
                ).fetchall()
                matched_item_ids = [int(row["id"]) for row in matched_rows]
                if not matched_item_ids:
                    return {"matched_items": 0, "removed_sources": 0, "deleted_events": 0, "updated_events": 0}

                affected_rows = conn.execute(
                    """
                    SELECT DISTINCT event_id
                    FROM newsflash_event_sources
                    WHERE item_id = ANY(%s)
                    """,
                    (matched_item_ids,),
                ).fetchall()
                affected_event_ids = {str(row["event_id"]) for row in affected_rows}
                if not affected_event_ids:
                    return {"matched_items": len(matched_item_ids), "removed_sources": 0, "deleted_events": 0, "updated_events": 0}

                removed_rows = conn.execute(
                    """
                    DELETE FROM newsflash_event_sources
                    WHERE item_id = ANY(%s)
                    RETURNING event_id
                    """,
                    (matched_item_ids,),
                ).fetchall()
                removed_sources = len(removed_rows)

                deleted_rows = conn.execute(
                    """
                    DELETE FROM newsflash_events e
                    WHERE e.event_id = ANY(%s)
                      AND NOT EXISTS (
                          SELECT 1
                          FROM newsflash_event_sources s
                          WHERE s.event_id = e.event_id
                      )
                    RETURNING event_id
                    """,
                    (list(affected_event_ids),),
                ).fetchall()
                deleted_event_ids = {str(row["event_id"]) for row in deleted_rows}
                remaining_event_ids = affected_event_ids - deleted_event_ids
                if remaining_event_ids:
                    self._update_event_summaries(conn, remaining_event_ids)
            conn.commit()

        return {
            "matched_items": len(matched_item_ids),
            "removed_sources": removed_sources,
            "deleted_events": len(deleted_event_ids),
            "updated_events": len(remaining_event_ids),
        }

    def _update_event_summaries(self, conn, event_ids: set[str]) -> None:
        for event_id in event_ids:
            conn.execute(
                """
                WITH source_rows AS (
                    SELECT
                        s.event_id,
                        i.id,
                        i.source,
                        i.title,
                        COALESCE(i.published_at, i.first_seen_at) AS source_time
                    FROM newsflash_event_sources s
                    JOIN newsflash_items i ON i.id = s.item_id
                    WHERE s.event_id = %s
                ),
                aggregate_rows AS (
                    SELECT
                        event_id,
                        count(DISTINCT source) AS source_count,
                        count(DISTINCT source) FILTER (WHERE source <> 'odaily') AS competitor_source_count,
                        bool_or(source = 'odaily') AS has_odaily,
                        min(source_time) AS event_time
                    FROM source_rows
                    GROUP BY event_id
                ),
                first_row AS (
                    SELECT DISTINCT ON (event_id)
                        event_id, id, source, title, source_time
                    FROM source_rows
                    ORDER BY event_id, source_time ASC NULLS LAST, id ASC
                )
                UPDATE newsflash_events e
                SET representative_item_id = first_row.id,
                    representative_title = first_row.title,
                    event_time = aggregate_rows.event_time,
                    first_source = first_row.source,
                    first_published_at = first_row.source_time,
                    source_count = aggregate_rows.source_count,
                    competitor_source_count = aggregate_rows.competitor_source_count,
                    has_odaily = aggregate_rows.has_odaily,
                    updated_at = now()
                FROM aggregate_rows
                JOIN first_row ON first_row.event_id = aggregate_rows.event_id
                WHERE e.event_id = aggregate_rows.event_id
                """,
                (event_id,),
            )

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


def normalize_exclude_term(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def _row_to_newsflash_item(row: dict[str, Any]) -> NewsflashItemRecord:
    return NewsflashItemRecord(
        id=int(row["id"]),
        source=str(row["source"]),
        source_item_id=str(row["source_item_id"]),
        source_url=row.get("source_url"),
        title=row.get("title"),
        content=str(row["content"]),
        published_at=row.get("published_at"),
        first_seen_at=row.get("first_seen_at"),
        metadata=row.get("metadata") or {},
    )
