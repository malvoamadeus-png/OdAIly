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


CONSOLE_AUTH_SCHEMA_SQL = """
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
DROP POLICY IF EXISTS competitor_filter_keywords_console_admin_all ON competitor_filter_keywords;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON competitor_filter_keywords FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE competitor_filter_keywords_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON competitor_filter_keywords TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE competitor_filter_keywords_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY competitor_filter_keywords_console_admin_all ON competitor_filter_keywords
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
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

CREATE OR REPLACE FUNCTION assert_newsflash_event_has_source()
RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM newsflash_events e WHERE e.event_id = NEW.event_id)
       AND NOT EXISTS (
           SELECT 1
           FROM newsflash_event_sources s
           WHERE s.event_id = NEW.event_id
       ) THEN
        RAISE EXCEPTION 'newsflash event % has no linked source item', NEW.event_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION prune_empty_newsflash_event_after_source_change()
RETURNS trigger AS $$
BEGIN
    DELETE FROM newsflash_events e
    WHERE e.event_id = OLD.event_id
      AND NOT EXISTS (
          SELECT 1
          FROM newsflash_event_sources s
          WHERE s.event_id = e.event_id
      );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_newsflash_event_requires_source ON newsflash_events;
CREATE CONSTRAINT TRIGGER trg_newsflash_event_requires_source
AFTER INSERT OR UPDATE ON newsflash_events
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION assert_newsflash_event_has_source();

DROP TRIGGER IF EXISTS trg_newsflash_event_prune_empty_after_source_change ON newsflash_event_sources;
CREATE CONSTRAINT TRIGGER trg_newsflash_event_prune_empty_after_source_change
AFTER DELETE OR UPDATE OF event_id ON newsflash_event_sources
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION prune_empty_newsflash_event_after_source_change();

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
JOIN newsflash_event_sources s ON s.event_id = e.event_id
JOIN newsflash_items i ON i.id = s.item_id
LEFT JOIN newsflash_event_favorites f ON f.event_id = e.event_id AND f.favorite = true
LEFT JOIN newsflash_event_notes n ON n.event_id = e.event_id
GROUP BY e.event_id, f.favorite, n.note;

DO $$
BEGIN
    IF current_setting('server_version_num')::integer >= 150000 THEN
        EXECUTE 'ALTER VIEW newsflash_event_summary SET (security_invoker = true)';
    END IF;
END
$$;

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
DROP POLICY IF EXISTS newsflash_items_console_admin_select ON newsflash_items;
DROP POLICY IF EXISTS newsflash_events_console_admin_select ON newsflash_events;
DROP POLICY IF EXISTS newsflash_event_sources_console_admin_select ON newsflash_event_sources;
DROP POLICY IF EXISTS newsflash_event_favorites_console_admin_all ON newsflash_event_favorites;
DROP POLICY IF EXISTS newsflash_event_notes_console_admin_all ON newsflash_event_notes;
DROP POLICY IF EXISTS newsflash_item_notes_console_admin_all ON newsflash_item_notes;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary FROM anon;
        REVOKE ALL PRIVILEGES ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE newsflash_event_sources_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE newsflash_event_sources_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY newsflash_items_console_admin_select ON newsflash_items
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY newsflash_events_console_admin_select ON newsflash_events
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY newsflash_event_sources_console_admin_select ON newsflash_event_sources
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY newsflash_event_favorites_console_admin_all ON newsflash_event_favorites
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY newsflash_event_notes_console_admin_all ON newsflash_event_notes
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY newsflash_item_notes_console_admin_all ON newsflash_item_notes
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
    END IF;
END
$$;
"""


WRITER3_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS writer3_contexts (
    id bigserial PRIMARY KEY,
    task_id bigint REFERENCES tasks(id) ON DELETE SET NULL,
    current_source text,
    current_source_item_id text,
    current_source_url text,
    current_title text,
    current_content text,
    current_published_at timestamptz,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'skipped', 'sent', 'failed')),
    locked_by text,
    locked_until timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    analysis_model text,
    writer_model text,
    writer_reasoning_effort text,
    analysis_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    candidates jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_text text,
    evidence_source_item_ids text[] NOT NULL DEFAULT ARRAY[]::text[],
    telegram_text text,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    skip_reason text,
    last_error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'writer3_contexts'
          AND constraint_type = 'PRIMARY KEY'
          AND constraint_name = 'writer3_contexts_pkey'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.key_column_usage
        WHERE table_name = 'writer3_contexts'
          AND constraint_name = 'writer3_contexts_pkey'
          AND column_name = 'task_id'
    ) THEN
        ALTER TABLE writer3_contexts DROP CONSTRAINT writer3_contexts_pkey;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'writer3_contexts'
          AND column_name = 'id'
    ) THEN
        ALTER TABLE writer3_contexts ADD COLUMN id bigserial;
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'writer3_contexts'
          AND constraint_type = 'PRIMARY KEY'
          AND constraint_name = 'writer3_contexts_pkey'
    ) THEN
        ALTER TABLE writer3_contexts ADD PRIMARY KEY (id);
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'writer3_contexts'
          AND column_name = 'task_id'
          AND is_nullable = 'NO'
    ) THEN
        ALTER TABLE writer3_contexts ALTER COLUMN task_id DROP NOT NULL;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_source') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_source text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_source_item_id') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_source_item_id text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_source_url') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_source_url text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_title') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_title text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_content') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_content text;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'writer3_contexts' AND column_name = 'current_published_at') THEN
        ALTER TABLE writer3_contexts ADD COLUMN current_published_at timestamptz;
    END IF;

    UPDATE writer3_contexts
    SET current_source = COALESCE(current_source, 'task'),
        current_source_item_id = COALESCE(current_source_item_id, task_id::text)
    WHERE current_source IS NULL
       OR current_source_item_id IS NULL;

    ALTER TABLE writer3_contexts ALTER COLUMN current_source SET NOT NULL;
    ALTER TABLE writer3_contexts ALTER COLUMN current_source_item_id SET NOT NULL;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS writer3_contexts_current_source_item_key
ON writer3_contexts(current_source, current_source_item_id);

CREATE INDEX IF NOT EXISTS idx_writer3_contexts_status_lock
ON writer3_contexts(status, locked_until, updated_at ASC);

CREATE INDEX IF NOT EXISTS idx_writer3_contexts_sent_at
ON writer3_contexts(sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_writer3_contexts_current_published
ON writer3_contexts(current_published_at DESC NULLS LAST);

ALTER TABLE writer3_contexts ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS writer3_contexts_anon_select ON writer3_contexts;
DROP POLICY IF EXISTS writer3_contexts_console_admin_select ON writer3_contexts;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON writer3_contexts FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT ON writer3_contexts TO authenticated;
        EXECUTE 'CREATE POLICY writer3_contexts_console_admin_select ON writer3_contexts
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
"""


AUDITOR_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auditor_checks (
    id bigserial PRIMARY KEY,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    content_hash text NOT NULL,
    published_at timestamptz,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'passed', 'flagged', 'failed', 'skipped')),
    locked_by text,
    locked_until timestamptz,
    attempt_count integer NOT NULL DEFAULT 0,
    model text,
    prompt_version text NOT NULL,
    raw_output text,
    audit_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_text text,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    alerted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_item_id, content_hash, prompt_version)
);

CREATE INDEX IF NOT EXISTS idx_auditor_checks_status_lock
ON auditor_checks(status, locked_until, updated_at ASC);

CREATE INDEX IF NOT EXISTS idx_auditor_checks_source_item
ON auditor_checks(source_item_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_auditor_checks_published
ON auditor_checks(published_at DESC NULLS LAST);

ALTER TABLE auditor_checks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS auditor_checks_anon_select ON auditor_checks;
DROP POLICY IF EXISTS auditor_checks_console_admin_select ON auditor_checks;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON auditor_checks FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT ON auditor_checks TO authenticated;
        EXECUTE 'CREATE POLICY auditor_checks_console_admin_select ON auditor_checks
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
"""
