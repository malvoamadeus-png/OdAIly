from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .events import EventAssignment, EventSourceRecord, NewsflashItemRecord


class CompetitorEventStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_or_rebuild()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_or_rebuild(self) -> None:
        try:
            self._init_schema()
        except sqlite3.DatabaseError:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS newsflash_items (
                    id integer PRIMARY KEY,
                    source text NOT NULL,
                    source_item_id text NOT NULL,
                    source_url text,
                    title text,
                    content text NOT NULL,
                    published_at text,
                    first_seen_at text,
                    metadata_json text NOT NULL,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS event_sources (
                    item_id integer PRIMARY KEY,
                    event_id text NOT NULL,
                    role text,
                    match_method text,
                    similarity real,
                    matched_item_id integer,
                    ai_result_json text NOT NULL,
                    needs_review integer NOT NULL DEFAULT 0,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    state_key text PRIMARY KEY,
                    state_value text,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_event_sources_event_id ON event_sources(event_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_newsflash_items_source_time ON newsflash_items(published_at, first_seen_at)")
            conn.commit()

    def upsert_items(self, records: list[NewsflashItemRecord]) -> None:
        if not records:
            return
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO newsflash_items (
                    id, source, source_item_id, source_url, title, content,
                    published_at, first_seen_at, metadata_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source = excluded.source,
                    source_item_id = excluded.source_item_id,
                    source_url = excluded.source_url,
                    title = excluded.title,
                    content = excluded.content,
                    published_at = excluded.published_at,
                    first_seen_at = excluded.first_seen_at,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        record.id,
                        record.source,
                        record.source_item_id,
                        record.source_url,
                        record.title,
                        record.content,
                        record.published_at.isoformat() if record.published_at else None,
                        record.first_seen_at.isoformat() if record.first_seen_at else None,
                        json.dumps(record.metadata, ensure_ascii=False, sort_keys=True),
                        now,
                    )
                    for record in records
                ],
            )
            conn.commit()

    def upsert_event_sources(self, sources: list[EventSourceRecord]) -> None:
        if not sources:
            return
        self.upsert_items([source.item for source in sources])
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO event_sources (
                    item_id, event_id, role, match_method, similarity, matched_item_id,
                    ai_result_json, needs_review, updated_at
                )
                VALUES (?, ?, NULL, NULL, NULL, NULL, '{}', 0, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    updated_at = excluded.updated_at
                """,
                [(source.item.id, source.event_id, now) for source in sources],
            )
            conn.commit()

    def assign_item_to_event(self, assignment: EventAssignment) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO event_sources (
                    item_id, event_id, role, match_method, similarity, matched_item_id,
                    ai_result_json, needs_review, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    event_id = excluded.event_id,
                    role = excluded.role,
                    match_method = excluded.match_method,
                    similarity = excluded.similarity,
                    matched_item_id = excluded.matched_item_id,
                    ai_result_json = excluded.ai_result_json,
                    needs_review = excluded.needs_review,
                    updated_at = excluded.updated_at
                """,
                (
                    assignment.item_id,
                    assignment.event_id,
                    assignment.role,
                    assignment.match_method,
                    assignment.similarity,
                    assignment.matched_item_id,
                    json.dumps(assignment.ai_result, ensure_ascii=False, sort_keys=True),
                    1 if assignment.needs_review else 0,
                    now,
                ),
            )
            conn.commit()

    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]:
        if not item_ids:
            return []
        placeholders = ",".join("?" for _ in item_ids)
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
                    i.metadata_json
                FROM event_sources s
                JOIN newsflash_items i ON i.id = s.item_id
                WHERE i.id IN ({placeholders})
                ORDER BY i.id ASC
                """,
                tuple(item_ids),
            ).fetchall()
        return [_row_to_event_source(row) for row in rows]

    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]:
        params: list[Any] = [since.isoformat()]
        exclude_sql = ""
        if exclude_item_ids:
            placeholders = ",".join("?" for _ in exclude_item_ids)
            exclude_sql = f"AND i.id NOT IN ({placeholders})"
            params.extend(sorted(exclude_item_ids))
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
                    i.metadata_json
                FROM event_sources s
                JOIN newsflash_items i ON i.id = s.item_id
                WHERE COALESCE(i.published_at, i.first_seen_at) >= ?
                  {exclude_sql}
                ORDER BY COALESCE(i.published_at, i.first_seen_at) DESC, i.id DESC
                """,
                tuple(params),
            ).fetchall()
        return [_row_to_event_source(row) for row in rows]

    def has_recent_window(self, *, since: datetime) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM sync_state WHERE state_key = 'recent_window_since'",
            ).fetchone()
        if row is None or not row["state_value"]:
            return False
        cached_since = _parse_dt(row["state_value"])
        return cached_since is not None and cached_since <= since

    def mark_recent_window(self, *, since: datetime) -> None:
        now = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT state_value FROM sync_state WHERE state_key = 'recent_window_since'",
            ).fetchone()
            current_since = _parse_dt(existing["state_value"]) if existing and existing["state_value"] else None
            target_since = min(current_since, since) if current_since is not None else since
            conn.execute(
                """
                INSERT INTO sync_state (state_key, state_value, updated_at)
                VALUES ('recent_window_since', ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    state_value = excluded.state_value,
                    updated_at = excluded.updated_at
                """,
                (target_since.isoformat(), now),
            )
            conn.commit()


def _parse_dt(value: Any) -> datetime | None:
    text = str(value).strip() if value not in (None, "") else ""
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _row_to_event_source(row: sqlite3.Row) -> EventSourceRecord:
    metadata_json = str(row["metadata_json"]) if row["metadata_json"] is not None else "{}"
    return EventSourceRecord(
        event_id=str(row["event_id"]),
        item=NewsflashItemRecord(
            id=int(row["id"]),
            source=str(row["source"]),
            source_item_id=str(row["source_item_id"]),
            source_url=row["source_url"],
            title=row["title"],
            content=str(row["content"]),
            published_at=_parse_dt(row["published_at"]),
            first_seen_at=_parse_dt(row["first_seen_at"]),
            metadata=json.loads(metadata_json) if metadata_json else {},
        ),
    )
