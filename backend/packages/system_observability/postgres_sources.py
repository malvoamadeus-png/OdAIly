"""Compact, live business-state sources for the six-hour information feed."""

from .postgres_schema import SourceStateTriggerSpec, ensure_source_state_schema, install_source_state_trigger
from packages.common.pg_native import connect_postgres


def _source(schema: str, table: str, identity: str, *columns: str) -> SourceStateTriggerSpec:
    return SourceStateTriggerSpec(schema, table, (identity,), columns, f"trg_odaily_feed_{table}_v1")


BUSINESS_SOURCES = (
    _source("business_news", "business_message_inbox", "id", "status", "status_reason", "classification", "classification_reason", "writer4_title", "writer2_title"),
    _source("hyperliquid_news", "hl_news_candidates", "event_key", "coin"),
    _source("hyperliquid_news", "hl_news_dispatch", "event_key", "task_id", "status", "error"),
    _source("main", "whale_watch_activities", "id", "summary", "telegram_sent_at"),
    _source("main", "binance_square_attempts", "id", "slug_lower", "status", "error", "finished_at"),
    _source("main", "auditor_checks", "id", "status", "last_error"),
    _source("main", "fact_checks", "id", "status", "reason", "summary", "last_error"),
    _source("main", "fact_check_runs", "id", "check_id", "status", "summary"),
    _source("main", "editor_plugin_generation_logs", "id", "action", "status", "error_message"),
    _source("main", "newsflash_items", "id", "source", "source_item_id", "title"),
    _source("main", "newsflash_events", "event_id", "representative_title", "status", "needs_review"),
    _source("hottopic", "x_agent_analysis_jobs", "id", "tweet_id", "module", "status", "last_error"),
    _source("hottopic", "x_agent_sentiment_results", "id", "tweet_id", "instrument_name", "sentiment", "reason"),
    _source("hottopic", "x_agent_project_observations", "id", "project_name", "ticker"),
    _source("hottopic", "event_tracking_events", "event_id", "title", "status", "end_reason"),
    _source("hottopic", "event_tracking_publication_outbox", "outbox_id", "event_id", "status", "primary_task_id", "last_error"),
    _source("hottopic", "investment_leads", "id", "subject", "status"),
    _source("meme", "jobs", "id", "trigger_key", "status", "reason", "title"),
    _source("meme", "meme_grok_jobs", "trigger_key", "state", "error"),
    _source("gate", "trigger_events", "id", "symbol", "status", "title", "error", "push_status_code"),
    _source("stock", "trigger_events", "id", "symbol", "status", "title", "error"),
    _source("local_pipeline", "local_pipeline_jobs", "id", "task_id", "source", "source_item_id", "status", "last_error"),
)


def ensure_business_source_triggers(*, specs: tuple[SourceStateTriggerSpec, ...] = BUSINESS_SOURCES) -> None:
    """Install once per existing source; absent optional modules can be installed at a later startup."""
    with connect_postgres(schema="main", application_name="odaily-information-feed-sources") as conn:
        ensure_source_state_schema(conn)
        for spec in specs:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"feed-trigger:{spec.schema}.{spec.table}",))
                relation = conn.execute("SELECT to_regclass(%s) AS relation", (f"{spec.schema}.{spec.table}",)).fetchone()["relation"]
                if relation is None:
                    continue
                exists = conn.execute("SELECT 1 FROM pg_trigger WHERE tgrelid=%s::regclass AND tgname=%s AND NOT tgisinternal", (relation, spec.trigger_name)).fetchone()
                if not exists:
                    install_source_state_trigger(conn, spec, operations=("INSERT", "UPDATE"))
