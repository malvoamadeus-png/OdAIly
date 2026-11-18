from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction


_SCHEMA = """
CREATE TABLE IF NOT EXISTS msx_notice_state (
    singleton_key text PRIMARY KEY CHECK(singleton_key = 'global'), seeded_at timestamptz,
    last_polled_at timestamptz, last_success_at timestamptz, last_error text,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS msx_notice_seen_items (
    source_item_id text PRIMARY KEY, seeded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


class PostgresMSXNoticeStore:
    def __init__(self) -> None:
        from psycopg.types.json import Jsonb
        self._Jsonb = Jsonb
        self.initialize_schema()
        self.ensure_task_schema()

    @staticmethod
    def initialize_schema() -> None:
        checksum = hashlib.sha256(_SCHEMA.encode()).hexdigest()
        with connect_postgres(schema="main", application_name="odaily-msx-notice-migrate") as conn:
            apply_schema_migrations(conn, schema="main", component="msx_notice",
                migrations=(PostgresMigration(1, "MSX capture state", checksum,
                    lambda connection: connection.execute(_SCHEMA)),))

    def ensure_task_schema(self) -> None:
        from packages.x_processing.repository import PostgresXProcessingRepository
        PostgresXProcessingRepository().init_schema()

    def ensure_state(self) -> None:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-state") as conn:
            conn.execute("INSERT INTO msx_notice_state(singleton_key) VALUES ('global') ON CONFLICT DO NOTHING")

    def is_unseeded(self) -> bool:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-read") as conn:
            row = conn.execute("SELECT seeded_at FROM msx_notice_state WHERE singleton_key='global'").fetchone()
        return not row or not row["seeded_at"]

    def seed(self, ids: list[str]) -> None:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-seed") as conn:
            with postgres_transaction(conn):
                for item in ids:
                    conn.execute("INSERT INTO msx_notice_seen_items(source_item_id,seeded) VALUES (%s,true) ON CONFLICT DO NOTHING", (item,))
                conn.execute("UPDATE msx_notice_state SET seeded_at=now(),updated_at=now() WHERE singleton_key='global'")

    def unseen(self, ids: list[str]) -> set[str]:
        if not ids:
            return set()
        with connect_postgres(schema="main", application_name="odaily-msx-notice-read") as conn:
            rows = conn.execute("SELECT source_item_id FROM msx_notice_seen_items WHERE source_item_id=ANY(%s)", (ids,)).fetchall()
        return set(ids) - {row["source_item_id"] for row in rows}

    def mark_seen(self, item: str, seeded: bool) -> bool:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-seen") as conn:
            result = conn.execute("INSERT INTO msx_notice_seen_items(source_item_id,seeded) VALUES (%s,%s) ON CONFLICT DO NOTHING", (item, seeded))
            return result.rowcount == 1

    def record_state(self, success: bool, error: str | None) -> None:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-state") as conn:
            conn.execute("UPDATE msx_notice_state SET last_polled_at=now(),last_success_at=CASE WHEN %s THEN now() ELSE last_success_at END,last_error=%s,updated_at=now() WHERE singleton_key='global'", (success, error))

    def save_task(self, *, source_item_id: str, source_url: str, title: str, content: str,
                  published_at: datetime, raw: dict, metadata: dict) -> int:
        with connect_postgres(schema="main", application_name="odaily-msx-notice-task") as conn:
            with postgres_transaction(conn):
                conn.execute("""INSERT INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status)
                    VALUES('msx',%s,%s,%s,%s,%s,%s,%s,'pending') ON CONFLICT(source,source_item_id) DO NOTHING""",
                    (source_item_id, source_url, title, content, published_at, self._Jsonb(raw), self._Jsonb(metadata)))
                row = conn.execute("SELECT id FROM tasks WHERE source='msx' AND source_item_id=%s", (source_item_id,)).fetchone()
        if row is None:
            raise RuntimeError(f"failed to save MSX task: {source_item_id}")
        return int(row["id"])

    def record_heartbeat(self, *, worker_id: str, status: str, success: bool,
                         error: str | None, metadata: dict) -> None:
        now = datetime.now(UTC)
        with connect_postgres(schema="main", application_name="odaily-msx-notice-heartbeat") as conn:
            conn.execute("""INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata)
                VALUES('msx_notice',%s,%s,%s,%s,%s,%s) ON CONFLICT(component,worker_id) DO UPDATE SET
                status=excluded.status,last_seen_at=excluded.last_seen_at,
                last_success_at=COALESCE(excluded.last_success_at,pipeline_worker_heartbeats.last_success_at),
                last_error=excluded.last_error,metadata=excluded.metadata,updated_at=now()""",
                (worker_id, status, now, now if success else None, error[:2000] if error else None, self._Jsonb(metadata)))
