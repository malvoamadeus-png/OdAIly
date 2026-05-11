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


NEWSFLASH_EVENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS newsflash_items (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    content_hash text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_item_id)
);

CREATE TABLE IF NOT EXISTS newsflash_events (
    event_id text PRIMARY KEY,
    representative_item_id bigint REFERENCES newsflash_items(id) ON DELETE SET NULL,
    representative_title text,
    event_time timestamptz,
    first_source text,
    first_published_at timestamptz,
    source_count integer NOT NULL DEFAULT 0,
    competitor_source_count integer NOT NULL DEFAULT 0,
    has_odaily boolean NOT NULL DEFAULT false,
    status text NOT NULL DEFAULT 'active',
    needs_review boolean NOT NULL DEFAULT false,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS newsflash_event_sources (
    id bigserial PRIMARY KEY,
    event_id text NOT NULL REFERENCES newsflash_events(event_id) ON DELETE CASCADE,
    item_id bigint NOT NULL REFERENCES newsflash_items(id) ON DELETE CASCADE,
    source text NOT NULL,
    source_item_id text NOT NULL,
    role text NOT NULL DEFAULT 'supporting' CHECK (role IN ('primary', 'supporting')),
    match_method text NOT NULL DEFAULT 'new_event',
    similarity double precision,
    matched_item_id bigint REFERENCES newsflash_items(id) ON DELETE SET NULL,
    ai_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (event_id, item_id),
    UNIQUE (item_id)
);

CREATE TABLE IF NOT EXISTS newsflash_event_favorites (
    event_id text PRIMARY KEY REFERENCES newsflash_events(event_id) ON DELETE CASCADE,
    favorite boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS newsflash_event_notes (
    event_id text PRIMARY KEY REFERENCES newsflash_events(event_id) ON DELETE CASCADE,
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS newsflash_item_notes (
    item_id bigint PRIMARY KEY REFERENCES newsflash_items(id) ON DELETE CASCADE,
    note text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_newsflash_items_source_published
ON newsflash_items(source, published_at DESC NULLS LAST, first_seen_at DESC);

CREATE INDEX IF NOT EXISTS idx_newsflash_items_content_hash
ON newsflash_items(content_hash);

CREATE INDEX IF NOT EXISTS idx_newsflash_events_time
ON newsflash_events(event_time DESC NULLS LAST, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_newsflash_events_status
ON newsflash_events(status, needs_review, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_newsflash_event_sources_event
ON newsflash_event_sources(event_id);

CREATE INDEX IF NOT EXISTS idx_newsflash_event_sources_item
ON newsflash_event_sources(item_id);

CREATE OR REPLACE VIEW newsflash_event_summary AS
SELECT
    e.event_id,
    e.representative_title,
    e.event_time,
    e.first_source,
    e.first_published_at,
    e.source_count,
    e.competitor_source_count,
    e.has_odaily,
    e.status,
    e.needs_review,
    COALESCE(f.favorite, false) AS favorite,
    COALESCE(
        jsonb_agg(
            DISTINCT jsonb_build_object(
                'source', s.source,
                'title', i.title,
                'published_at', i.published_at,
                'source_url', i.source_url
            )
        ) FILTER (WHERE s.id IS NOT NULL),
        '[]'::jsonb
    ) AS sources,
    COALESCE(n.note, '') AS note
FROM newsflash_events e
LEFT JOIN newsflash_event_sources s ON s.event_id = e.event_id
LEFT JOIN newsflash_items i ON i.id = s.item_id
LEFT JOIN newsflash_event_favorites f ON f.event_id = e.event_id AND f.favorite = true
LEFT JOIN newsflash_event_notes n ON n.event_id = e.event_id
GROUP BY e.event_id, f.favorite, n.note;

ALTER TABLE newsflash_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_item_notes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS newsflash_items_anon_select ON newsflash_items;
DROP POLICY IF EXISTS newsflash_events_anon_select ON newsflash_events;
DROP POLICY IF EXISTS newsflash_event_sources_anon_select ON newsflash_event_sources;
DROP POLICY IF EXISTS newsflash_event_favorites_anon_all ON newsflash_event_favorites;
DROP POLICY IF EXISTS newsflash_event_notes_anon_all ON newsflash_event_notes;
DROP POLICY IF EXISTS newsflash_item_notes_anon_all ON newsflash_item_notes;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        GRANT USAGE ON SCHEMA public TO anon;
        GRANT SELECT ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary TO anon;
        GRANT SELECT, INSERT, UPDATE, DELETE ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes TO anon;
        GRANT USAGE, SELECT ON SEQUENCE newsflash_event_sources_id_seq TO anon;

        EXECUTE 'CREATE POLICY newsflash_items_anon_select ON newsflash_items FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_events_anon_select ON newsflash_events FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_event_sources_anon_select ON newsflash_event_sources FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_event_favorites_anon_all ON newsflash_event_favorites FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY newsflash_event_notes_anon_all ON newsflash_event_notes FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY newsflash_item_notes_anon_all ON newsflash_item_notes FOR ALL TO anon USING (true) WITH CHECK (true)';
    END IF;
END
$$;
"""
