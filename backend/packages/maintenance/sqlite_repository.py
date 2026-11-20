from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
import time

from packages.common.paths import get_paths
from packages.common.storage import connect_sqlite, load_storage_settings

from .repository import MaintenanceCleanupResult


class SQLiteMaintenanceRepository:
    def __init__(self, path: Path | None = None, *, feedback_path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path
        self.feedback_path = feedback_path or get_paths().runtime_dir / "editor_plugin_local.sqlite"

    def cleanup(self, *, dry_run: bool = True, retention_days: int = 7, feedback_retention_days: int = 90,
                completed_field_retention_days: int = 5, include_module_retention: bool = True) -> MaintenanceCleanupResult:
        now=datetime.now(UTC); retention=(now-timedelta(days=retention_days)).isoformat(); feedback=(now-timedelta(days=feedback_retention_days)).isoformat()
        deleted={}; cleared={}; has_more=False
        with connect_sqlite(self.path) as conn:
            deadline = time.monotonic() + 10
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("PRAGMA busy_timeout=3000")
            feedback_table = "editor_plugin_local_feedbacks"
            if self.feedback_path.exists() and self.feedback_path.resolve() != self.path.resolve():
                conn.execute("ATTACH DATABASE ? AS plugin_feedback", (str(self.feedback_path),))
                feedback_table = "plugin_feedback.editor_plugin_local_feedbacks"
            if include_module_retention:
                for table,column,cutoff in (
                    ("editor_plugin_generation_logs","created_at",retention),("editor_plugin_receipts",None,None),
                    ("editor_plugin_feedbacks","created_at",feedback),("whale_watch_activities","created_at",retention),
                    ("whale_watch_hyperliquid_activities","created_at",retention),("pipeline_alerts","created_at",retention),
                    ("pipeline_worker_heartbeats","updated_at",retention)):
                    deleted[table]=self._delete(conn,table,column,cutoff,dry_run)
                    if not dry_run and deleted[table] >= 500:
                        where = f"julianday({column})<julianday(?)" if column else "1=1"
                        params = (cutoff,) if column else ()
                        has_more = has_more or self._has_rows(conn, table, where, params)
            field_cutoff = (now - timedelta(days=completed_field_retention_days)).isoformat()
            task_protection = self._task_retention_protection(conn, "t", feedback_table=feedback_table)
            terminal = "'discarded','duplicate','auto_published','published','notified','event_tracking_cancelled','expired','legacy_skipped','judge_failed','search_failed','write_failed','format_failed','publisher_failed'"
            task_columns = self._columns(conn, "tasks") if self._exists(conn, "tasks") else set()
            if {"id", "status", "updated_at", "raw_payload", "metadata", "title", "content"} <= task_columns:
                metadata = "json_patch('{}',json_object('account_username',json_extract(metadata,'$.account_username'),'author_username',json_extract(metadata,'$.author_username'),'author_display_name',json_extract(metadata,'$.author_display_name'),'effective_author_name',json_extract(metadata,'$.effective_author_name'),'site_key',json_extract(metadata,'$.site_key'),'site_display_name',json_extract(metadata,'$.site_display_name'),'source_group',json_extract(metadata,'$.source_group'),'source_label',json_extract(metadata,'$.source_label'),'source_kind',json_extract(metadata,'$.source_kind')))"
                task_scope = f"SELECT t.id FROM tasks t WHERE t.updated_at < ? AND t.status IN ({terminal}) AND {task_protection}"
                where = f"id IN ({task_scope}) AND (COALESCE(raw_payload,'{{}}') NOT IN ('','{{}}','null') OR COALESCE(metadata,'{{}}') <> {metadata})"
                if dry_run:
                    total = int(conn.execute(f"SELECT count(*) FROM tasks WHERE {where}", (field_cutoff,)).fetchone()[0])
                    cleared["tasks_payloads"] = total
                    has_more = has_more or total > 500
                else:
                    rows = conn.execute(f"SELECT rowid FROM tasks WHERE {where} ORDER BY id LIMIT 500", (field_cutoff,)).fetchall()
                    rowids = [int(row[0]) for row in rows]
                    if rowids:
                        placeholders = ",".join("?" for _ in rowids)
                        cleared["tasks_payloads"] = int(conn.execute(f"UPDATE tasks SET raw_payload='{{}}',metadata={metadata} WHERE rowid IN ({placeholders})", rowids).rowcount)
                    else:
                        cleared["tasks_payloads"] = 0
                    has_more = has_more or bool(conn.execute(f"SELECT 1 FROM tasks WHERE {where} LIMIT 1", (field_cutoff,)).fetchone())
            else:
                cleared["tasks_payloads"] = 0
            if self._exists(conn, "x_task_pipeline") and self._columns(conn, "x_task_pipeline") >= {"task_id", "judge_output", "search_result", "writer_output", "draft_title", "draft_content"} and {"id", "status", "updated_at", "title", "content"} <= task_columns:
                where = f"task_id IN (SELECT t.id FROM tasks t WHERE t.updated_at < ? AND t.status IN ({terminal}) AND {task_protection}) AND (COALESCE(judge_output,'{{}}') NOT IN ('','{{}}','null') OR COALESCE(search_result,'{{}}') NOT IN ('','{{}}','null') OR COALESCE(writer_output,'{{}}') NOT IN ('','{{}}','null') OR draft_title IS NOT NULL OR draft_content IS NOT NULL)"
                if dry_run:
                    total = int(conn.execute(f"SELECT count(*) FROM x_task_pipeline WHERE {where}", (field_cutoff,)).fetchone()[0])
                    cleared["x_task_pipeline_outputs"] = total
                    has_more = has_more or total > 500
                else:
                    rows = conn.execute(f"SELECT rowid FROM x_task_pipeline WHERE {where} ORDER BY task_id LIMIT 500", (field_cutoff,)).fetchall()
                    rowids = [int(row[0]) for row in rows]
                    if rowids:
                        placeholders = ",".join("?" for _ in rowids)
                        cleared["x_task_pipeline_outputs"] = int(conn.execute(f"UPDATE x_task_pipeline SET judge_output='{{}}',search_result='{{}}',writer_output='{{}}',draft_title=NULL,draft_content=NULL WHERE rowid IN ({placeholders})", rowids).rowcount)
                    else:
                        cleared["x_task_pipeline_outputs"] = 0
                    has_more = has_more or bool(conn.execute(f"SELECT 1 FROM x_task_pipeline WHERE {where} LIMIT 1", (field_cutoff,)).fetchone())
            else:
                cleared["x_task_pipeline_outputs"] = 0
            for key in ("odaily_reference_items_payloads", "newsflash_items_payloads", "auditor_checks_outputs", "writer3_contexts_outputs"):
                cleared[key] = 0
            conn.rollback() if dry_run else conn.commit()
        return MaintenanceCleanupResult(dry_run=dry_run,retention_days=retention_days,feedback_retention_days=feedback_retention_days,
            completed_field_retention_days=completed_field_retention_days,deleted=deleted,cleared=cleared,has_more=has_more)

    def cleanup_main_pipeline_fields(self, *, dry_run: bool = False, completed_field_retention_days: int = 5) -> MaintenanceCleanupResult:
        return self.cleanup(
            dry_run=dry_run,
            completed_field_retention_days=completed_field_retention_days,
            include_module_retention=False,
        )

    @staticmethod
    def _exists(conn,table:str)->bool:return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())
    @staticmethod
    def _columns(conn,table:str)->set[str]:return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _delete(self,conn,table:str,column:str|None,cutoff:str|None,dry_run:bool)->int:
        if not self._exists(conn,table):return 0
        where=f"julianday({column})<julianday(?)" if column else "1=1"; args=(cutoff,) if column else ()
        rows=conn.execute(f"SELECT rowid FROM {table} WHERE {where} ORDER BY rowid LIMIT 500",args).fetchall()
        if dry_run:return len(rows)
        rowids=[int(row[0]) for row in rows]
        if not rowids:return 0
        placeholders=",".join("?" for _ in rowids)
        return int(conn.execute(f"DELETE FROM {table} WHERE rowid IN ({placeholders})",rowids).rowcount)

    def _has_rows(self, conn, table: str, where: str, params: tuple) -> bool:
        return bool(conn.execute(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1", params).fetchone())

    def _task_retention_protection(self, conn, task_alias: str, *, feedback_table: str) -> str:
        clauses = []
        metadata_exists = self._exists(conn, "newsflash_production_metadata")
        metadata_columns = self._columns(conn, "newsflash_production_metadata") if metadata_exists else set()
        if metadata_exists:
            for table in ("auditor_checks", "fact_checks"):
                if self._exists(conn, table):
                    columns = self._columns(conn, table)
                    if "status" not in columns:
                        clauses.append("0=1")
                    elif "source_item_id" in columns and {"odaily_id", "delivery_key"} <= metadata_columns:
                        clauses.append(f"NOT EXISTS (SELECT 1 FROM {table} a JOIN newsflash_production_metadata m ON m.odaily_id=a.source_item_id WHERE m.delivery_key='odaily-task-' || {task_alias}.id AND a.status IN ('pending','processing'))")
                    else:
                        clauses.append(f"NOT EXISTS (SELECT 1 FROM {table} WHERE status IN ('pending','processing'))")
        else:
            for table in ("auditor_checks", "fact_checks"):
                if self._exists(conn, table):
                    clauses.append(f"NOT EXISTS (SELECT 1 FROM {table} WHERE status IN ('pending','processing'))")
        if "." in feedback_table:
            schema, bare_feedback_table = feedback_table.split(".", 1)
            feedback_exists = bool(conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='table' AND name=?", (bare_feedback_table,)).fetchone())
            feedback_columns = {str(row[1]) for row in conn.execute(f"PRAGMA {schema}.table_info({bare_feedback_table})").fetchall()} if feedback_exists else set()
        else:
            bare_feedback_table = feedback_table
            feedback_exists = self._exists(conn, bare_feedback_table)
            feedback_columns = self._columns(conn, bare_feedback_table) if feedback_exists else set()
        if feedback_exists and {"manual_rejection_status", "feed_item_id", "feed_kind", "feedback"} <= feedback_columns:
            pending_intents = conn.execute(
                f"SELECT 1 FROM {feedback_table} WHERE manual_rejection_status IN ('pending','queued') LIMIT 1"
            ).fetchone()
            if pending_intents:
                audit_columns = self._columns(conn, "auditor_checks") if self._exists(conn, "auditor_checks") else set()
                if {"id", "source_item_id"} <= audit_columns and metadata_exists and {"delivery_key", "odaily_id"} <= metadata_columns:
                    clauses.append(f"NOT EXISTS (SELECT 1 FROM {feedback_table} fb JOIN auditor_checks a ON fb.feed_item_id='auditor:' || a.id JOIN newsflash_production_metadata m ON m.odaily_id=a.source_item_id WHERE fb.feed_kind='auditor_alert' AND fb.feedback='reject' AND fb.manual_rejection_status IN ('pending','queued') AND m.delivery_key='odaily-task-' || {task_alias}.id)")
                else:
                    clauses.append("0=1")
        if self._exists(conn, "search_event_candidates"):
            if {"primary_task_id", "status", "expires_at"} <= self._columns(conn, "search_event_candidates"):
                clauses.append(f"NOT EXISTS (SELECT 1 FROM search_event_candidates c WHERE c.primary_task_id={task_alias}.id AND c.status='active' AND (c.expires_at IS NULL OR julianday(c.expires_at)>julianday('now'))) ")
            else:
                clauses.append("0=1")
        return " AND ".join(clauses) if clauses else "1=1"


def create_maintenance_repository(database_url: str | None = None) -> SQLiteMaintenanceRepository:
    if database_url is not None:
        raise ValueError("Configure ODAILY_POSTGRES_DSN instead of passing a database URL")
    if load_storage_settings().backend == "postgres":
        from .repository import PostgresMaintenanceRepository

        return PostgresMaintenanceRepository()
    return SQLiteMaintenanceRepository(load_storage_settings().sqlite_path)
