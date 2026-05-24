from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Protocol

from packages.common.pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_capture.repository import _import_psycopg, get_database_url, utc_now

from .models import DiscoveredPage, NonMainstreamMediaSettings, NonMainstreamMediaSource, ParsedArticle, SiteDefinition, SourceRunStats


CONFIG_NOTIFY_CHANNEL = "non_mainstream_media_config_changed"


class NonMainstreamMediaRepository(Protocol):
    def init_schema(self) -> None: ...
    def sync_sources(self, site_definitions: list[SiteDefinition]) -> None: ...
    def get_settings(self) -> NonMainstreamMediaSettings: ...
    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        jitter_seconds: int | None = None,
    ) -> NonMainstreamMediaSettings: ...
    def list_sources(self, *, include_disabled: bool = False) -> list[NonMainstreamMediaSource]: ...
    def update_source(self, source_id: int, *, enabled: bool | None = None) -> NonMainstreamMediaSource: ...
    def mark_source_seeded(self, source: NonMainstreamMediaSource, source_item_ids: list[str]) -> None: ...
    def mark_seen(self, source: NonMainstreamMediaSource, source_item_id: str, *, seeded: bool) -> bool: ...
    def unseen_source_item_ids(self, site_key: str, source_item_ids: list[str]) -> set[str]: ...
    def save_task(self, source: NonMainstreamMediaSource, article: ParsedArticle) -> bool: ...
    def save_alert_task(self, source: NonMainstreamMediaSource, page: DiscoveredPage) -> bool: ...
    def record_source_run(self, stats: SourceRunStats, *, started_at: datetime, finished_at: datetime) -> None: ...
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


def _row_to_settings(row: dict[str, Any]) -> NonMainstreamMediaSettings:
    return NonMainstreamMediaSettings(
        global_interval_seconds=int(row["global_interval_seconds"]),
        jitter_seconds=int(row["jitter_seconds"]),
        updated_at=row.get("updated_at"),
    )


def _row_to_source(row: dict[str, Any]) -> NonMainstreamMediaSource:
    return NonMainstreamMediaSource(
        id=int(row["id"]),
        site_key=str(row["site_key"]),
        display_name=str(row["display_name"]),
        homepage_url=str(row["homepage_url"]),
        capture_method=str(row["capture_method"]),
        pipeline_mode=str(row.get("pipeline_mode") or "write_flow"),
        enabled=bool(row["enabled"]),
        seeded_at=row.get("seeded_at"),
        last_polled_at=row.get("last_polled_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class PostgresNonMainstreamMediaRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def sync_sources(self, site_definitions: list[SiteDefinition]) -> None:
        if not site_definitions:
            return
        with self._connect() as conn:
            for site in site_definitions:
                conn.execute(
                    """
                    INSERT INTO non_mainstream_media_sources (
                        site_key, display_name, homepage_url, capture_method, pipeline_mode, enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, true)
                    ON CONFLICT (site_key) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        homepage_url = EXCLUDED.homepage_url,
                        capture_method = EXCLUDED.capture_method,
                        pipeline_mode = EXCLUDED.pipeline_mode,
                        updated_at = now()
                    WHERE non_mainstream_media_sources.display_name IS DISTINCT FROM EXCLUDED.display_name
                       OR non_mainstream_media_sources.homepage_url IS DISTINCT FROM EXCLUDED.homepage_url
                       OR non_mainstream_media_sources.capture_method IS DISTINCT FROM EXCLUDED.capture_method
                       OR non_mainstream_media_sources.pipeline_mode IS DISTINCT FROM EXCLUDED.pipeline_mode
                    """,
                    (site.site_key, site.display_name, site.homepage_url, site.capture_method, site.pipeline_mode),
                )
            conn.commit()

    def notify_config_changed(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (CONFIG_NOTIFY_CHANNEL, "manual"))
            conn.commit()

    def get_settings(self) -> NonMainstreamMediaSettings:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO non_mainstream_media_settings (singleton_key)
                VALUES ('global')
                ON CONFLICT (singleton_key) DO NOTHING
                RETURNING *
                """
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM non_mainstream_media_settings WHERE singleton_key = 'global'"
                ).fetchone()
            conn.commit()
            return _row_to_settings(row)

    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        jitter_seconds: int | None = None,
    ) -> NonMainstreamMediaSettings:
        current = self.get_settings()
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE non_mainstream_media_settings
                SET global_interval_seconds = %(global_interval_seconds)s,
                    jitter_seconds = %(jitter_seconds)s,
                    config_version = config_version + 1,
                    updated_at = now()
                WHERE singleton_key = 'global'
                RETURNING *
                """,
                {
                    "global_interval_seconds": (
                        global_interval_seconds
                        if global_interval_seconds is not None
                        else current.global_interval_seconds
                    ),
                    "jitter_seconds": jitter_seconds if jitter_seconds is not None else current.jitter_seconds,
                },
            ).fetchone()
            conn.commit()
            return _row_to_settings(row)

    def list_sources(self, *, include_disabled: bool = False) -> list[NonMainstreamMediaSource]:
        where = "" if include_disabled else "WHERE enabled = true"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM non_mainstream_media_sources
                {where}
                ORDER BY enabled DESC, pipeline_mode ASC, display_name ASC, site_key ASC
                """
            ).fetchall()
        return [_row_to_source(row) for row in rows]

    def update_source(self, source_id: int, *, enabled: bool | None = None) -> NonMainstreamMediaSource:
        if enabled is None:
            raise ValueError("no fields to update")
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE non_mainstream_media_sources
                SET enabled = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (enabled, source_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"non mainstream media source not found: {source_id}")
            conn.commit()
            return _row_to_source(row)

    def mark_source_seeded(self, source: NonMainstreamMediaSource, source_item_ids: list[str]) -> None:
        with self._connect() as conn:
            for source_item_id in source_item_ids:
                conn.execute(
                    """
                    INSERT INTO non_mainstream_media_seen_items (site_key, source_item_id, seeded)
                    VALUES (%s, %s, true)
                    ON CONFLICT (site_key, source_item_id) DO NOTHING
                    """,
                    (source.site_key, source_item_id),
                )
            conn.execute(
                """
                UPDATE non_mainstream_media_sources
                SET seeded_at = COALESCE(seeded_at, now()),
                    updated_at = now()
                WHERE id = %s
                """,
                (source.id,),
            )
            conn.commit()

    def mark_seen(self, source: NonMainstreamMediaSource, source_item_id: str, *, seeded: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO non_mainstream_media_seen_items (site_key, source_item_id, seeded)
                VALUES (%s, %s, %s)
                ON CONFLICT (site_key, source_item_id) DO NOTHING
                RETURNING site_key
                """,
                (source.site_key, source_item_id, seeded),
            ).fetchone()
            conn.commit()
            return row is not None

    def unseen_source_item_ids(self, site_key: str, source_item_ids: list[str]) -> set[str]:
        if not source_item_ids:
            return set()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT source_item_id
                FROM non_mainstream_media_seen_items
                WHERE site_key = %s
                  AND source_item_id = ANY(%s)
                """,
                (site_key, source_item_ids),
            ).fetchall()
        seen = {str(row["source_item_id"]) for row in rows}
        return set(source_item_ids) - seen

    def save_task(self, source: NonMainstreamMediaSource, article: ParsedArticle) -> bool:
        metadata = {
            **article.metadata,
            "site_key": source.site_key,
            "site_display_name": source.display_name,
            "capture_method": source.capture_method,
            "pipeline_mode": source.pipeline_mode,
            "content_format": article.content_format,
            "author_names": article.author_names,
            "tags": article.tags,
            "categories": article.categories,
            "excerpt": article.excerpt,
            "canonical_url": article.canonical_url,
            "source_kind": "non_mainstream_media",
        }
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO tasks (
                    source, source_item_id, source_url, title, content, published_at, raw_payload, metadata, status
                )
                VALUES (
                    'non_mainstream_media',
                    %(source_item_id)s,
                    %(source_url)s,
                    %(title)s,
                    %(content)s,
                    %(published_at)s,
                    %(raw_payload)s,
                    %(metadata)s,
                    'pending'
                )
                ON CONFLICT (source, source_item_id) DO NOTHING
                RETURNING id
                """,
                {
                    "source_item_id": article.canonical_url,
                    "source_url": article.canonical_url,
                    "title": article.title,
                    "content": article.content,
                    "published_at": article.published_at,
                    "raw_payload": self._Jsonb(article.raw_payload),
                    "metadata": self._Jsonb(metadata),
                },
            ).fetchone()
            conn.commit()
            return row is not None

    def save_alert_task(self, source: NonMainstreamMediaSource, page: DiscoveredPage) -> bool:
        content = (page.excerpt or page.title or page.detail_url).strip()
        metadata = {
            "site_key": source.site_key,
            "site_display_name": source.display_name,
            "capture_method": source.capture_method,
            "pipeline_mode": source.pipeline_mode,
            "excerpt": page.excerpt,
            "source_kind": "external_media_alert",
        }
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO tasks (
                    source, source_item_id, source_url, title, content, raw_payload, metadata, status
                )
                VALUES (
                    'external_media_alert',
                    %(source_item_id)s,
                    %(source_url)s,
                    %(title)s,
                    %(content)s,
                    %(raw_payload)s,
                    %(metadata)s,
                    'pending'
                )
                ON CONFLICT (source, source_item_id) DO NOTHING
                RETURNING id
                """,
                {
                    "source_item_id": page.source_item_id,
                    "source_url": page.detail_url,
                    "title": page.title,
                    "content": content,
                    "raw_payload": self._Jsonb(
                        {
                            "detail_url": page.detail_url,
                            "title": page.title,
                            "excerpt": page.excerpt,
                        }
                    ),
                    "metadata": self._Jsonb(metadata),
                },
            ).fetchone()
            conn.commit()
            return row is not None

    def record_source_run(self, stats: SourceRunStats, *, started_at: datetime, finished_at: datetime) -> None:
        del started_at
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE non_mainstream_media_sources
                SET last_polled_at = %(finished_at)s,
                    last_success_at = CASE WHEN %(status)s = 'success' THEN %(finished_at)s ELSE last_success_at END,
                    last_error = CASE WHEN %(status)s = 'success' THEN NULL ELSE %(error)s END,
                    updated_at = now()
                WHERE id = %(source_id)s
                """,
                {
                    "finished_at": finished_at,
                    "status": stats.status,
                    "error": stats.error,
                    "source_id": stats.source.id,
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


class InMemoryNonMainstreamMediaRepository:
    def __init__(self) -> None:
        self.settings = NonMainstreamMediaSettings()
        self.sources: dict[int, NonMainstreamMediaSource] = {}
        self._source_id = 1
        self.seen: dict[tuple[str, str], dict[str, Any]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []

    def init_schema(self) -> None:
        return None

    def sync_sources(self, site_definitions: list[SiteDefinition]) -> None:
        by_key = {source.site_key: source for source in self.sources.values()}
        for site in site_definitions:
            existing = by_key.get(site.site_key)
            if existing is None:
                source = NonMainstreamMediaSource(
                    id=self._source_id,
                    site_key=site.site_key,
                    display_name=site.display_name,
                    homepage_url=site.homepage_url,
                    capture_method=site.capture_method,
                    pipeline_mode=site.pipeline_mode,
                )
                self.sources[self._source_id] = source
                self._source_id += 1
                continue
            self.sources[existing.id] = NonMainstreamMediaSource(
                **{
                    **asdict(existing),
                    "display_name": site.display_name,
                    "homepage_url": site.homepage_url,
                    "capture_method": site.capture_method,
                    "pipeline_mode": site.pipeline_mode,
                    "updated_at": utc_now(),
                }
            )

    def get_settings(self) -> NonMainstreamMediaSettings:
        return self.settings

    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        jitter_seconds: int | None = None,
    ) -> NonMainstreamMediaSettings:
        self.settings = NonMainstreamMediaSettings(
            global_interval_seconds=(
                global_interval_seconds if global_interval_seconds is not None else self.settings.global_interval_seconds
            ),
            jitter_seconds=jitter_seconds if jitter_seconds is not None else self.settings.jitter_seconds,
            updated_at=utc_now(),
        )
        return self.settings

    def list_sources(self, *, include_disabled: bool = False) -> list[NonMainstreamMediaSource]:
        sources = list(self.sources.values())
        if not include_disabled:
            sources = [source for source in sources if source.enabled]
        return sorted(sources, key=lambda item: (not item.enabled, item.display_name, item.site_key))

    def update_source(self, source_id: int, *, enabled: bool | None = None) -> NonMainstreamMediaSource:
        source = self.sources[source_id]
        updated = NonMainstreamMediaSource(
            **{
                **asdict(source),
                "enabled": source.enabled if enabled is None else enabled,
                "updated_at": utc_now(),
            }
        )
        self.sources[source_id] = updated
        return updated

    def mark_source_seeded(self, source: NonMainstreamMediaSource, source_item_ids: list[str]) -> None:
        for source_item_id in source_item_ids:
            self.seen.setdefault((source.site_key, source_item_id), {"seeded": True, "created_at": utc_now()})
        current = self.sources[source.id]
        self.sources[source.id] = NonMainstreamMediaSource(
            **{
                **asdict(current),
                "seeded_at": current.seeded_at or utc_now(),
                "updated_at": utc_now(),
            }
        )

    def mark_seen(self, source: NonMainstreamMediaSource, source_item_id: str, *, seeded: bool) -> bool:
        key = (source.site_key, source_item_id)
        if key in self.seen:
            return False
        self.seen[key] = {"seeded": seeded, "created_at": utc_now()}
        return True

    def unseen_source_item_ids(self, site_key: str, source_item_ids: list[str]) -> set[str]:
        return {
            source_item_id
            for source_item_id in source_item_ids
            if (site_key, source_item_id) not in self.seen
        }

    def save_task(self, source: NonMainstreamMediaSource, article: ParsedArticle) -> bool:
        if any(
            item["source"] == "non_mainstream_media" and item["source_item_id"] == article.canonical_url
            for item in self.tasks
        ):
            return False
        self.tasks.append(
            {
                "source": "non_mainstream_media",
                "source_item_id": article.canonical_url,
                "source_url": article.canonical_url,
                "title": article.title,
                "content": article.content,
                "published_at": article.published_at,
                "metadata": {
                    **article.metadata,
                    "site_key": source.site_key,
                    "site_display_name": source.display_name,
                    "capture_method": source.capture_method,
                    "pipeline_mode": source.pipeline_mode,
                    "content_format": article.content_format,
                    "author_names": article.author_names,
                    "tags": article.tags,
                    "categories": article.categories,
                    "excerpt": article.excerpt,
                    "canonical_url": article.canonical_url,
                    "source_kind": "non_mainstream_media",
                },
                "raw_payload": article.raw_payload,
                "status": "pending",
            }
        )
        return True

    def save_alert_task(self, source: NonMainstreamMediaSource, page: DiscoveredPage) -> bool:
        if any(
            item["source"] == "external_media_alert" and item["source_item_id"] == page.source_item_id
            for item in self.tasks
        ):
            return False
        self.tasks.append(
            {
                "source": "external_media_alert",
                "source_item_id": page.source_item_id,
                "source_url": page.detail_url,
                "title": page.title,
                "content": page.excerpt or page.title or page.detail_url,
                "published_at": None,
                "metadata": {
                    "site_key": source.site_key,
                    "site_display_name": source.display_name,
                    "capture_method": source.capture_method,
                    "pipeline_mode": source.pipeline_mode,
                    "excerpt": page.excerpt,
                    "source_kind": "external_media_alert",
                },
                "raw_payload": {
                    "detail_url": page.detail_url,
                    "title": page.title,
                    "excerpt": page.excerpt,
                },
                "status": "pending",
            }
        )
        return True

    def record_source_run(self, stats: SourceRunStats, *, started_at: datetime, finished_at: datetime) -> None:
        del started_at
        current = self.sources[stats.source.id]
        self.sources[stats.source.id] = NonMainstreamMediaSource(
            **{
                **asdict(current),
                "last_polled_at": finished_at,
                "last_success_at": finished_at if stats.status == "success" else current.last_success_at,
                "last_error": None if stats.status == "success" else stats.error,
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


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + CONSOLE_AUTH_SCHEMA_SQL + """
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

CREATE TABLE IF NOT EXISTS non_mainstream_media_settings (
    singleton_key text PRIMARY KEY DEFAULT 'global',
    global_interval_seconds integer NOT NULL DEFAULT 60 CHECK (global_interval_seconds BETWEEN 10 AND 3600),
    jitter_seconds integer NOT NULL DEFAULT 5 CHECK (jitter_seconds BETWEEN 0 AND 300),
    config_version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO non_mainstream_media_settings (singleton_key)
VALUES ('global')
ON CONFLICT (singleton_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS non_mainstream_media_sources (
    id bigserial PRIMARY KEY,
    site_key text NOT NULL UNIQUE,
    display_name text NOT NULL,
    homepage_url text NOT NULL,
    capture_method text NOT NULL CHECK (capture_method IN ('html_request', 'browser_render')),
    pipeline_mode text NOT NULL DEFAULT 'write_flow',
    enabled boolean NOT NULL DEFAULT true,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE non_mainstream_media_sources
ADD COLUMN IF NOT EXISTS pipeline_mode text NOT NULL DEFAULT 'write_flow';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'non_mainstream_media_sources_pipeline_mode_check'
    ) THEN
        ALTER TABLE non_mainstream_media_sources
        ADD CONSTRAINT non_mainstream_media_sources_pipeline_mode_check
        CHECK (pipeline_mode IN ('write_flow', 'alert_only'));
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS non_mainstream_media_seen_items (
    id bigserial PRIMARY KEY,
    site_key text NOT NULL REFERENCES non_mainstream_media_sources(site_key) ON DELETE CASCADE,
    source_item_id text NOT NULL,
    seeded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (site_key, source_item_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_non_mainstream_status_created
ON tasks(source, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_non_mainstream_sources_enabled
ON non_mainstream_media_sources(enabled, site_key);

CREATE INDEX IF NOT EXISTS idx_non_mainstream_seen_site_key
ON non_mainstream_media_seen_items(site_key, created_at DESC);

CREATE OR REPLACE FUNCTION notify_non_mainstream_media_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'non_mainstream_media_config_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP)::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_non_mainstream_media_settings_notify ON non_mainstream_media_settings;
CREATE TRIGGER trg_non_mainstream_media_settings_notify
AFTER INSERT OR UPDATE OR DELETE ON non_mainstream_media_settings
FOR EACH ROW EXECUTE FUNCTION notify_non_mainstream_media_config_changed();

DROP TRIGGER IF EXISTS trg_non_mainstream_media_sources_notify ON non_mainstream_media_sources;
CREATE TRIGGER trg_non_mainstream_media_sources_notify
AFTER INSERT OR DELETE OR UPDATE OF display_name, homepage_url, capture_method, pipeline_mode, enabled ON non_mainstream_media_sources
FOR EACH ROW EXECUTE FUNCTION notify_non_mainstream_media_config_changed();

ALTER TABLE non_mainstream_media_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE non_mainstream_media_sources ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS non_mainstream_media_settings_anon_all ON non_mainstream_media_settings;
DROP POLICY IF EXISTS non_mainstream_media_sources_anon_all ON non_mainstream_media_sources;
DROP POLICY IF EXISTS non_mainstream_media_settings_console_admin_all ON non_mainstream_media_settings;
DROP POLICY IF EXISTS non_mainstream_media_sources_console_admin_all ON non_mainstream_media_sources;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON non_mainstream_media_settings, non_mainstream_media_sources FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE non_mainstream_media_sources_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON non_mainstream_media_settings, non_mainstream_media_sources TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE non_mainstream_media_sources_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY non_mainstream_media_settings_console_admin_all ON non_mainstream_media_settings
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY non_mainstream_media_sources_console_admin_all ON non_mainstream_media_sources
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
    END IF;
END
$$;
"""
