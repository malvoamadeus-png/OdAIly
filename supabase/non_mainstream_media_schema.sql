-- Non-mainstream media capture console and worker schema.
-- Run through `python backend/src/main.py non-mainstream-media-init-db` when possible.

CREATE TABLE IF NOT EXISTS console_admins (
    email text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION is_console_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.console_admins
        WHERE lower(email) = lower(COALESCE(auth.jwt() ->> 'email', ''))
    );
$$;

ALTER TABLE console_admins ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS console_admins_authenticated_self_select ON console_admins;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON console_admins FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT ON console_admins TO authenticated;
        EXECUTE 'CREATE POLICY console_admins_authenticated_self_select ON console_admins
            FOR SELECT TO authenticated
            USING (lower(email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
    END IF;
END
$$;

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

CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
    component text NOT NULL,
    worker_id text NOT NULL,
    status text NOT NULL DEFAULT 'running',
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    last_error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, worker_id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_heartbeats_component_seen
ON pipeline_worker_heartbeats(component, last_seen_at DESC);

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
    enabled boolean NOT NULL DEFAULT true,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

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
AFTER INSERT OR DELETE OR UPDATE OF display_name, homepage_url, capture_method, enabled ON non_mainstream_media_sources
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
