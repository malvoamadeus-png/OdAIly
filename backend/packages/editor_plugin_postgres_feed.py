from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction
from packages.editor_plugin_local_store import normalize_feed_lane


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS editor_plugin_local_feed_items (
    feed_item_id text NOT NULL,
    feed_kind text NOT NULL,
    lane text NOT NULL,
    priority integer NOT NULL DEFAULT 0,
    title text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    badges_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    status_label text,
    status_tone text,
    occurred_at timestamptz NOT NULL,
    source_url text,
    detail_url text,
    action_schema_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    meta_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(feed_item_id,feed_kind)
);
CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feed_lane_time
ON editor_plugin_local_feed_items(lane,occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feed_time
ON editor_plugin_local_feed_items(occurred_at DESC);
"""


class PostgresEditorPluginFeedStore:
    def __init__(self) -> None:
        self.application_name = "odaily-editor-plugin-feed"

    @contextmanager
    def _connect(self, *, autocommit: bool = False):
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            if autocommit:
                yield connection
            else:
                with postgres_transaction(connection):
                    yield connection

    def init_schema(self) -> None:
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            apply_schema_migrations(
                connection,
                schema="main",
                component="editor_plugin_feed",
                migrations=(PostgresMigration(
                    1, "editor_plugin_feed_initial", sha256(SCHEMA_SQL.encode()).hexdigest(),
                    lambda conn: conn.execute(SCHEMA_SQL),
                ),),
            )

    def upsert_feed_items(self, rows: list[dict[str, Any]]) -> int:
        self.init_schema()
        from psycopg.types.json import Jsonb

        inserted = 0
        now = datetime.now(UTC)
        with self._connect() as connection:
            for row in rows:
                item_id = str(row.get("feed_item_id") or "").strip()
                kind = str(row.get("feed_kind") or "").strip()
                if not item_id or not kind:
                    continue
                occurred_at = row.get("occurred_at") or now
                if not isinstance(occurred_at, datetime):
                    occurred_at = datetime.fromisoformat(str(occurred_at).replace("Z", "+00:00"))
                if occurred_at.tzinfo is None:
                    occurred_at = occurred_at.replace(tzinfo=UTC)
                connection.execute(
                    """INSERT INTO editor_plugin_local_feed_items(
                        feed_item_id,feed_kind,lane,priority,title,summary,badges_json,status_label,status_tone,
                        occurred_at,source_url,detail_url,action_schema_json,meta_json,created_at,updated_at
                    ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(feed_item_id,feed_kind) DO UPDATE SET lane=EXCLUDED.lane,priority=EXCLUDED.priority,
                    title=EXCLUDED.title,summary=EXCLUDED.summary,badges_json=EXCLUDED.badges_json,
                    status_label=EXCLUDED.status_label,status_tone=EXCLUDED.status_tone,occurred_at=EXCLUDED.occurred_at,
                    source_url=EXCLUDED.source_url,detail_url=EXCLUDED.detail_url,action_schema_json=EXCLUDED.action_schema_json,
                    meta_json=EXCLUDED.meta_json,updated_at=EXCLUDED.updated_at""",
                    (item_id, kind, normalize_feed_lane(kind, str(row.get("lane") or "high")), int(row.get("priority") or 0),
                     str(row.get("title") or ""), str(row.get("summary") or ""), Jsonb(row.get("badges") or []),
                     row.get("status_label"), row.get("status_tone"), occurred_at.astimezone(UTC), row.get("source_url"),
                     row.get("detail_url"), Jsonb(row.get("action_schema") or {"type": "read"}), Jsonb(row.get("meta_json") or {}), now, now),
                )
                inserted += 1
        return inserted


class PostgresFactAuditorFeedWriter:
    def __init__(self, store: PostgresEditorPluginFeedStore | None = None) -> None:
        self.store = store or PostgresEditorPluginFeedStore()

    def upsert_fact_auditor_alert(self, *, check_id: int, source_item_id: str, source_url: str | None,
                                  title: str | None, summary: str, audit_result: dict[str, Any],
                                  occurred_at: datetime | None = None) -> None:
        from packages.editor_plugin_feed_writer import _odaily_newsflash_url

        self.store.upsert_feed_items([{
            "feed_item_id": f"fact_auditor:{check_id}", "feed_kind": "auditor_alert", "lane": "high",
            "priority": 100, "title": title or "事实审查者提醒", "summary": summary or "事实审查者发现疑点",
            "badges": [], "status_label": "事实审查者", "status_tone": "warning",
            "occurred_at": occurred_at or datetime.now(UTC), "source_url": source_url,
            "detail_url": _odaily_newsflash_url(source_item_id, source_url),
            "action_schema": {"type": "feedback", "actions": ["accept", "reject"]},
            "meta_json": {"source_item_id": source_item_id, "check_id": check_id, "fact_status": audit_result.get("status")},
        }])


__all__ = ["PostgresEditorPluginFeedStore", "PostgresFactAuditorFeedWriter"]
