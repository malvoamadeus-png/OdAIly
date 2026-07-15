from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.postgres import build_psycopg_connect_kwargs
from packages.x_processing.repository import _import_psycopg, get_database_url


@dataclass(frozen=True)
class MaintenanceCleanupResult:
    dry_run: bool
    retention_days: int
    feedback_retention_days: int
    completed_field_retention_days: int
    deleted: dict[str, int]
    cleared: dict[str, int]


class PostgresMaintenanceRepository:
    COMPLETED_TASK_STATUSES = (
        "'discarded', 'duplicate', 'auto_published', 'ready_review', 'publisher_failed', 'notified'"
    )

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()
        self.application_name = "odaily-maintenance"

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=self._dict_row,
                autocommit=autocommit,
                application_name=self.application_name,
            ),
        )

    def cleanup(
        self,
        *,
        dry_run: bool = True,
        retention_days: int = 7,
        feedback_retention_days: int = 90,
        completed_field_retention_days: int = 7,
    ) -> MaintenanceCleanupResult:
        now = datetime.now(UTC)
        retention_cutoff = now - timedelta(days=retention_days)
        feedback_cutoff = now - timedelta(days=feedback_retention_days)
        field_cutoff = now - timedelta(days=completed_field_retention_days)
        deleted: dict[str, int] = {}
        cleared: dict[str, int] = {}

        with self._connect() as conn:
            deleted["editor_plugin_generation_logs"] = self._delete_or_count(
                conn,
                "editor_plugin_generation_logs",
                "created_at < %(cutoff)s",
                {"cutoff": retention_cutoff},
                dry_run=dry_run,
            )
            deleted["editor_plugin_receipts"] = self._delete_or_count(
                conn,
                "editor_plugin_receipts",
                "true",
                {},
                dry_run=dry_run,
            )
            deleted["editor_plugin_feedbacks"] = self._delete_or_count(
                conn,
                "editor_plugin_feedbacks",
                "created_at < %(cutoff)s",
                {"cutoff": feedback_cutoff},
                dry_run=dry_run,
            )
            deleted["whale_watch_activities"] = self._delete_or_count(
                conn,
                "whale_watch_activities",
                "created_at < %(cutoff)s",
                {"cutoff": retention_cutoff},
                dry_run=dry_run,
            )
            deleted["whale_watch_hyperliquid_activities"] = self._delete_or_count(
                conn,
                "whale_watch_hyperliquid_activities",
                "created_at < %(cutoff)s",
                {"cutoff": retention_cutoff},
                dry_run=dry_run,
            )
            deleted["pipeline_alerts"] = self._delete_or_count(
                conn,
                "pipeline_alerts",
                "created_at < %(cutoff)s",
                {"cutoff": retention_cutoff},
                dry_run=dry_run,
            )
            deleted["pipeline_worker_heartbeats"] = self._delete_or_count(
                conn,
                "pipeline_worker_heartbeats",
                "updated_at < %(cutoff)s",
                {"cutoff": retention_cutoff},
                dry_run=dry_run,
            )

            cleared["tasks_payloads"] = self._clear_or_count(
                conn,
                table="tasks",
                assignments=(
                    "raw_payload = '{}'::jsonb, "
                    "metadata = jsonb_strip_nulls(jsonb_build_object("
                    "'account_username', metadata -> 'account_username', "
                    "'author_username', metadata -> 'author_username', "
                    "'author_display_name', metadata -> 'author_display_name', "
                    "'effective_author_name', metadata -> 'effective_author_name', "
                    "'site_key', metadata -> 'site_key', "
                    "'site_display_name', metadata -> 'site_display_name', "
                    "'source_group', metadata -> 'source_group', "
                    "'source_label', metadata -> 'source_label', "
                    "'source_kind', metadata -> 'source_kind')), "
                    "updated_at = now()"
                ),
                where=(
                    "updated_at < %(cutoff)s "
                    f"AND status IN ({self.COMPLETED_TASK_STATUSES}) "
                    "AND (raw_payload <> '{}'::jsonb OR metadata <> jsonb_strip_nulls(jsonb_build_object("
                    "'account_username', metadata -> 'account_username', "
                    "'author_username', metadata -> 'author_username', "
                    "'author_display_name', metadata -> 'author_display_name', "
                    "'effective_author_name', metadata -> 'effective_author_name', "
                    "'site_key', metadata -> 'site_key', "
                    "'site_display_name', metadata -> 'site_display_name', "
                    "'source_group', metadata -> 'source_group', "
                    "'source_label', metadata -> 'source_label', "
                    "'source_kind', metadata -> 'source_kind')))"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
            )
            cleared["odaily_reference_items_payloads"] = self._clear_or_count(
                conn,
                table="odaily_reference_items",
                assignments="raw_payload = '{}'::jsonb, metadata = '{}'::jsonb, updated_at = now()",
                where=(
                    "updated_at < %(cutoff)s "
                    "AND (raw_payload <> '{}'::jsonb OR metadata <> '{}'::jsonb)"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
            )
            cleared["newsflash_items_payloads"] = self._clear_or_count(
                conn,
                table="newsflash_items",
                assignments="raw_payload = '{}'::jsonb, metadata = '{}'::jsonb, updated_at = now()",
                where=(
                    "updated_at < %(cutoff)s "
                    "AND (raw_payload <> '{}'::jsonb OR metadata <> '{}'::jsonb)"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
            )
            cleared["x_task_pipeline_outputs"] = self._clear_or_count(
                conn,
                table="x_task_pipeline",
                assignments=(
                    "judge_output = '{}'::jsonb, search_result = '{}'::jsonb, writer_output = '{}'::jsonb, "
                    "publisher_output = '{}'::jsonb, push_result = '{}'::jsonb, telegram_result = '{}'::jsonb, "
                    "updated_at = now()"
                ),
                where=(
                    "updated_at < %(cutoff)s "
                    f"AND task_id IN (SELECT id FROM tasks WHERE status IN ({self.COMPLETED_TASK_STATUSES})) "
                    "AND (judge_output <> '{}'::jsonb OR search_result <> '{}'::jsonb OR writer_output <> '{}'::jsonb "
                    "OR publisher_output <> '{}'::jsonb OR push_result <> '{}'::jsonb OR telegram_result <> '{}'::jsonb)"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
                required_columns={
                    "judge_output",
                    "search_result",
                    "writer_output",
                    "publisher_output",
                    "push_result",
                    "telegram_result",
                },
            )
            cleared["auditor_checks_outputs"] = self._clear_or_count(
                conn,
                table="auditor_checks",
                assignments="raw_output = NULL, telegram_result = '{}'::jsonb, metadata = '{}'::jsonb, updated_at = now()",
                where=(
                    "updated_at < %(cutoff)s "
                    "AND status IN ('passed', 'flagged', 'failed') "
                    "AND (raw_output IS NOT NULL OR telegram_result <> '{}'::jsonb OR metadata <> '{}'::jsonb)"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
            )
            cleared["writer3_contexts_outputs"] = self._clear_or_count(
                conn,
                table="writer3_contexts",
                assignments="telegram_result = '{}'::jsonb, metadata = '{}'::jsonb, updated_at = now()",
                where=(
                    "updated_at < %(cutoff)s "
                    "AND status IN ('sent', 'skipped', 'failed') "
                    "AND (telegram_result <> '{}'::jsonb OR metadata <> '{}'::jsonb)"
                ),
                params={"cutoff": field_cutoff},
                dry_run=dry_run,
            )

            if dry_run:
                conn.rollback()
            else:
                conn.commit()

        return MaintenanceCleanupResult(
            dry_run=dry_run,
            retention_days=retention_days,
            feedback_retention_days=feedback_retention_days,
            completed_field_retention_days=completed_field_retention_days,
            deleted=deleted,
            cleared=cleared,
        )

    def _table_exists(self, conn, table: str) -> bool:
        row = conn.execute("SELECT to_regclass(%s) AS table_oid", (f"public.{table}",)).fetchone()
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
        rows = conn.execute(f"DELETE FROM {table} WHERE {where} RETURNING 1", params).fetchall()
        return len(rows)

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
    ) -> int:
        if not self._table_exists(conn, table):
            return 0
        if required_columns and not self._columns_exist(conn, table, required_columns):
            return 0
        if dry_run:
            row = conn.execute(f"SELECT count(*) AS count FROM {table} WHERE {where}", params).fetchone()
            return int(row["count"] or 0)
        rows = conn.execute(f"UPDATE {table} SET {assignments} WHERE {where} RETURNING 1", params).fetchall()
        return len(rows)

    def _columns_exist(self, conn, table: str, columns: set[str]) -> bool:
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = %s
              AND column_name = ANY(%s)
            """,
            (table, list(columns)),
        ).fetchall()
        found = {str(row["column_name"]) for row in rows}
        return columns.issubset(found)
