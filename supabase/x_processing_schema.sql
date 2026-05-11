-- X processing, competitor monitor, and searcher schema.
-- Run through `python backend/src/main.py x-process-init-db` when possible,
-- because the command also seeds prompt templates from docs/*.txt.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_by text;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS locked_until timestamptz;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_tasks_x_status_lock
ON tasks(source, status, locked_until, created_at ASC);

CREATE TABLE IF NOT EXISTS odaily_reference_items (
    source_item_id text PRIMARY KEY,
    source_url text,
    title text,
    content text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_templates (
    template_key text PRIMARY KEY,
    display_name text NOT NULL,
    active_version_id bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS prompt_template_versions (
    id bigserial PRIMARY KEY,
    template_key text NOT NULL REFERENCES prompt_templates(template_key) ON DELETE CASCADE,
    version_number integer NOT NULL,
    content text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    UNIQUE (template_key, version_number)
);

CREATE TABLE IF NOT EXISTS x_task_pipeline (
    task_id bigint PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    news_type text CHECK (news_type IS NULL OR news_type IN ('regular', 'onchain', 'funding')),
    candidate_id bigint,
    judge_model text,
    judge_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt_template_key text REFERENCES prompt_templates(template_key),
    prompt_version_id bigint REFERENCES prompt_template_versions(id),
    writer_model text,
    writer_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    draft_title text,
    draft_content text,
    final_title text,
    final_content text,
    push_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS candidate_id bigint;

CREATE TABLE IF NOT EXISTS search_event_candidates (
    id bigserial PRIMARY KEY,
    primary_task_id bigint REFERENCES tasks(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'active',
    title text,
    content text NOT NULL,
    content_hash text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS search_event_sources (
    id bigserial PRIMARY KEY,
    candidate_id bigint NOT NULL REFERENCES search_event_candidates(id) ON DELETE CASCADE,
    task_id bigint REFERENCES tasks(id) ON DELETE SET NULL,
    source text NOT NULL,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    role text NOT NULL CHECK (role IN ('primary', 'supporting')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (candidate_id, task_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'x_task_pipeline_candidate_fk'
    ) THEN
        ALTER TABLE x_task_pipeline
        ADD CONSTRAINT x_task_pipeline_candidate_fk
        FOREIGN KEY (candidate_id) REFERENCES search_event_candidates(id) ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_news_type ON x_task_pipeline(news_type);
CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_candidate ON x_task_pipeline(candidate_id);
CREATE INDEX IF NOT EXISTS idx_odaily_reference_published ON odaily_reference_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_candidates_status_expires ON search_event_candidates(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_search_candidates_hash ON search_event_candidates(content_hash);
CREATE INDEX IF NOT EXISTS idx_search_sources_candidate ON search_event_sources(candidate_id);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_template ON prompt_template_versions(template_key, version_number DESC);

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

CREATE INDEX IF NOT EXISTS idx_newsflash_items_content_hash ON newsflash_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_newsflash_events_time ON newsflash_events(event_time DESC NULLS LAST, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_newsflash_events_status ON newsflash_events(status, needs_review, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_newsflash_event_sources_event ON newsflash_event_sources(event_id);
CREATE INDEX IF NOT EXISTS idx_newsflash_event_sources_item ON newsflash_event_sources(item_id);

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

CREATE OR REPLACE FUNCTION notify_x_task_queue_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'x_task_queue_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP, 'status', NEW.status)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tasks_x_queue_notify ON tasks;
CREATE TRIGGER trg_tasks_x_queue_notify
AFTER INSERT OR UPDATE OF status ON tasks
FOR EACH ROW
WHEN (NEW.source IN ('x', 'blockbeats', 'panews', 'jinse'))
EXECUTE FUNCTION notify_x_task_queue_changed();

CREATE OR REPLACE FUNCTION notify_prompt_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'prompt_config_changed',
        json_build_object('template_key', NEW.template_key, 'active_version_id', NEW.active_version_id)::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prompt_templates_notify ON prompt_templates;
CREATE TRIGGER trg_prompt_templates_notify
AFTER UPDATE OF active_version_id ON prompt_templates
FOR EACH ROW
WHEN (OLD.active_version_id IS DISTINCT FROM NEW.active_version_id)
EXECUTE FUNCTION notify_prompt_config_changed();

ALTER TABLE prompt_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_template_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_task_pipeline ENABLE ROW LEVEL SECURITY;
ALTER TABLE odaily_reference_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_item_notes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS prompt_templates_anon_all ON prompt_templates;
DROP POLICY IF EXISTS prompt_template_versions_anon_all ON prompt_template_versions;
DROP POLICY IF EXISTS x_task_pipeline_anon_select ON x_task_pipeline;
DROP POLICY IF EXISTS odaily_reference_items_anon_select ON odaily_reference_items;
DROP POLICY IF EXISTS search_event_candidates_anon_select ON search_event_candidates;
DROP POLICY IF EXISTS search_event_sources_anon_select ON search_event_sources;
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
        GRANT SELECT, INSERT, UPDATE ON prompt_templates TO anon;
        GRANT SELECT, INSERT, UPDATE ON prompt_template_versions TO anon;
        GRANT SELECT ON x_task_pipeline, odaily_reference_items, search_event_candidates, search_event_sources TO anon;
        GRANT SELECT ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary TO anon;
        GRANT SELECT, INSERT, UPDATE, DELETE ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes TO anon;
        GRANT USAGE, SELECT ON SEQUENCE prompt_template_versions_id_seq, newsflash_event_sources_id_seq TO anon;

        EXECUTE 'CREATE POLICY prompt_templates_anon_all ON prompt_templates FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY prompt_template_versions_anon_all ON prompt_template_versions FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY x_task_pipeline_anon_select ON x_task_pipeline FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY odaily_reference_items_anon_select ON odaily_reference_items FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY search_event_candidates_anon_select ON search_event_candidates FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY search_event_sources_anon_select ON search_event_sources FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_items_anon_select ON newsflash_items FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_events_anon_select ON newsflash_events FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_event_sources_anon_select ON newsflash_event_sources FOR SELECT TO anon USING (true)';
        EXECUTE 'CREATE POLICY newsflash_event_favorites_anon_all ON newsflash_event_favorites FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY newsflash_event_notes_anon_all ON newsflash_event_notes FOR ALL TO anon USING (true) WITH CHECK (true)';
        EXECUTE 'CREATE POLICY newsflash_item_notes_anon_all ON newsflash_item_notes FOR ALL TO anon USING (true) WITH CHECK (true)';
    END IF;
END
$$;
