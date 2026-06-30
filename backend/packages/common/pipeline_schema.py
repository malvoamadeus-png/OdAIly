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


EDITOR_PLUGIN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS editor_plugin_users (
    email text PRIMARY KEY,
    display_name text,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS editor_plugin_feedbacks (
    id bigserial PRIMARY KEY,
    feed_item_id text NOT NULL,
    feed_kind text NOT NULL CHECK (feed_kind IN ('newsflash', 'auditor_alert', 'external_media_alert', 'writer3_context', 'whale_onchain', 'whale_hyperliquid')),
    feedback text NOT NULL CHECK (feedback IN ('accept', 'reject')),
    actor_user_id uuid,
    actor_email text NOT NULL,
    actor_display_name text,
    acted_at timestamptz NOT NULL DEFAULT now(),
    session_id text,
    extra_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS editor_plugin_receipts (
    id bigserial PRIMARY KEY,
    feed_item_id text NOT NULL,
    feed_kind text NOT NULL CHECK (feed_kind IN ('newsflash', 'auditor_alert', 'external_media_alert', 'writer3_context', 'whale_onchain', 'whale_hyperliquid')),
    viewer_user_id uuid,
    viewer_email text NOT NULL,
    viewer_display_name text,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    seen_count integer NOT NULL DEFAULT 1,
    session_id text,
    extra_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (feed_item_id, feed_kind, viewer_email)
);

CREATE TABLE IF NOT EXISTS editor_plugin_sessions (
    token_hash text PRIMARY KEY,
    user_id uuid,
    email text NOT NULL,
    display_name text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS editor_plugin_generation_logs (
    id bigserial PRIMARY KEY,
    action text NOT NULL CHECK (action IN ('search', 'generate')),
    actor_user_id uuid,
    actor_email text NOT NULL,
    actor_display_name text,
    source_type text NOT NULL,
    platform text NOT NULL,
    post_id text,
    post_url text,
    author_display_name text,
    author_handle text,
    posted_at timestamptz,
    request_text text NOT NULL,
    route text,
    result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('success', 'failed')),
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_feedbacks_actor_created
ON editor_plugin_feedbacks(actor_email, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_feedbacks_feed_created
ON editor_plugin_feedbacks(feed_item_id, feed_kind, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_receipts_viewer_updated
ON editor_plugin_receipts(viewer_email, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_receipts_feed_updated
ON editor_plugin_receipts(feed_item_id, feed_kind, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_sessions_email_expires
ON editor_plugin_sessions(email, expires_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_generation_logs_actor_created
ON editor_plugin_generation_logs(actor_email, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_editor_plugin_generation_logs_action_created
ON editor_plugin_generation_logs(action, created_at DESC);

CREATE OR REPLACE FUNCTION is_editor_plugin_user()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
    SELECT EXISTS (
        SELECT 1
        FROM public.editor_plugin_users
        WHERE enabled = true
          AND lower(email) = lower(COALESCE(auth.jwt() ->> 'email', ''))
    );
$$;

CREATE OR REPLACE FUNCTION editor_plugin_profile()
RETURNS TABLE (
    email text,
    display_name text,
    enabled boolean
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    IF NOT is_editor_plugin_user() THEN
        RETURN;
    END IF;

    RETURN QUERY
    SELECT
        u.email,
        COALESCE(NULLIF(u.display_name, ''), split_part(u.email, '@', 1)) AS display_name,
        u.enabled
    FROM editor_plugin_users u
    WHERE lower(u.email) = lower(COALESCE(auth.jwt() ->> 'email', ''))
      AND u.enabled = true
    LIMIT 1;
END;
$$;

CREATE OR REPLACE FUNCTION editor_plugin_state(p_feed_item_ids text[])
RETURNS TABLE (
    feed_item_id text,
    feed_kind text,
    first_seen_at timestamptz,
    last_seen_at timestamptz,
    seen_count integer,
    latest_feedback text,
    latest_feedback_at timestamptz
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_email text := lower(COALESCE(auth.jwt() ->> 'email', ''));
BEGIN
    IF NOT is_editor_plugin_user() OR p_feed_item_ids IS NULL OR array_length(p_feed_item_ids, 1) IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY
    WITH latest_feedback_rows AS (
        SELECT DISTINCT ON (f.feed_item_id, f.feed_kind)
            f.feed_item_id,
            f.feed_kind,
            f.feedback,
            f.acted_at
        FROM editor_plugin_feedbacks f
        WHERE lower(f.actor_email) = v_email
          AND f.feed_item_id = ANY(p_feed_item_ids)
        ORDER BY f.feed_item_id, f.feed_kind, f.acted_at DESC, f.id DESC
    )
    SELECT
        lf.feed_item_id,
        lf.feed_kind,
        NULL::timestamptz AS first_seen_at,
        NULL::timestamptz AS last_seen_at,
        NULL::integer AS seen_count,
        lf.feedback AS latest_feedback,
        lf.acted_at AS latest_feedback_at
    FROM latest_feedback_rows lf;
END;
$$;

CREATE OR REPLACE FUNCTION editor_plugin_mark_seen(
    p_feed_item_id text,
    p_feed_kind text,
    p_session_id text DEFAULT NULL,
    p_extra_json jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_email text := lower(COALESCE(auth.jwt() ->> 'email', ''));
    v_display_name text;
BEGIN
    IF NOT is_editor_plugin_user() THEN
        RAISE EXCEPTION 'editor_plugin_access_denied';
    END IF;

    IF p_feed_item_id IS NULL OR btrim(p_feed_item_id) = '' THEN
        RAISE EXCEPTION 'editor_plugin_feed_item_id_required';
    END IF;

    RETURN jsonb_build_object('ok', true, 'recorded', false);
END;
$$;

CREATE OR REPLACE FUNCTION editor_plugin_submit_feedback(
    p_feed_item_id text,
    p_feed_kind text,
    p_feedback text,
    p_session_id text DEFAULT NULL,
    p_extra_json jsonb DEFAULT '{}'::jsonb
)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    v_email text := lower(COALESCE(auth.jwt() ->> 'email', ''));
    v_display_name text;
BEGIN
    IF NOT is_editor_plugin_user() THEN
        RAISE EXCEPTION 'editor_plugin_access_denied';
    END IF;

    IF p_feed_item_id IS NULL OR btrim(p_feed_item_id) = '' THEN
        RAISE EXCEPTION 'editor_plugin_feed_item_id_required';
    END IF;

    IF p_feedback NOT IN ('accept', 'reject') THEN
        RAISE EXCEPTION 'editor_plugin_invalid_feedback';
    END IF;

    SELECT COALESCE(NULLIF(display_name, ''), split_part(email, '@', 1))
    INTO v_display_name
    FROM editor_plugin_users
    WHERE lower(email) = v_email
      AND enabled = true
    LIMIT 1;

    INSERT INTO editor_plugin_feedbacks (
        feed_item_id,
        feed_kind,
        feedback,
        actor_user_id,
        actor_email,
        actor_display_name,
        acted_at,
        session_id,
        extra_json
    )
    VALUES (
        btrim(p_feed_item_id),
        p_feed_kind,
        p_feedback,
        auth.uid(),
        v_email,
        v_display_name,
        now(),
        p_session_id,
        COALESCE(p_extra_json, '{}'::jsonb)
    );

    RETURN jsonb_build_object('ok', true);
END;
$$;

CREATE OR REPLACE FUNCTION editor_plugin_feed(p_limit integer DEFAULT 120)
RETURNS TABLE (
    feed_item_id text,
    feed_kind text,
    lane text,
    priority integer,
    title text,
    summary text,
    badges jsonb,
    status_label text,
    status_tone text,
    occurred_at timestamptz,
    source_url text,
    detail_url text,
    action_schema jsonb,
    meta_json jsonb
)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $$
DECLARE
    parts text[] := ARRAY[]::text[];
    sql text;
    v_high_limit integer;
    v_ai_limit integer;
    v_low_limit integer;
    v_high_newsflash_limit integer;
    v_high_auditor_limit integer;
    v_low_writer3_limit integer;
    v_low_whale_limit integer;
    v_source_candidate_limit integer;
BEGIN
    IF NOT is_editor_plugin_user() THEN
        RETURN;
    END IF;

    IF p_limit IS NULL OR p_limit < 1 THEN
        p_limit := 120;
    END IF;

    v_high_limit := GREATEST(1, CEIL(p_limit * 0.6)::integer);
    v_ai_limit := GREATEST(1, CEIL(p_limit * 0.2)::integer);
    v_low_limit := GREATEST(1, p_limit - v_high_limit - v_ai_limit);
    v_high_newsflash_limit := GREATEST(1, CEIL(v_high_limit * 0.45)::integer);
    v_high_auditor_limit := GREATEST(0, v_high_limit - v_high_newsflash_limit);
    IF v_high_limit > 1 AND v_high_auditor_limit = 0 THEN
        v_high_auditor_limit := 1;
        v_high_newsflash_limit := GREATEST(0, v_high_limit - 1);
    END IF;

    v_low_writer3_limit := GREATEST(1, CEIL(v_low_limit * 0.6)::integer);
    v_low_whale_limit := GREATEST(0, v_low_limit - v_low_writer3_limit);
    IF v_low_limit > 1 AND v_low_whale_limit = 0 THEN
        v_low_whale_limit := 1;
        v_low_writer3_limit := GREATEST(0, v_low_limit - 1);
    END IF;
    v_source_candidate_limit := GREATEST(p_limit, 40);

    IF to_regclass('public.x_task_pipeline') IS NOT NULL AND to_regclass('public.tasks') IS NOT NULL THEN
        parts := array_append(parts, $newsflash$
            SELECT *
            FROM (
            SELECT
                'newsflash:' || p.task_id::text AS feed_item_id,
                'newsflash'::text AS feed_kind,
                CASE
                    WHEN t.source = 'ai_source' OR COALESCE(xa.is_ai_source, false) THEN 'ai'
                    ELSE 'high'
                END AS lane,
                CASE WHEN t.status = 'ready_review' THEN 92 ELSE 88 END AS priority,
                COALESCE(p.final_title, t.title, '未命名快讯') AS title,
                COALESCE(p.final_content, t.content) AS summary,
                jsonb_build_array(
                    jsonb_build_object(
                        'label', '来源',
                        'value', CASE t.source
                            WHEN 'x' THEN 'X'
                            WHEN 'non_mainstream_media' THEN '外媒'
                            WHEN 'ai_source' THEN 'AI信源'
                            WHEN 'blockbeats' THEN 'BlockBeats'
                            WHEN 'panews' THEN 'PANews'
                            WHEN 'jinse' THEN '金色财经'
                            ELSE t.source
                        END,
                        'tone', 'neutral'
                    ),
                    CASE
                        WHEN p.news_type IS NOT NULL THEN
                            jsonb_build_object(
                                'label', '领域',
                                'value', CASE p.news_type
                                    WHEN 'onchain' THEN '链上'
                                    WHEN 'funding' THEN '融资'
                                    WHEN 'non_mainstream_media' THEN '外媒'
                                    WHEN 'ai_source' THEN 'AI信源'
                                    WHEN 'mainstream_media' THEN '外媒'
                                    ELSE '常规'
                                END,
                                'tone', 'neutral'
                            )
                        ELSE NULL
                    END,
                    CASE
                        WHEN COALESCE(p.writer_feature_mode_enabled, false) THEN
                            jsonb_build_object('label', '模式', 'value', '特色', 'tone', 'accent')
                        ELSE NULL
                    END
                ) AS badges,
                CASE WHEN t.status = 'ready_review' THEN '挂后台' ELSE '已直发' END AS status_label,
                CASE WHEN t.status = 'ready_review' THEN 'manual' ELSE 'success' END AS status_tone,
                COALESCE(p.publisher_decided_at, t.updated_at, t.created_at) AS occurred_at,
                t.source_url,
                t.source_url AS detail_url,
                jsonb_build_object('type', 'read') AS action_schema,
                jsonb_build_object(
                    'task_id', t.id,
                    'source', t.source,
                    'task_status', t.status,
                    'publisher_decision', p.publisher_decision,
                    'publisher_reason_code', p.publisher_reason_code,
                    'publisher_category', p.publisher_category,
                    'news_type', p.news_type,
                    'x_account_is_ai_source', COALESCE(xa.is_ai_source, false),
                    'feature_mode_enabled', COALESCE(p.writer_feature_mode_enabled, false)
                ) AS meta_json
            FROM x_task_pipeline p
            JOIN tasks t
              ON t.id = p.task_id
            LEFT JOIN x_capture_accounts xa
              ON t.source = 'x'
             AND xa.username_lower = lower(COALESCE(t.metadata ->> 'account_username', t.metadata ->> 'author_username', ''))
            WHERE t.status IN ('auto_published', 'ready_review')
              AND COALESCE(p.final_title, t.title) IS NOT NULL
              AND COALESCE(p.final_content, t.content) <> ''
              AND COALESCE(p.publisher_decided_at, t.updated_at, t.created_at) >= now() - interval '30 minutes'
            ORDER BY COALESCE(p.publisher_decided_at, t.updated_at, t.created_at) DESC, p.task_id DESC
            LIMIT $9
            ) newsflash_candidates
        $newsflash$);
    END IF;

    IF to_regclass('public.auditor_checks') IS NOT NULL THEN
        parts := array_append(parts, $auditor$
            SELECT *
            FROM (
            SELECT
                'auditor:' || a.id::text AS feed_item_id,
                'auditor_alert'::text AS feed_kind,
                'high'::text AS lane,
                100 AS priority,
                COALESCE(a.title, '审核者提醒') AS title,
                COALESCE(NULLIF(a.telegram_text, ''), NULLIF(a.audit_result ->> 'summary', ''), '审核者发现疑似文本问题') AS summary,
                '[]'::jsonb AS badges,
                '审核者'::text AS status_label,
                'warning'::text AS status_tone,
                COALESCE(a.alerted_at, a.updated_at, a.created_at) AS occurred_at,
                a.source_url AS source_url,
                CASE
                    WHEN a.source_item_id ~ '^[0-9]+$' THEN 'https://www.odaily.news/zh-CN/newsflash/' || a.source_item_id
                    ELSE a.source_url
                END AS detail_url,
                jsonb_build_object('type', 'feedback', 'actions', jsonb_build_array('accept', 'reject')) AS action_schema,
                jsonb_build_object(
                    'source_item_id', a.source_item_id,
                    'severity', a.audit_result ->> 'severity',
                    'issues', COALESCE(a.audit_result -> 'issues', '[]'::jsonb),
                    'audit_summary', a.audit_result ->> 'summary'
                ) AS meta_json
            FROM auditor_checks a
            WHERE a.status = 'flagged'
              AND COALESCE(a.alerted_at, a.updated_at, a.created_at) >= now() - interval '30 minutes'
            ORDER BY COALESCE(a.alerted_at, a.updated_at, a.created_at) DESC, a.id DESC
            LIMIT $9
            ) auditor_candidates
        $auditor$);
    END IF;

    IF to_regclass('public.external_media_alert_pipeline') IS NOT NULL AND to_regclass('public.tasks') IS NOT NULL THEN
        parts := array_append(parts, $external_media_alert$
            SELECT *
            FROM (
            SELECT
                'external_media_alert:' || p.task_id::text AS feed_item_id,
                'external_media_alert'::text AS feed_kind,
                'high'::text AS lane,
                86 AS priority,
                COALESCE(NULLIF(t.title, ''), NULLIF(t.content, ''), '外媒标题提醒') AS title,
                COALESCE(NULLIF(t.content, ''), NULLIF(t.title, ''), '外媒标题提醒暂无摘要') AS summary,
                jsonb_build_array(
                    jsonb_build_object(
                        'label', '来源',
                        'value', COALESCE(NULLIF(t.metadata ->> 'site_display_name', ''), '外媒'),
                        'tone', 'neutral'
                    ),
                    jsonb_build_object(
                        'label', '类型',
                        'value', '标题提醒',
                        'tone', 'accent'
                    )
                ) AS badges,
                '标题提醒'::text AS status_label,
                'warning'::text AS status_tone,
                COALESCE(t.updated_at, t.created_at) AS occurred_at,
                t.source_url,
                t.source_url AS detail_url,
                jsonb_build_object('type', 'read') AS action_schema,
                jsonb_build_object(
                    'task_id', t.id,
                    'source', t.source,
                    'task_status', t.status,
                    'site_key', t.metadata ->> 'site_key',
                    'pipeline_mode', t.metadata ->> 'pipeline_mode',
                    'discovery_mode', t.metadata ->> 'discovery_mode'
                ) AS meta_json
            FROM external_media_alert_pipeline p
            JOIN tasks t
              ON t.id = p.task_id
            WHERE t.source IN ('external_media_alert', 'ai_source_alert')
              AND t.status = 'notified'
              AND COALESCE(t.updated_at, t.created_at) >= now() - interval '30 minutes'
            ORDER BY COALESCE(t.updated_at, t.created_at) DESC, p.task_id DESC
            LIMIT $9
            ) external_media_alert_candidates
        $external_media_alert$);
    END IF;

    IF to_regclass('public.writer3_contexts') IS NOT NULL THEN
        parts := array_append(parts, $writer3$
            SELECT *
            FROM (
            SELECT
                'writer3:' || w.id::text AS feed_item_id,
                'writer3_context'::text AS feed_kind,
                'low'::text AS lane,
                64 AS priority,
                COALESCE(w.current_title, '此前消息') AS title,
                CASE
                    WHEN NULLIF(w.current_content, '') IS NOT NULL AND NULLIF(w.context_text, '') IS NOT NULL THEN
                        '原文：' || w.current_content || E'\n\n此前消息：' || w.context_text
                    WHEN NULLIF(w.current_content, '') IS NOT NULL THEN
                        '原文：' || w.current_content
                    WHEN NULLIF(w.context_text, '') IS NOT NULL THEN
                        '此前消息：' || w.context_text
                    ELSE COALESCE(NULLIF(w.telegram_text, ''), '此前消息暂无摘要')
                END AS summary,
                '[]'::jsonb AS badges,
                '此前消息'::text AS status_label,
                'info'::text AS status_tone,
                COALESCE(w.sent_at, w.updated_at, w.created_at) AS occurred_at,
                w.current_source_url AS source_url,
                CASE
                    WHEN w.current_source_item_id ~ '^[0-9]+$' THEN 'https://www.odaily.news/zh-CN/newsflash/' || w.current_source_item_id
                    ELSE w.current_source_url
                END AS detail_url,
                jsonb_build_object('type', 'feedback', 'actions', jsonb_build_array('accept', 'reject')) AS action_schema,
                jsonb_build_object(
                    'context_id', w.id,
                    'current_source', w.current_source,
                    'current_source_item_id', w.current_source_item_id,
                    'evidence_source_item_ids', COALESCE(to_jsonb(w.evidence_source_item_ids), '[]'::jsonb)
                ) AS meta_json
            FROM writer3_contexts w
            WHERE w.status = 'sent'
              AND COALESCE(w.sent_at, w.updated_at, w.created_at) >= now() - interval '30 minutes'
            ORDER BY COALESCE(w.sent_at, w.updated_at, w.created_at) DESC, w.id DESC
            LIMIT $9
            ) writer3_candidates
        $writer3$);
    END IF;

    IF to_regclass('public.whale_watch_activities') IS NOT NULL AND to_regclass('public.whale_watch_addresses') IS NOT NULL THEN
        parts := array_append(parts, $whale$
            SELECT *
            FROM (
            SELECT
                'whale_onchain:' || a.id::text AS feed_item_id,
                'whale_onchain'::text AS feed_kind,
                'low'::text AS lane,
                52 AS priority,
                COALESCE(NULLIF(addr.label, ''), left(addr.address, 6) || '...' || right(addr.address, 4)) AS title,
                COALESCE(NULLIF(a.summary, ''), NULLIF(a.telegram_text, ''), '链上巨鲸信号') AS summary,
                '[]'::jsonb AS badges,
                '新信号'::text AS status_label,
                'info'::text AS status_tone,
                a.created_at AS occurred_at,
                a.tx_url AS source_url,
                a.tx_url AS detail_url,
                jsonb_build_object('type', 'read') AS action_schema,
                jsonb_build_object(
                    'address', addr.address,
                    'address_label', addr.label,
                    'chain_key', a.chain_key,
                    'activity_type', a.activity_type,
                    'direction', a.direction
                ) AS meta_json
            FROM whale_watch_activities a
            JOIN whale_watch_addresses addr
              ON addr.id = a.address_id
            WHERE a.created_at >= now() - interval '30 minutes'
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT $9
            ) whale_candidates
        $whale$);
    END IF;

    IF to_regclass('public.whale_watch_hyperliquid_activities') IS NOT NULL AND to_regclass('public.whale_watch_hyperliquid_addresses') IS NOT NULL THEN
        parts := array_append(parts, $hyper$
            SELECT *
            FROM (
            SELECT
                'whale_hyperliquid:' || a.id::text AS feed_item_id,
                'whale_hyperliquid'::text AS feed_kind,
                'low'::text AS lane,
                48 AS priority,
                COALESCE(NULLIF(addr.label, ''), left(addr.address, 6) || '...' || right(addr.address, 4)) AS title,
                COALESCE(NULLIF(a.summary, ''), NULLIF(a.telegram_text, ''), 'Hyperliquid 巨鲸信号') AS summary,
                '[]'::jsonb AS badges,
                '新信号'::text AS status_label,
                'info'::text AS status_tone,
                a.created_at AS occurred_at,
                a.tx_url AS source_url,
                a.tx_url AS detail_url,
                jsonb_build_object('type', 'read') AS action_schema,
                jsonb_build_object(
                    'address', addr.address,
                    'address_label', addr.label,
                    'coin', a.coin,
                    'direction', a.direction,
                    'notional_usd', a.notional_usd,
                    'alert_kind', a.alert_kind
                ) AS meta_json
            FROM whale_watch_hyperliquid_activities a
            JOIN whale_watch_hyperliquid_addresses addr
              ON addr.id = a.address_id
            WHERE a.created_at >= now() - interval '30 minutes'
            ORDER BY a.created_at DESC, a.id DESC
            LIMIT $9
            ) hyper_candidates
        $hyper$);
    END IF;

    IF array_length(parts, 1) IS NULL THEN
        RETURN;
    END IF;

    sql := 'WITH combined AS (' || array_to_string(parts, ' UNION ALL ') || '),
        tagged AS (
            SELECT
                combined.*,
                CASE
                    WHEN lane = ''high'' AND feed_kind = ''newsflash'' THEN ''high_newsflash''
                    WHEN lane = ''high'' AND feed_kind = ''auditor_alert'' THEN ''high_auditor''
                    WHEN lane = ''high'' AND feed_kind = ''external_media_alert'' THEN ''high_external_media_alert''
                    WHEN lane = ''ai'' AND feed_kind = ''newsflash'' THEN ''ai_newsflash''
                    WHEN lane = ''low'' AND feed_kind = ''writer3_context'' THEN ''low_writer3''
                    WHEN lane = ''low'' AND feed_kind IN (''whale_onchain'', ''whale_hyperliquid'') THEN ''low_whale''
                    ELSE lane || '':'' || feed_kind
                END AS quota_group
            FROM combined
        ),
        ranked AS (
            SELECT
                tagged.*,
                ROW_NUMBER() OVER (
                    PARTITION BY lane
                    ORDER BY priority DESC, occurred_at DESC
                ) AS lane_rank,
                ROW_NUMBER() OVER (
                    PARTITION BY quota_group
                    ORDER BY priority DESC, occurred_at DESC
                ) AS group_rank
            FROM tagged
        ),
        high_reserved AS (
            SELECT *
            FROM ranked
            WHERE lane = ''high''
              AND (
                (quota_group = ''high_auditor'' AND group_rank <= $4)
                OR (quota_group = ''high_newsflash'' AND group_rank <= $5)
                OR (quota_group = ''high_external_media_alert'' AND group_rank <= GREATEST(1, CEIL($1 * 0.15)::integer))
              )
        ),
        low_reserved AS (
            SELECT *
            FROM ranked
            WHERE lane = ''low''
              AND (
                (quota_group = ''low_writer3'' AND group_rank <= $6)
                OR (quota_group = ''low_whale'' AND group_rank <= $7)
              )
        ),
        high_fill AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                quota_group,
                lane_rank,
                group_rank
            FROM (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        ORDER BY priority DESC, occurred_at DESC
                    ) AS fill_rank
                FROM ranked r
                WHERE r.lane = ''high''
                  AND NOT EXISTS (
                    SELECT 1
                    FROM high_reserved h
                    WHERE h.feed_item_id = r.feed_item_id
                      AND h.feed_kind = r.feed_kind
                  )
            ) fill
            WHERE fill_rank <= GREATEST(0, $1 - (SELECT COUNT(*) FROM high_reserved))
        ),
        low_fill AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                quota_group,
                lane_rank,
                group_rank
            FROM (
                SELECT
                    r.*,
                    ROW_NUMBER() OVER (
                        ORDER BY priority DESC, occurred_at DESC
                    ) AS fill_rank
                FROM ranked r
                WHERE r.lane = ''low''
                  AND NOT EXISTS (
                    SELECT 1
                    FROM low_reserved h
                    WHERE h.feed_item_id = r.feed_item_id
                      AND h.feed_kind = r.feed_kind
                  )
            ) fill
            WHERE fill_rank <= GREATEST(0, $3 - (SELECT COUNT(*) FROM low_reserved))
        ),
        selected_ai AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                quota_group,
                lane_rank,
                group_rank
            FROM ranked
            WHERE lane = ''ai''
              AND lane_rank <= $2
        ),
        selected_high AS (
            SELECT * FROM high_reserved
            UNION ALL
            SELECT * FROM high_fill
        ),
        selected_low AS (
            SELECT * FROM low_reserved
            UNION ALL
            SELECT * FROM low_fill
        ),
        ordered_high AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                ROW_NUMBER() OVER (
                    ORDER BY group_rank, priority DESC, occurred_at DESC
                ) AS display_rank
            FROM selected_high
        ),
        ordered_ai AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                ROW_NUMBER() OVER (
                    ORDER BY priority DESC, occurred_at DESC
                ) AS display_rank
            FROM selected_ai
        ),
        ordered_low AS (
            SELECT
                feed_item_id,
                feed_kind,
                lane,
                priority,
                title,
                summary,
                badges,
                status_label,
                status_tone,
                occurred_at,
                source_url,
                detail_url,
                action_schema,
                meta_json,
                ROW_NUMBER() OVER (
                    ORDER BY group_rank, priority DESC, occurred_at DESC
                ) AS display_rank
            FROM selected_low
        ),
        selected AS (
            SELECT * FROM ordered_high
            UNION ALL
            SELECT * FROM ordered_ai
            UNION ALL
            SELECT * FROM ordered_low
        )
        SELECT
            feed_item_id,
            feed_kind,
            lane,
            priority,
            title,
            summary,
            badges,
            status_label,
            status_tone,
            occurred_at,
            source_url,
            detail_url,
            action_schema,
            meta_json
        FROM selected
        ORDER BY
            CASE
                WHEN lane = ''high'' THEN 0
                WHEN lane = ''ai'' THEN 1
                ELSE 2
            END,
            display_rank
        LIMIT $8';

    RETURN QUERY EXECUTE sql USING
        v_high_limit,
        v_ai_limit,
        v_low_limit,
        v_high_auditor_limit,
        v_high_newsflash_limit,
        v_low_writer3_limit,
        v_low_whale_limit,
        p_limit,
        v_source_candidate_limit;
END;
$$;

ALTER TABLE editor_plugin_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE editor_plugin_feedbacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE editor_plugin_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE editor_plugin_generation_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS editor_plugin_users_self_select ON editor_plugin_users;
DROP POLICY IF EXISTS editor_plugin_feedbacks_self_select ON editor_plugin_feedbacks;
DROP POLICY IF EXISTS editor_plugin_feedbacks_self_insert ON editor_plugin_feedbacks;
DROP POLICY IF EXISTS editor_plugin_receipts_self_select ON editor_plugin_receipts;
DROP POLICY IF EXISTS editor_plugin_receipts_self_insert ON editor_plugin_receipts;
DROP POLICY IF EXISTS editor_plugin_receipts_self_update ON editor_plugin_receipts;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON editor_plugin_users, editor_plugin_feedbacks, editor_plugin_receipts, editor_plugin_sessions, editor_plugin_generation_logs FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE editor_plugin_feedbacks_id_seq, editor_plugin_receipts_id_seq, editor_plugin_generation_logs_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT ON editor_plugin_users TO authenticated;
        GRANT SELECT, INSERT ON editor_plugin_feedbacks TO authenticated;
        GRANT SELECT, INSERT, UPDATE ON editor_plugin_receipts TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE editor_plugin_feedbacks_id_seq, editor_plugin_receipts_id_seq TO authenticated;
        GRANT EXECUTE ON FUNCTION editor_plugin_profile() TO authenticated;
        GRANT EXECUTE ON FUNCTION editor_plugin_state(text[]) TO authenticated;
        GRANT EXECUTE ON FUNCTION editor_plugin_mark_seen(text, text, text, jsonb) TO authenticated;
        GRANT EXECUTE ON FUNCTION editor_plugin_submit_feedback(text, text, text, text, jsonb) TO authenticated;
        GRANT EXECUTE ON FUNCTION editor_plugin_feed(integer) TO authenticated;

        EXECUTE 'CREATE POLICY editor_plugin_users_self_select ON editor_plugin_users
            FOR SELECT TO authenticated
            USING (lower(email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
        EXECUTE 'CREATE POLICY editor_plugin_feedbacks_self_select ON editor_plugin_feedbacks
            FOR SELECT TO authenticated
            USING (lower(actor_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
        EXECUTE 'CREATE POLICY editor_plugin_feedbacks_self_insert ON editor_plugin_feedbacks
            FOR INSERT TO authenticated
            WITH CHECK (lower(actor_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
        EXECUTE 'CREATE POLICY editor_plugin_receipts_self_select ON editor_plugin_receipts
            FOR SELECT TO authenticated
            USING (lower(viewer_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
        EXECUTE 'CREATE POLICY editor_plugin_receipts_self_insert ON editor_plugin_receipts
            FOR INSERT TO authenticated
            WITH CHECK (lower(viewer_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
        EXECUTE 'CREATE POLICY editor_plugin_receipts_self_update ON editor_plugin_receipts
            FOR UPDATE TO authenticated
            USING (lower(viewer_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))
            WITH CHECK (lower(viewer_email) = lower(COALESCE(auth.jwt() ->> ''email'', '''')))';
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
