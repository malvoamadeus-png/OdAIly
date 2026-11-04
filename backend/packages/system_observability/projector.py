from __future__ import annotations

import gzip
import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly
from .registry import SOURCE_TO_BUSINESS
from .store import SystemInformationStore, timestamp_ms
from .news import duplicate_target, json_object, news_title, task_admission
from .error_policy import failure_category


STATUS_LABELS = {
    'judging': ('价值判断中', 'processing'), 'deduping': ('查重中', 'processing'),
    'writing': ('写作中', 'processing'), 'formatting': ('稿件整理中', 'processing'), 'publishing': ('投递中', 'processing'),
    "pending": ("已收集", "processing"), "judged": ("价值判断完成", "processing"),
    "searched": ("查重完成", "processing"), "deduped": ("已通过查重及前置判断", "processing"),
    "duplicate": ("发现重复，停止写作", "stopped"), "discarded": ("判断后停止", "stopped"),
    "written": ("已生成草稿", "processing"), "formatted": ("已整理稿件", "processing"),
    "publisher_pending": ("等待投递", "processing"), "ready_review": ("挂后台", "backoffice"),
    "auto_published": ("已直发", "direct_publish"), "published": ("已发布", "direct_publish"),
}

ADDITIONAL_PROJECTION_INTERVAL_MS = 15_000
ADDITIONAL_TAIL_RECHECK_INTERVAL_MS = 5 * 60_000
ADDITIONAL_TAIL_RECHECK_LIMIT = 25
SOURCE_OUTBOX_RETENTION_MS = 24 * 60 * 60_000
SOURCE_OUTBOX_CLEANUP_INTERVAL_MS = 5 * 60_000
SOURCE_OUTBOX_CLEANUP_BATCH_SIZE = 5_000
SOURCE_AUDIT_TRIGGER_VERSION = 5
RETENTION_ONLY_DELETE_TABLES = frozenset({"x_capture_attempts"})
NON_SEMANTIC_AUDIT_COLUMNS = frozenset({
    "updated_at", "last_seen_at", "last_polled_at", "last_checked_at", "heartbeat_at",
    "last_attempt_at", "last_success_at", "polled_at", "checked_at",
})


@dataclass(frozen=True, slots=True)
class TableSpec:
    database_key: str
    table: str
    business_key: str
    source: str
    record_kind: str = "business"


TABLE_SPECS = (
    TableSpec("main", "x_capture_settings", "x", "x_capture", "configuration"),
    TableSpec("main", "x_capture_accounts", "x", "x_capture", "configuration"),
    TableSpec("main", "binance_square_settings", "binance_square", "binance_square", "configuration"),
    TableSpec("main", "binance_square_accounts", "binance_square", "binance_square", "configuration"),
    TableSpec("main", "jin10_settings", "jin10", "jin10", "configuration"),
    TableSpec("main", "non_mainstream_media_settings", "crypto_media", "non_mainstream_media", "configuration"),
    TableSpec("main", "non_mainstream_media_sources", "crypto_media", "non_mainstream_media", "configuration"),
    TableSpec("main", "whale_watch_addresses", "whale", "whale_watch", "configuration"),
    TableSpec("main", "whale_watch_hyperliquid_settings", "whale", "whale_watch_hyperliquid", "configuration"),
    TableSpec("main", "whale_watch_hyperliquid_addresses", "whale", "whale_watch_hyperliquid", "configuration"),
    TableSpec("hyperliquid", "hl_news_addresses", "hyperliquid_news", "hyperliquid_news", "configuration"),
    TableSpec("main", "prompt_templates", "x", "x_capture", "configuration"),
    TableSpec("main", "prompt_template_versions", "x", "x_capture", "configuration"),
    TableSpec("main", "publisher_settings", "x", "x_capture", "configuration"),
    TableSpec("main", "publisher_channels", "x", "x_capture", "configuration"),
    TableSpec("main", "publisher_rule_config", "x", "x_capture", "configuration"),
    TableSpec("main", "source_exclusion_rule_groups", "x", "x_capture", "configuration"),
    TableSpec("gate", "settings", "gate_market", "gate_market", "configuration"),
    TableSpec("gate", "symbol_config", "gate_market", "gate_market", "configuration"),
    TableSpec("gate", "templates", "gate_market", "gate_market", "configuration"),
    TableSpec("stock", "instruments", "stock_market", "stock_market", "configuration"),
    TableSpec("stock", "settings", "stock_market", "stock_market", "configuration"),
    TableSpec("main", "tasks", "unmapped_source", "main_pipeline"),
    TableSpec("main", "business_message_inbox", "business_news", "business_telegram"),
    TableSpec("main", "whale_watch_activities", "whale", "whale_watch"),
    TableSpec("main", "whale_watch_hyperliquid_activities", "whale", "whale_watch_hyperliquid"),
    TableSpec("hyperliquid", "hl_news_candidates", "hyperliquid_news", "hyperliquid_news"),
    TableSpec("hyperliquid", "hl_news_dispatch", "hyperliquid_news", "hyperliquid_news"),
    TableSpec("main", "x_capture_attempts", "x", "x_capture"),
    TableSpec("main", "binance_square_attempts", "binance_square", "binance_square"),
    TableSpec("main", "auditor_checks", "post_publish_review", "text_audit"),
    TableSpec("main", "fact_checks", "post_publish_review", "fact_audit"),
    TableSpec("main", "fact_check_runs", "post_publish_review", "fact_audit"),
    TableSpec("main", "editor_plugin_generation_logs", "editor_plugin", "editor_plugin"),
    TableSpec("main", "newsflash_items", "crypto_media", "competitor_news"),
    TableSpec("main", "newsflash_events", "crypto_media", "competitor_event"),
    TableSpec("main", "newsflash_event_sources", "crypto_media", "competitor_event_source"),
    TableSpec("hot", "hottopic_events", "x_agent", "hot_topic"),
    TableSpec("hot", "x_agent_analysis_jobs", "x_agent", "x_agent_analysis"),
    TableSpec("hot", "x_agent_sentiment_results", "x_agent", "market_sentiment"),
    TableSpec("hot", "x_agent_project_observations", "x_agent", "project_promotion"),
    TableSpec("hot", "event_tracking_events", "x_agent", "event_tracking"),
    TableSpec("hot", "event_tracking_publication_outbox", "x_agent", "auto_newsflash"),
    TableSpec("hot", "investment_leads", "x_agent", "investment_leads"),
    TableSpec("meme", "jobs", "meme", "meme_scanner"),
    TableSpec("meme", "meme_grok_jobs", "meme", "meme_grok"),
    TableSpec("gate", "trigger_events", "gate_market", "gate_market"),
    TableSpec("stock", "trigger_events", "stock_market", "stock_market"),
    TableSpec("queue", "local_pipeline_jobs", "unmapped_source", "local_pipeline"),
)

AUDITED_TABLES = frozenset({
    "x_capture_settings", "x_capture_accounts", "binance_square_settings", "binance_square_accounts",
    "jin10_settings", "non_mainstream_media_settings", "non_mainstream_media_sources",
    "whale_watch_addresses", "whale_watch_hyperliquid_settings", "whale_watch_hyperliquid_addresses",
    "hl_news_addresses", "prompt_templates", "prompt_template_versions", "publisher_settings", "publisher_channels",
    "publisher_rule_config", "source_exclusion_rule_groups", "settings",
    "symbol_config", "templates", "instruments",
    "tasks", "x_capture_attempts", "binance_square_attempts", "business_message_inbox",
    "hl_news_candidates", "hl_news_dispatch", "auditor_checks", "fact_checks", "fact_check_runs",
    "editor_plugin_generation_logs", "newsflash_items", "newsflash_events", "newsflash_event_sources",
    "hottopic_events", "x_agent_analysis_jobs", "x_agent_sentiment_results",
    "x_agent_project_observations", "event_tracking_events",
    "event_tracking_publication_outbox", "investment_leads", "jobs",
    "meme_grok_jobs", "trigger_events", "local_pipeline_jobs",
})

CONFIG_AUDIT_COLUMN_NAMES = frozenset({
    "id", "singleton_key", "username", "username_lower", "slug", "slug_lower", "site_key",
    "display_name", "enabled", "is_ai_source", "interval_seconds", "global_interval_seconds",
    "jitter_seconds", "max_concurrency", "config_version", "version_number", "active_version_id",
    "feature_mode_enabled", "channel", "template_key", "group_name", "rule_type", "source_group",
    "pipeline_mode", "discovery_mode", "capture_method", "address", "kind", "market", "timezone",
    "window_start_local", "window_end_local", "auto_publish_enabled", "force_backoffice",
    "publish_mode", "threshold_percent", "merge_window_seconds", "symbol", "enabled_at", "channel_key",
    "key", "value", "label", "threshold", "price_precision", "unit", "active", "rule_key",
    "description", "scopes", "terms", "match_target", "title_template", "body_template",
})

CONFIGURATION_TABLES = frozenset(spec.table for spec in TABLE_SPECS if spec.record_kind == "configuration")

AUDIT_COLUMN_NAMES = frozenset({
    "id", "source", "source_item_id", "event_id", "event_key", "identity_key", "check_id",
    "tweet_id", "task_id", "update_id", "attempt_id", "tx_hash", "fill_key",
    "trigger_key", "status", "state", "result", "decision", "stage", "kind",
    "type", "title", "name", "symbol", "address", "contract_address", "reason",
    "status_reason", "classification_reason", "summary", "error", "last_error",
    "error_message", "error_code", "failure_reason", "attempts", "retry_count",
    "created_at", "updated_at", "started_at", "finished_at", "completed_at",
    "received_at", "sent_at", "published_at", "next_attempt_at", "first_seen_at",
    "last_seen_at", "decision_status", "success", "is_preview", "run_id",
    "reason_code", "publisher_reason_code", "priority_status", "http_status",
    "status_code", "push_status_code", "response_code", "receipt_id", "post_id",
    "message_id", "official_id", "result_json", "push_result", "push_response",
    "publish_result", "submission_result", "error_summary",
})


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SourceAuditInstaller:
    """Install compact SQLite triggers so short-lived status transitions survive polling gaps."""

    def install(self, database_path: Path, specs: tuple[TableSpec, ...]) -> dict[str, int]:
        installed = skipped = 0
        with sqlite3.connect(database_path, timeout=15, factory=ClosingSQLiteConnection) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS odaily_observability_outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    table_name TEXT NOT NULL,
                    source_row_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    happened_at_ms INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_odaily_observability_outbox_time ON odaily_observability_outbox(happened_at_ms)")
            tables = sorted({spec.table for spec in specs if spec.table in AUDITED_TABLES})
            database_key = specs[0].database_key if specs else ""
            conn.execute("""
                CREATE TABLE IF NOT EXISTS odaily_observability_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                )
            """)
            version_key = f"source_audit_trigger_version:{database_key}"
            version_row = conn.execute(
                "SELECT meta_value FROM odaily_observability_meta WHERE meta_key=?", (version_key,),
            ).fetchone()
            current_version = str(version_row["meta_value"]) if version_row else ""
            if current_version != str(SOURCE_AUDIT_TRIGGER_VERSION):
                for table in tables:
                    prefix = "odaily_obs_" + re.sub(r"[^a-zA-Z0-9_]", "_", table)
                    for operation in ("insert", "update", "delete"):
                        conn.execute(f"DROP TRIGGER IF EXISTS {_quote_identifier(f'{prefix}_{operation}')}")
            for table in tables:
                table_sql = _quote_identifier(table)
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
                if not exists:
                    skipped += 1
                    continue
                columns = [str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table_sql})")]
                allowed_columns = CONFIG_AUDIT_COLUMN_NAMES if table in CONFIGURATION_TABLES else AUDIT_COLUMN_NAMES
                selected = [name for name in columns if name.casefold() in allowed_columns and not re.search(r"secret|token|password|cookie|authorization|api.?key|credential", name, re.I)]
                if table == "settings":
                    selected = [name for name in selected if name.casefold() in {"key", "value"}]
                if not selected:
                    skipped += 1
                    continue
                try:
                    conn.execute(f"SELECT rowid FROM {table_sql} LIMIT 0")
                except sqlite3.Error:
                    skipped += 1
                    continue
                object_pairs = ",".join(
                    f"'{name}',CASE WHEN typeof(NEW.{_quote_identifier(name)})='text' "
                    f"THEN substr(NEW.{_quote_identifier(name)},1,2000) ELSE NEW.{_quote_identifier(name)} END"
                    for name in selected
                )
                old_pairs = object_pairs.replace("NEW.", "OLD.")
                semantic_columns = [name for name in selected if name.casefold() not in NON_SEMANTIC_AUDIT_COLUMNS]
                changed = " OR ".join(
                    f"OLD.{_quote_identifier(name)} IS NOT NEW.{_quote_identifier(name)}" for name in semantic_columns
                ) or "0"
                allowed_setting_keys = {
                    "gate": ("mode", "poll_interval_seconds"),
                    "stock": ("poll_interval_seconds", "instrument_refresh_seconds"),
                }.get(database_key, ())
                prefix = "odaily_obs_" + re.sub(r"[^a-zA-Z0-9_]", "_", table)
                operation_specs = (
                    ("insert", "NEW", ""),
                    ("update", "NEW", f" WHEN {changed}"),
                    ("delete", "OLD", ""),
                )
                if table in RETENTION_ONLY_DELETE_TABLES:
                    operation_specs = operation_specs[:2]
                for operation, reference, when in operation_specs:
                    trigger = _quote_identifier(f"{prefix}_{operation}")
                    pairs = old_pairs if reference == "OLD" else object_pairs
                    if table == "settings":
                        setting_keys = ",".join("'" + key.replace("'", "''") + "'" for key in allowed_setting_keys)
                        key_predicate = f"{reference}.{_quote_identifier('key')} IN ({setting_keys})" if setting_keys else "0"
                        when += (" AND " if when else " WHEN ") + key_predicate
                    conn.execute(f"""
                        CREATE TRIGGER IF NOT EXISTS {trigger}
                        AFTER {operation.upper()} ON {table_sql}{when}
                        BEGIN
                            INSERT INTO odaily_observability_outbox(
                                event_id,table_name,source_row_id,operation,happened_at_ms,payload_json
                            ) VALUES (
                                lower(hex(randomblob(16))),'{table}',CAST({reference}.rowid AS TEXT),
                                '{operation}',CAST((julianday('now')-2440587.5)*86400000 AS INTEGER),
                                json_object({pairs})
                            );
                        END
                    """)
                installed += 1
            conn.execute(
                "INSERT INTO odaily_observability_meta(meta_key,meta_value) VALUES(?,?) "
                "ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value",
                (version_key, str(SOURCE_AUDIT_TRIGGER_VERSION)),
            )
            conn.commit()
        return {"installed": installed, "skipped": skipped}


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _result(status: str, business_key: str, row: dict[str, Any]) -> str:
    value = status.casefold().replace("-", "_")
    if failure_category(_first(row, 'last_error', 'error', 'reason')) == 'expected_result':
        return 'stopped'
    if any(term in value for term in ("failed", "failure", "error", "exhausted")) or _first(row, "last_error", "error"):
        return "failed"
    if value in {"ready_review", "pending_review", "backoffice", "pending_backend"}:
        return "backoffice"
    if value in {"auto_published", "published", "sent", "sent_success", "success", "succeeded"}:
        return "internal_complete" if business_key == "x_agent" else "direct_publish"
    if value in {"ignored", "discarded", "duplicate", "skipped", "ended", "exempt"}:
        return "stopped"
    if value in {"pending", "queued", "processing", "running", "retry", "waiting", "deferred", "deduped", "publisher_pending", 'judging', 'deduping', 'writing', 'formatting', 'publishing'}:
        return "processing"
    return "unknown"


def _generic_snapshot(spec: TableSpec, source_database: Path, row: dict[str, Any], observed_at_ms: int, started_at_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    raw_status = str(_first(row, "status", "state", "result_status") or "unknown")
    if spec.record_kind == "configuration":
        raw_status = "enabled" if row.get("enabled") in (1, True, "1", "true") else "disabled" if "enabled" in row else "configured"
    title = str(_first(row, "title", "writer4_title", "writer2_title", "project_name", "instrument_name", "display_name", "username", "slug", "site_key", "template_key", "group_name", "symbol", "coin", "address", "tx_hash", "event_key", "tweet_id") or f"{spec.source} {row.get('id', row.get('__obs_rowid__', ''))}")
    summary = str(_first(row, "status_reason", "reason", "summary", "last_error", "error", "classification_reason", "result", "kind", "status") or "")
    if spec.record_kind == "configuration":
        fields = [
            f"{key}={value}" for key, value in row.items()
            if key not in {"id", "__obs_rowid__", "created_at", "updated_at", "last_polled_at", "last_success_at"}
            and value is not None
        ]
        summary = " · ".join(fields)[:1200]
    source_id = str(_first(row, "source_item_id", "event_key", "event_id", "tweet_id", "tx_hash", "fill_key", "trigger_key", "identity_key", "update_id", "username_lower", "slug_lower", "site_key", "template_key", "rule_key", "channel_key", "key", "id", "__obs_rowid__") or "")
    created_value = _first(row, "created_at", "received_at", "queued_at", "observed_at", "at", "posted_at", "published_at", "fill_time", "first_seen_ms", "created_ms")
    updated_value = _first(row, "updated_at", "completed_at", "last_mentioned_at", "last_seen_at", "sent_at", "telegram_sent_at")
    created_ms = timestamp_ms(created_value)
    updated_ms = timestamp_ms(updated_value) or created_ms
    stage = "覆盖配置" if spec.record_kind == "configuration" else str(_first(row, "stage", "current_stage") or {
        "business_news": "商务处理", "whale": "提醒", "hyperliquid_news": "事件识别", "meme": "Meme处理",
        "gate_market": "行情播报", "stock_market": "行情播报", "x_agent": "内部研究", "editor_plugin": "人工辅助生成",
        "post_publish_review": "发布后审查", "scheduled_brief": "定时简报", "unmapped_source": "主流程队列",
    }.get(spec.business_key, "处理中"))
    raw_source = str(_first(row, "source", "module", "trigger_kind", "action", "platform") or "")
    if spec.business_key == "unmapped_source":
        source_value = raw_source or spec.source
        business_key = SOURCE_TO_BUSINESS.get(source_value, "unmapped_source")
    elif raw_source and SOURCE_TO_BUSINESS.get(raw_source) == spec.business_key:
        source_value = raw_source
        business_key = spec.business_key
    else:
        source_value = spec.source
        business_key = spec.business_key
    source_database_key = "main" if spec.database_key == "hyperliquid" else spec.database_key
    item_id = (
        f"task:{source_value}:{source_id or row.get('__obs_rowid__')}"
        if spec.table == "tasks"
        else f"{source_database_key}:{spec.table}:{source_id or row.get('__obs_rowid__')}"
    )
    snapshot = {
        "item_id": item_id,
        "business_key": business_key, "source": source_value, "source_id": source_id or None,
        "task_id": int(row["task_id"]) if str(row.get("task_id") or "").isdigit() else None,
        "title": title, "summary": summary[:1200], "source_url": _first(row, "source_url", "post_url", "tx_url", "url", "official_url"),
        "stage": stage, "status": raw_status, "result": "configuration_change" if spec.record_kind == "configuration" else _result(raw_status, spec.business_key, row),
        "failure_category": failure_category(_first(row, 'last_error', 'error', 'error_message')) if _result(raw_status, spec.business_key, row) == 'failed' else None,
        "created_at_ms": created_ms, "updated_at_ms": updated_ms,
        "source_database": str(source_database), "source_table": spec.table,
        "source_row_id": str(row.get("id", row.get("__obs_rowid__"))),
        "legacy_history": created_ms is None or created_ms < started_at_ms,
        "reason": summary[:1200] or None, "error_code": str(_first(row, "error_code") or "unknown") if _first(row, "last_error", "error") else None,
        "search_extra": " ".join(str(value) for key, value in row.items() if key != "__obs_rowid__" and isinstance(value, (str, int, float))),
        "news_visible": False,
    }
    # Only actual draft/news outputs are standalone news; technical records remain diagnostic.
    if spec.table == 'business_message_inbox' and _first(row, 'writer4_title', 'writer2_title', 'title'):
        snapshot['news_visible'] = bool(_first(row, 'writer4_title', 'writer2_title') or raw_status in {'ready_review', 'published', 'sent'})
    if spec.database_key == 'meme' and spec.table == 'jobs':
        payload = json_object(row.get('payload_json') or row.get('payload'))
        draft_title = _first(row, 'draft_title', 'title') or _first(payload, 'title', 'draft_title')
        if draft_title and raw_status not in {'discarded', 'skipped', 'expired', 'filtered'}:
            snapshot['title'] = news_title(draft_title)
            snapshot['news_visible'] = True
    snapshot['admission'] = 'accepted' if snapshot['news_visible'] else 'unassessed'
    return snapshot, {"recorded_scope": "source_table_snapshot", "row": row, "observed_at_ms": observed_at_ms}


def _stage(status: str) -> str:
    value = status.casefold()
    if "judg" in value:
        return "判断"
    if "search" in value or "dedup" in value or "duplicate" in value:
        return "查重"
    if value.startswith('writ'):
        return "写作"
    if "format" in value:
        return "稿件整理"
    if "publish" in value or "review" in value or "published" in value:
        return "投递"
    if value in {"pending", "collected"}:
        return "已收集"
    return "业务停止" if value in {"discarded", "ignored", "off_topic"} else "处理中"


def _decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return __import__("json").loads(value)
    except (ValueError, TypeError):
        return value


class MainTaskProjector:
    def __init__(
        self,
        source_database: Path,
        store: SystemInformationStore,
        *,
        batch_size: int = 500,
        outbox_batch_size: int = 2_000,
        additional_databases: dict[str, Path] | None = None,
        table_specs: tuple[TableSpec, ...] = TABLE_SPECS,
    ) -> None:
        self.source_database = source_database
        self.store = store
        self.batch_size = max(25, min(batch_size, 2000))
        self.outbox_batch_size = max(25, min(outbox_batch_size, 5000))
        self.additional_databases = additional_databases or {}
        self.table_specs = table_specs
        self.audit_installer = SourceAuditInstaller()

    def _read(self) -> sqlite3.Connection:
        return connect_sqlite_readonly(self.source_database, timeout_seconds=10)

    def run_once(self) -> dict[str, int]:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        self._cleanup_dependency_errors(now_ms)
        started_at_ms = int(self.store.get_state("observability_started_at", str(now_ms)) or now_ms)
        last_id = int(self.store.get_state("main_tasks_watermark", "0") or 0)
        imported = changed = 0
        try:
            with self._read() as conn:
                columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(tasks)")}
                if "id" not in columns:
                    raise RuntimeError("主库缺少 tasks.id")
                selected = [name for name in (
                    "id", "source", "source_item_id", "source_url", "title", "content", "status", "created_at", "updated_at", "metadata", "raw_payload",
                ) if name in columns]
                pipeline_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(x_task_pipeline)")}
                pipeline_selected = [name for name in (
                    "news_type", "judge_output", "search_result", "draft_title", "final_title", "final_content", "writer_output", "publisher_output", "publisher_reason_code", "last_error", "push_result",
                    "judge_completed_at", "search_completed_at", "write_completed_at", "format_completed_at", "publisher_decided_at", "publish_completed_at",
                    "updated_at",
                ) if name in pipeline_columns]
                if not (pipeline_selected and "task_id" in pipeline_columns):
                    pipeline_selected = []

                def select_rows(where: str, arguments: tuple[Any, ...], order: str, limit: int) -> list[sqlite3.Row]:
                    query = f"SELECT t.{',t.'.join(selected)}"
                    if pipeline_selected:
                        query += "," + ",".join(f"p.{name} AS pipeline_{name}" for name in pipeline_selected)
                    query += " FROM tasks t"
                    if pipeline_selected:
                        query += " LEFT JOIN x_task_pipeline p ON p.task_id=t.id"
                    query += f" {where} ORDER BY t.id {order} LIMIT ?"
                    return conn.execute(query, (*arguments, limit)).fetchall()

                new_rows = select_rows("WHERE t.id>?", (last_id,), "ASC", self.batch_size)
                tail_rows = select_rows("WHERE t.id<=?", (last_id or 2**63 - 1,), "DESC", min(self.batch_size, 300))
                rows_by_id = {int(row["id"]): row for row in (*new_rows, *tail_rows)}
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='odaily_observability_outbox'").fetchone():
                    audit_watermark = int(self.store.get_state('source_audit_main_watermark', '0') or 0)
                    dirty = conn.execute("SELECT source_row_id FROM (SELECT source_row_id,table_name FROM odaily_observability_outbox WHERE seq>? ORDER BY seq LIMIT ?) WHERE table_name='tasks'", (audit_watermark, self.outbox_batch_size)).fetchall()
                    dirty_ids = sorted({int(r[0]) for r in dirty if str(r[0]).isdigit()} - rows_by_id.keys())
                    if dirty_ids:
                        extra = select_rows('WHERE t.id IN (' + ','.join('?' for _ in dirty_ids) + ')', tuple(dirty_ids), 'ASC', len(dirty_ids))
                        rows_by_id.update({int(r['id']): r for r in extra})
                attempt_events: list[sqlite3.Row] = []
                attempt_rows: dict[int, sqlite3.Row] = {}
                attempt_event_watermark = int(self.store.get_state("main_attempt_events_watermark", "0") or 0)
                attempt_event_table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_processing_attempt_events'"
                ).fetchone()
                if attempt_event_table:
                    attempt_events = conn.execute(
                        "SELECT id,attempt_id,task_id,event_type,status,happened_at,error_code,error_message,call_no,executor,response_gzip,response_sha256,response_bytes,response_ref,response_excerpt,response_archive_error "
                        "FROM x_processing_attempt_events WHERE id>? ORDER BY id LIMIT ?",
                        (attempt_event_watermark, min(self.batch_size, 500)),
                    ).fetchall()
                    event_task_ids = sorted({int(event["task_id"]) for event in attempt_events} - rows_by_id.keys())
                    if event_task_ids:
                        extra = select_rows(
                            "WHERE t.id IN (" + ",".join("?" for _ in event_task_ids) + ")",
                            tuple(event_task_ids), "ASC", len(event_task_ids),
                        )
                        rows_by_id.update({int(row["id"]): row for row in extra})
                    attempt_ids = sorted({int(event["attempt_id"]) for event in attempt_events})
                    if attempt_ids:
                        attempt_rows = {
                            int(row["id"]): row for row in conn.execute(
                                "SELECT id,task_id,stage,attempt_no,executor,status,started_at,response_received_at,finished_at,"
                                "input_json,rule_json,response_gzip,response_sha256,response_bytes,response_ref,response_excerpt,response_archive_error,result_json,error_code,error_message "
                                "FROM x_processing_attempts WHERE id IN (" + ",".join("?" for _ in attempt_ids) + ")",
                                attempt_ids,
                            ).fetchall()
                        }
                events_by_task: dict[int, list[sqlite3.Row]] = {}
                for event in attempt_events:
                    events_by_task.setdefault(int(event["task_id"]), []).append(event)
                rows = [rows_by_id[key] for key in sorted(rows_by_id)]
                for row in rows:
                    value = dict(row)
                    source = str(value.get("source") or "unknown")
                    status = str(value.get("status") or "unknown")
                    label, result = STATUS_LABELS.get(status, (status.replace("_", " "), "failed" if status.endswith("failed") else "unknown"))
                    title = news_title(value.get('pipeline_final_title'), value.get('pipeline_draft_title'), value.get('title'), value.get('content'))
                    writer_output = _decode(value.get("pipeline_writer_output"))
                    publisher_output = _decode(value.get("pipeline_publisher_output"))
                    reason = None
                    if isinstance(publisher_output, dict):
                        reason = publisher_output.get("reason")
                    if not reason and isinstance(writer_output, dict):
                        reason = writer_output.get("reason")
                    error = value.get("pipeline_last_error")
                    summary = str(error or reason or label)
                    created_at_ms = timestamp_ms(value.get("created_at"))
                    updated_at_ms = max(
                        [stamp for stamp in [timestamp_ms(value.get('updated_at')), created_at_ms] if stamp is not None] +
                        [timestamp_ms(value.get(f"pipeline_{key}")) for key in (
                            "judge_completed_at", "search_completed_at", "write_completed_at", "format_completed_at",
                            "publisher_decided_at", "publish_completed_at", "updated_at",
                        ) if timestamp_ms(value.get(f"pipeline_{key}")) is not None],
                        default=None,
                    )
                    pipeline = {key.removeprefix('pipeline_'): value[key] for key in value if key.startswith('pipeline_')}
                    admission = task_admission(status, pipeline, source)
                    metadata = json_object(value.get('metadata'))
                    task_id = int(value["id"])
                    stage = _stage(status)
                    snapshot = {
                        "item_id": f"task:{source}:{value.get('source_item_id') or task_id}",
                        "business_key": SOURCE_TO_BUSINESS.get(source, "unmapped_source"),
                        "source": source, "source_id": str(value.get("source_item_id") or task_id), "task_id": task_id,
                        "title": title, "summary": summary[:1200], "source_url": value.get("source_url"),
                        "stage": stage, "status": status, "result": result,
                        "failure_category": "program_or_unknown" if status.endswith("failed") else None,
                        "created_at_ms": created_at_ms, "updated_at_ms": updated_at_ms,
                        "source_database": str(self.source_database), "source_table": "tasks", "source_row_id": task_id,
                        "legacy_history": created_at_ms is None or created_at_ms < started_at_ms, "reason": str(reason or "")[:1200] or None,
                        "error_code": "unknown" if error else None,
                        "admission": admission, "news_visible": admission in {'accepted', 'duplicate'} and source not in {'stock_market', 'gate_market'},
                        "duplicate_target": duplicate_target(pipeline),
                        'source_name': _first(metadata, 'effective_author_name', 'author_username', 'account_username', 'site_display_name', 'source_label') or source,
                        'source_updated_at_ms': timestamp_ms(_first(metadata, 'source_updated_at', 'source_modified_at')),
                        "search_extra": " ".join(
                            [str(value.get(key) or "") for key in ("content", "metadata", "source_item_id", "source_url")]
                            + [str(value.get(key) or "") for key in ("pipeline_final_content", "pipeline_writer_output", "pipeline_publisher_output")]
                        ),
                    }
                    evidence = {
                        "recorded_scope": "legacy_current_snapshot",
                        "history_note": "此历史记录只保留当前任务状态；早期阶段尝试和当时未保存的理由不可补造。",
                        "task": value,
                        "pipeline": {key.removeprefix("pipeline_"): value.get(key) for key in value if key.startswith("pipeline_")},
                        "observed_at_ms": now_ms,
                    }
                    changed += int(self.store.upsert_snapshot(snapshot, evidence))
                    imported += 1
                    for event in events_by_task.get(task_id, []):
                        attempt = attempt_rows.get(int(event["attempt_id"]))
                        event_evidence = dict(event)
                        compressed_response = event_evidence.pop("response_gzip", None)
                        response_ref = event_evidence.pop("response_ref", None)
                        response_excerpt = event_evidence.pop("response_excerpt", None)
                        response_archive_error = event_evidence.pop("response_archive_error", None)
                        attempt_evidence: dict[str, Any] = {"event": event_evidence}
                        if attempt is not None:
                            attempt_evidence["attempt"] = {
                                key: attempt[key] for key in (
                                    "stage", "attempt_no", "executor", "status", "started_at", "response_received_at",
                                    "finished_at", "input_json", "rule_json", "response_sha256", "response_bytes",
                                    "response_archive_error", "result_json", "error_code", "error_message",
                                )
                            }
                            if event["event_type"] == "response_received" and compressed_response:
                                try:
                                    attempt_evidence["raw_model_response"] = gzip.decompress(bytes(compressed_response)).decode("utf-8")
                                except (OSError, EOFError, UnicodeDecodeError):
                                    attempt_evidence["raw_model_response"] = {"unavailable": True, "sha256": event["response_sha256"]}
                            elif event["event_type"] == "response_received" and response_ref:
                                if self.store.load_evidence(str(response_ref)) is None and response_excerpt:
                                    attempt_evidence["raw_model_response_excerpt"] = str(response_excerpt)
                                    attempt_evidence["raw_model_response_sha256"] = event["response_sha256"]
                                    attempt_evidence["raw_model_response_bytes"] = event["response_bytes"]
                                    response_ref = None
                            elif event["event_type"] == "response_received" and response_excerpt:
                                attempt_evidence["raw_model_response_excerpt"] = str(response_excerpt)
                                attempt_evidence["raw_model_response_sha256"] = event["response_sha256"]
                                attempt_evidence["raw_model_response_bytes"] = event["response_bytes"]
                            if response_archive_error:
                                attempt_evidence["response_archive_error"] = response_archive_error
                        attempt_no = int(attempt["attempt_no"]) if attempt is not None else 0
                        stage_key = str(attempt["stage"] if attempt is not None else "unknown").casefold()
                        stage_label = {
                            "judge": "价值判断", "judge_crypto": "价值判断", "judge_ai": "价值判断",
                            "judge_jin10": "金十分类", "search": "查重", "write": "写作",
                            "format_publish": "稿件整理", "publish": "投递",
                        }.get(stage_key, stage_key.replace("_", " "))
                        event_type = str(event["event_type"])
                        summaries = {
                            "started": f"开始第 {attempt_no} 次{stage_label}尝试",
                            "response_received": f"已保存{stage_label}模型原始返回",
                            "succeeded": f"第 {attempt_no} 次{stage_label}尝试成功",
                            "failed": f"第 {attempt_no} 次{stage_label}尝试失败",
                            "stopped": f"第 {attempt_no} 次{stage_label}尝试停止",
                        }
                        if event_type == "response_received" and response_archive_error:
                            summaries[event_type] = f"{stage_label}模型返回归档失败，仅保留摘要"
                        happened_at_ms = timestamp_ms(event["happened_at"])
                        self.store.append_event(
                            item_id=snapshot["item_id"], event_id=f"stage-attempt:{event['id']}",
                            event_type="stage_attempt", stage=stage_label, status=str(event["status"]),
                            result="failed" if event["status"] == "failed" else "unknown",
                            happened_at_ms=happened_at_ms, summary=summaries.get(event_type, "写作阶段状态更新"),
                            reason=event["error_message"] or response_archive_error,
                            error_code=event["error_code"],
                            evidence_ref=str(response_ref) if event_type == "response_received" and response_ref else None,
                            evidence=attempt_evidence if not (event_type == "response_received" and response_ref) else None,
                            details={"attempt_id": event["attempt_id"], "attempt_no": attempt_no,
                                     "event_type": event_type, "call_no": event["call_no"],
                                     "executor": event["executor"], "response_sha256": event["response_sha256"],
                                     "response_bytes": event["response_bytes"],
                                     "response_archive_error": response_archive_error,
                                     "input": _decode(attempt["input_json"]) if attempt is not None else {},
                                     "rules": _decode(attempt["rule_json"]) if attempt is not None else {}},
                        )
                        if event_type == "response_received":
                            self.store.unpin_evidence(f"x-attempt-response:{event['attempt_id']}:{event['call_no']}")
                current_watermark = max(last_id, max((int(row["id"]) for row in new_rows), default=last_id))
                if new_rows:
                    self.store.set_state("main_tasks_watermark", current_watermark)
                if attempt_events:
                    self.store.set_state("main_attempt_events_watermark", max(int(event["id"]) for event in attempt_events))
                remaining_tasks = conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE id>?", (current_watermark,),
                ).fetchone()[0]
            self.store.set_state("main_tasks_lag", int(remaining_tasks or 0))
            self.store.set_state("main_tasks_last_success", now_ms)
            self.store.set_state("main_tasks_last_error", "")
            extra_read, extra_changed = self._project_additional(now_ms, started_at_ms)
            self.store.rebuild_search_index(self.batch_size)
            return {"read": imported + extra_read, "changed": changed + extra_changed}
        except Exception as exc:
            self.store.set_state("main_tasks_last_error", f"{type(exc).__name__}: {exc}"[:1000])
            raise

    def _cleanup_dependency_errors(self, now_ms: int) -> None:
        if now_ms - int(self.store.get_state('dependency_cleanup_last_ms', '0') or 0) < 60_000:
            return
        from .error_retention import cleanup_dependency_errors, cleanup_observation_errors, dependency_error_totals
        paths = {'main': self.source_database, **self.additional_databases}
        for database_key, path in paths.items():
            if not path.is_file():
                continue
            try:
                cleanup_dependency_errors(path, tables=[spec.table for spec in self.table_specs if spec.database_key == database_key], database_key=database_key, now_ms=now_ms)
                self.store.record_external_error_totals(database_key, dependency_error_totals(path))
                self.store.set_state(f'dependency_cleanup_{database_key}_error', '')
            except sqlite3.Error as exc:
                self.store.set_state(f'dependency_cleanup_{database_key}_error', f'{type(exc).__name__}: {exc}'[:300])
        cleanup_observation_errors(self.store, now_ms=now_ms)
        from .business_retention import trim_archived_task_payloads
        try:
            trimmed = trim_archived_task_payloads(self.source_database, self.store, now_ms=now_ms, dry_run=False)
            self.store.set_state('task_payload_retention_last_result', json.dumps(trimmed))
            self.store.set_state('task_payload_retention_error', '')
        except sqlite3.Error as exc:
            self.store.set_state('task_payload_retention_error', f'{type(exc).__name__}: {exc}'[:300])
        self.store.set_state('dependency_cleanup_last_ms', now_ms)

    def _project_additional(self, now_ms: int, started_at_ms: int) -> tuple[int, int]:
        last_sync = int(self.store.get_state("additional_projection_last_ms", "0") or 0)
        if now_ms - last_sync < ADDITIONAL_PROJECTION_INTERVAL_MS:
            return 0, 0
        last_tail_check = int(self.store.get_state("additional_tail_recheck_last_ms", "0") or 0)
        recheck_tails = now_ms - last_tail_check >= ADDITIONAL_TAIL_RECHECK_INTERVAL_MS
        imported = changed = 0
        specs_by_database: dict[str, list[TableSpec]] = {}
        for spec in self.table_specs:
            specs_by_database.setdefault(spec.database_key, []).append(spec)
        for database_key, specs in specs_by_database.items():
            path = self.additional_databases.get(database_key)
            audit_state = f"source_audit_{database_key}"
            if path is None or not path.is_file():
                self.store.set_state(f"adapter_{database_key}_audit_status", "unavailable")
                continue
            try:
                if self.store.get_state(f"{audit_state}_trigger_version") != str(SOURCE_AUDIT_TRIGGER_VERSION):
                    result = self.audit_installer.install(path, tuple(specs))
                    self.store.set_state(f"{audit_state}_tables", result["installed"])
                    self.store.set_state(f"{audit_state}_skipped", result["skipped"])
                    self.store.set_state(f"{audit_state}_installed", "1")
                    self.store.set_state(f"{audit_state}_trigger_version", SOURCE_AUDIT_TRIGGER_VERSION)
                self.store.set_state(f"adapter_{database_key}_audit_status", "ready")
                self.store.set_state(f"adapter_{database_key}_audit_tables", self.store.get_state(f"{audit_state}_tables", "0"))
                self.store.set_state(f"adapter_{database_key}_audit_skipped", self.store.get_state(f"{audit_state}_skipped", "0"))
                if int(self.store.get_state(f"{audit_state}_skipped", "0") or 0):
                    self.store.set_state(f"adapter_{database_key}_audit_status", "partial")
                outbox_read, outbox_changed = self._project_source_outbox(database_key, path, specs, now_ms)
                self.store.set_state(f"{audit_state}_last_error", "")
                imported += outbox_read
                changed += outbox_changed
            except Exception as exc:
                self.store.set_state(f"adapter_{database_key}_audit_status", "error")
                self.store.set_state(f"{audit_state}_last_error", f"{type(exc).__name__}: {exc}"[:1000])
        for spec in self.table_specs:
            path = self.additional_databases.get(spec.database_key)
            state_key = f"adapter_{spec.database_key}_{spec.table}"
            if path is None or not path.is_file():
                self.store.set_state(f"{state_key}_status", "unavailable")
                continue
            if spec.database_key == "main" and spec.table == "tasks":
                self.store.set_state(f"{state_key}_status", "ready")
                self.store.set_state(f"{state_key}_last_success", now_ms)
                continue
            try:
                with self._read_path(path) as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (spec.table,),
                    ).fetchone()
                    if not exists:
                        self.store.set_state(f"{state_key}_status", "table_missing")
                        continue
                    watermark = int(self.store.get_state(f"{state_key}_watermark", "0") or 0)
                    query_table = '"' + spec.table.replace('"', '""') + '"'
                    new_rows = conn.execute(
                        f"SELECT rowid AS __obs_rowid__,* FROM {query_table} WHERE rowid>? ORDER BY rowid ASC LIMIT ?",
                        (watermark, min(self.batch_size, 50)),
                    ).fetchall()
                    tail_rows = conn.execute(
                        f"SELECT rowid AS __obs_rowid__,* FROM {query_table} WHERE rowid<=? ORDER BY rowid DESC LIMIT ?",
                        (watermark or 2**63 - 1, ADDITIONAL_TAIL_RECHECK_LIMIT),
                    ).fetchall() if recheck_tails else []
                    rows_by_id = {int(row["__obs_rowid__"]): row for row in (*new_rows, *tail_rows)}
                    rows = [rows_by_id[key] for key in sorted(rows_by_id)]
                    for raw in rows:
                        row = dict(raw)
                        snapshot, evidence = _generic_snapshot(spec, path, row, now_ms, started_at_ms)
                        self._link_to_news(conn, snapshot, row)
                        changed += int(self.store.upsert_snapshot(snapshot, evidence))
                        imported += 1
                    if new_rows:
                        self.store.set_state(
                            f"{state_key}_watermark",
                            max(watermark, max(int(row["__obs_rowid__"]) for row in new_rows)),
                        )
                    if rows or self.store.get_state(f"{state_key}_status") != "ready":
                        self.store.set_state(f"{state_key}_status", "ready")
                        self.store.set_state(f"{state_key}_last_success", now_ms)
                        self.store.set_state(f"{state_key}_last_error", "")
            except Exception as exc:
                self.store.set_state(f"{state_key}_status", "error")
                self.store.set_state(f"{state_key}_last_error", f"{type(exc).__name__}: {exc}"[:1000])
        if recheck_tails:
            self.store.set_state("additional_tail_recheck_last_ms", now_ms)
        self.store.set_state("additional_projection_last_ms", now_ms)
        return imported, changed

    def _project_source_outbox(
        self, database_key: str, path: Path, specs: list[TableSpec], now_ms: int,
    ) -> tuple[int, int]:
        watermark_key = f"source_audit_{database_key}_watermark"
        watermark = int(self.store.get_state(watermark_key, "0") or 0)
        by_table = {spec.table: spec for spec in specs}
        with self._read_path(path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='odaily_observability_outbox'"
            ).fetchone()
            if not exists:
                return 0, 0
            rows = conn.execute(
                "SELECT seq,event_id,table_name,source_row_id,operation,happened_at_ms,payload_json "
                "FROM odaily_observability_outbox WHERE seq>? ORDER BY seq LIMIT ?",
                (watermark, self.outbox_batch_size),
            ).fetchall()
        entries: list[dict[str, Any]] = []
        with self._read_path(path) as link_conn:
            for event in rows:
                spec = by_table.get(str(event["table_name"]))
                if spec is None:
                    continue
                if spec.table in RETENTION_ONLY_DELETE_TABLES and str(event["operation"]) == "delete":
                    continue
                payload = _decode(event["payload_json"])
                if not isinstance(payload, dict):
                    payload = {}
                source_row = {**payload, "__obs_rowid__": event["source_row_id"]}
                if spec.table == "tasks" and str(event["source_row_id"]).isdigit():
                    source_row["task_id"] = int(event["source_row_id"])
                snapshot, _evidence = _generic_snapshot(spec, path, source_row, now_ms, 0)
                if spec.table == "tasks":
                    task_status = str(snapshot["status"])
                    label, result = STATUS_LABELS.get(
                        task_status, (task_status.replace("_", " "), "failed" if task_status.endswith("failed") else "unknown"),
                    )
                    admission = task_admission(task_status, {}, snapshot["source"])
                    snapshot.update({
                        "business_key": SOURCE_TO_BUSINESS.get(snapshot["source"], "unmapped_source"),
                        "task_id": int(event["source_row_id"]) if str(event["source_row_id"]).isdigit() else None,
                        "stage": _stage(task_status), "status": task_status, "result": result,
                        "summary": label, "admission": admission,
                        "news_visible": admission in {"accepted", "duplicate"}
                        and snapshot["source"] not in {"stock_market", "gate_market"},
                    })
                if spec.table != "tasks":
                    self._link_to_news(link_conn, snapshot, source_row)
                snapshot["legacy_history"] = False
                snapshot["updated_at_ms"] = timestamp_ms(
                    payload.get("updated_at") or payload.get("completed_at") or payload.get("last_seen_at")
                ) or int(event["happened_at_ms"])
                compact_evidence = {
                    "recorded_scope": "transactional_source_outbox",
                    "operation": str(event["operation"]),
                    "source_table": spec.table,
                    "source_row_id": str(event["source_row_id"]),
                    "payload": payload,
                }
                status = str(snapshot["status"])
                result = str(snapshot["result"])
                operation = str(event["operation"])
                reason = _first(payload, "last_error", "error_message", "failure_reason", "status_reason", "reason")
                config_fields = [f"{key}={value}" for key, value in payload.items() if key not in {"id", "created_at", "updated_at"} and value is not None]
                summary = str(
                    reason or (
                        "配置生效变更：" + " · ".join(config_fields) if spec.record_kind == "configuration"
                        else
                        f"{spec.source} 记录已删除" if operation == "delete"
                        else f"{spec.source} {operation} 状态变更：{status}"
                    )
                )[:1200]
                entries.append({
                    "snapshot": snapshot,
                    "evidence": compact_evidence,
                    "only_if_newer": spec.table == "tasks",
                    "event_id": f"source-audit:{database_key}:{event['event_id']}",
                    "stage": str(snapshot["stage"]),
                    "status": "deleted" if operation == "delete" else status,
                    "result": "unknown" if operation == "delete" else result,
                    "happened_at_ms": int(event["happened_at_ms"]),
                    "summary": summary,
                    "reason": str(reason)[:1200] if reason else None,
                    "error_code": str(_first(payload, "error_code") or "unknown") if reason else None,
                    "details": {"operation": operation, "source_table": spec.table, "source_row_id": str(event["source_row_id"])},
                })
        imported = changed = 0
        if rows:
            last_seq = int(rows[-1]["seq"])
            imported, changed = self.store.apply_source_audit_batch(
                entries, watermark_key=watermark_key, watermark=last_seq,
            )
            watermark = last_seq
        self._cleanup_source_outbox(database_key, path, watermark, now_ms)
        return imported, changed

    def _link_to_news(self, conn: sqlite3.Connection, snapshot: dict[str, Any], row: dict[str, Any]) -> None:
        table = snapshot['source_table']
        task_id = row.get('task_id')
        if not task_id and table not in {'auditor_checks', 'fact_checks', 'fact_check_runs', 'newsflash_items'}:
            return
        databases = {str(r[1]): str(r[2]) for r in conn.execute('PRAGMA database_list')}
        current_path = Path(databases.get('main', '')).resolve()
        primary_path = self.source_database.resolve()
        if current_path != primary_path and 'odaily_main' not in databases:
            conn.execute('ATTACH DATABASE ? AS odaily_main', (str(primary_path),))
            databases['odaily_main'] = str(primary_path)
        primary_schema = 'odaily_main.' if current_path != primary_path else ''
        tables = {str(r[0]) for r in conn.execute(
            f"SELECT name FROM {primary_schema}sqlite_master WHERE type='table'")}
        if table == 'newsflash_items' and 'tasks' in tables:
            task = conn.execute(f'SELECT id FROM {primary_schema}tasks WHERE source=? AND source_item_id=? ORDER BY id LIMIT 2', (row.get('source'), row.get('source_item_id'))).fetchall()
            if len(task) == 1:
                task_id = task[0][0]
        if table == 'fact_check_runs' and 'fact_checks' in tables:
            check = conn.execute(f'SELECT source_item_id FROM {primary_schema}fact_checks WHERE id=?', (row.get('check_id'),)).fetchone()
            row = {**row, 'source_item_id': check[0] if check else None}
        if table in {'auditor_checks', 'fact_checks', 'fact_check_runs'} and 'newsflash_production_metadata' in tables:
            match = conn.execute(f'SELECT delivery_key FROM {primary_schema}newsflash_production_metadata WHERE odaily_id=? AND id_match_status=\'matched\'', (row.get('source_item_id'),)).fetchone()
            if match:
                key = str(match[0])
                if key.startswith('odaily-task-'):
                    task_id = key.removeprefix('odaily-task-')
        if task_id and 'tasks' in tables:
            task = conn.execute(f'SELECT id,source,source_item_id FROM {primary_schema}tasks WHERE id=?', (task_id,)).fetchone()
            if task:
                snapshot['parent_item_id'] = f"task:{task['source']}:{task['source_item_id'] or task['id']}"
                snapshot['news_visible'] = False

    def _cleanup_source_outbox(self, database_key: str, path: Path, watermark: int, now_ms: int) -> None:
        cleanup_state_key = f"source_audit_{database_key}_cleanup_last_ms"
        last_cleanup = int(self.store.get_state(cleanup_state_key, "0") or 0)
        if now_ms - last_cleanup < SOURCE_OUTBOX_CLEANUP_INTERVAL_MS:
            return
        self.store.set_state(cleanup_state_key, now_ms)
        source_cutoff = now_ms - SOURCE_OUTBOX_RETENTION_MS
        with self._read_path(path) as conn:
            eligible = conn.execute(
                "SELECT 1 FROM odaily_observability_outbox WHERE seq<=? AND happened_at_ms<? LIMIT 1",
                (watermark, source_cutoff),
            ).fetchone()
        if not eligible:
            return
        with sqlite3.connect(path, timeout=2, factory=ClosingSQLiteConnection) as conn:
            conn.execute("PRAGMA busy_timeout=2000")
            conn.execute(
                "DELETE FROM odaily_observability_outbox WHERE seq IN ("
                "SELECT seq FROM odaily_observability_outbox WHERE seq<=? AND happened_at_ms<? "
                "ORDER BY seq LIMIT ?)",
                (watermark, source_cutoff, SOURCE_OUTBOX_CLEANUP_BATCH_SIZE),
            )

    @staticmethod
    def _read_path(path: Path) -> sqlite3.Connection:
        return connect_sqlite_readonly(path, timeout_seconds=10)


class ProjectionWorker:
    def __init__(self, projector: MainTaskProjector, *, interval_seconds: float = 5.0) -> None:
        self.projector = projector
        self.interval_seconds = max(1.0, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="system-information-projector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_once(self) -> dict[str, int]:
        return self.projector.run_once()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = self.run_once()
                if result["changed"]:
                    print(f"[odaily] system information projection read={result['read']} changed={result['changed']}")
            except Exception as exc:
                print(f"[odaily] system information projection failed: {type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_seconds)
