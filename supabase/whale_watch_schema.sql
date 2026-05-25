-- Whale Watch console and worker schema.
-- Prefer `python backend/src/main.py whale-watch-init-db` because it stays in
-- lockstep with backend/packages/whale_watch/repository.py.

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

CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
    component text NOT NULL,
    worker_id text NOT NULL,
    status text NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    last_success_at timestamptz,
    last_error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (component, worker_id)
);

CREATE TABLE IF NOT EXISTS whale_watch_addresses (
    id bigserial PRIMARY KEY,
    address text NOT NULL,
    address_lower text NOT NULL UNIQUE,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT whale_watch_address_format CHECK (address_lower ~ '^0x[0-9a-f]{40}$')
);

CREATE TABLE IF NOT EXISTS whale_watch_chain_states (
    address_id bigint NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,
    chain_key text NOT NULL,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    last_seen_block bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (address_id, chain_key)
);

CREATE TABLE IF NOT EXISTS whale_watch_activities (
    id bigserial PRIMARY KEY,
    address_id bigint NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,
    chain_key text NOT NULL,
    tx_hash text NOT NULL,
    activity_fingerprint text NOT NULL,
    activity_type text NOT NULL CHECK (activity_type IN ('transfer', 'swap')),
    direction text CHECK (direction IS NULL OR direction IN ('in', 'out')),
    counterparty text,
    block_number bigint NOT NULL,
    tx_timestamp timestamptz,
    summary text NOT NULL,
    telegram_text text NOT NULL,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_sent_at timestamptz,
    tx_url text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (address_id, chain_key, tx_hash, activity_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_whale_watch_addresses_enabled
ON whale_watch_addresses(enabled, address_lower);

CREATE INDEX IF NOT EXISTS idx_whale_watch_activities_created
ON whale_watch_activities(created_at DESC);

CREATE OR REPLACE FUNCTION notify_whale_watch_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'whale_watch_config_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP)::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_whale_watch_addresses_notify ON whale_watch_addresses;
CREATE TRIGGER trg_whale_watch_addresses_notify
AFTER INSERT OR UPDATE OR DELETE ON whale_watch_addresses
FOR EACH ROW EXECUTE FUNCTION notify_whale_watch_config_changed();

ALTER TABLE whale_watch_addresses ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_chain_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_activities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS whale_watch_addresses_anon_all ON whale_watch_addresses;
DROP POLICY IF EXISTS whale_watch_chain_states_anon_all ON whale_watch_chain_states;
DROP POLICY IF EXISTS whale_watch_activities_anon_all ON whale_watch_activities;
DROP POLICY IF EXISTS whale_watch_addresses_console_admin_all ON whale_watch_addresses;
DROP POLICY IF EXISTS whale_watch_chain_states_console_admin_select ON whale_watch_chain_states;
DROP POLICY IF EXISTS whale_watch_activities_console_admin_select ON whale_watch_activities;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON whale_watch_addresses, whale_watch_chain_states, whale_watch_activities FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE whale_watch_addresses_id_seq, whale_watch_activities_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON whale_watch_addresses TO authenticated;
        GRANT SELECT ON whale_watch_chain_states, whale_watch_activities TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE whale_watch_addresses_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY whale_watch_addresses_console_admin_all ON whale_watch_addresses
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_chain_states_console_admin_select ON whale_watch_chain_states
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_activities_console_admin_select ON whale_watch_activities
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
