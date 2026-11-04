from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.common.storage import connect_sqlite, load_storage_settings

from .repository import MaintenanceCleanupResult


class SQLiteMaintenanceRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def cleanup(self, *, dry_run: bool = True, retention_days: int = 7, feedback_retention_days: int = 90,
                completed_field_retention_days: int = 7) -> MaintenanceCleanupResult:
        now=datetime.now(UTC); retention=(now-timedelta(days=retention_days)).isoformat(); feedback=(now-timedelta(days=feedback_retention_days)).isoformat()
        deleted={}; cleared={}
        with connect_sqlite(self.path) as conn:
            for table,column,cutoff in (
                ("editor_plugin_generation_logs","created_at",retention),("editor_plugin_receipts",None,None),
                ("editor_plugin_feedbacks","created_at",feedback),("whale_watch_activities","created_at",retention),
                ("whale_watch_hyperliquid_activities","created_at",retention),("pipeline_alerts","created_at",retention),
                ("pipeline_worker_heartbeats","updated_at",retention)):
                deleted[table]=self._delete(conn,table,column,cutoff,dry_run)
            # Business payloads are trimmed by the verified observation archive policy.
            # Keep source identity, duplicate targets, publication decisions and unresolved audits.
            for key in ("tasks_payloads", "odaily_reference_items_payloads", "newsflash_items_payloads",
                        "x_task_pipeline_outputs", "auditor_checks_outputs", "writer3_contexts_outputs"):
                cleared[key] = 0
            conn.rollback() if dry_run else conn.commit()
        return MaintenanceCleanupResult(dry_run=dry_run,retention_days=retention_days,feedback_retention_days=feedback_retention_days,
            completed_field_retention_days=completed_field_retention_days,deleted=deleted,cleared=cleared)

    @staticmethod
    def _exists(conn,table:str)->bool:return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())
    @staticmethod
    def _columns(conn,table:str)->set[str]:return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _delete(self,conn,table:str,column:str|None,cutoff:str|None,dry_run:bool)->int:
        if not self._exists(conn,table):return 0
        where=f"julianday({column})<julianday(?)" if column else "1=1"; args=(cutoff,) if column else ()
        count=int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",args).fetchone()[0])
        if not dry_run:conn.execute(f"DELETE FROM {table} WHERE {where}",args)
        return count


def create_maintenance_repository(database_url: str | None = None) -> SQLiteMaintenanceRepository:
    del database_url
    return SQLiteMaintenanceRepository(load_storage_settings().sqlite_path)
