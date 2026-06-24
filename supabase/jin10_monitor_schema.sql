-- Jin10 monitor console and worker schema.
-- Prefer running `python backend/src/main.py jin10-init-db`.

CREATE TABLE IF NOT EXISTS jin10_settings (
    singleton_key text PRIMARY KEY DEFAULT 'global',
    enabled boolean NOT NULL DEFAULT false,
    interval_seconds integer NOT NULL DEFAULT 60,
    endpoint_url text NOT NULL DEFAULT 'https://www.jin10.com/flash_newest.js',
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
ALTER TABLE jin10_settings ADD COLUMN IF NOT EXISTS endpoint_url text NOT NULL DEFAULT 'https://www.jin10.com/flash_newest.js';
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
