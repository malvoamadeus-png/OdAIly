-- X capture console schema.
-- Run this once in Supabase SQL Editor before using the Vercel console.
-- It creates the console tables, authenticated admin RLS policies,
-- and Postgres NOTIFY triggers consumed by the Linux worker.

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

CREATE TABLE IF NOT EXISTS x_capture_settings (
    singleton_key text PRIMARY KEY DEFAULT 'global',
    global_interval_seconds integer NOT NULL DEFAULT 30 CHECK (global_interval_seconds BETWEEN 5 AND 3600),
    max_concurrency integer NOT NULL DEFAULT 2 CHECK (max_concurrency BETWEEN 1 AND 20),
    jitter_seconds integer NOT NULL DEFAULT 5 CHECK (jitter_seconds BETWEEN 0 AND 300),
    config_version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO x_capture_settings (singleton_key)
VALUES ('global')
ON CONFLICT (singleton_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS x_capture_accounts (
    id bigserial PRIMARY KEY,
    username text NOT NULL,
    username_lower text NOT NULL UNIQUE,
    display_name text,
    write_name text,
    profile_url text,
    enabled boolean NOT NULL DEFAULT true,
    is_ai_source boolean NOT NULL DEFAULT false,
    interval_seconds integer CHECK (interval_seconds IS NULL OR interval_seconds BETWEEN 5 AND 3600),
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE x_capture_accounts ADD COLUMN IF NOT EXISTS write_name text;
ALTER TABLE x_capture_accounts ADD COLUMN IF NOT EXISTS is_ai_source boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS x_seen_tweets (
    tweet_id text PRIMARY KEY,
    account_id bigint REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
    username_lower text NOT NULL,
    seeded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

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

CREATE TABLE IF NOT EXISTS x_capture_attempts (
    id bigserial PRIMARY KEY,
    account_id bigint REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
    username_lower text NOT NULL,
    status text NOT NULL,
    source text NOT NULL DEFAULT 'fxtwitter',
    candidate_count integer NOT NULL DEFAULT 0,
    seeded_count integer NOT NULL DEFAULT 0,
    new_count integer NOT NULL DEFAULT 0,
    saved_count integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS competitor_filter_keywords (
    id bigserial PRIMARY KEY,
    term text NOT NULL UNIQUE,
    term_normalized text NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO competitor_filter_keywords (term, term_normalized, enabled)
VALUES
    ('跌破', '跌破', true),
    ('突破', '突破', true),
    ('爆仓', '爆仓', true),
    ('Bitget', 'bitget', true)
ON CONFLICT (term_normalized) DO UPDATE
SET term = EXCLUDED.term,
    updated_at = now();

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_x_capture_accounts_enabled ON x_capture_accounts(enabled);
CREATE INDEX IF NOT EXISTS idx_x_seen_tweets_username ON x_seen_tweets(username_lower);
CREATE INDEX IF NOT EXISTS idx_tasks_source_status_created ON tasks(source, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_capture_attempts_started ON x_capture_attempts(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_capture_attempts_noop_recent
ON x_capture_attempts(account_id, finished_at DESC, id DESC)
WHERE status = 'success' AND new_count = 0 AND saved_count = 0;
CREATE INDEX IF NOT EXISTS idx_competitor_filter_keywords_enabled ON competitor_filter_keywords(enabled, term_normalized);
CREATE UNIQUE INDEX IF NOT EXISTS competitor_filter_keywords_term_normalized_key ON competitor_filter_keywords(term_normalized);

CREATE OR REPLACE FUNCTION notify_x_capture_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'x_capture_config_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP)::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_x_capture_settings_notify ON x_capture_settings;
CREATE TRIGGER trg_x_capture_settings_notify
AFTER INSERT OR UPDATE OR DELETE ON x_capture_settings
FOR EACH ROW EXECUTE FUNCTION notify_x_capture_config_changed();

DROP TRIGGER IF EXISTS trg_x_capture_accounts_notify ON x_capture_accounts;
CREATE TRIGGER trg_x_capture_accounts_notify
AFTER INSERT OR UPDATE OR DELETE ON x_capture_accounts
FOR EACH ROW EXECUTE FUNCTION notify_x_capture_config_changed();

ALTER TABLE x_capture_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_capture_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_capture_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE competitor_filter_keywords ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS x_capture_settings_anon_all ON x_capture_settings;
DROP POLICY IF EXISTS x_capture_accounts_anon_all ON x_capture_accounts;
DROP POLICY IF EXISTS x_capture_attempts_anon_select ON x_capture_attempts;
DROP POLICY IF EXISTS competitor_filter_keywords_anon_all ON competitor_filter_keywords;
DROP POLICY IF EXISTS tasks_x_anon_select ON tasks;
DROP POLICY IF EXISTS x_capture_settings_console_admin_all ON x_capture_settings;
DROP POLICY IF EXISTS x_capture_accounts_console_admin_all ON x_capture_accounts;
DROP POLICY IF EXISTS x_capture_attempts_console_admin_select ON x_capture_attempts;
DROP POLICY IF EXISTS competitor_filter_keywords_console_admin_all ON competitor_filter_keywords;
DROP POLICY IF EXISTS tasks_x_console_admin_select ON tasks;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON x_capture_settings, x_capture_accounts, competitor_filter_keywords, x_capture_attempts, tasks FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE x_capture_accounts_id_seq, competitor_filter_keywords_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON x_capture_settings, x_capture_accounts TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON competitor_filter_keywords TO authenticated;
        GRANT SELECT ON x_capture_attempts, tasks TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE x_capture_accounts_id_seq, competitor_filter_keywords_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY x_capture_settings_console_admin_all ON x_capture_settings
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY x_capture_accounts_console_admin_all ON x_capture_accounts
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY x_capture_attempts_console_admin_select ON x_capture_attempts
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY competitor_filter_keywords_console_admin_all ON competitor_filter_keywords
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY tasks_x_console_admin_select ON tasks
            FOR SELECT TO authenticated USING (is_console_admin() AND source = ''x'')';
    END IF;
END
$$;
