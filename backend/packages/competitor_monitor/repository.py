from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from packages.common.time_utils import SHANGHAI_TZ
from packages.x_processing.repository import SCHEMA_SQL, _import_psycopg, get_database_url
from packages.x_processing.searcher import content_hash, normalize_for_embedding

from .events import EventAssignment, EventSourceRecord, NewsflashItemRecord, generate_event_id
from .fetchers import NewsflashItem


class CompetitorMonitorRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_enabled_filter_keywords(self) -> list[str]: ...
    def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]: ...
    def save_items_for_pipeline(self, items: list[NewsflashItem]) -> tuple[list[tuple[NewsflashItem, int]], int]: ...
    def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]: ...
    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]: ...
    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]: ...
    def create_event_with_source(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str: ...
    def assign_item_to_event(self, assignment: EventAssignment) -> None: ...
    def update_event_summaries(self, event_ids: set[str]) -> None: ...
    def prune_excluded_event_sources(self, terms: list[str] | None = None) -> dict[str, int]: ...
    def prune_orphan_events(self) -> int: ...
    def repair_newsflash_timestamps(self) -> dict[str, int]: ...
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
        task_records, reference_count = self.save_items_for_pipeline(items)
        return len(task_records), reference_count

    def save_items_for_pipeline(self, items: list[NewsflashItem]) -> tuple[list[tuple[NewsflashItem, int]], int]:
        task_records: list[tuple[NewsflashItem, int]] = []
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
                    ON CONFLICT (source, source_item_id) DO UPDATE SET
                        source_url = CASE
                            WHEN EXCLUDED.source = 'jinse' THEN EXCLUDED.source_url
                            ELSE COALESCE(EXCLUDED.source_url, tasks.source_url)
                        END,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = tasks.updated_at
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
                    task_records.append((item, int(row["id"])))
            conn.commit()
        return task_records, reference_count

    def _legacy_save_items(self, items: list[NewsflashItem]) -> tuple[int, int]:
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
                    ON CONFLICT (source, source_item_id) DO UPDATE SET
                        source_url = CASE
                            WHEN EXCLUDED.source = 'jinse' THEN EXCLUDED.source_url
                            ELSE COALESCE(EXCLUDED.source_url, tasks.source_url)
                        END,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = now()
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

    def create_event_with_source(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str:
        event_id = generate_event_id()
        event_time = item.published_at or item.first_seen_at
        with self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO newsflash_events (
                        event_id, representative_item_id, representative_title, event_time,
                        first_source, first_published_at, source_count, competitor_source_count,
                        has_odaily, needs_review, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s)
                    """,
                    (
                        event_id,
                        item.id,
                        item.title,
                        event_time,
                        item.source,
                        event_time,
                        0 if item.source == "odaily" else 1,
                        item.source == "odaily",
                        needs_review,
                        self._Jsonb({"created_from": {"source": item.source, "source_item_id": item.source_item_id}}),
                    ),
                )
                inserted = conn.execute(
                    """
                    INSERT INTO newsflash_event_sources (
                        event_id, item_id, source, source_item_id, role, match_method,
                        similarity, matched_item_id, ai_result, metadata
                    )
                    VALUES (%s, %s, %s, %s, 'primary', 'new_event', NULL, NULL, '{}'::jsonb, %s)
                    ON CONFLICT (item_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event_id,
                        item.id,
                        item.source,
                        item.source_item_id,
                        self._Jsonb({"needs_review": needs_review}),
                    ),
                ).fetchone()
                if inserted is None:
                    existing = conn.execute(
                        "SELECT event_id FROM newsflash_event_sources WHERE item_id = %s",
                        (item.id,),
                    ).fetchone()
                    conn.execute("DELETE FROM newsflash_events WHERE event_id = %s", (event_id,))
                    if existing is not None:
                        event_id = str(existing["event_id"])
            conn.commit()
        return event_id

    def assign_item_to_event(self, assignment: EventAssignment) -> None:
        with self._connect() as conn:
            with conn.transaction():
                previous = conn.execute(
                    "SELECT event_id FROM newsflash_event_sources WHERE item_id = %s FOR UPDATE",
                    (assignment.item_id,),
                ).fetchone()
                previous_event_id = str(previous["event_id"]) if previous else None
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
                if previous_event_id and previous_event_id != assignment.event_id:
                    deleted = conn.execute(
                        """
                        DELETE FROM newsflash_events e
                        WHERE e.event_id = %s
                          AND NOT EXISTS (
                              SELECT 1
                              FROM newsflash_event_sources s
                              WHERE s.event_id = e.event_id
                          )
                        RETURNING event_id
                        """,
                        (previous_event_id,),
                    ).fetchone()
                    if deleted is None:
                        self._update_event_summaries(conn, {previous_event_id})
            conn.commit()

    def update_event_summaries(self, event_ids: set[str]) -> None:
        if not event_ids:
            return
        with self._connect() as conn:
            self._update_event_summaries(conn, event_ids)
            conn.commit()

    def prune_orphan_events(self) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """
                DELETE FROM newsflash_events e
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM newsflash_event_sources s
                    WHERE s.event_id = e.event_id
                )
                RETURNING event_id
                """
            ).fetchall()
            conn.commit()
        return len(rows)

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

    def repair_newsflash_timestamps(self) -> dict[str, int]:
        updated_item_ids: list[int] = []
        with self._connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """
                    SELECT id, source, published_at, raw_payload
                    FROM newsflash_items
                    WHERE raw_payload IS NOT NULL
                    ORDER BY id ASC
                    """
                ).fetchall()
                for row in rows:
                    raw_value = extract_raw_published_at(str(row["source"]), row.get("raw_payload") or {})
                    fixed = parse_datetime(raw_value)
                    if fixed is None:
                        continue
                    current = row.get("published_at")
                    if isinstance(current, datetime) and current == fixed:
                        continue
                    conn.execute(
                        "UPDATE newsflash_items SET published_at = %s, updated_at = now() WHERE id = %s",
                        (fixed, row["id"]),
                    )
                    updated_item_ids.append(int(row["id"]))

                affected_event_ids: set[str] = set()
                if updated_item_ids:
                    affected_rows = conn.execute(
                        """
                        SELECT DISTINCT event_id
                        FROM newsflash_event_sources
                        WHERE item_id = ANY(%s)
                        """,
                        (updated_item_ids,),
                    ).fetchall()
                    affected_event_ids = {str(row["event_id"]) for row in affected_rows}
                    if affected_event_ids:
                        self._update_event_summaries(conn, affected_event_ids)
            conn.commit()
        return {"updated_items": len(updated_item_ids), "updated_events": len(affected_event_ids)}

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


RAW_PUBLISHED_AT_FIELDS: dict[str, tuple[str, ...]] = {
    "blockbeats": ("create_time", "created_at", "publish_time", "published_at"),
    "panews": ("publishedAt", "createdAt"),
    "jinse": ("created_at", "published_at"),
    "odaily": ("publishDate", "publishedAt", "createdAt", "createTime"),
}


def extract_raw_published_at(source: str, payload: dict[str, Any]) -> Any:
    for field in RAW_PUBLISHED_AT_FIELDS.get(source, ()):
        value = payload.get(field)
        if value not in (None, ""):
            return value
    return None


def parse_datetime(value: Any):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=SHANGHAI_TZ)
    if isinstance(value, (int, float)) and value > 1000000000:
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        numeric = float(text)
        if numeric > 1000000000:
            return datetime.fromtimestamp(numeric, tz=UTC)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=SHANGHAI_TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=SHANGHAI_TZ)
        except ValueError:
            pass
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
