-- X processing, competitor monitor, and searcher schema.
-- Run through `python backend/src/main.py x-process-init-db` when possible,
-- because the command also seeds prompt templates from docs/*.txt.

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
    feature_mode_enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE prompt_templates ADD COLUMN IF NOT EXISTS feature_mode_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS prompt_template_versions (
    id bigserial PRIMARY KEY,
    template_key text NOT NULL REFERENCES prompt_templates(template_key) ON DELETE CASCADE,
    version_number integer NOT NULL,
    content text NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    deleted_at timestamptz,
    UNIQUE (template_key, version_number)
);

ALTER TABLE prompt_template_versions ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

CREATE TABLE IF NOT EXISTS publisher_settings (
    singleton_key text PRIMARY KEY,
    enabled boolean NOT NULL DEFAULT true,
    timezone text NOT NULL DEFAULT 'Asia/Shanghai',
    window_start_local time NOT NULL DEFAULT '00:01',
    window_end_local time NOT NULL DEFAULT '07:30',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE publisher_settings ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true;
ALTER TABLE publisher_settings ADD COLUMN IF NOT EXISTS timezone text NOT NULL DEFAULT 'Asia/Shanghai';
ALTER TABLE publisher_settings ADD COLUMN IF NOT EXISTS window_start_local time NOT NULL DEFAULT '00:01';
ALTER TABLE publisher_settings ADD COLUMN IF NOT EXISTS window_end_local time NOT NULL DEFAULT '07:30';

INSERT INTO publisher_settings (
    singleton_key,
    enabled,
    timezone,
    window_start_local,
    window_end_local
)
VALUES ('global', true, 'Asia/Shanghai', '00:01', '07:30')
ON CONFLICT (singleton_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS publisher_channels (
    channel_key text PRIMARY KEY,
    display_name text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE publisher_channels ADD COLUMN IF NOT EXISTS display_name text;
ALTER TABLE publisher_channels ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT false;

INSERT INTO publisher_channels (channel_key, display_name, enabled)
VALUES
    ('external_media', '外媒', true),
    ('x', 'X', false),
    ('competitor', '竞品', false),
    ('jin10', '金十', false)
ON CONFLICT (channel_key) DO UPDATE
SET display_name = EXCLUDED.display_name,
    updated_at = now();

CREATE TABLE IF NOT EXISTS publisher_rule_config (
    singleton_key text PRIMARY KEY,
    config_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt_text text NOT NULL DEFAULT '',
    updated_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE publisher_rule_config ADD COLUMN IF NOT EXISTS config_json jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE publisher_rule_config ADD COLUMN IF NOT EXISTS prompt_text text NOT NULL DEFAULT '';
ALTER TABLE publisher_rule_config ADD COLUMN IF NOT EXISTS updated_by text;

CREATE TABLE IF NOT EXISTS x_task_pipeline (
    task_id bigint PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    news_type text CHECK (news_type IS NULL OR news_type IN ('regular', 'onchain', 'funding', 'non_mainstream_media', 'ai_source', 'mainstream_media')),
    candidate_id bigint,
    judge_model text,
    judge_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    judge_completed_at timestamptz,
    search_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    search_completed_at timestamptz,
    prompt_template_key text REFERENCES prompt_templates(template_key),
    prompt_version_id bigint REFERENCES prompt_template_versions(id),
    writer_feature_mode_enabled boolean,
    writer_model text,
    writer_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    draft_title text,
    draft_content text,
    write_completed_at timestamptz,
    final_title text,
    final_content text,
    format_completed_at timestamptz,
    publisher_channel text,
    publisher_model text,
    publisher_category text,
    publisher_decision text,
    publisher_reason_code text,
    publisher_output jsonb NOT NULL DEFAULT '{}'::jsonb,
    publisher_decided_at timestamptz,
    publish_completed_at timestamptz,
    push_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS candidate_id bigint;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS judge_completed_at timestamptz;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS search_completed_at timestamptz;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS writer_feature_mode_enabled boolean;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS write_completed_at timestamptz;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS format_completed_at timestamptz;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_channel text;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_model text;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_category text;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_decision text;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_reason_code text;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_output jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publisher_decided_at timestamptz;
ALTER TABLE x_task_pipeline ADD COLUMN IF NOT EXISTS publish_completed_at timestamptz;

ALTER TABLE x_task_pipeline DROP CONSTRAINT IF EXISTS x_task_pipeline_publisher_channel_check;
ALTER TABLE x_task_pipeline
    ADD CONSTRAINT x_task_pipeline_publisher_channel_check
    CHECK (publisher_channel IS NULL OR publisher_channel IN ('external_media', 'x', 'competitor', 'jin10'));

ALTER TABLE x_task_pipeline DROP CONSTRAINT IF EXISTS x_task_pipeline_publisher_category_check;
ALTER TABLE x_task_pipeline
    ADD CONSTRAINT x_task_pipeline_publisher_category_check
    CHECK (
        publisher_category IS NULL
        OR publisher_category IN ('policy_regulation', 'people_view', 'major_project_progress', 'funding', 'other')
    );

ALTER TABLE x_task_pipeline DROP CONSTRAINT IF EXISTS x_task_pipeline_publisher_decision_check;
ALTER TABLE x_task_pipeline
    ADD CONSTRAINT x_task_pipeline_publisher_decision_check
    CHECK (publisher_decision IS NULL OR publisher_decision IN ('auto_publish', 'manual_review', 'failed'));

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

DO $$
BEGIN
    IF current_setting('server_version_num')::integer >= 150000 THEN
        EXECUTE 'ALTER VIEW newsflash_event_summary SET (security_invoker = true)';
    END IF;
END
$$;

DROP TRIGGER IF EXISTS trg_tasks_x_queue_notify ON tasks;
DROP FUNCTION IF EXISTS notify_x_task_queue_changed();

CREATE OR REPLACE FUNCTION notify_prompt_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'prompt_config_changed',
        json_build_object(
            'template_key',
            NEW.template_key,
            'active_version_id',
            NEW.active_version_id,
            'feature_mode_enabled',
            NEW.feature_mode_enabled
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_prompt_templates_notify ON prompt_templates;
CREATE TRIGGER trg_prompt_templates_notify
AFTER UPDATE OF active_version_id, feature_mode_enabled ON prompt_templates
FOR EACH ROW
WHEN (
    OLD.active_version_id IS DISTINCT FROM NEW.active_version_id
    OR OLD.feature_mode_enabled IS DISTINCT FROM NEW.feature_mode_enabled
)
EXECUTE FUNCTION notify_prompt_config_changed();

ALTER TABLE prompt_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompt_template_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE publisher_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE publisher_channels ENABLE ROW LEVEL SECURITY;
ALTER TABLE publisher_rule_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_task_pipeline ENABLE ROW LEVEL SECURITY;
ALTER TABLE odaily_reference_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE search_event_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE auditor_checks ENABLE ROW LEVEL SECURITY;
ALTER TABLE writer3_contexts ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_favorites ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_event_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE newsflash_item_notes ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS prompt_templates_anon_all ON prompt_templates;
DROP POLICY IF EXISTS prompt_template_versions_anon_all ON prompt_template_versions;
DROP POLICY IF EXISTS publisher_settings_anon_all ON publisher_settings;
DROP POLICY IF EXISTS publisher_channels_anon_all ON publisher_channels;
DROP POLICY IF EXISTS publisher_rule_config_anon_all ON publisher_rule_config;
DROP POLICY IF EXISTS x_task_pipeline_anon_select ON x_task_pipeline;
DROP POLICY IF EXISTS odaily_reference_items_anon_select ON odaily_reference_items;
DROP POLICY IF EXISTS search_event_candidates_anon_select ON search_event_candidates;
DROP POLICY IF EXISTS search_event_sources_anon_select ON search_event_sources;
DROP POLICY IF EXISTS auditor_checks_anon_select ON auditor_checks;
DROP POLICY IF EXISTS writer3_contexts_anon_select ON writer3_contexts;
DROP POLICY IF EXISTS newsflash_items_anon_select ON newsflash_items;
DROP POLICY IF EXISTS newsflash_events_anon_select ON newsflash_events;
DROP POLICY IF EXISTS newsflash_event_sources_anon_select ON newsflash_event_sources;
DROP POLICY IF EXISTS newsflash_event_favorites_anon_all ON newsflash_event_favorites;
DROP POLICY IF EXISTS newsflash_event_notes_anon_all ON newsflash_event_notes;
DROP POLICY IF EXISTS newsflash_item_notes_anon_all ON newsflash_item_notes;
DROP POLICY IF EXISTS prompt_templates_console_admin_all ON prompt_templates;
DROP POLICY IF EXISTS prompt_template_versions_console_admin_all ON prompt_template_versions;
DROP POLICY IF EXISTS publisher_settings_console_admin_all ON publisher_settings;
DROP POLICY IF EXISTS publisher_channels_console_admin_all ON publisher_channels;
DROP POLICY IF EXISTS publisher_rule_config_console_admin_all ON publisher_rule_config;
DROP POLICY IF EXISTS x_task_pipeline_console_admin_select ON x_task_pipeline;
DROP POLICY IF EXISTS odaily_reference_items_console_admin_select ON odaily_reference_items;
DROP POLICY IF EXISTS search_event_candidates_console_admin_select ON search_event_candidates;
DROP POLICY IF EXISTS search_event_sources_console_admin_select ON search_event_sources;
DROP POLICY IF EXISTS auditor_checks_console_admin_select ON auditor_checks;
DROP POLICY IF EXISTS writer3_contexts_console_admin_select ON writer3_contexts;
DROP POLICY IF EXISTS newsflash_items_console_admin_select ON newsflash_items;
DROP POLICY IF EXISTS newsflash_events_console_admin_select ON newsflash_events;
DROP POLICY IF EXISTS newsflash_event_sources_console_admin_select ON newsflash_event_sources;
DROP POLICY IF EXISTS newsflash_event_favorites_console_admin_all ON newsflash_event_favorites;
DROP POLICY IF EXISTS newsflash_event_notes_console_admin_all ON newsflash_event_notes;
DROP POLICY IF EXISTS newsflash_item_notes_console_admin_all ON newsflash_item_notes;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON prompt_templates, prompt_template_versions, publisher_settings, publisher_channels, publisher_rule_config, x_task_pipeline, odaily_reference_items, search_event_candidates, search_event_sources, auditor_checks FROM anon;
        REVOKE ALL PRIVILEGES ON writer3_contexts FROM anon;
        REVOKE ALL PRIVILEGES ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary FROM anon;
        REVOKE ALL PRIVILEGES ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE prompt_template_versions_id_seq, newsflash_event_sources_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON prompt_templates TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON prompt_template_versions TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON publisher_settings TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON publisher_channels TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON publisher_rule_config TO authenticated;
        GRANT SELECT ON x_task_pipeline, odaily_reference_items, search_event_candidates, search_event_sources, auditor_checks TO authenticated;
        GRANT SELECT ON writer3_contexts TO authenticated;
        GRANT SELECT ON newsflash_items, newsflash_events, newsflash_event_sources, newsflash_event_summary TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON newsflash_event_favorites, newsflash_event_notes, newsflash_item_notes TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE prompt_template_versions_id_seq, newsflash_event_sources_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY prompt_templates_console_admin_all ON prompt_templates
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY prompt_template_versions_console_admin_all ON prompt_template_versions
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY publisher_settings_console_admin_all ON publisher_settings
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY publisher_channels_console_admin_all ON publisher_channels
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY publisher_rule_config_console_admin_all ON publisher_rule_config
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY x_task_pipeline_console_admin_select ON x_task_pipeline
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY odaily_reference_items_console_admin_select ON odaily_reference_items
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY search_event_candidates_console_admin_select ON search_event_candidates
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY search_event_sources_console_admin_select ON search_event_sources
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY auditor_checks_console_admin_select ON auditor_checks
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY writer3_contexts_console_admin_select ON writer3_contexts
            FOR SELECT TO authenticated USING (is_console_admin())';
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
