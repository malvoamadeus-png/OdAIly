from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from dotenv import load_dotenv

from packages.auditor import create_auditor_repository
from packages.common.editor_plugin_auth import create_editor_plugin_auth_repository
from packages.common.source_exclusions import create_source_exclusion_repository
from packages.common.storage import connect_sqlite, load_storage_settings
from packages.competitor_monitor import create_competitor_monitor_repository
from packages.external_media_alert import create_external_media_alert_repository
from packages.jin10_monitor.repository import create_jin10_monitor_repository
from packages.non_mainstream_media import create_non_mainstream_media_repository
from packages.pipeline_supervisor import create_pipeline_supervisor_repository
from packages.pipeline_timing import create_pipeline_timing_repository
from packages.whale_watch import create_whale_watch_hyperliquid_repository, create_whale_watch_repository
from packages.writer3 import create_writer3_repository
from packages.x_capture.repository import create_x_capture_repository
from packages.x_processing.repository import create_x_processing_repository


# Parent/config tables precede dependent/event tables. Authentication tables are
# deliberately excluded because the new release has one deployment-owned account.
LEGACY_TABLES = (
    "x_capture_settings", "x_capture_accounts", "publisher_settings", "publisher_channels",
    "publisher_rule_config", "prompt_templates", "prompt_template_versions",
    "source_exclusion_rule_groups", "non_mainstream_media_settings", "non_mainstream_media_sources",
    "jin10_settings", "whale_watch_addresses", "whale_watch_hyperliquid_settings",
    "whale_watch_hyperliquid_addresses", "tasks", "odaily_reference_items", "newsflash_items",
    "newsflash_events", "newsflash_event_sources", "newsflash_event_favorites", "newsflash_event_notes",
    "newsflash_item_notes", "x_task_pipeline", "external_media_alert_pipeline", "search_event_candidates",
    "search_event_sources", "x_capture_attempts", "x_seen_tweets", "media_newsflash",
    "non_mainstream_media_seen_items", "jin10_seen_items", "whale_watch_chain_states",
    "whale_watch_activities", "whale_watch_hyperliquid_states", "whale_watch_hyperliquid_activities",
    "auditor_checks", "writer3_contexts", "pipeline_worker_heartbeats", "pipeline_alerts",
    "pipeline_timing_snapshots", "editor_plugin_users", "editor_plugin_generation_logs",
    "editor_plugin_receipts", "editor_plugin_feedbacks",
)


@dataclass(frozen=True)
class ImportTableResult:
    table: str
    source_rows: int
    imported_rows: int
    destination_rows: int
    shared_columns: tuple[str, ...]


def initialize_sqlite_schema() -> Path:
    path = load_storage_settings().sqlite_path
    repositories = (
        create_x_capture_repository(), create_x_processing_repository(), create_source_exclusion_repository(),
        create_non_mainstream_media_repository(), create_external_media_alert_repository(),
        create_competitor_monitor_repository(), create_jin10_monitor_repository(),
        create_whale_watch_repository(), create_whale_watch_hyperliquid_repository(),
        create_writer3_repository(), create_auditor_repository(), create_pipeline_supervisor_repository(),
        create_editor_plugin_auth_repository(), create_pipeline_timing_repository(),
    )
    for repository in repositories:
        initialize = getattr(repository, "init_schema", None)
        if callable(initialize):
            initialize()
    with connect_sqlite(path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pipeline_timing_snapshots (
                id integer PRIMARY KEY AUTOINCREMENT, window_hours integer NOT NULL, generated_at text NOT NULL,
                payload text NOT NULL DEFAULT '{}', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(window_hours, generated_at))"""
        )
        conn.commit()
    return path


def _encode(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bytes)):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return str(value)


def import_legacy_database(
    *, execute: bool, truncate: bool = False, batch_size: int = 1000, sample_rows_per_table: int | None = None
) -> list[ImportTableResult]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    if sample_rows_per_table is not None and sample_rows_per_table < 1:
        raise ValueError("sample_rows_per_table must be >= 1")
    load_dotenv()
    source_url = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not source_url:
        raise RuntimeError("legacy source database URL is not configured")
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError("psycopg is required only while running the one-time legacy import") from exc

    path = initialize_sqlite_schema()
    results: list[ImportTableResult] = []
    with psycopg.connect(source_url, row_factory=dict_row, application_name="odaily-one-time-sqlite-import") as source:
        with connect_sqlite(path) as destination:
            destination.execute("PRAGMA foreign_keys=OFF")
            for table in LEGACY_TABLES:
                source_exists = source.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",)).fetchone()["name"]
                destination_exists = destination.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not source_exists or not destination_exists:
                    continue
                source_columns = [row["column_name"] for row in source.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
                    (table,),
                ).fetchall()]
                destination_columns = [row["name"] for row in destination.execute(f'PRAGMA table_info("{table}")').fetchall()]
                columns = tuple(column for column in source_columns if column in destination_columns)
                if not columns:
                    continue
                source_count = int(source.execute(f'SELECT count(*) AS count FROM "{table}"').fetchone()["count"])
                if execute and truncate:
                    destination.execute(f'DELETE FROM "{table}"')
                imported = 0
                if execute:
                    quoted = ",".join(f'"{column}"' for column in columns)
                    placeholders = ",".join("?" for _ in columns)
                    insert_sql = f'INSERT OR REPLACE INTO "{table}" ({quoted}) VALUES ({placeholders})'
                    with source.cursor(name=f"import_{table}") as cursor:
                        limit_sql = f" LIMIT {int(sample_rows_per_table)}" if sample_rows_per_table is not None else ""
                        cursor.execute(f'SELECT {quoted} FROM "{table}"{limit_sql}')
                        while batch := cursor.fetchmany(batch_size):
                            destination.executemany(insert_sql, [tuple(_encode(row[column]) for column in columns) for row in batch])
                            imported += len(batch)
                    destination.commit()
                destination_count = int(destination.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0])
                result = ImportTableResult(table, source_count, imported, destination_count, columns)
                results.append(result)
                print(
                    f"[odaily] import progress table={table} source={source_count} "
                    f"imported={imported} destination={destination_count}",
                    flush=True,
                )
            destination.execute("PRAGMA foreign_keys=ON")
            integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"SQLite integrity check failed: {integrity}")
    if execute:
        mismatches = [
            item for item in results
            if item.destination_rows != (
                min(item.source_rows, sample_rows_per_table)
                if sample_rows_per_table is not None
                else item.source_rows
            )
        ]
        if mismatches:
            names = ", ".join(f"{item.table}:{item.source_rows}->{item.destination_rows}" for item in mismatches)
            raise RuntimeError(f"row-count verification failed: {names}")
    return results
