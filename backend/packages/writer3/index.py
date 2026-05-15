from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .matching import score_candidate
from .models import AnalysisResult, OdailyReference, Writer3Candidate


class Writer3Index:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS odaily_references (
                    source_item_id text PRIMARY KEY,
                    source_url text,
                    title text,
                    content text NOT NULL,
                    published_at text,
                    metadata_json text NOT NULL,
                    raw_payload_json text NOT NULL,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_writer3_odaily_published ON odaily_references(published_at DESC)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS writer3_telegram_confirmations (
                    message_id integer PRIMARY KEY,
                    chat_id text NOT NULL,
                    context_id integer NOT NULL,
                    current_source text NOT NULL,
                    current_source_item_id text NOT NULL,
                    current_message_text text NOT NULL,
                    sent_at text NOT NULL,
                    confirmed_at text,
                    confirmed_by_id text,
                    confirmed_by_username text,
                    confirmed_by_name text,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_writer3_confirm_context ON writer3_telegram_confirmations(context_id)"
            )
            conn.commit()

    def upsert_references(self, references: Iterable[OdailyReference]) -> int:
        rows = [
            (
                item.source_item_id,
                item.source_url,
                item.title,
                item.content,
                item.published_at.isoformat() if item.published_at else None,
                json.dumps(item.metadata, ensure_ascii=False, sort_keys=True),
                json.dumps(item.raw_payload, ensure_ascii=False, sort_keys=True),
                datetime.now(UTC).isoformat(),
            )
            for item in references
        ]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO odaily_references (
                    source_item_id, source_url, title, content, published_at,
                    metadata_json, raw_payload_json, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_item_id) DO UPDATE SET
                    source_url = excluded.source_url,
                    title = excluded.title,
                    content = excluded.content,
                    published_at = excluded.published_at,
                    metadata_json = excluded.metadata_json,
                    raw_payload_json = excluded.raw_payload_json,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def prune_before(self, cutoff: datetime) -> int:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM odaily_references WHERE published_at IS NOT NULL AND published_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def upsert_telegram_confirmation(
        self,
        *,
        message_id: int,
        chat_id: str,
        context_id: int,
        current_source: str,
        current_source_item_id: str,
        current_message_text: str,
        sent_at: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO writer3_telegram_confirmations (
                    message_id, chat_id, context_id, current_source, current_source_item_id,
                    current_message_text, sent_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    chat_id = excluded.chat_id,
                    context_id = excluded.context_id,
                    current_source = excluded.current_source,
                    current_source_item_id = excluded.current_source_item_id,
                    current_message_text = excluded.current_message_text,
                    sent_at = excluded.sent_at,
                    updated_at = excluded.updated_at
                """,
                (
                    message_id,
                    chat_id,
                    context_id,
                    current_source,
                    current_source_item_id,
                    current_message_text,
                    sent_at.isoformat(),
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()

    def get_telegram_confirmation_by_message_id(self, message_id: int) -> dict[str, object] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT message_id, chat_id, context_id, current_source, current_source_item_id,
                       current_message_text, sent_at, confirmed_at, confirmed_by_id, confirmed_by_username,
                       confirmed_by_name
                FROM writer3_telegram_confirmations
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def confirm_telegram_message(
        self,
        *,
        message_id: int,
        confirmed_at: datetime,
        confirmed_by_id: str | None,
        confirmed_by_username: str | None,
        confirmed_by_name: str | None,
    ) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE writer3_telegram_confirmations
                SET confirmed_at = COALESCE(confirmed_at, ?),
                    confirmed_by_id = COALESCE(confirmed_by_id, ?),
                    confirmed_by_username = COALESCE(confirmed_by_username, ?),
                    confirmed_by_name = COALESCE(confirmed_by_name, ?),
                    updated_at = ?
                WHERE message_id = ?
                """,
                (
                    confirmed_at.isoformat(),
                    confirmed_by_id,
                    confirmed_by_username,
                    confirmed_by_name,
                    datetime.now(UTC).isoformat(),
                    message_id,
                ),
            )
            conn.commit()
            return bool(cursor.rowcount)

    def search(
        self,
        *,
        analysis: AnalysisResult,
        current_time: datetime | None,
        history_days: int,
        candidate_limit: int,
        exclude_source_item_id: str | None = None,
    ) -> list[Writer3Candidate]:
        upper = current_time or datetime.now(UTC)
        lower = upper - timedelta(days=history_days)
        rows = self._list_between(lower=lower, upper=upper)
        scored: list[Writer3Candidate] = []
        for reference in rows:
            if exclude_source_item_id and reference.source_item_id == exclude_source_item_id:
                continue
            candidate = score_candidate(reference=reference, analysis=analysis, current_time=current_time)
            if candidate is not None:
                scored.append(candidate)
        scored.sort(key=lambda item: (item.score, item.published_at or datetime.min.replace(tzinfo=UTC)), reverse=True)
        return scored[:candidate_limit] if candidate_limit > 0 else scored

    def _list_between(self, *, lower: datetime, upper: datetime) -> list[OdailyReference]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_item_id, source_url, title, content, published_at, metadata_json, raw_payload_json
                FROM odaily_references
                WHERE published_at >= ? AND published_at < ?
                ORDER BY published_at DESC
                """,
                (lower.isoformat(), upper.isoformat()),
            ).fetchall()
        return [_row_to_reference(row) for row in rows]


def _row_to_reference(row: sqlite3.Row) -> OdailyReference:
    published_at = _parse_datetime(row["published_at"])
    return OdailyReference(
        source_item_id=str(row["source_item_id"]),
        source_url=row["source_url"],
        title=row["title"],
        content=str(row["content"]),
        published_at=published_at,
        metadata=_loads_json(row["metadata_json"]),
        raw_payload=_loads_json(row["raw_payload_json"]),
    )


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _loads_json(value: str | None) -> dict:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}
