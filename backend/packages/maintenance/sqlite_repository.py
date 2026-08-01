from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.common.storage import connect_sqlite, load_storage_settings

from .repository import MaintenanceCleanupResult


COMPLETED = ("discarded", "duplicate", "auto_published", "ready_review", "publisher_failed", "notified")
KEEP_METADATA = ("account_username", "author_username", "author_display_name", "effective_author_name", "site_key", "site_display_name", "source_group", "source_label", "source_kind")


class SQLiteMaintenanceRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def cleanup(self, *, dry_run: bool = True, retention_days: int = 7, feedback_retention_days: int = 90,
                completed_field_retention_days: int = 7) -> MaintenanceCleanupResult:
        now=datetime.now(UTC); retention=(now-timedelta(days=retention_days)).isoformat(); feedback=(now-timedelta(days=feedback_retention_days)).isoformat(); fields=(now-timedelta(days=completed_field_retention_days)).isoformat()
        deleted={}; cleared={}
        with connect_sqlite(self.path) as conn:
            for table,column,cutoff in (
                ("editor_plugin_generation_logs","created_at",retention),("editor_plugin_receipts",None,None),
                ("editor_plugin_feedbacks","created_at",feedback),("whale_watch_activities","created_at",retention),
                ("whale_watch_hyperliquid_activities","created_at",retention),("pipeline_alerts","created_at",retention),
                ("pipeline_worker_heartbeats","updated_at",retention)):
                deleted[table]=self._delete(conn,table,column,cutoff,dry_run)
            cleared["tasks_payloads"]=self._clear_tasks(conn,fields,dry_run)
            for key,table in (("odaily_reference_items_payloads","odaily_reference_items"),("newsflash_items_payloads","newsflash_items")):
                cleared[key]=self._clear(conn,table,"raw_payload='{}',metadata='{}',updated_at=CURRENT_TIMESTAMP","updated_at<? AND (raw_payload<>'{}' OR metadata<>'{}')",(fields,),dry_run,{"raw_payload","metadata","updated_at"})
            cleared["x_task_pipeline_outputs"]=self._clear(conn,"x_task_pipeline","judge_output='{}',search_result='{}',writer_output='{}',publisher_output='{}',push_result='{}',telegram_result='{}',updated_at=CURRENT_TIMESTAMP",
                f"updated_at<? AND task_id IN (SELECT id FROM tasks WHERE status IN ({','.join('?' for _ in COMPLETED)})) AND (judge_output<>'{{}}' OR search_result<>'{{}}' OR writer_output<>'{{}}' OR publisher_output<>'{{}}' OR push_result<>'{{}}' OR telegram_result<>'{{}}')",
                (fields,*COMPLETED),dry_run,{"judge_output","search_result","writer_output","publisher_output","push_result","telegram_result"})
            cleared["auditor_checks_outputs"]=self._clear(conn,"auditor_checks","raw_output=NULL,telegram_result='{}',metadata='{}',updated_at=CURRENT_TIMESTAMP",
                "updated_at<? AND status IN ('passed','flagged','failed') AND (raw_output IS NOT NULL OR telegram_result<>'{}' OR metadata<>'{}')",(fields,),dry_run,{"raw_output","telegram_result","metadata"})
            cleared["writer3_contexts_outputs"]=self._clear(conn,"writer3_contexts","telegram_result='{}',metadata='{}',updated_at=CURRENT_TIMESTAMP",
                "updated_at<? AND status IN ('sent','skipped','failed') AND (telegram_result<>'{}' OR metadata<>'{}')",(fields,),dry_run,{"telegram_result","metadata"})
            conn.rollback() if dry_run else conn.commit()
        return MaintenanceCleanupResult(dry_run=dry_run,retention_days=retention_days,feedback_retention_days=feedback_retention_days,
            completed_field_retention_days=completed_field_retention_days,deleted=deleted,cleared=cleared)

    @staticmethod
    def _exists(conn,table:str)->bool:return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())
    @staticmethod
    def _columns(conn,table:str)->set[str]:return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _delete(self,conn,table:str,column:str|None,cutoff:str|None,dry_run:bool)->int:
        if not self._exists(conn,table):return 0
        where=f"{column}<?" if column else "1=1"; args=(cutoff,) if column else ()
        count=int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",args).fetchone()[0])
        if not dry_run:conn.execute(f"DELETE FROM {table} WHERE {where}",args)
        return count

    def _clear(self,conn,table:str,assignments:str,where:str,args:tuple,dry_run:bool,required:set[str])->int:
        if not self._exists(conn,table) or not required.issubset(self._columns(conn,table)):return 0
        count=int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}",args).fetchone()[0])
        if not dry_run:conn.execute(f"UPDATE {table} SET {assignments} WHERE {where}",args)
        return count

    def _clear_tasks(self,conn,cutoff:str,dry_run:bool)->int:
        if not self._exists(conn,"tasks") or not {"raw_payload","metadata","status","updated_at"}.issubset(self._columns(conn,"tasks")):return 0
        marks=','.join('?' for _ in COMPLETED); rows=conn.execute(f"SELECT id,raw_payload,metadata FROM tasks WHERE updated_at<? AND status IN ({marks}) AND (raw_payload<>'{{}}' OR metadata<>'{{}}')",(cutoff,*COMPLETED)).fetchall()
        if not dry_run:
            for row in rows:
                source=json.loads(row["metadata"] or "{}"); kept={k:source[k] for k in KEEP_METADATA if k in source and source[k] is not None}
                conn.execute("UPDATE tasks SET raw_payload='{}',metadata=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(json.dumps(kept,ensure_ascii=False),row["id"]))
        return len(rows)


def create_maintenance_repository(database_url: str | None = None) -> SQLiteMaintenanceRepository:
    del database_url
    return SQLiteMaintenanceRepository(load_storage_settings().sqlite_path)
