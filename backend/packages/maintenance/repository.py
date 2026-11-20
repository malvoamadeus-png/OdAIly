from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction
from packages.common.pipeline_schema import PIPELINE_MONITORING_SCHEMA_SQL
from packages.common.storage import load_storage_settings


@dataclass(frozen=True)
class MaintenanceCleanupResult:
    dry_run: bool
    retention_days: int
    feedback_retention_days: int
    completed_field_retention_days: int
    deleted: dict[str, int]
    cleared: dict[str, int]
    has_more: bool = False


class PostgresMaintenanceRepository:
    COMPLETED_TASK_STATUSES = (
        "'discarded', 'duplicate', 'auto_published', 'publisher_failed', 'notified'"
    )

    def __init__(self, database_url: str | None = None) -> None:
        if database_url is not None:
            raise ValueError("Configure ODAILY_POSTGRES_DSN instead of passing a database URL")
        self.application_name = "odaily-maintenance"

    @contextmanager
    def _connect(self, *, autocommit: bool = False):
        with connect_postgres(schema="main", application_name=self.application_name) as conn:
            if autocommit:
                yield conn
            else:
                with postgres_transaction(conn):
                    yield conn

    def init_schema(self) -> None:
        with connect_postgres(schema="main", application_name=self.application_name) as conn:
            apply_schema_migrations(
                conn,
                schema="main",
                component="maintenance",
                migrations=(PostgresMigration(
                    1, "maintenance_initial", sha256(PIPELINE_MONITORING_SCHEMA_SQL.encode()).hexdigest(),
                    lambda connection: connection.execute(PIPELINE_MONITORING_SCHEMA_SQL),
                ),),
            )

    def cleanup(
        self,
        *,
        dry_run: bool = True,
        retention_days: int = 7,
        feedback_retention_days: int = 90,
        completed_field_retention_days: int = 5,
        include_module_retention: bool = True,
    ) -> MaintenanceCleanupResult:
        now = datetime.now(UTC)
        retention_cutoff = now - timedelta(days=retention_days)
        feedback_cutoff = now - timedelta(days=feedback_retention_days)
        deleted: dict[str, int] = {}
        cleared: dict[str, int] = {}
        has_more = False

        with self._connect() as conn:
            conn.execute("SET LOCAL statement_timeout = '10s'")
            conn.execute("SET LOCAL lock_timeout = '3s'")
            if include_module_retention:
                deleted["editor_plugin_generation_logs"] = self._delete_or_count(
                    conn,
                    "editor_plugin_generation_logs",
                    "created_at < %(cutoff)s",
                    {"cutoff": retention_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["editor_plugin_generation_logs"] >= 500 and self._has_rows(conn, "editor_plugin_generation_logs", "created_at < %(cutoff)s", {"cutoff": retention_cutoff}))
                deleted["editor_plugin_receipts"] = self._delete_or_count(
                    conn,
                    "editor_plugin_receipts",
                    "true",
                    {},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["editor_plugin_receipts"] >= 500 and self._has_rows(conn, "editor_plugin_receipts", "true", {}))
                deleted["editor_plugin_feedbacks"] = self._delete_or_count(
                    conn,
                    "editor_plugin_feedbacks",
                    "created_at < %(cutoff)s",
                    {"cutoff": feedback_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["editor_plugin_feedbacks"] >= 500 and self._has_rows(conn, "editor_plugin_feedbacks", "created_at < %(cutoff)s", {"cutoff": feedback_cutoff}))
                deleted["whale_watch_activities"] = self._delete_or_count(
                    conn,
                    "whale_watch_activities",
                    "created_at < %(cutoff)s",
                    {"cutoff": retention_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["whale_watch_activities"] >= 500 and self._has_rows(conn, "whale_watch_activities", "created_at < %(cutoff)s", {"cutoff": retention_cutoff}))
                deleted["whale_watch_hyperliquid_activities"] = self._delete_or_count(
                    conn,
                    "whale_watch_hyperliquid_activities",
                    "created_at < %(cutoff)s",
                    {"cutoff": retention_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["whale_watch_hyperliquid_activities"] >= 500 and self._has_rows(conn, "whale_watch_hyperliquid_activities", "created_at < %(cutoff)s", {"cutoff": retention_cutoff}))
                deleted["pipeline_alerts"] = self._delete_or_count(
                    conn,
                    "pipeline_alerts",
                    "created_at < %(cutoff)s",
                    {"cutoff": retention_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["pipeline_alerts"] >= 500 and self._has_rows(conn, "pipeline_alerts", "created_at < %(cutoff)s", {"cutoff": retention_cutoff}))
                deleted["pipeline_worker_heartbeats"] = self._delete_or_count(
                    conn,
                    "pipeline_worker_heartbeats",
                    "updated_at < %(cutoff)s",
                    {"cutoff": retention_cutoff},
                    dry_run=dry_run,
                )
                has_more = has_more or (not dry_run and deleted["pipeline_worker_heartbeats"] >= 500 and self._has_rows(conn, "pipeline_worker_heartbeats", "updated_at < %(cutoff)s", {"cutoff": retention_cutoff}))

            field_cutoff = now - timedelta(days=completed_field_retention_days)
            task_protection = self._task_retention_protection(conn, "t")
            terminal = "'discarded','duplicate','auto_published','published','notified','event_tracking_cancelled','expired','legacy_skipped','judge_failed','search_failed','write_failed','format_failed','publisher_failed'"
            metadata_keep = "jsonb_strip_nulls(jsonb_build_object('account_username',metadata->'account_username','author_username',metadata->'author_username','author_display_name',metadata->'author_display_name','effective_author_name',metadata->'effective_author_name','site_key',metadata->'site_key','site_display_name',metadata->'site_display_name','source_group',metadata->'source_group','source_label',metadata->'source_label','source_kind',metadata->'source_kind'))"
            task_scope = f"SELECT t.id FROM tasks t WHERE t.updated_at < %(cutoff)s AND t.status IN ({terminal}) AND {task_protection}"
            task_where = f"id IN ({task_scope}) AND (COALESCE(raw_payload,'{{}}'::jsonb) <> '{{}}'::jsonb OR COALESCE(metadata,'{{}}'::jsonb) <> {metadata_keep})"
            task_assignments = f"raw_payload='{{}}'::jsonb,metadata={metadata_keep}"
            task_params = {"cutoff": field_cutoff}
            cleared["tasks_payloads"] = self._clear_or_count(conn, table="tasks", assignments=task_assignments, where=task_where, params=task_params, dry_run=dry_run, required_columns={"id","raw_payload","metadata","status","updated_at","title","content"}, batch_limit=500, order_by="id")
            has_more = has_more or (not dry_run and self._has_rows(conn, "tasks", task_where, task_params))

            pipeline_where = f"task_id IN (SELECT t.id FROM tasks t WHERE t.updated_at < %(cutoff)s AND t.status IN ({terminal}) AND {task_protection}) AND (judge_output <> '{{}}'::jsonb OR search_result <> '{{}}'::jsonb OR writer_output <> '{{}}'::jsonb OR draft_title IS NOT NULL OR draft_content IS NOT NULL)"
            pipeline_assignments = "judge_output='{}'::jsonb,search_result='{}'::jsonb,writer_output='{}'::jsonb,draft_title=NULL,draft_content=NULL"
            pipeline_params = {"cutoff": field_cutoff}
            if self._columns_exist(conn, "tasks", {"id", "title", "content", "status", "updated_at"}):
                cleared["x_task_pipeline_outputs"] = self._clear_or_count(conn, table="x_task_pipeline", assignments=pipeline_assignments, where=pipeline_where, params=pipeline_params, dry_run=dry_run, required_columns={"task_id","judge_output","search_result","writer_output","draft_title","draft_content"}, batch_limit=500, order_by="task_id")
            else:
                cleared["x_task_pipeline_outputs"] = 0
            has_more = has_more or (not dry_run and self._has_rows(conn, "x_task_pipeline", pipeline_where, pipeline_params))

        return MaintenanceCleanupResult(
            dry_run=dry_run,
            retention_days=retention_days,
            feedback_retention_days=feedback_retention_days,
            completed_field_retention_days=completed_field_retention_days,
            deleted=deleted,
            cleared=cleared,
            has_more=has_more,
        )

    def cleanup_main_pipeline_fields(self, *, dry_run: bool = False, completed_field_retention_days: int = 5) -> MaintenanceCleanupResult:
        return self.cleanup(
            dry_run=dry_run,
            completed_field_retention_days=completed_field_retention_days,
            include_module_retention=False,
        )

    def _table_exists(self, conn, table: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) AS table_oid", (f"main.{table}",)).fetchone()
        return row is not None and row["table_oid"] is not None

    def _delete_or_count(
        self,
        conn,
        table: str,
        where: str,
        params: dict[str, Any],
        *,
        dry_run: bool,
    ) -> int:
        if not self._table_exists(conn, table):
            return 0
        if dry_run:
            row = conn.execute(f"SELECT count(*) AS count FROM {table} WHERE {where}", params).fetchone()
            return int(row["count"] or 0)
        rows = conn.execute(
            f"WITH batch AS (SELECT ctid FROM {table} WHERE {where} ORDER BY ctid LIMIT 500) "
            f"DELETE FROM {table} WHERE ctid IN (SELECT ctid FROM batch) RETURNING 1",
            params,
        ).fetchall()
        return len(rows)

    def _has_rows(self, conn, table: str, where: str, params: dict[str, Any]) -> bool:
        if not self._table_exists(conn, table):
            return False
        return bool(conn.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params).fetchone())

    def _clear_or_count(
        self,
        conn,
        *,
        table: str,
        assignments: str,
        where: str,
        params: dict[str, Any],
        dry_run: bool,
        required_columns: set[str] | None = None,
        batch_limit: int = 500,
        order_by: str = "ctid",
    ) -> int:
        if not self._table_exists(conn, table):
            return 0
        if required_columns and not self._columns_exist(conn, table, required_columns):
            return 0
        if dry_run:
            row = conn.execute(f"SELECT count(*) AS count FROM {table} WHERE {where}", params).fetchone()
            return int(row["count"] or 0)
        result = conn.execute(f"WITH batch AS (SELECT ctid FROM {table} WHERE {where} ORDER BY {order_by} LIMIT %(limit)s) UPDATE {table} SET {assignments} WHERE ctid IN (SELECT ctid FROM batch)", {**params, "limit": max(1, min(int(batch_limit), 5000))})
        return result.rowcount or 0

    def _task_retention_protection(self, conn, task_alias: str) -> str:
        clauses: list[str] = []
        metadata_exists = self._table_exists(conn, "newsflash_production_metadata")
        metadata_columns = {str(row["column_name"]) for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name='newsflash_production_metadata'"
        ).fetchall()} if metadata_exists else set()
        if metadata_exists:
            for table in ("auditor_checks", "fact_checks"):
                if not self._table_exists(conn, table):
                    continue
                columns = {str(row["column_name"]) for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name=%s", (table,)).fetchall()}
                if "status" not in columns:
                    clauses.append("false")
                    continue
                if "source_item_id" not in columns or not {"odaily_id", "delivery_key"} <= metadata_columns:
                    clauses.append(f"NOT EXISTS (SELECT 1 FROM {table} WHERE status IN ('pending','processing'))")
                    continue
                clauses.append(
                    f"NOT EXISTS (SELECT 1 FROM {table} a JOIN newsflash_production_metadata m ON m.odaily_id=a.source_item_id "
                    f"WHERE m.delivery_key='odaily-task-' || {task_alias}.id::text AND a.status IN ('pending','processing'))"
                )
        else:
            for table in ("auditor_checks", "fact_checks"):
                if self._table_exists(conn, table):
                    clauses.append(f"NOT EXISTS (SELECT 1 FROM {table} WHERE status IN ('pending','processing'))")
        if self._table_exists(conn, "editor_plugin_local_feedbacks"):
            required_feedback = {"feed_item_id", "feed_kind", "feedback", "manual_rejection_status"}
            pending = conn.execute(
                "SELECT 1 FROM editor_plugin_local_feedbacks WHERE manual_rejection_status IN ('pending','queued') LIMIT 1"
            ).fetchone() if self._columns_exist(conn, "editor_plugin_local_feedbacks", required_feedback) else None
            if pending:
                if metadata_exists and {"delivery_key", "odaily_id"} <= metadata_columns and self._columns_exist(conn, "auditor_checks", {"id", "source_item_id"}):
                    clauses.append(
                        f"NOT EXISTS (SELECT 1 FROM editor_plugin_local_feedbacks fb "
                        f"JOIN auditor_checks a ON fb.feed_item_id='auditor:' || a.id::text "
                        f"JOIN newsflash_production_metadata m ON m.odaily_id=a.source_item_id "
                        f"WHERE fb.feed_kind='auditor_alert' AND fb.feedback='reject' "
                        f"AND fb.manual_rejection_status IN ('pending','queued') "
                        f"AND m.delivery_key='odaily-task-' || {task_alias}.id::text)"
                    )
                else:
                    clauses.append("false")
        if self._table_exists(conn, "search_event_candidates"):
            if self._columns_exist(conn, "search_event_candidates", {"primary_task_id", "status", "expires_at"}):
                clauses.append(f"NOT EXISTS (SELECT 1 FROM search_event_candidates c WHERE c.primary_task_id={task_alias}.id AND c.status='active' AND (c.expires_at IS NULL OR c.expires_at>now()))")
            else:
                clauses.append("false")
        return " AND ".join(clauses) if clauses else "true"

    def _columns_exist(self, conn, table: str, columns: set[str]) -> bool:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'main'
              AND table_name = %s
              AND column_name = ANY(%s)
            """,
            (table, list(columns)),
        ).fetchall()
        found = {str(row["column_name"]) for row in rows}
        return columns.issubset(found)
