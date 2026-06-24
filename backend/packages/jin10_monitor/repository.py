from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from packages.common.pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url

from .models import DEFAULT_JIN10_ENDPOINT_URL, DEFAULT_JIN10_HEADERS, JIN10_SOURCE, Jin10Item, Jin10RunResult, Jin10Settings


JIN10_CONFIG_NOTIFY_CHANNEL = "jin10_config_changed"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Jin10MonitorRepository(Protocol):
    def init_schema(self) -> None: ...
    def get_settings(self) -> Jin10Settings: ...
    def update_settings(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        endpoint_url: str | None = None,
        channel: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> Jin10Settings: ...
    def has_seen_items(self) -> bool: ...
    def mark_seeded(self, source_item_ids: list[str]) -> None: ...
    def unseen_source_item_ids(self, source_item_ids: list[str]) -> set[str]: ...
    def mark_seen(self, source_item_id: str, *, seeded: bool) -> bool: ...
    def save_task(self, item: Jin10Item) -> int | None: ...
    def record_run(self, result: Jin10RunResult, *, finished_at: datetime) -> None: ...
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


def _row_to_settings(row: dict[str, Any]) -> Jin10Settings:
    return Jin10Settings(
        enabled=bool(row.get("enabled")),
        interval_seconds=int(row.get("interval_seconds") or 60),
        endpoint_url=str(row.get("endpoint_url") or DEFAULT_JIN10_ENDPOINT_URL),
        channel=row.get("channel"),
        request_headers={
            str(key): str(value)
            for key, value in (row.get("request_headers") or DEFAULT_JIN10_HEADERS).items()
            if value is not None
        },
        last_polled_at=row.get("last_polled_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        updated_at=row.get("updated_at"),
    )


class PostgresJin10MonitorRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(JIN10_SCHEMA_SQL)
            conn.commit()

    def get_settings(self) -> Jin10Settings:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO jin10_settings (singleton_key)
                VALUES ('global')
                ON CONFLICT (singleton_key) DO NOTHING
                RETURNING *
                """
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM jin10_settings WHERE singleton_key = 'global'").fetchone()
            conn.commit()
        return _row_to_settings(row)

    def update_settings(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        endpoint_url: str | None = None,
        channel: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> Jin10Settings:
        current = self.get_settings()
        next_headers = request_headers if request_headers is not None else current.request_headers
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE jin10_settings
                SET enabled = %(enabled)s,
                    interval_seconds = %(interval_seconds)s,
                    endpoint_url = %(endpoint_url)s,
                    channel = %(channel)s,
                    request_headers = %(request_headers)s,
                    updated_at = now()
                WHERE singleton_key = 'global'
                RETURNING *
                """,
                {
                    "enabled": current.enabled if enabled is None else enabled,
                    "interval_seconds": interval_seconds if interval_seconds is not None else current.interval_seconds,
                    "endpoint_url": endpoint_url if endpoint_url is not None else current.endpoint_url,
                    "channel": current.channel if channel is None else (channel or None),
                    "request_headers": self._Jsonb(next_headers),
                },
            ).fetchone()
            conn.execute("SELECT pg_notify(%s, %s)", (JIN10_CONFIG_NOTIFY_CHANNEL, "manual"))
            conn.commit()
        return _row_to_settings(row)

    def has_seen_items(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT 1 FROM jin10_seen_items LIMIT 1").fetchone()
        return row is not None

    def mark_seeded(self, source_item_ids: list[str]) -> None:
        with self._connect() as conn:
            for source_item_id in source_item_ids:
                conn.execute(
                    """
                    INSERT INTO jin10_seen_items (source_item_id, seeded)
                    VALUES (%s, true)
                    ON CONFLICT (source_item_id) DO NOTHING
                    """,
                    (source_item_id,),
                )
            conn.commit()

    def unseen_source_item_ids(self, source_item_ids: list[str]) -> set[str]:
        if not source_item_ids:
            return set()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_item_id
                FROM jin10_seen_items
                WHERE source_item_id = ANY(%s)
                """,
                (source_item_ids,),
            ).fetchall()
        seen = {str(row["source_item_id"]) for row in rows}
        return set(source_item_ids) - seen

    def mark_seen(self, source_item_id: str, *, seeded: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO jin10_seen_items (source_item_id, seeded)
                VALUES (%s, %s)
                ON CONFLICT (source_item_id) DO NOTHING
                RETURNING source_item_id
                """,
                (source_item_id, seeded),
            ).fetchone()
            conn.commit()
        return row is not None

    def save_task(self, item: Jin10Item) -> int | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO tasks (
                    source, source_item_id, source_url, title, content, published_at, raw_payload, metadata, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending')
                ON CONFLICT (source, source_item_id) DO UPDATE SET
                    updated_at = tasks.updated_at
                RETURNING id
                """,
                (
                    JIN10_SOURCE,
                    item.source_item_id,
                    item.source_url,
                    item.title,
                    item.content,
                    item.published_at,
                    self._Jsonb(item.raw_payload),
                    self._Jsonb({**item.metadata, "source_kind": JIN10_SOURCE}),
                ),
            ).fetchone()
            conn.commit()
        return int(row["id"]) if row is not None else None

    def record_run(self, result: Jin10RunResult, *, finished_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jin10_settings
                SET last_polled_at = %(finished_at)s,
                    last_success_at = CASE WHEN %(success)s THEN %(finished_at)s ELSE last_success_at END,
                    last_error = CASE WHEN %(success)s THEN NULL ELSE %(error)s END,
                    updated_at = now()
                WHERE singleton_key = 'global'
                """,
                {
                    "finished_at": finished_at,
                    "success": result.status == "success",
                    "error": (result.error or result.status)[:2000],
                },
            )
            conn.commit()

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


class InMemoryJin10MonitorRepository:
    def __init__(self) -> None:
        self.settings = Jin10Settings()
        self.seen: dict[str, dict[str, Any]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []
        self.runs: list[Jin10RunResult] = []

    def init_schema(self) -> None:
        return None

    def get_settings(self) -> Jin10Settings:
        return self.settings

    def update_settings(
        self,
        *,
        enabled: bool | None = None,
        interval_seconds: int | None = None,
        endpoint_url: str | None = None,
        channel: str | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> Jin10Settings:
        self.settings = Jin10Settings(
            **{
                **asdict(self.settings),
                "enabled": self.settings.enabled if enabled is None else enabled,
                "interval_seconds": interval_seconds if interval_seconds is not None else self.settings.interval_seconds,
                "endpoint_url": endpoint_url if endpoint_url is not None else self.settings.endpoint_url,
                "channel": self.settings.channel if channel is None else (channel or None),
                "request_headers": request_headers if request_headers is not None else self.settings.request_headers,
                "updated_at": utc_now(),
            }
        )
        return self.settings

    def has_seen_items(self) -> bool:
        return bool(self.seen)

    def mark_seeded(self, source_item_ids: list[str]) -> None:
        for source_item_id in source_item_ids:
            self.seen.setdefault(source_item_id, {"seeded": True, "created_at": utc_now()})

    def unseen_source_item_ids(self, source_item_ids: list[str]) -> set[str]:
        return {source_item_id for source_item_id in source_item_ids if source_item_id not in self.seen}

    def mark_seen(self, source_item_id: str, *, seeded: bool) -> bool:
        if source_item_id in self.seen:
            return False
        self.seen[source_item_id] = {"seeded": seeded, "created_at": utc_now()}
        return True

    def save_task(self, item: Jin10Item) -> int | None:
        for task in self.tasks:
            if task["source_item_id"] == item.source_item_id:
                return None
        task_id = len(self.tasks) + 1
        self.tasks.append({"id": task_id, "source": JIN10_SOURCE, **asdict(item)})
        return task_id

    def record_run(self, result: Jin10RunResult, *, finished_at: datetime) -> None:
        self.runs.append(result)
        self.settings = Jin10Settings(
            **{
                **asdict(self.settings),
                "last_polled_at": finished_at,
                "last_success_at": finished_at if result.status == "success" else self.settings.last_success_at,
                "last_error": None if result.status == "success" else result.error or result.status,
                "updated_at": utc_now(),
            }
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


JIN10_SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + CONSOLE_AUTH_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS jin10_settings (
    singleton_key text PRIMARY KEY DEFAULT 'global',
    enabled boolean NOT NULL DEFAULT false,
    interval_seconds integer NOT NULL DEFAULT 60,
    endpoint_url text NOT NULL DEFAULT 'https://4a735ea38f8146198dc205d2e2d1bd28.z3c.jin10.com/flash',
    channel text,
    request_headers jsonb NOT NULL DEFAULT '{
        "x-app-id": "bVBF4FyRTn5NJF5n",
        "x-version": "1.0.0",
        "referer": "https://www.jin10.com/",
        "origin": "https://www.jin10.com",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    }'::jsonb,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT false;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS interval_seconds integer NOT NULL DEFAULT 60;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS endpoint_url text NOT NULL DEFAULT 'https://4a735ea38f8146198dc205d2e2d1bd28.z3c.jin10.com/flash';
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS channel text;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS request_headers jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS last_polled_at timestamptz;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS last_success_at timestamptz;
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS last_error text;

INSERT INTO jin10_settings (singleton_key)
VALUES ('global')
ON CONFLICT (singleton_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS jin10_seen_items (
    source_item_id text PRIMARY KEY,
    seeded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION notify_jin10_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('jin10_config_changed', json_build_object('singleton_key', NEW.singleton_key)::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jin10_settings_notify ON jin10_settings;
CREATE TRIGGER trg_jin10_settings_notify
AFTER UPDATE OF enabled, interval_seconds, endpoint_url, channel, request_headers ON jin10_settings
FOR EACH ROW
EXECUTE FUNCTION notify_jin10_config_changed();

ALTER TABLE jin10_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE jin10_seen_items ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS jin10_settings_console_admin_all ON jin10_settings;
DROP POLICY IF EXISTS jin10_seen_items_console_admin_select ON jin10_seen_items;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON jin10_settings, jin10_seen_items FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON jin10_settings TO authenticated;
        GRANT SELECT ON jin10_seen_items TO authenticated;
        EXECUTE 'CREATE POLICY jin10_settings_console_admin_all ON jin10_settings
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY jin10_seen_items_console_admin_select ON jin10_seen_items
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
"""
