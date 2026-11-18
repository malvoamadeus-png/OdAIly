from __future__ import annotations

import hashlib
import argparse
import codecs
import gzip
import json
import math
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.types.json import Jsonb

from packages.common.pg_native import connect_postgres
from packages.common.storage import load_postgres_settings


DATA_ROOT = Path("/data")


@dataclass(frozen=True, slots=True)
class TableMapping:
    source_table: str
    target_schema: str
    target_table: str
    json_columns: tuple[str, ...] = ()
    timestamp_columns: tuple[str, ...] = ()
    legacy_typed: bool = False
    optional_source: bool = False
    column_map: tuple[tuple[str, str], ...] = ()
    transform: str | None = None


@dataclass(frozen=True, slots=True)
class SourceMapping:
    source_key: str
    target_schema: str
    tables: tuple[TableMapping, ...]
    retired_tables: tuple[str, ...] = ()


@contextmanager
def _connect_native_import_postgres(*, schema: str, application_name: str):
    """Use migration timeouts for long COPY, verification, and audit statements."""
    settings = load_postgres_settings()
    with connect_postgres(schema=schema, application_name=application_name, settings=settings) as connection:
        connection.execute(
            "SELECT pg_catalog.set_config('statement_timeout', %s, false), "
            "pg_catalog.set_config('lock_timeout', %s, false), "
            "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, false)",
            (
                f"{settings.migration_statement_timeout_ms}ms",
                f"{settings.migration_lock_timeout_ms}ms",
                f"{settings.migration_idle_transaction_timeout_ms}ms",
            ),
        )
        yield connection


def _configure_sqlite_temp_directory() -> None:
    configured = os.getenv("ODAILY_SQLITE_TMPDIR") or os.getenv("SQLITE_TMPDIR") or os.getenv("TMPDIR")
    if not configured:
        return
    directory = Path(configured).expanduser().resolve()
    data_root = DATA_ROOT.resolve()
    if directory != data_root and data_root not in directory.parents:
        raise RuntimeError("SQLite migration temp directory must be under /data")
    if not directory.is_dir():
        raise RuntimeError(f"SQLite migration temp directory does not exist: {directory}")
    if not os.access(directory, os.W_OK | os.X_OK):
        raise RuntimeError(f"SQLite migration temp directory is not writable: {directory}")
    sqlite3.temp_directory = str(directory)


def _tables(source_key: str, schema: str, names: tuple[str, ...]) -> tuple[TableMapping, ...]:
    return tuple(TableMapping(name, schema, name) for name in names)


NATIVE_SOURCES = {
    "main": SourceMapping(
        "main", "main", _tables("main", "main", (
            "tasks", "x_task_pipeline", "x_processing_attempts", "x_processing_attempt_events",
            "x_capture_settings", "x_capture_accounts", "x_capture_attempts", "x_seen_tweets",
            "publisher_settings", "publisher_channels", "publisher_rule_config",
            "prompt_templates", "prompt_template_versions", "source_exclusion_rule_groups",
            "newsflash_items", "newsflash_events", "newsflash_event_sources", "newsflash_event_exclusions",
            "search_event_candidates", "search_event_sources", "pipeline_worker_heartbeats",
            "pipeline_alerts", "odaily_reference_items", "external_media_alert_pipeline",
            "auditor_checks", "fact_checks", "fact_check_runs", "media_newsflash",
            "non_mainstream_media_settings", "non_mainstream_media_sources",
            "non_mainstream_media_seen_items", "jin10_settings", "jin10_seen_items",
            "whale_watch_addresses", "whale_watch_chain_states", "whale_watch_activities",
            "whale_watch_hyperliquid_settings", "whale_watch_hyperliquid_addresses",
            "whale_watch_hyperliquid_states", "whale_watch_hyperliquid_activities", "writer3_contexts",
            "binance_square_accounts", "binance_square_attempts", "binance_square_seen_posts", "binance_square_settings",
            "local_auth_sessions", "local_auth_users", "newsflash_daily_modes", "newsflash_metadata_state",
            "newsflash_operation_audit", "newsflash_operation_facts", "newsflash_production_metadata",
            "newsflash_quality_week_rules", "newsflash_reporting_weeks", "newsflash_roster", "newsflash_roster_aliases",
            "newsflash_shift_assignments", "editor_plugin_feedbacks", "editor_plugin_generation_logs",
            "editor_plugin_receipts", "editor_plugin_users",
        )) + tuple(
            TableMapping(name, "legacy_main", name, legacy_typed=True)
            for name in (
                "business_message_inbox", "business_telegram_groups", "business_telegram_recent_messages", "business_telegram_state",
                "hl_news_account_pool", "hl_news_addresses", "hl_news_candidate_addresses", "hl_news_candidate_scenarios",
                "hl_news_candidate_timeline", "hl_news_candidates", "hl_news_dispatch", "hl_news_fills", "hl_news_metadata",
                "hl_news_normal_state", "hl_news_state", "hl_news_test_cases", "msx_notice_seen_items", "msx_notice_state",
                "pipeline_timing_snapshots", "odaily_error_retention_state", "odaily_external_error_daily",
                "odaily_observability_meta", "odaily_observability_outbox",
            )
        ),
    ),
    "hottopic": SourceMapping(
        "hottopic", "hottopic", (
            TableMapping("hottopic_meta", "hottopic", "hottopic_meta"),
        ) + _tables("hottopic", "hottopic", (
            "hottopic_accounts", "hottopic_inbox", "hottopic_account_cleanup", "hottopic_events", "hottopic_migrations",
            "x_agent_analysis_jobs", "x_agent_sentiment_results", "x_agent_sentiment_snapshots",
            "x_agent_project_observations", "batches", "content_items", "claims", "topics", "threads",
            "memberships", "topic_participations", "retrieval_state", "brief_revisions", "topic_evidence",
            "decision_audit", "topic_merges", "topic_pair_reviews", "topic_claim_reviews",
            "topic_merge_scan_state", "topic_merge_scans", "event_tracking_prompt_versions",
            "event_tracking_events", "event_tracking_dismissals", "event_tracking_cycles",
            "event_tracking_topic_assessments", "event_tracking_topic_observations", "event_tracking_topic_links",
            "event_tracking_account_discoveries", "event_tracking_accounts", "event_tracking_bindings",
            "event_tracking_inbox", "event_tracking_updates", "event_tracking_publication_outbox",
            "event_tracking_audit", "yesterday_hot_discussion_runs", "investment_leads_meta",
            "investment_lead_materials", "investment_lead_observations",
            "investment_lead_tasks", "investment_lead_events", "investment_leads", "investment_lead_replay_imports",
            "investment_lead_revisions", "investment_lead_sources", "investment_lead_usage",
            "investment_lead_identity_searches", "investment_lead_scheduler_cursor",
        )) + (TableMapping("investment_lead_maintenance_progress", "hottopic", "investment_lead_maintenance_progress", optional_source=True),) + tuple(
            TableMapping(name, "legacy_hottopic", name, legacy_typed=True)
            for name in ("odaily_error_retention_state", "odaily_external_error_daily", "odaily_observability_meta", "odaily_observability_outbox")
        ),
        (),
    ),
    "hyperliquid": SourceMapping(
        "hyperliquid", "hyperliquid_news", _tables("hyperliquid", "hyperliquid_news", (
            "hl_news_addresses", "hl_news_fills", "hl_news_monitor_fill_keys", "hl_news_compact_history",
            "hl_news_compact_work", "hl_news_compact_segments", "hl_news_state", "hl_news_normal_state",
            "hl_news_normal_schedule", "hl_news_candidates", "hl_news_candidate_timeline",
            "hl_news_candidate_addresses", "hl_news_candidate_scenarios", "hl_news_dispatch",
            "hl_news_account_pool", "hl_news_metadata", "hl_news_test_cases", "hl_news_address_names",
        )),
        (),
    ),
}


def _manifest_native_sources() -> dict[str, SourceMapping]:
    specs = {
        "ai_review": ("system_observability", "ai_review_daily_usage ai_review_items ai_review_run_state"),
        "business_news": ("business_news", "business_telegram_groups business_message_inbox business_telegram_recent_messages business_telegram_state odaily_business_storage"),
        "gate_market": ("gate", "alert_state price_samples settings symbol_config symbol_state templates trigger_events"),
        "stock_market": ("stock", "broadcast_state instruments price_samples settings trigger_events"),
        "predict_market_discovery": ("predict_market", "predict_market_meta predict_market_observations predict_market_scan_runs"),
        "predict_html_discovery": ("main", "predict_html_feed_baselines predict_html_scan_runs predict_html_topics"),
        "macro_newsflash": ("macro_newsflash", "macro_metric_state macro_newsflash_events macro_newsflash_meta macro_release_schedule"),
        "editor_plugin_local": ("main", "editor_plugin_local_feed_items editor_plugin_local_feedbacks editor_plugin_local_reads editor_plugin_local_sessions"),
        "ai_text_self_upgrade": ("legacy_ai_text_self_upgrade", "ai_text_self_upgrade_decisions ai_text_self_upgrade_items ai_text_self_upgrade_runs ai_text_self_upgrade_state"),
        "msx_notice": ("main", "msx_notice_seen_items msx_notice_state msx_notice_storage"),
        "pipeline_timing": ("pipeline_timing", "pipeline_timing_snapshots"),
        "local_pipeline": ("local_pipeline", "local_pipeline_jobs"),
        "meme_scanner": ("meme", "ca_mentions daily_hot_chains daily_hot_scans daily_hot_seen jobs key_breakthrough_results market_cap_milestones meme_grok_jobs observations scanner_meta tg_candidates token_snapshots"),
        "writer3": ("main", "odaily_references writer3_telegram_confirmations"),
        "system_information": ("system_observability", "system_information_decision_samples system_information_events system_information_evidence system_information_evidence_pins system_information_expired_evidence_candidates system_information_external_error_daily system_information_items system_information_state system_information_storage_samples system_review_analysis_attempts system_review_channel_observations system_review_decisions system_review_items system_review_runs system_review_state system_review_usage"),
        "small_evidence": ("system_observability", "small_evidence"),
        "hyperliquid": ("hyperliquid_news", ""),
        "ai_text_self_upgrade_observability": ("system_observability", "ai_text_self_upgrade_decisions ai_text_self_upgrade_items ai_text_self_upgrade_resolutions ai_text_self_upgrade_runs ai_text_self_upgrade_state"),
        "searcher": ("main", "x_search_documents x_search_embeddings"),
    }
    result = {
        key: SourceMapping(key, schema, _tables(key, schema, tuple(names.split())))
        for key, (schema, names) in specs.items()
    }
    result["predict_html_discovery"] = SourceMapping("predict_html_discovery", "main", (
        TableMapping("feed_baselines", "main", "predict_html_feed_baselines"),
        TableMapping("html_scan_runs", "main", "predict_html_scan_runs"),
        TableMapping("html_topics", "main", "predict_html_topics"),
    ))
    result["gate_market"] = replace(result["gate_market"], tables=result["gate_market"].tables + (
        TableMapping("legacy_symbol_state", "legacy_gate", "legacy_symbol_state", legacy_typed=True, optional_source=True),
    ))
    result["writer3"] = SourceMapping("writer3", "main", (
        TableMapping("odaily_references", "main", "writer3_odaily_references"),
        TableMapping("writer3_telegram_confirmations", "main", "writer3_telegram_confirmations"),
    ))
    result["searcher"] = SourceMapping("searcher", "main", (
        TableMapping("documents", "main", "x_search_documents"),
        TableMapping("embeddings", "main", "x_search_embeddings"),
    ))
    result["msx_notice"] = SourceMapping("msx_notice", "main", (
        TableMapping("msx_notice_seen_items", "main", "msx_notice_seen_items"),
        TableMapping("msx_notice_state", "main", "msx_notice_state"),
        TableMapping("msx_notice_storage", "legacy_msx_notice", "msx_notice_storage", legacy_typed=True),
    ))
    result["business_news"] = replace(result["business_news"], tables=tuple(
        TableMapping(name, "business_news", name) for name in (
            "business_telegram_groups", "business_message_inbox", "business_telegram_recent_messages", "business_telegram_state",
        )
    ) + (TableMapping("odaily_business_storage", "legacy_business_news", "odaily_business_storage", legacy_typed=True),))
    result["stock_market"] = replace(result["stock_market"], tables=result["stock_market"].tables + (
        TableMapping("trigger_state", "legacy_stock", "trigger_state", legacy_typed=True, optional_source=True),
    ))
    result["small_evidence"] = SourceMapping("small_evidence", "system_observability", (
        TableMapping("evidence", "system_observability", "small_evidence", column_map=(
            ("ref", "evidence_ref"), ("payload", "compressed_payload"),
        ), transform="small_evidence"),
    ))
    # Search postings are a rebuildable projection. The six-hour information
    # feed migration preserves its source items and evidence, but does not
    # carry historical token indexes into the new database.
    result["system_information"] = replace(
        result["system_information"],
        retired_tables=("system_information_search",),
    )
    result["ai_text_self_upgrade"] = replace(result["ai_text_self_upgrade"], tables=tuple(
        TableMapping(name, "legacy_ai_text_self_upgrade_runtime", name, legacy_typed=True)
        for name in ("ai_text_self_upgrade_decisions", "ai_text_self_upgrade_items", "ai_text_self_upgrade_runs", "ai_text_self_upgrade_state")
    ))
    result["ai_text_self_upgrade_observability"] = replace(result["ai_text_self_upgrade_observability"], tables=(
        TableMapping("ai_text_self_upgrade_items", "system_observability", "ai_text_self_upgrade_items"),
        TableMapping("ai_text_self_upgrade_decisions", "system_observability", "ai_text_self_upgrade_decisions"),
        TableMapping("ai_text_self_upgrade_runs", "system_observability", "ai_text_self_upgrade_runs"),
        TableMapping("ai_text_self_upgrade_state", "system_observability", "ai_text_self_upgrade_state"),
        TableMapping("ai_text_self_upgrade_resolutions", "system_observability", "ai_text_self_upgrade_resolutions", optional_source=True),
    ))
    result["hyperliquid"] = NATIVE_SOURCES["hyperliquid"]
    result["ai_text_self_upgrade_observability"] = replace(result["ai_text_self_upgrade_observability"], target_schema="system_observability")
    for source in ("ai_review", "business_news", "gate_market", "stock_market", "predict_market_discovery",
                   "predict_html_discovery", "macro_newsflash", "editor_plugin_local", "ai_text_self_upgrade",
                   "msx_notice", "pipeline_timing", "local_pipeline", "meme_scanner", "writer3",
                   "system_information", "hyperliquid",
                   "searcher"):
        result[source] = replace(result[source],
            tables=result[source].tables + tuple(
                TableMapping(name, f"legacy_{source}", name, legacy_typed=True, optional_source=True)
                for name in ("odaily_error_retention_state", "odaily_external_error_daily", "odaily_observability_meta", "odaily_observability_outbox")
            ))
    return result


NATIVE_SOURCES.update(_manifest_native_sources())


def register_native_imported_source_identities(*, source_path: Path, batch_size: int = 2000) -> dict[str, Any]:
    """Rebuild imported source identity indexes without reimporting native tables."""
    path = source_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    from packages.system_observability.postgres_store import PostgresSystemInformationStore

    store = PostgresSystemInformationStore()
    skipped: set[tuple[str, str, str]] = set()
    registered = 0
    conflict_count = 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        source.row_factory = sqlite3.Row
        batch: list[dict[str, Any]] = []
        cursor = source.execute(
            "SELECT source_database,source_table,source_row_id,item_id FROM system_information_items "
            "WHERE source_database IS NOT NULL AND source_table IS NOT NULL "
            "AND source_row_id IS NOT NULL AND item_id IS NOT NULL ORDER BY item_id"
        )
        while rows := cursor.fetchmany(max(1, min(5000, batch_size))):
            batch.extend(dict(row) for row in rows)
            if len(batch) < max(1, min(5000, batch_size)):
                continue
            before = len(skipped)
            registered += store.register_imported_source_identities(
                batch, source_schema_map=_observability_source_schema_map(), max_batch=batch_size,
                skip_identities=skipped,
            )
            conflict_count += len(skipped) - before
            batch.clear()
        if batch:
            before = len(skipped)
            registered += store.register_imported_source_identities(
                batch, source_schema_map=_observability_source_schema_map(), max_batch=batch_size,
                skip_identities=skipped,
            )
            conflict_count += len(skipped) - before
    return {"source_path": str(path), "registered": registered,
            "skipped_conflicting_identities": conflict_count}


def import_native_sqlite_source(
    *, source_key: str, source_path: Path, batch_size: int = 2000,
    source_mapping: SourceMapping | None = None, max_batch_bytes: int = 16 * 1024 * 1024,
    max_row_bytes: int = 64 * 1024 * 1024,
) -> dict[str, Any]:
    """Import an explicitly mapped SQLite file into native PostgreSQL tables.

    The function refuses unknown tables, column drift, PK drift and required
    destination columns lacking a source/default. Each COPY batch is atomic;
    conflicting keys are never replaced. Re-running a completed import fails
    on uniqueness rather than overwriting destination data.
    """
    if batch_size < 1 or batch_size > 20_000:
        raise ValueError("batch_size must be between 1 and 20000")
    if max_batch_bytes < 1024 or max_row_bytes < max_batch_bytes:
        raise ValueError("byte limits require max_row_bytes >= max_batch_bytes >= 1024")
    mapping = source_mapping or NATIVE_SOURCES.get(source_key)
    if mapping is None or mapping.source_key != source_key:
        raise ValueError(f"No native SQLite mapping registered for source {source_key!r}")
    path = source_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    _configure_sqlite_temp_directory()
    sqlite = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    sqlite.execute("PRAGMA temp_store=FILE")
    sqlite.row_factory = sqlite3.Row
    try:
        sqlite.execute("BEGIN")
        schema = _schema(sqlite)
        source_tables = {table["name"]: table for table in schema}
        allowed = {table.source_table for table in mapping.tables} | set(mapping.retired_tables)
        unknown = sorted(set(source_tables) - allowed)
        if unknown:
            raise RuntimeError(f"Unmapped SQLite tables for {source_key}: {', '.join(unknown)}")
        reports = []
        for table_map in _order_by_foreign_keys(sqlite, mapping.tables):
            table = table_map.source_table
            if table not in source_tables and table_map.optional_source:
                continue
            if table not in source_tables:
                raise RuntimeError(f"Mapped source table is absent: {source_key}/{table}")
            spec = source_tables[table]
            source_columns = tuple(column[1] for column in spec["columns"])
            if source_key in {"main", "hyperliquid"} and table == "hl_news_metadata":
                reports.append(_import_hl_news_metadata_streaming(
                    sqlite, source_key, spec, table_map, batch_size=batch_size,
                ))
                continue
            target_name = dict(table_map.column_map)
            if table_map.transform == "search_postings":
                report = _import_search_postings(
                    sqlite, source_key, spec, table_map, batch_size=batch_size,
                    max_batch_bytes=max_batch_bytes, max_row_bytes=max_row_bytes,
                )
                reports.append(report)
                continue
            destination_columns = _destination_columns(source_columns, table_map)
            source_pk = tuple(column[1] for column in sorted(spec["columns"], key=lambda column: column[5]) if column[5])
            destination_pk = tuple(target_name.get(name, name) for name in source_pk)
            if not source_pk:
                raise RuntimeError(f"Native import requires a declared source primary key: {source_key}/{table}")
            if table_map.legacy_typed:
                _ensure_legacy_target(spec, table_map)
            with _connect_native_import_postgres(schema=table_map.target_schema, application_name="odaily-native-sqlite-import") as conn:
                target_columns = conn.execute(
                    "SELECT column_name,is_nullable,column_default,udt_name,udt_schema,is_identity FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (table_map.target_schema, table_map.target_table),
                ).fetchall()
                if not target_columns:
                    raise RuntimeError(f"Native PostgreSQL table missing: {table_map.target_schema}.{table_map.target_table}")
                target_by_name = {row["column_name"]: row for row in target_columns}
                missing = sorted(set(destination_columns) - set(target_by_name))
                if missing:
                    raise RuntimeError(f"Unmapped native columns {table_map.target_schema}.{table_map.target_table}: {missing}")
                required_missing = [row["column_name"] for row in target_columns
                                    if row["column_name"] not in destination_columns and row["is_nullable"] == "NO"
                                    and row["column_default"] is None and row["is_identity"] != "YES"]
                if required_missing:
                    raise RuntimeError(f"Required target columns lack source/default: {required_missing}")
                target_keys = conn.execute(
                    "SELECT tc.constraint_name,kcu.column_name FROM information_schema.table_constraints tc "
                    "JOIN information_schema.key_column_usage kcu USING(constraint_catalog,constraint_schema,constraint_name) "
                    "WHERE tc.table_schema=%s AND tc.table_name=%s AND tc.constraint_type IN ('PRIMARY KEY','UNIQUE') "
                    "GROUP BY tc.constraint_name,kcu.ordinal_position,kcu.column_name "
                    "ORDER BY tc.constraint_name,kcu.ordinal_position",
                    (table_map.target_schema, table_map.target_table),
                ).fetchall()
                target_key_sets: dict[str, list[str]] = {}
                for row in target_keys:
                    target_key_sets.setdefault(row["constraint_name"], []).append(row["column_name"])
                if destination_pk not in [tuple(columns) for columns in target_key_sets.values()]:
                    raise RuntimeError(
                        f"Source identity has no matching PostgreSQL primary/unique key for {source_key}/{table}: "
                        f"source={destination_pk}, target={target_key_sets}"
                    )
                effective = replace(
                    table_map,
                    json_columns=tuple(sorted(set(table_map.json_columns) | ({source for source, dest in zip(source_columns, destination_columns, strict=True) if target_by_name[dest]["udt_name"] in {"json","jsonb"}} if table_map.transform is None else set()))),
                    timestamp_columns=tuple(sorted(set(table_map.timestamp_columns) | ({source for source, dest in zip(source_columns, destination_columns, strict=True) if target_by_name[dest]["udt_name"] in {"timestamp","timestamptz"}} if table_map.transform is None else set()))),
                )
                target_identifier = sql.Identifier(table_map.target_schema, table_map.target_table)
                checkpoint_identifier = sql.Identifier(table_map.target_schema, "native_sqlite_import_checkpoints")
                before = int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(target_identifier)).fetchone()["n"])
                conn.execute(sql.SQL(
                    "CREATE TABLE IF NOT EXISTS {}("
                    "source_key text NOT NULL,source_table text NOT NULL,schema_sha256 text NOT NULL,"
                    "last_identity jsonb,imported_rows bigint NOT NULL DEFAULT 0,digest_sum text NOT NULL DEFAULT '0',"
                    "source_rows bigint,source_digest_sum text,destination_base_count bigint NOT NULL DEFAULT 0,"
                    "PRIMARY KEY(source_key,source_table))"
                ).format(checkpoint_identifier))
                conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS source_rows bigint").format(checkpoint_identifier))
                conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS source_digest_sum text").format(checkpoint_identifier))
                conn.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS destination_base_count bigint NOT NULL DEFAULT 0").format(checkpoint_identifier))
                checkpoint = conn.execute(sql.SQL(
                    "SELECT schema_sha256,last_identity,imported_rows,digest_sum,source_rows,source_digest_sum,destination_base_count "
                    "FROM {} WHERE source_key=%s AND source_table=%s"
                ).format(checkpoint_identifier), (source_key, table)).fetchone()
                if checkpoint and checkpoint["schema_sha256"] != hashlib.sha256(json.dumps({"schema": spec, "mapping": table_map},sort_keys=True,default=str).encode()).hexdigest():
                    raise RuntimeError(f"Source schema changed during native import: {source_key}/{table}")
            identity_sql = ",".join(_sqlite_identifier(key) for key in source_pk)
            quoted_table = _sqlite_identifier(table)
            source_count: int | None = None
            source_digest_sum: int | None = None
            schema_sha = hashlib.sha256(json.dumps({"schema": spec, "mapping": table_map},sort_keys=True,default=str).encode()).hexdigest()
            last_identity = tuple(checkpoint["last_identity"]) if checkpoint and checkpoint["last_identity"] else None
            imported = int(checkpoint["imported_rows"]) if checkpoint else 0
            digest_sum = int(checkpoint["digest_sum"] or "0") if checkpoint else 0
            destination_base_count = int(checkpoint["destination_base_count"]) if checkpoint else before
            if before != destination_base_count + imported:
                raise RuntimeError(f"Native target changed during import: {table_map.target_schema}.{table_map.target_table}")
            if checkpoint:
                source_count = 0
                source_digest_sum = 0
                full_source_fingerprint = checkpoint["source_rows"] is not None
                source_cursor = sqlite.execute(f"SELECT * FROM {quoted_table} ORDER BY {identity_sql}")
                def source_row_digest(row) -> int:
                    values = _convert_source_row(
                        row, source_key=source_key, table=table, source_pk=source_pk,
                        source_columns=source_columns, mapping=effective, target_columns=target_by_name,
                    )
                    return int.from_bytes(hashlib.sha256(
                        _canonical_json(_canonical_converted(destination_columns, values, effective, target_by_name)).encode()
                    ).digest(), "big")
                if not full_source_fingerprint:
                    prefix_rows = []
                    for row in source_cursor:
                        if last_identity is not None:
                            prefix_rows.append(row)
                            if tuple(row[key] for key in source_pk) == last_identity:
                                break
                    _validate_checkpoint_source_prefix(
                        rows=prefix_rows, source_pk=source_pk, last_identity=last_identity,
                        imported_rows=imported, digest_sum=digest_sum, row_digest=source_row_digest,
                    )
                    source_count = None
                    source_digest_sum = None
                else:
                    for row in source_cursor:
                        source_count += 1
                        source_digest_sum = (source_digest_sum + source_row_digest(row)) % (1 << 256)
                    if (int(checkpoint["source_rows"]) != source_count
                            or checkpoint["source_digest_sum"] != str(source_digest_sum)):
                        raise RuntimeError(f"Source rows changed during native import: {source_key}/{table}")
            while True:
                if last_identity is None:
                    batch_cursor = sqlite.execute(
                        f"SELECT * FROM {quoted_table} ORDER BY {identity_sql} LIMIT ?", (batch_size,),
                    )
                    batch = _BoundedCursor(batch_cursor).fetch(batch_size, max_batch_bytes, max_row_bytes)
                else:
                    comparison = "(" + identity_sql + ")>(" + ",".join("?" for _ in source_pk) + ")"
                    batch_cursor = sqlite.execute(
                        f"SELECT * FROM {quoted_table} WHERE {comparison} ORDER BY {identity_sql} LIMIT ?",
                        (*last_identity, batch_size),
                    )
                    batch = _BoundedCursor(batch_cursor).fetch(batch_size, max_batch_bytes, max_row_bytes)
                if not batch:
                    break
                converted = []
                for row in batch:
                    values = _convert_source_row(
                        row, source_key=source_key, table=table, source_pk=source_pk,
                        source_columns=source_columns, mapping=effective, target_columns=target_by_name,
                    )
                    converted.append(values)
                    canonical = _canonical_json(_canonical_converted(destination_columns, values, effective, target_by_name)).encode()
                    digest_sum = (digest_sum + int.from_bytes(hashlib.sha256(canonical).digest(), "big")) % (1 << 256)
                with _connect_native_import_postgres(schema=table_map.target_schema, application_name="odaily-native-sqlite-import") as conn:
                    with conn.transaction():
                        conn.execute("SET CONSTRAINTS ALL DEFERRED")
                        owned_triggers = _owned_outbox_triggers(conn, table_map)
                        for trigger in owned_triggers:
                            conn.execute(sql.SQL("ALTER TABLE {} DISABLE TRIGGER {}").format(target_identifier, sql.Identifier(trigger)))
                        with conn.cursor() as pg_cursor:
                            copy_columns = sql.SQL(",").join(sql.Identifier(name) for name in destination_columns)
                            copy_query = sql.SQL("COPY {} ({}) FROM STDIN").format(target_identifier, copy_columns)
                            with pg_cursor.copy(copy_query) as copy:
                                for values in converted:
                                    copy.write_row(values)
                        for trigger in owned_triggers:
                            conn.execute(sql.SQL("ALTER TABLE {} ENABLE TRIGGER {}").format(target_identifier, sql.Identifier(trigger)))
                        last_identity = tuple(batch[-1][key] for key in source_pk)
                        imported += len(converted)
                        conn.execute(sql.SQL(
                            "INSERT INTO {}(source_key,source_table,schema_sha256,last_identity,imported_rows,digest_sum,source_rows,source_digest_sum,destination_base_count) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_key,source_table) DO UPDATE SET "
                            "schema_sha256=excluded.schema_sha256,last_identity=excluded.last_identity,"
                            "imported_rows=excluded.imported_rows,digest_sum=excluded.digest_sum,"
                            "source_rows=excluded.source_rows,source_digest_sum=excluded.source_digest_sum,"
                            "destination_base_count=excluded.destination_base_count"
                        ).format(sql.Identifier(table_map.target_schema, "native_sqlite_import_checkpoints")),
                            (source_key,table,schema_sha,Jsonb([_json_value(value) for value in last_identity]),imported,str(digest_sum),source_count,str(source_digest_sum) if source_digest_sum is not None else None,destination_base_count),
                        )
            if source_count is None:
                source_count = imported
                source_digest_sum = digest_sum
            if checkpoint is None or checkpoint["source_rows"] is None:
                with _connect_native_import_postgres(schema=table_map.target_schema, application_name="odaily-native-sqlite-checkpoint") as conn:
                    conn.execute(sql.SQL(
                        "UPDATE {} SET source_rows=%s,source_digest_sum=%s WHERE source_key=%s AND source_table=%s"
                    ).format(sql.Identifier(table_map.target_schema, "native_sqlite_import_checkpoints")),
                        (source_count, str(source_digest_sum), source_key, table),
                    )
            with _connect_native_import_postgres(schema=table_map.target_schema, application_name="odaily-native-sqlite-verify") as conn:
                after = int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(target_identifier)).fetchone()["n"])
                verify_digest = 0
                verified_rows = 0
                with conn.transaction():
                    verify_cursor = conn.cursor()
                    source_verify_cursor = sqlite.execute(f"SELECT * FROM {quoted_table} ORDER BY {identity_sql}")
                    source_verify_bounded = _BoundedCursor(source_verify_cursor)
                    columns_sql = sql.SQL(",").join(
                        sql.SQL("{}::text AS {}").format(sql.Identifier(name), sql.Identifier(name))
                        if target_by_name[name]["udt_name"] in {"json", "jsonb"} else sql.Identifier(name)
                        for name in destination_columns
                    )
                    json_names = [name for name in destination_columns if target_by_name[name]["udt_name"] in {"json", "jsonb"}]
                    json_null_flags = sql.SQL(",").join(
                        sql.SQL("({} IS NULL) AS {}").format(sql.Identifier(name), sql.Identifier(f"__is_null_{name}"))
                        for name in json_names
                    )
                    if json_names:
                        columns_sql = sql.SQL("{},{}").format(columns_sql, json_null_flags)
                    target_columns_sql = sql.SQL(",").join(sql.Identifier("target", name) for name in destination_pk)
                    while source_verify_batch := source_verify_bounded.fetch(batch_size, max_batch_bytes, max_row_bytes):
                        identities = [tuple(row[key] for key in source_pk) for row in source_verify_batch]
                        key_columns = sql.SQL(",").join(sql.Identifier(name) for name in destination_pk)
                        key_row = sql.SQL("(") + sql.SQL(",").join(
                            sql.SQL("CAST({} AS {})").format(
                                sql.Placeholder(),
                                sql.Identifier(target_by_name[name]["udt_schema"], target_by_name[name]["udt_name"]),
                            )
                            for name in destination_pk
                        ) + sql.SQL(")")
                        key_values = sql.SQL(",").join(key_row for _ in identities)
                        identity_params = tuple(value for identity in identities for value in identity)
                        verify_cursor.execute(
                            sql.SQL("WITH verify_keys ({}) AS (VALUES {}) SELECT {} FROM {} AS target "
                                    "JOIN verify_keys USING ({}) ORDER BY {}").format(
                                key_columns, key_values,
                                sql.SQL(",").join(
                                    sql.SQL("target.{}::text AS {}").format(sql.Identifier(name), sql.Identifier(name))
                                    if target_by_name[name]["udt_name"] in {"json", "jsonb"}
                                    else sql.SQL("target.{} AS {}").format(sql.Identifier(name), sql.Identifier(name))
                                    for name in destination_columns
                                ) + (sql.SQL(",") + sql.SQL(",").join(
                                    sql.SQL("(target.{} IS NULL) AS {}").format(sql.Identifier(name), sql.Identifier(f"__is_null_{name}"))
                                    for name in json_names
                                ) if json_names else sql.SQL("")),
                                target_identifier, key_columns, target_columns_sql,
                            ), identity_params,
                        )
                        destination_rows = verify_cursor.fetchall()
                        if len(destination_rows) != len(source_verify_batch):
                            raise RuntimeError(f"Native target is missing source identities: {source_key}/{table}")
                        for destination_row in destination_rows:
                            normalized = {}
                            for name in destination_columns:
                                value = destination_row[name]
                                if target_by_name[name]["udt_name"] in {"json", "jsonb"}:
                                    normalized[name] = ({"type": "sql_null"} if destination_row[f"__is_null_{name}"]
                                                        else {"type": "json", "value": _decode_json(value)})
                                elif target_by_name[name]["udt_name"] == "bytea" and value is not None:
                                    normalized[name] = {"blob_hex": bytes(value).hex()}
                                elif target_by_name[name]["udt_name"] == "date" and value is not None:
                                    normalized[name] = value.isoformat()
                                else:
                                    normalized[name] = _json_value(value)
                            canonical = _canonical_json(normalized).encode()
                            verify_digest = (verify_digest + int.from_bytes(hashlib.sha256(canonical).digest(), "big")) % (1 << 256)
                            verified_rows += 1
                    verify_cursor.close()
            if source_count != imported or after != destination_base_count + imported or verified_rows != source_count or verify_digest != digest_sum or source_digest_sum != digest_sum:
                raise RuntimeError(f"Native import verification failed {source_key}/{table}: source={source_count} imported={imported} before={before} after={after}")
            sequence_value = _source_sequence_value(sqlite, table)
            sequence_columns = [
                name for name in source_pk
                if target_by_name[target_name.get(name, name)]["is_identity"] == "YES"
                or (target_by_name[target_name.get(name, name)]["column_default"] and "nextval(" in target_by_name[target_name.get(name, name)]["column_default"])
            ]
            if sequence_columns:
                if len(sequence_columns) != 1 or len(source_pk) != 1:
                    raise RuntimeError(f"Native importer only reconciles single-column identity primary keys: {source_key}/{table}")
                sequence_column = target_name.get(sequence_columns[0], sequence_columns[0])
                with _connect_native_import_postgres(schema=table_map.target_schema, application_name="odaily-native-sqlite-sequence") as conn:
                    max_value = conn.execute(sql.SQL("SELECT max({}) AS n FROM {}").format(sql.Identifier(sequence_column), target_identifier)).fetchone()["n"]
                    high_water = max(int(sequence_value or 0), int(max_value or 0))
                    sequence_name = conn.execute("SELECT pg_get_serial_sequence(%s,%s) AS name", (f"{table_map.target_schema}.{table_map.target_table}", sequence_column)).fetchone()["name"]
                    if sequence_name and high_water:
                        conn.execute("SELECT setval(%s::regclass,%s,true)", (sequence_name, high_water))
            reports.append({"source_table": table, "target_schema": table_map.target_schema,
                            "target_table": table_map.target_table, "source_rows": source_count,
                            "imported_rows": imported, "destination_before": before,
                            "destination_after": after, "last_identity": list(last_identity) if last_identity else None,
                            "source_sequence_high_water": sequence_value,
                            "source_digest_sum": digest_sum.to_bytes(32,"big").hex()})
        mapping_by_source = {table.source_table: table for table in mapping.tables}
        fk_audits = []
        for child in mapping.tables:
            groups: dict[int, list[sqlite3.Row | tuple[Any, ...]]] = {}
            for fk in sqlite.execute(f"PRAGMA foreign_key_list({_sqlite_identifier(child.source_table)})"):
                groups.setdefault(int(fk[0]), []).append(fk)
            for fk_rows in groups.values():
                fk_rows.sort(key=lambda row: int(row[1]))
                parent = mapping_by_source.get(str(fk_rows[0][2]))
                if parent is None or parent.target_schema != child.target_schema:
                    continue
                pairs = [(dict(child.column_map).get(str(fk[3]), str(fk[3])),
                          dict(parent.column_map).get(str(fk[4]), str(fk[4]))) for fk in fk_rows]
                join = sql.SQL(" AND ").join(
                    sql.SQL("c.{}=p.{}").format(sql.Identifier(child_column), sql.Identifier(parent_column))
                    for child_column, parent_column in pairs
                )
                nonnull = sql.SQL(" AND ").join(
                    sql.SQL("c.{} IS NOT NULL").format(sql.Identifier(child_column))
                    for child_column, _ in pairs
                )
                with _connect_native_import_postgres(schema=child.target_schema, application_name="odaily-native-sqlite-fk-audit") as conn:
                    orphan_count = int(conn.execute(sql.SQL(
                        "SELECT count(*) AS n FROM {} c WHERE {} AND NOT EXISTS "
                        "(SELECT 1 FROM {} p WHERE {})"
                    ).format(sql.Identifier(child.target_schema, child.target_table), nonnull,
                         sql.Identifier(parent.target_schema, parent.target_table), join)).fetchone()["n"])
                if orphan_count:
                    raise RuntimeError(f"PostgreSQL foreign-key orphan audit failed: {child.source_table}{pairs} -> {parent.source_table}: {orphan_count}")
                fk_audits.append({"child_table": child.source_table, "column_pairs": pairs,
                                  "parent_table": parent.source_table,
                                  "orphan_rows": 0})
        if source_key == "system_information":
            from packages.system_observability.postgres_store import PostgresSystemInformationStore

            store = PostgresSystemInformationStore()
            sqlite.row_factory = sqlite3.Row
            identity_cursor = sqlite.execute(
                "SELECT source_database,source_table,source_row_id,item_id FROM system_information_items "
                "WHERE source_database IS NOT NULL AND source_table IS NOT NULL "
                "AND source_row_id IS NOT NULL AND item_id IS NOT NULL ORDER BY item_id"
            )
            identity_count = 0
            identity_conflicts: set[tuple[str, str, str]] = set()
            identity_conflict_count = 0
            while rows := identity_cursor.fetchmany(2000):
                before_conflicts = len(identity_conflicts)
                identity_count += store.register_imported_source_identities(
                    (dict(row) for row in rows),
                    source_schema_map=_observability_source_schema_map(),
                    max_batch=2000,
                    skip_identities=identity_conflicts,
                )
                identity_conflict_count += len(identity_conflicts) - before_conflicts
            return {"source_key": source_key, "source_path": str(path), "tables": reports,
                    "table_count": len(reports), "foreign_key_audits": fk_audits,
                    "source_identities_registered": identity_count,
                    "source_identity_conflicts_skipped": identity_conflict_count}
        return {"source_key": source_key, "source_path": str(path), "tables": reports,
                "table_count": len(reports), "foreign_key_audits": fk_audits}
    finally:
        sqlite.close()


def _initialize_main_schema() -> None:
    __import__("packages.x_processing.repository", fromlist=["PostgresXProcessingRepository"]).PostgresXProcessingRepository.initialize_schema()
    __import__("packages.external_media_alert.repository", fromlist=["PostgresExternalMediaAlertRepository"]).PostgresExternalMediaAlertRepository().init_schema()
    __import__("packages.competitor_monitor.repository", fromlist=["PostgresCompetitorMonitorRepository"]).PostgresCompetitorMonitorRepository().init_schema()
    __import__("packages.writer3.repository", fromlist=["PostgresWriter3Repository"]).PostgresWriter3Repository().init_schema()
    __import__("packages.x_capture.repository", fromlist=["PostgresXCaptureRepository"]).PostgresXCaptureRepository.initialize_schema()
    # Initializers must install DDL only; constructors may seed default users.
    __import__("packages.common.postgres_local_auth", fromlist=["PostgresLocalAuthRepository"]).PostgresLocalAuthRepository.initialize_schema()
    __import__("packages.common.postgres_editor_plugin_auth", fromlist=["PostgresEditorPluginAuthRepository"]).PostgresEditorPluginAuthRepository.initialize_schema()
    _initialize_editor_plugin_history_schema()
    __import__("packages.editor_plugin_postgres_local_store", fromlist=["PostgresEditorPluginLocalStore"]).PostgresEditorPluginLocalStore.initialize_schema()
    __import__("packages.auditor.repository", fromlist=["PostgresAuditorRepository"]).PostgresAuditorRepository().init_schema()
    __import__("packages.fact_auditor.postgres_repository", fromlist=["PostgresFactReviewRepository"]).PostgresFactReviewRepository().init_schema()
    __import__("packages.non_mainstream_media.repository", fromlist=["PostgresNonMainstreamMediaRepository"]).PostgresNonMainstreamMediaRepository().init_schema()
    __import__("packages.jin10_monitor.repository", fromlist=["PostgresJin10MonitorRepository"]).PostgresJin10MonitorRepository().init_schema()
    __import__("packages.maintenance.repository", fromlist=["PostgresMaintenanceRepository"]).PostgresMaintenanceRepository().init_schema()
    __import__("packages.pipeline_supervisor.repository", fromlist=["PostgresPipelineSupervisorRepository"]).PostgresPipelineSupervisorRepository().init_schema()
    __import__("packages.binance_square.postgres_repository", fromlist=["PostgresBinanceSquareRepository"]).PostgresBinanceSquareRepository.initialize_schema()
    from packages.postgres_newsflash_operations import CHECKSUM, SCHEMA_SQL
    from packages.common.pg_native import PostgresMigration, apply_schema_migrations
    with connect_postgres(schema="main", application_name="odaily-newsflash-operations-migrate") as conn:
        apply_schema_migrations(conn, schema="main", component="newsflash_operations", migrations=(
            PostgresMigration(1, "newsflash operations native schema", CHECKSUM, lambda c: c.execute(SCHEMA_SQL)),
        ))
    __import__("packages.whale_watch.repository", fromlist=["PostgresWhaleWatchRepository"]).PostgresWhaleWatchRepository.initialize_schema()
    __import__("packages.whale_watch.postgres_hyperliquid_repository", fromlist=["PostgresWhaleWatchHyperliquidRepository"]).PostgresWhaleWatchHyperliquidRepository.initialize_schema()


def _initialize_editor_plugin_history_schema() -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS editor_plugin_feedbacks (
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, feed_item_id text NOT NULL,
            feed_kind text NOT NULL, feedback text NOT NULL, actor_user_id text,
            actor_email text NOT NULL, actor_display_name text, acted_at timestamptz NOT NULL DEFAULT now(),
            session_id text, extra_json jsonb NOT NULL DEFAULT '{}'::jsonb, created_at timestamptz NOT NULL DEFAULT now())""",
        """CREATE TABLE IF NOT EXISTS editor_plugin_receipts (
            id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, feed_item_id text NOT NULL,
            feed_kind text NOT NULL,
            viewer_user_id text, viewer_email text NOT NULL, viewer_display_name text,
            first_seen_at timestamptz NOT NULL DEFAULT now(), last_seen_at timestamptz NOT NULL DEFAULT now(),
            seen_count integer NOT NULL DEFAULT 1, session_id text, extra_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(feed_item_id,feed_kind,viewer_email))""",
    )
    with connect_postgres(schema="main", application_name="odaily-editor-plugin-history-migrate") as conn:
        for statement in statements:
            conn.execute(statement)


SCHEMA_INITIALIZERS: dict[str, Any] = {
    "predict_market_discovery": lambda: __import__("packages.predict_market_discovery.postgres_store", fromlist=["PostgresPredictMarketStore"]).PostgresPredictMarketStore.initialize_schema(),
    "predict_html_discovery": lambda: __import__("packages.predict_market_discovery.postgres_html_store", fromlist=["PostgresHtmlDiscoveryStore"]).PostgresHtmlDiscoveryStore.initialize_schema(),
    "macro_newsflash": lambda: __import__("packages.macro_newsflash.postgres_store", fromlist=["PostgresMacroNewsflashStore"]).PostgresMacroNewsflashStore.initialize_schema(),
    "gate_market": lambda: __import__("packages.gate_market_broadcast.postgres_store", fromlist=["PostgresGateMarketStore"]).PostgresGateMarketStore.initialize_schema(),
    "stock_market": lambda: __import__("packages.stock_market_broadcast.postgres_store", fromlist=["PostgresStockMarketStore"]).PostgresStockMarketStore.initialize_schema(),
    "writer3": lambda: __import__("packages.writer3.postgres_index", fromlist=["PostgresWriter3Index"]).PostgresWriter3Index.initialize_schema(),
    "searcher": lambda: __import__("packages.x_processing.postgres_search_cache", fromlist=["PostgresSearchCache"]).PostgresSearchCache.initialize_schema(),
    "msx_notice": lambda: __import__("packages.postgres_msx_notice_store", fromlist=["PostgresMSXNoticeStore"]).PostgresMSXNoticeStore.initialize_schema(),
    "editor_plugin_local": lambda: __import__("packages.editor_plugin_postgres_local_store", fromlist=["PostgresEditorPluginLocalStore"]).PostgresEditorPluginLocalStore.initialize_schema(),
    "editor_plugin_auth": lambda: __import__("packages.common.postgres_editor_plugin_auth", fromlist=["PostgresEditorPluginAuthRepository"]).PostgresEditorPluginAuthRepository.initialize_schema(),
    "ai_review": lambda: __import__("packages.ai_review_postgres", fromlist=["PostgresAIReviewRepository"]).PostgresAIReviewRepository.initialize_schema(),
    "business_news": lambda: __import__("packages.business_news.postgres_repository", fromlist=["PostgresBusinessNewsRepository"]).PostgresBusinessNewsRepository.initialize_schema(),
    "ai_text_self_upgrade_observability": lambda: __import__("packages.ai_text_self_upgrade.postgres_repository", fromlist=["PostgresAITextSelfUpgradeRepository"]).PostgresAITextSelfUpgradeRepository.initialize_schema(),
    "system_information": lambda: _initialize_system_information_schema(),
    "small_evidence": lambda: __import__("packages.system_observability.postgres_store", fromlist=["PostgresSystemInformationStore"]).PostgresSystemInformationStore.initialize_schema(),
    "local_pipeline": lambda: __import__("packages.local_pipeline.postgres_queue", fromlist=["PostgresLocalPipelineQueue"]).PostgresLocalPipelineQueue.initialize_schema(),
    "meme_scanner": lambda: __import__("packages.meme_scanner.postgres_store", fromlist=["PostgresMemeStore"]).PostgresMemeStore.initialize_schema(),
    "pipeline_timing": lambda: _initialize_pipeline_timing_schema(),
    "hottopic": lambda: _initialize_hottopic_schema(),
    "hyperliquid": lambda: _initialize_hyperliquid_schema(),
    "main": _initialize_main_schema,
}


def _initialize_hyperliquid_schema() -> None:
    from packages.hyperliquid_news.postgres_store import PostgresNewsStore
    from pathlib import Path
    PostgresNewsStore.initialize_schema()
    from packages.hyperliquid_news.postgres_naming import PostgresNamingStore
    PostgresNamingStore.initialize_schema()
    from packages.whale_watch.postgres_hyperliquid_repository import PostgresWhaleWatchHyperliquidRepository
    PostgresWhaleWatchHyperliquidRepository.initialize_schema()


def _initialize_hottopic_schema() -> None:
    from packages.hottopic.postgres_schema import initialize_hottopic_schema
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema

    initialize_hottopic_schema()
    initialize_investment_leads_schema()


def _initialize_pipeline_timing_schema() -> None:
    from packages.pipeline_timing import PostgresPipelineTimingLocalStore
    PostgresPipelineTimingLocalStore.initialize_schema()


def _observability_source_schema_map() -> dict[str, str]:
    return {
        "/opt/OdAIly/data/database/odaily.sqlite": "main",
        "/data/OdAIly/system-observability/system_information.sqlite": "system_observability",
        "/opt/OdAIly/data/runtime/hottopic.sqlite": "hottopic",
        "/data/OdAIly/hyperliquid/hyperliquid.sqlite": "hyperliquid_news",
        "/data/OdAIly/hottopic/hottopic.sqlite": "hottopic",
        "/data/OdAIly/meme/meme.sqlite": "meme",
        "/data/OdAIly/business/business.sqlite": "business_news",
        "/data/OdAIly/gate/gate.sqlite": "gate",
        "/data/OdAIly/stock/stock.sqlite": "stock",
        "/data/OdAIly/local_pipeline/local_pipeline.sqlite": "local_pipeline",
        "/data/OdAIly/predict/predict.sqlite": "predict_market",
        "/data/OdAIly/macro_newsflash/macro_newsflash.sqlite": "macro_newsflash",
        "/opt/OdAIly/data/system-observability/system_information/small_evidence.sqlite": "system_observability",
        "small_evidence.sqlite": "system_observability",
        "ai_text_self_upgrade.sqlite": "system_observability",
        "odaily.sqlite": "main",
        "system_information.sqlite": "system_observability",
        "hyperliquid.sqlite": "hyperliquid_news",
        "hottopic.sqlite": "hottopic",
        "meme_scanner.sqlite3": "meme",
        "business_news.sqlite": "business_news",
        "gate_market.sqlite": "gate",
        "stock_market.sqlite": "stock",
        "local_pipeline.sqlite": "local_pipeline",
        "predict_market_discovery.sqlite": "predict_market",
        "macro_newsflash.sqlite": "macro_newsflash",
        "editor_plugin_local.sqlite": "main",
        "pipeline_timing.sqlite": "pipeline_timing",
        "ai_review.sqlite": "system_observability",
        "predict_html_discovery.sqlite": "main",
        "msx_notice.sqlite": "main",
        "searcher.sqlite": "main",
        "competitor_monitor.sqlite": "main",
        "writer3.sqlite": "main",
    }


def _initialize_system_information_schema() -> None:
    from packages.system_observability.postgres_store import PostgresSystemInformationStore
    PostgresSystemInformationStore.initialize_native_schema()


def initialize_native_schemas(source_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Create native destination schemas without running domain constructors or seed logic."""
    selected = source_keys or tuple(NATIVE_SOURCES)
    unknown = sorted(set(selected) - set(NATIVE_SOURCES))
    if unknown:
        raise ValueError(f"Unknown native source keys: {unknown}")
    native_selected = tuple(key for key in selected if any(not table.legacy_typed for table in NATIVE_SOURCES[key].tables))
    missing = sorted(key for key in native_selected if key not in SCHEMA_INITIALIZERS)
    if missing:
        raise RuntimeError(f"Native schema-only initializers are not implemented: {missing}")
    initialized = []
    for key in native_selected:
        SCHEMA_INITIALIZERS[key]()
        initialized.append(key)
    return {"initialized": initialized, "missing_schema_initializers": missing}


def preflight_native_sqlite_source(*, source_key: str, source_path: Path,
                                   source_mapping: SourceMapping | None = None) -> dict[str, Any]:
    mapping = source_mapping or NATIVE_SOURCES.get(source_key)
    if mapping is None or mapping.source_key != source_key:
        raise ValueError(f"No native SQLite mapping registered for source {source_key!r}")
    path = source_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as source:
        schema = _schema(source)
        tables = {item["name"]: item for item in schema}
        unknown = sorted(set(tables) - {item.source_table for item in mapping.tables} - set(mapping.retired_tables))
        findings = []
        for item in mapping.tables:
            if item.source_table not in tables:
                if not item.optional_source:
                    findings.append({"table": item.source_table, "error": "missing source table"})
                continue
            source_spec = tables[item.source_table]
            columns = tuple(column[1] for column in source_spec["columns"])
            pk = tuple(column[1] for column in sorted(source_spec["columns"], key=lambda column: column[5]) if column[5])
            findings.append({"table": item.source_table, "columns": columns, "primary_key": pk,
                             "target": f"{item.target_schema}.{item.target_table}",
                             "legacy_typed": item.legacy_typed, "column_map": dict(item.column_map)})
        return {"source_key": source_key, "source_path": str(path), "source_tables": sorted(tables),
                "unknown_tables": unknown, "findings": findings,
                "ready_for_import": not unknown and not any(item.get("error") for item in findings)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preflight or import one native SQLite source")
    parser.add_argument("source_key", nargs="?", choices=sorted(NATIVE_SOURCES))
    parser.add_argument("source_path", nargs="?", type=Path)
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--initialize-schemas", action="store_true",
                        help="install all registered PostgreSQL schemas without runtime seed data")
    parser.add_argument("--register-identities-only", action="store_true",
                        help="rebuild imported source identity indexes without reimporting source tables")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--max-batch-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--max-row-bytes", type=int, default=64 * 1024 * 1024)
    args = parser.parse_args(argv)
    if args.initialize_schemas:
        if args.source_key or args.source_path or args.preflight:
            parser.error("--initialize-schemas does not accept source arguments or --preflight")
        result = initialize_native_schemas()
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0
    if args.register_identities_only:
        if args.source_key != "system_information" or args.preflight:
            parser.error("--register-identities-only requires source_key system_information and does not accept --preflight")
        result = register_native_imported_source_identities(source_path=args.source_path, batch_size=args.batch_size)
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
        return 0
    if not args.source_key or args.source_path is None:
        parser.error("source_key and source_path are required unless --initialize-schemas is used")
    result = (preflight_native_sqlite_source(source_key=args.source_key, source_path=args.source_path)
              if args.preflight else import_native_sqlite_source(
                  source_key=args.source_key, source_path=args.source_path, batch_size=args.batch_size,
                  max_batch_bytes=args.max_batch_bytes, max_row_bytes=args.max_row_bytes))
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    return 0 if (not args.preflight or result["ready_for_import"]) else 1


def _schema(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for name, create_sql in connection.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        columns = connection.execute(f'PRAGMA table_info("{name}")').fetchall()
        result.append({"name": name, "create_sql": create_sql, "columns": [tuple(row) for row in columns]})
    return result


def _row_size(row: sqlite3.Row | tuple[Any, ...]) -> int:
    total = 0
    for value in row:
        if isinstance(value, str):
            total += len(value.encode("utf-8"))
        elif isinstance(value, (bytes, bytearray, memoryview)):
            total += len(value)
        elif value is not None:
            total += 16
    return total


_HL_METADATA_CHUNK_BYTES = 1024 * 1024
_HL_METADATA_STREAM_PROTOCOL = "utf8-md5-payload-v2"


def _import_hl_news_metadata_streaming(sqlite: sqlite3.Connection, source_key: str,
                                       source_spec: dict[str, Any], mapping: TableMapping,
                                       *, batch_size: int) -> dict[str, Any]:
    """Preserve legacy Hyperliquid TEXT metadata without materializing huge rows."""
    if tuple(column[1] for column in source_spec["columns"]) != ("key", "payload"):
        raise RuntimeError("Unexpected hl_news_metadata source schema")
    if not mapping.legacy_typed and source_key != "hyperliquid":
        raise RuntimeError("hl_news_metadata streaming import requires an approved target mapping")
    target = sql.Identifier(mapping.target_schema, mapping.target_table)
    checkpoint_table = sql.Identifier(mapping.target_schema, "native_sqlite_stream_import_checkpoints")
    checkpoint = None
    with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-schema") as conn:
        if mapping.legacy_typed:
            _ensure_legacy_target(source_spec, mapping)
        conn.execute(sql.SQL(
            "CREATE TABLE IF NOT EXISTS {}(source_key text NOT NULL,source_table text NOT NULL,"
            "schema_sha256 text NOT NULL,last_identity text,imported_rows bigint NOT NULL DEFAULT 0,"
            "digest_sum text NOT NULL DEFAULT '0',source_rows bigint,source_digest_sum text,"
            "destination_base_count bigint NOT NULL DEFAULT 0,PRIMARY KEY(source_key,source_table))"
        ).format(checkpoint_table))
        columns = conn.execute(
            "SELECT column_name,is_nullable,column_default,udt_name,is_identity FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
            (mapping.target_schema, mapping.target_table),
        ).fetchall()
        target_types = [(row["column_name"], row["udt_name"]) for row in columns]
        if target_types not in ([ ("key", "text"), ("payload", "text") ],
                                [ ("key", "text"), ("payload", "jsonb") ]):
            raise RuntimeError("Unexpected hl_news_metadata target schema")
        payload_udt = dict(target_types)["payload"]
        before = int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(target)).fetchone()["n"])
        checkpoint = conn.execute(sql.SQL(
            "SELECT * FROM {} WHERE source_key=%s AND source_table=%s"
        ).format(checkpoint_table), (source_key, "hl_news_metadata")).fetchone()
        schema_sha = hashlib.sha256(json.dumps({"schema": source_spec, "mapping": mapping,
                                               "stream_protocol": _HL_METADATA_STREAM_PROTOCOL},
                                              sort_keys=True, default=str).encode()).hexdigest()
        upgrade_checkpoint = bool(checkpoint and checkpoint["schema_sha256"] != schema_sha)
        legacy_checkpoint_upgrade = upgrade_checkpoint
        if upgrade_checkpoint and int(checkpoint["imported_rows"] or 0) == 0:
            raise RuntimeError("Source schema changed during streamed legacy import")
        imported = int(checkpoint["imported_rows"]) if checkpoint else 0
        last_key = checkpoint["last_identity"] if checkpoint else None
        digest_sum = int(checkpoint["digest_sum"] or "0") if checkpoint else 0
        base_count = int(checkpoint["destination_base_count"]) if checkpoint else before
        if before != base_count + imported:
            raise RuntimeError("Streamed legacy target changed during import")
        if checkpoint is None and before:
            raise RuntimeError("Streamed legacy target is not empty before first import")
        if checkpoint is None:
            conn.execute(sql.SQL(
                "INSERT INTO {}(source_key,source_table,schema_sha256,destination_base_count) VALUES(%s,%s,%s,%s)"
            ).format(checkpoint_table), (source_key, "hl_news_metadata", schema_sha, before))

    checkpoint_last_key = last_key
    source_count = 0
    source_digest_sum = 0
    verified_prefix_count = 0
    prefix_digest = 0
    old_prefix_digest = 0
    rows = sqlite.execute('SELECT rowid,"key",length(CAST("payload" AS BLOB)) FROM "hl_news_metadata" ORDER BY "key"')
    for rowid, key, byte_count in rows:
        key = str(key)
        payload_hash = hashlib.md5(usedforsecurity=False)
        decoder = codecs.getincrementaldecoder("utf-8")()
        with sqlite.blobopen("hl_news_metadata", "payload", int(rowid), readonly=True) as blob:
            remaining = len(blob)
            while remaining:
                raw = blob.read(min(_HL_METADATA_CHUNK_BYTES, remaining))
                if not raw:
                    raise RuntimeError(f"Source payload changed while streaming hl_news_metadata/{key}")
                payload_hash.update(raw)
                decoder.decode(raw, final=False)
                remaining -= len(raw)
            decoder.decode(b"", final=True)
        payload_digest = payload_hash.hexdigest()
        if payload_udt == "jsonb":
            payload_digest = _hl_metadata_source_jsonb_digest(sqlite, int(rowid), key, mapping)
        row_digest = _hl_metadata_row_digest(key, payload_digest)
        old_row_digest = None
        if upgrade_checkpoint and checkpoint_last_key is not None and key <= checkpoint_last_key:
            old_row_digest = _hl_metadata_legacy_row_digest(sqlite, int(rowid), key)
        source_count += 1
        source_digest_sum = (source_digest_sum + row_digest) % (1 << 256)
        if checkpoint_last_key is not None and key <= checkpoint_last_key:
            verified_prefix_count += 1
            prefix_digest = (prefix_digest + row_digest) % (1 << 256)
            old_prefix_digest = (old_prefix_digest + int(old_row_digest or 0)) % (1 << 256)
        valid_checkpoint_digests = {prefix_digest}
        if upgrade_checkpoint:
            valid_checkpoint_digests.add(old_prefix_digest)
        if key == checkpoint_last_key and (verified_prefix_count != imported or digest_sum not in valid_checkpoint_digests):
            raise RuntimeError("Streamed legacy checkpoint prefix mismatch")
        if key == checkpoint_last_key and upgrade_checkpoint:
            if not _verify_hl_metadata_target_prefix(
                    sqlite, int(rowid), key, mapping, payload_udt,
                    expected_v2_digest=prefix_digest, expected_count=imported):
                raise RuntimeError("Existing streamed checkpoint target does not match its SQLite source")
            with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-upgrade") as conn:
                conn.execute(sql.SQL(
                    "UPDATE {} SET schema_sha256=%s,digest_sum=%s WHERE source_key=%s AND source_table=%s"
                ).format(checkpoint_table), (schema_sha, str(prefix_digest), source_key, "hl_news_metadata"))
            digest_sum = prefix_digest
            upgrade_checkpoint = False
        if checkpoint_last_key is not None and key <= checkpoint_last_key:
            continue

        with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-copy") as conn:
            with conn.transaction():
                conn.execute("CREATE TEMP TABLE hl_metadata_stage (key text NOT NULL, part_index bigint NOT NULL, chunk text, PRIMARY KEY(key,part_index)) ON COMMIT DROP")
                with sqlite.blobopen("hl_news_metadata", "payload", int(rowid), readonly=True) as blob:
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    remaining = len(blob)
                    part_index = 0
                    while remaining:
                        raw = blob.read(min(_HL_METADATA_CHUNK_BYTES, remaining))
                        text_chunk = decoder.decode(raw, final=False)
                        if text_chunk:
                            conn.execute("INSERT INTO hl_metadata_stage VALUES(%s,%s,%s)", (key, part_index, text_chunk))
                            part_index += 1
                        remaining -= len(raw)
                    text_chunk = decoder.decode(b"", final=True)
                    if text_chunk:
                        conn.execute("INSERT INTO hl_metadata_stage VALUES(%s,%s,%s)", (key, part_index, text_chunk))
                    if part_index == 0 and not text_chunk:
                        conn.execute("INSERT INTO hl_metadata_stage VALUES(%s,0,'')", (key,))
                payload_expr = "string_agg(chunk,'' ORDER BY part_index)" + ("::jsonb" if payload_udt == "jsonb" else "")
                conn.execute(sql.SQL(
                    "INSERT INTO {}(key,payload) SELECT key," + payload_expr + " "
                    "FROM hl_metadata_stage WHERE key=%s GROUP BY key"
                ).format(target), (key,))
                imported += 1
                last_key = key
                digest_sum = (digest_sum + row_digest) % (1 << 256)
                conn.execute(sql.SQL(
                    "INSERT INTO {}(source_key,source_table,schema_sha256,last_identity,imported_rows,digest_sum,destination_base_count) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_key,source_table) DO UPDATE SET "
                    "last_identity=excluded.last_identity,imported_rows=excluded.imported_rows,digest_sum=excluded.digest_sum"
                ).format(checkpoint_table), (source_key, "hl_news_metadata", schema_sha, last_key, imported, str(digest_sum), base_count))

    if checkpoint_last_key is not None and verified_prefix_count != int(checkpoint["imported_rows"]):
        raise RuntimeError("Streamed legacy checkpoint identity is absent from source")
    with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-verify") as conn:
        after = int(conn.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(target)).fetchone()["n"])
        target_rows = conn.execute(sql.SQL(
            "SELECT key,md5({}::text) AS payload_md5 FROM {} ORDER BY key"
        ).format(sql.Identifier("payload"), target))
        target_digest_sum = 0
        verified_rows = 0
        for target_row in target_rows:
            key, payload_md5 = target_row["key"], target_row["payload_md5"]
            row_digest = _hl_metadata_row_digest(str(key), str(payload_md5))
            target_digest_sum = (target_digest_sum + row_digest) % (1 << 256)
            verified_rows += 1
        # jsonb normalizes JSON text, so its server-side textual digest is not
        # byte-identical to SQLite's raw UTF-8 digest. Row count and source
        # digest remain authoritative; target_digest_sum is diagnostic there.
        digest_mismatch = payload_udt == "text" and target_digest_sum != digest_sum
        if (source_count != imported or after != base_count + imported or verified_rows != source_count
                or source_digest_sum != digest_sum or digest_mismatch):
            raise RuntimeError(f"Streamed legacy verification failed main/hl_news_metadata: source={source_count} imported={imported}")
        if checkpoint and not legacy_checkpoint_upgrade and checkpoint["source_rows"] is not None and (
                int(checkpoint["source_rows"]) != source_count or checkpoint["source_digest_sum"] != str(source_digest_sum)):
            raise RuntimeError("Source rows changed during streamed legacy import")
        conn.execute(sql.SQL("UPDATE {} SET source_rows=%s,source_digest_sum=%s WHERE source_key=%s AND source_table=%s").format(checkpoint_table),
                     (source_count, str(source_digest_sum), source_key, "hl_news_metadata"))
    return {"source_table": "hl_news_metadata", "target_schema": mapping.target_schema,
            "target_table": mapping.target_table, "source_rows": source_count, "imported_rows": imported,
            "destination_before": before, "destination_after": after, "last_identity": last_key,
            "source_digest_sum": digest_sum.to_bytes(32, "big").hex(), "streamed_payload": True}


def _hl_metadata_row_digest(key: str, payload_md5: str) -> int:
    return int.from_bytes(hashlib.sha256(
        _canonical_json({"key": key, "payload_md5": payload_md5}).encode("utf-8")
    ).digest(), "big")


def _hl_metadata_legacy_row_digest(sqlite: sqlite3.Connection, rowid: int, key: str) -> int:
    """Reproduce the old whole-row canonical digest while reading TEXT incrementally."""
    hasher = hashlib.sha256()
    hasher.update(b'{"key":')
    hasher.update(_canonical_json(key).encode("utf-8"))
    hasher.update(b',"payload":')
    hasher.update(b'"')
    decoder = codecs.getincrementaldecoder("utf-8")()
    with sqlite.blobopen("hl_news_metadata", "payload", rowid, readonly=True) as blob:
        while raw := blob.read(_HL_METADATA_CHUNK_BYTES):
            text = decoder.decode(raw, final=False)
            if text:
                hasher.update(_canonical_json_string_fragment(text).encode("utf-8"))
        text = decoder.decode(b"", final=True)
        if text:
            hasher.update(_canonical_json_string_fragment(text).encode("utf-8"))
    hasher.update(b'"}')
    return int.from_bytes(hasher.digest(), "big")


def _canonical_json_string_fragment(value: str) -> str:
    return "".join(json.dumps(char, ensure_ascii=False)[1:-1] for char in value)


def _hl_metadata_source_jsonb_digest(sqlite: sqlite3.Connection, rowid: int, key: str,
                                     mapping: TableMapping) -> str:
    target = sql.Identifier(mapping.target_schema, mapping.target_table)
    with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-jsonb-digest") as conn:
        with conn.transaction():
            conn.execute("CREATE TEMP TABLE hl_metadata_jsonb_digest_stage (part_index bigint PRIMARY KEY, chunk text) ON COMMIT DROP")
            with sqlite.blobopen("hl_news_metadata", "payload", rowid, readonly=True) as blob:
                decoder = codecs.getincrementaldecoder("utf-8")()
                remaining = len(blob)
                index = 0
                while remaining:
                    raw = blob.read(min(_HL_METADATA_CHUNK_BYTES, remaining))
                    text = decoder.decode(raw, final=False)
                    if text:
                        conn.execute("INSERT INTO hl_metadata_jsonb_digest_stage VALUES(%s,%s)", (index, text))
                        index += 1
                    remaining -= len(raw)
                text = decoder.decode(b"", final=True)
                if text:
                    conn.execute("INSERT INTO hl_metadata_jsonb_digest_stage VALUES(%s,%s)", (index, text))
                if index == 0 and not text:
                    conn.execute("INSERT INTO hl_metadata_jsonb_digest_stage VALUES(0,'')")
            return str(conn.execute(
                "SELECT md5((string_agg(chunk,'' ORDER BY part_index))::jsonb::text) AS digest "
                "FROM hl_metadata_jsonb_digest_stage"
            ).fetchone()["digest"])


def _verify_hl_metadata_target_prefix(sqlite: sqlite3.Connection, rowid: int, key: str,
                                      mapping: TableMapping, payload_udt: str,
                                      *, expected_v2_digest: int, expected_count: int) -> bool:
    """Compare the full committed prefix against the source before checkpoint upgrade."""
    target = sql.Identifier(mapping.target_schema, mapping.target_table)
    with _connect_native_import_postgres(schema=mapping.target_schema, application_name="odaily-native-stream-prefix-audit") as conn:
        destination_rows = conn.execute(sql.SQL(
            "SELECT key,md5(payload::text) AS digest FROM {} WHERE key<=%s ORDER BY key"
        ).format(target), (key,))
        target_digest = 0
        target_count = 0
        for destination in destination_rows:
            target_digest = (target_digest + _hl_metadata_row_digest(str(destination["key"]), str(destination["digest"]))) % (1 << 256)
            target_count += 1
        if target_count != expected_count or target_digest != expected_v2_digest:
            return False
        return True


class _BoundedCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self.cursor = cursor
        self.pending = None

    def fetch(self, row_limit: int, byte_limit: int, max_row_bytes: int) -> list[sqlite3.Row]:
        rows = []
        used = 0
        while len(rows) < row_limit:
            row = self.pending if self.pending is not None else self.cursor.fetchone()
            self.pending = None
            if row is None:
                break
            size = _row_size(row)
            if size > max_row_bytes:
                raise RuntimeError(f"SQLite row exceeds configured max_row_bytes ({size} > {max_row_bytes})")
            if rows and used + size > byte_limit:
                self.pending = row
                break
            rows.append(row)
            used += size
        return rows


def _fetch_bounded(cursor: sqlite3.Cursor, row_limit: int, byte_limit: int,
                   max_row_bytes: int, *, state: _BoundedCursor | None = None) -> list[sqlite3.Row]:
    bounded = state or _BoundedCursor(cursor)
    return bounded.fetch(row_limit, byte_limit, max_row_bytes)


def _validate_checkpoint_source_prefix(*, rows, source_pk: tuple[str, ...], last_identity: tuple[Any, ...] | None,
                                       imported_rows: int, digest_sum: int, row_digest) -> tuple[int, int]:
    if last_identity is None:
        if imported_rows != 0 or digest_sum != 0:
            raise RuntimeError("Source rows changed during native import: checkpoint has no committed identity")
        return 0, 0
    prefix_count = prefix_digest = 0
    matched = False
    for row in rows:
        if matched:
            break
        prefix_count += 1
        prefix_digest = (prefix_digest + int(row_digest(row))) % (1 << 256)
        if tuple(row[key] for key in source_pk) == last_identity:
            matched = True
    if not matched or prefix_count != imported_rows or prefix_digest != digest_sum:
        raise RuntimeError(f"Source rows changed during native import: checkpoint prefix mismatch")
    return prefix_count, prefix_digest


def _sqlite_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _order_by_foreign_keys(connection: sqlite3.Connection, mappings: tuple[TableMapping, ...]) -> tuple[TableMapping, ...]:
    by_name = {mapping.source_table: mapping for mapping in mappings}
    dependencies: dict[str, set[str]] = {name: set() for name in by_name}
    for name in by_name:
        for row in connection.execute(f"PRAGMA foreign_key_list({_sqlite_identifier(name)})"):
            parent = str(row[2])
            if parent in by_name and by_name[parent].target_schema == by_name[name].target_schema:
                dependencies[name].add(parent)
    targets = {(mapping.target_schema, mapping.target_table): mapping.source_table for mapping in mappings}
    for mapping in mappings:
        with connect_postgres(schema=mapping.target_schema, application_name="odaily-native-import-dependencies") as conn:
            parents = conn.execute(
                "SELECT ccu.table_schema,ccu.table_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.constraint_column_usage ccu USING(constraint_catalog,constraint_schema,constraint_name) "
                "WHERE tc.table_schema=%s AND tc.table_name=%s AND tc.constraint_type='FOREIGN KEY'",
                (mapping.target_schema, mapping.target_table),
            ).fetchall()
        for parent in parents:
            source_parent = targets.get((parent["table_schema"], parent["table_name"]))
            if source_parent is not None and source_parent != mapping.source_table:
                dependencies[mapping.source_table].add(source_parent)
    ordered: list[str] = []
    pending = {name: set(parents) for name, parents in dependencies.items()}
    while pending:
        ready = sorted(name for name, parents in pending.items() if not parents)
        if not ready:
            raise RuntimeError(f"Cyclic source foreign-key dependency in {sorted(pending)}; destination constraints require explicit review")
        ordered.extend(ready)
        for name in ready:
            pending.pop(name)
        for parents in pending.values():
            parents.difference_update(ready)
    return tuple(by_name[name] for name in ordered)


def _owned_outbox_triggers(connection, mapping: TableMapping) -> tuple[str, ...]:
    rows = connection.execute(
        "SELECT t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s "
        "AND NOT t.tgisinternal AND pg_get_triggerdef(t.oid) ILIKE %s",
        (mapping.target_schema, mapping.target_table, "%odaily_capture_source_state%"),
    ).fetchall()
    return tuple(str(row["tgname"]) for row in rows)


def _json_value(value: Any) -> Any:
    if isinstance(value, Jsonb):
        return value.obj
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat() if value.tzinfo else value.replace(tzinfo=UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decode_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value, object_pairs_hook=_unique_json_object, parse_constant=_reject_json_constant,
                          parse_float=Decimal, parse_int=int)
    return value


def _is_invalid_json(value: str) -> bool:
    try:
        _decode_json(value)
        return False
    except (json.JSONDecodeError, ValueError):
        return True


def _canonical_converted(columns: tuple[str, ...], converted: tuple[Any, ...], mapping: TableMapping,
                         target_columns: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name, value in zip(columns, converted, strict=True):
        if name in mapping.json_columns:
            if value is None:
                result[name] = {"type": "sql_null"}
            elif isinstance(value, Jsonb):
                result[name] = {"type": "json", "value": value.obj}
            else:
                result[name] = {"type": "json", "value": value}
        elif target_columns[name]["udt_name"] == "bytea" and value is not None:
            result[name] = {"blob_hex": bytes(value).hex()}
        else:
            result[name] = _json_value(value)
    return result


def _destination_columns(source_columns: tuple[str, ...], mapping: TableMapping) -> tuple[str, ...]:
    if mapping.transform == "small_evidence":
        required = {"ref", "payload"}
        if not required.issubset(source_columns):
            raise RuntimeError("small evidence source requires ref and payload columns")
        return ("evidence_ref", "compressed_payload", "byte_size", "created_at")
    names = dict(mapping.column_map)
    return tuple(names.get(name, name) for name in source_columns)


def _convert_row(row: sqlite3.Row, source_columns: tuple[str, ...], mapping: TableMapping,
                 target_columns: dict[str, Any]) -> tuple[Any, ...]:
    if mapping.transform == "small_evidence":
        compressed = bytes(row["payload"])
        content = gzip.decompress(compressed)
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(row["ref"]):
            raise ValueError(f"Small evidence content hash mismatch for {row['ref']}")
        created_ms = row["created_at_ms"] if "created_at_ms" in source_columns else None
        created_at = datetime.fromtimestamp(int(created_ms) / 1000, UTC) if created_ms is not None else datetime.fromtimestamp(0, UTC)
        return str(row["ref"]), compressed, len(content), created_at
    names = dict(mapping.column_map)
    return tuple(_convert(row[name], name, mapping, target_columns[names.get(name, name)]) for name in source_columns)


def _convert_source_row(row: sqlite3.Row, *, source_key: str, table: str, source_pk: tuple[str, ...],
                        source_columns: tuple[str, ...], mapping: TableMapping,
                        target_columns: dict[str, Any]) -> tuple[Any, ...]:
    try:
        return _convert_row(row, source_columns, mapping, target_columns)
    except json.JSONDecodeError as exc:
        identity = tuple(row[key] for key in source_pk)
        column = next((name for name in source_columns
                       if name in mapping.json_columns and isinstance(row[name], str)
                       and _is_invalid_json(row[name])), "unknown")
        raise ValueError(
            f"Invalid JSON source value at {source_key}/{table} identity={identity!r} "
            f"column={column!r}: {exc.msg} at char {exc.pos}"
        ) from exc


def _canonical_json(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Non-finite JSON number")
        rendered = format(value, "f")
        sign = rendered.startswith("-")
        unsigned = rendered[1:] if sign else rendered
        if "." in unsigned:
            unsigned = unsigned.rstrip("0").rstrip(".")
        if unsigned in {"", "0"}:
            return "0"
        return ("-" if sign else "") + unsigned
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            json.dumps(str(key), ensure_ascii=False) + ":" + _canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    if isinstance(value, bytes):
        return _canonical_json({"blob_hex": value.hex()})
    if isinstance(value, datetime):
        return _canonical_json(value.isoformat())
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def _source_sequence_value(connection: sqlite3.Connection, table: str) -> int | None:
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone()
    if not exists:
        return None
    row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _convert(value: Any, column: str, mapping: TableMapping, target_column: dict[str, Any]) -> Any:
    if value is None:
        return None
    if column in mapping.json_columns:
        if value is None:
            return Jsonb(None, dumps=_canonical_json)
        if isinstance(value, str):
            try:
                decoded = _decode_json(value)
            except json.JSONDecodeError:
                decoded = value
            return Jsonb(decoded, dumps=_canonical_json)
        return Jsonb(value, dumps=_canonical_json)
    if column in mapping.timestamp_columns and isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            from datetime import UTC
            result = result.replace(tzinfo=UTC)
        return result
    if target_column["udt_name"] == "bool":
        if value not in (0, 1, False, True):
            raise ValueError(f"Invalid SQLite boolean in {mapping.source_table}.{column}: {value!r}")
        return bool(value)
    if target_column["udt_name"] == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if target_column["udt_name"].startswith("_") and isinstance(value, str):
        decoded = _decode_json(value)
        if not isinstance(decoded, list):
            raise ValueError(f"SQLite JSON array expected in {mapping.source_table}.{column}")
        if not all(isinstance(item, str) for item in decoded):
            raise ValueError(f"SQLite string array expected in {mapping.source_table}.{column}")
        return decoded
    if isinstance(value, memoryview):
        return bytes(value)
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def _ensure_legacy_target(source_spec: dict[str, Any], mapping: TableMapping) -> None:
    from psycopg import sql

    definitions = []
    primary = []
    for column in sorted(source_spec["columns"], key=lambda value: value[0]):
        source_type = str(column[2] or "").upper()
        target_type = "bigint" if "INT" in source_type else "double precision" if any(token in source_type for token in ("REAL", "FLOA", "DOUB")) else "bytea" if "BLOB" in source_type else "text"
        definitions.append(sql.SQL("{} {}{}").format(
            sql.Identifier(column[1]), sql.SQL(target_type),
            sql.SQL(" NOT NULL") if column[3] else sql.SQL(""),
        ))
        if int(column[5]):
            primary.append((int(column[5]), str(column[1])))
    if primary:
        definitions.append(sql.SQL("PRIMARY KEY ({})").format(
            sql.SQL(",").join(sql.Identifier(name) for _, name in sorted(primary)),
        ))
    statement = sql.SQL("CREATE TABLE IF NOT EXISTS {}.{} ({})").format(
        sql.Identifier(mapping.target_schema), sql.Identifier(mapping.target_table), sql.SQL(",").join(definitions),
    )
    with connect_postgres(schema="main", application_name="odaily-native-legacy-schema") as conn:
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(mapping.target_schema)))
        conn.execute(statement)


if __name__ == "__main__":
    raise SystemExit(main())
