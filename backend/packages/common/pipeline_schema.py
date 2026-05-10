PIPELINE_MONITORING_SCHEMA_SQL = """
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

CREATE TABLE IF NOT EXISTS pipeline_alerts (
    alert_key text PRIMARY KEY,
    last_sent_at timestamptz NOT NULL,
    last_message text NOT NULL,
    send_count integer NOT NULL DEFAULT 1,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_alerts_last_sent
ON pipeline_alerts(last_sent_at DESC);
"""


COMPETITOR_FILTER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS competitor_filter_keywords (
    id bigserial PRIMARY KEY,
    term text NOT NULL UNIQUE,
    term_normalized text NOT NULL UNIQUE,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'competitor_filter_keywords'
          AND column_name = 'term_normalized'
    ) THEN
        ALTER TABLE competitor_filter_keywords ADD COLUMN term_normalized text;
        UPDATE competitor_filter_keywords
        SET term_normalized = lower(regexp_replace(trim(term), '\\s+', ' ', 'g'))
        WHERE term_normalized IS NULL;
        ALTER TABLE competitor_filter_keywords ALTER COLUMN term_normalized SET NOT NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_competitor_filter_keywords_enabled
ON competitor_filter_keywords(enabled, term_normalized);

CREATE UNIQUE INDEX IF NOT EXISTS competitor_filter_keywords_term_normalized_key
ON competitor_filter_keywords(term_normalized);

INSERT INTO competitor_filter_keywords (term, term_normalized, enabled)
VALUES
    ('跌破', '跌破', true),
    ('突破', '突破', true),
    ('爆仓', '爆仓', true),
    ('Bitget', 'bitget', true)
ON CONFLICT (term_normalized) DO UPDATE
SET term = EXCLUDED.term,
    updated_at = now();

ALTER TABLE competitor_filter_keywords ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS competitor_filter_keywords_anon_all ON competitor_filter_keywords;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        GRANT USAGE ON SCHEMA public TO anon;
        GRANT SELECT, INSERT, UPDATE, DELETE ON competitor_filter_keywords TO anon;
        GRANT USAGE, SELECT ON SEQUENCE competitor_filter_keywords_id_seq TO anon;

        EXECUTE 'CREATE POLICY competitor_filter_keywords_anon_all ON competitor_filter_keywords FOR ALL TO anon USING (true) WITH CHECK (true)';
    END IF;
END
$$;
"""
