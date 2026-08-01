from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL

from .models import DEFAULT_JIN10_ENDPOINT_URL, DEFAULT_JIN10_HEADERS, JIN10_SOURCE, Jin10Item, Jin10RunResult, Jin10Settings


def _dt(value):
    if not value: return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class SQLiteJin10MonitorRepository:
    def __init__(self, path: Path) -> None:
        self.path = path; self.init_schema()

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(SQLITE_SCHEMA_SQL + """
            CREATE TABLE IF NOT EXISTS jin10_settings(singleton_key text PRIMARY KEY, enabled integer NOT NULL DEFAULT 0, interval_seconds integer NOT NULL DEFAULT 60, endpoint_url text NOT NULL, channel text, request_headers text NOT NULL DEFAULT '{}', last_polled_at text, last_success_at text, last_error text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS jin10_seen_items(source_item_id text PRIMARY KEY, seeded integer NOT NULL DEFAULT 0, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.execute("INSERT OR IGNORE INTO jin10_settings(singleton_key, endpoint_url, request_headers) VALUES ('global', ?, ?)", (DEFAULT_JIN10_ENDPOINT_URL, json.dumps(DEFAULT_JIN10_HEADERS)))
            conn.commit()

    def get_settings(self) -> Jin10Settings:
        with connect_sqlite(self.path) as conn: row=conn.execute("SELECT * FROM jin10_settings WHERE singleton_key='global'").fetchone()
        headers=json.loads(row["request_headers"] or "{}")
        return Jin10Settings(enabled=bool(row["enabled"]), interval_seconds=int(row["interval_seconds"]), endpoint_url=row["endpoint_url"], channel=row["channel"], request_headers=headers, last_polled_at=_dt(row["last_polled_at"]), last_success_at=_dt(row["last_success_at"]), last_error=row["last_error"], updated_at=_dt(row["updated_at"]))

    def update_settings(self, *, enabled=None, interval_seconds=None, endpoint_url=None, channel=None, request_headers=None) -> Jin10Settings:
        current=self.get_settings()
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE jin10_settings SET enabled=?, interval_seconds=?, endpoint_url=?, channel=?, request_headers=?, updated_at=CURRENT_TIMESTAMP WHERE singleton_key='global'", (int(current.enabled if enabled is None else enabled), interval_seconds if interval_seconds is not None else current.interval_seconds, endpoint_url if endpoint_url is not None else current.endpoint_url, current.channel if channel is None else channel or None, json.dumps(request_headers if request_headers is not None else current.request_headers)))
            conn.commit()
        return self.get_settings()

    def has_seen_items(self) -> bool:
        with connect_sqlite(self.path) as conn: return conn.execute("SELECT 1 FROM jin10_seen_items LIMIT 1").fetchone() is not None

    def mark_seeded(self, source_item_ids: list[str]) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executemany("INSERT OR IGNORE INTO jin10_seen_items(source_item_id, seeded) VALUES (?,1)", ((item,) for item in source_item_ids)); conn.commit()

    def unseen_source_item_ids(self, source_item_ids: list[str]) -> set[str]:
        if not source_item_ids: return set()
        with connect_sqlite(self.path) as conn: rows=conn.execute(f"SELECT source_item_id FROM jin10_seen_items WHERE source_item_id IN ({','.join('?' for _ in source_item_ids)})", source_item_ids).fetchall()
        return set(source_item_ids)-{row["source_item_id"] for row in rows}

    def mark_seen(self, source_item_id: str, *, seeded: bool) -> bool:
        with connect_sqlite(self.path) as conn: cur=conn.execute("INSERT OR IGNORE INTO jin10_seen_items(source_item_id, seeded) VALUES (?,?)", (source_item_id,int(seeded))); conn.commit(); return cur.rowcount>0

    def save_task(self, item: Jin10Item) -> int | None:
        with connect_sqlite(self.path) as conn:
            conn.execute("INSERT OR IGNORE INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES (?,?,?,?,?,?,?,?, 'pending')", (JIN10_SOURCE,item.source_item_id,item.source_url,item.title,item.content,item.published_at.isoformat() if item.published_at else None,json.dumps(item.raw_payload,ensure_ascii=False),json.dumps({**item.metadata,"source_kind":JIN10_SOURCE},ensure_ascii=False)))
            row=conn.execute("SELECT id FROM tasks WHERE source=? AND source_item_id=?",(JIN10_SOURCE,item.source_item_id)).fetchone(); conn.commit(); return int(row["id"]) if row else None

    def record_run(self, result: Jin10RunResult, *, finished_at: datetime) -> None:
        with connect_sqlite(self.path) as conn: conn.execute("UPDATE jin10_settings SET last_polled_at=?, last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END, last_error=CASE WHEN ? THEN NULL ELSE ? END, updated_at=CURRENT_TIMESTAMP WHERE singleton_key='global'",(finished_at.isoformat(),int(result.status=="success"),finished_at.isoformat(),int(result.status=="success"),(result.error or result.status)[:2000])); conn.commit()

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error: str | None=None, metadata: dict[str,Any]|None=None) -> None:
        now=datetime.now(UTC).isoformat()
        with connect_sqlite(self.path) as conn: conn.execute("INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata) VALUES (?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,last_success_at=COALESCE(excluded.last_success_at,pipeline_worker_heartbeats.last_success_at),last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(component,worker_id,status,now,now if success else None,error[:2000] if error else None,json.dumps(metadata or {}))); conn.commit()
