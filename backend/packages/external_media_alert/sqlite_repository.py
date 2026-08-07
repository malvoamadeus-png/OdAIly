from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.common.time_utils import utc_iso
from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.searcher import SearchDocument
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL, _dt, _json, _record

from .models import (
    ALERT_NOTIFY_TASK_SOURCES, ALERT_PROMPT_KEY, ALERT_TASK_SOURCE, DOMAIN_WORKER_TASK_SOURCES,
    MAINSTREAM_MEDIA_TASK_SOURCE, STAGE_SPECS, AlertStage, DomainRoute,
    ExternalMediaAlertPipelineRecord, MediaNewsflashItem,
)
from .repository import build_mainstream_media_task_source_item_id, normalize_media_title_key


def _now() -> datetime:
    return datetime.now(UTC)


class SQLiteExternalMediaAlertRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.init_schema()

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(SQLITE_SCHEMA_SQL + """
            CREATE TABLE IF NOT EXISTS media_newsflash (
                id integer PRIMARY KEY AUTOINCREMENT, source text NOT NULL, title text NOT NULL,
                content text NOT NULL, source_url text, title_key text NOT NULL, published_at text,
                created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, title_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_media_newsflash_source_url
            ON media_newsflash(source, source_url) WHERE source_url IS NOT NULL;
            CREATE TABLE IF NOT EXISTS external_media_alert_pipeline (
                task_id integer PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                domain_route text, discard_reason text, prompt_template_key text,
                prompt_version_id integer, domain_model text, domain_output text NOT NULL DEFAULT '{}',
                search_result text NOT NULL DEFAULT '{}', telegram_result text NOT NULL DEFAULT '{}',
                last_error text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """)
            conn.commit()

    def claim_task(self, stage: AlertStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        spec = STAGE_SPECS[stage]
        sources = ALERT_NOTIFY_TASK_SOURCES if stage == "notify" else DOMAIN_WORKER_TASK_SOURCES
        now = _now()
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM tasks WHERE source IN ({','.join('?' for _ in sources)}) AND status IN (?,?) AND (locked_until IS NULL OR locked_until<?) ORDER BY created_at,id LIMIT 1",
                (*sources, spec.claim_status, spec.processing_status, now.isoformat()),
            ).fetchone()
            if row is None:
                conn.commit(); return None
            conn.execute("UPDATE tasks SET status=?,locked_by=?,locked_until=?,attempt_count=attempt_count+1,updated_at=? WHERE id=?", (spec.processing_status,worker_id,(now+timedelta(seconds=lock_seconds)).isoformat(),now.isoformat(),row["id"]))
            conn.execute("INSERT OR IGNORE INTO external_media_alert_pipeline(task_id) VALUES (?)", (row["id"],))
            claimed = conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
        return self._task(claimed)

    @staticmethod
    def _task(row) -> TaskRecord:
        data = _record(row)
        return TaskRecord(id=int(data["id"]),source=str(data["source"]),source_item_id=str(data["source_item_id"]),source_url=data.get("source_url"),title=data.get("title"),content=str(data["content"]),published_at=data.get("published_at"),raw_payload=data.get("raw_payload") or {},metadata=data.get("metadata") or {},status=str(data["status"]),created_at=data.get("created_at"),updated_at=data.get("updated_at"))

    def get_pipeline(self, task_id: int) -> ExternalMediaAlertPipelineRecord:
        with connect_sqlite(self.path) as conn: row=conn.execute("SELECT * FROM external_media_alert_pipeline WHERE task_id=?",(task_id,)).fetchone()
        if row is None: raise ValueError(f"external media alert pipeline row not found for task {task_id}")
        data=_record(row); route=data.get("domain_route") if data.get("domain_route")=="crypto" else None
        return ExternalMediaAlertPipelineRecord(task_id=task_id,domain_route=route,discard_reason=data.get("discard_reason"),prompt_template_key=data.get("prompt_template_key"),prompt_version_id=data.get("prompt_version_id"),domain_model=data.get("domain_model"),domain_output=data.get("domain_output") or {},search_result=data.get("search_result") or {},telegram_result=data.get("telegram_result") or {},last_error=data.get("last_error"))

    def get_task(self, task_id: int) -> TaskRecord:
        with connect_sqlite(self.path) as conn: row=conn.execute("SELECT * FROM tasks WHERE id=?",(task_id,)).fetchone()
        if row is None: raise ValueError(f"task not found: {task_id}")
        return self._task(row)

    def ensure_pipeline(self, task_id: int) -> None:
        with connect_sqlite(self.path) as conn: conn.execute("INSERT OR IGNORE INTO external_media_alert_pipeline(task_id) VALUES (?)",(task_id,)); conn.commit()

    def save_media_newsflash_items(self, items: list[MediaNewsflashItem]) -> tuple[int,int]:
        saved=duplicate=0
        with connect_sqlite(self.path) as conn:
            for item in items:
                key=normalize_media_title_key(item.title)
                if not key: duplicate+=1; continue
                published=item.published_at or _now()
                published_text = utc_iso(published)
                try:
                    conn.execute("INSERT INTO media_newsflash(source,title,content,source_url,title_key,published_at) VALUES (?,?,?,?,?,?)",(item.source,item.title,item.content,item.source_url,key,published_text))
                except Exception as exc:
                    if "UNIQUE constraint failed" in str(exc): duplicate+=1; continue
                    raise
                source_id=build_mainstream_media_task_source_item_id(source=item.source,source_url=item.source_url,title=item.title)
                metadata={**item.metadata,"source_kind":MAINSTREAM_MEDIA_TASK_SOURCE,"origin_source":item.source,"media_source_item_id":source_id,"original_title":item.title,"excerpt":item.content}
                conn.execute("INSERT OR IGNORE INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES (?,?,?,?,?,?,?,?, 'pending')",(MAINSTREAM_MEDIA_TASK_SOURCE,source_id,item.source_url,item.title,item.content,published_text,_json(item.raw_payload),_json(metadata)))
                saved+=1
            conn.commit()
        return saved,duplicate

    def get_active_prompt(self, template_key: str = ALERT_PROMPT_KEY) -> PromptTemplateVersion:
        with connect_sqlite(self.path) as conn: row=conn.execute("SELECT v.*,t.feature_mode_enabled,t.feature_mode_text FROM prompt_templates t JOIN prompt_template_versions v ON v.id=t.active_version_id WHERE t.template_key=? AND v.deleted_at IS NULL",(template_key,)).fetchone()
        if row is None: raise ValueError(f"active prompt not found: {template_key}")
        data=_record(row); return PromptTemplateVersion(id=int(data["id"]),template_key=str(data["template_key"]),version_number=int(data["version_number"]),content=str(data["content"]),feature_mode_enabled=bool(data.get("feature_mode_enabled")),feature_mode_text=str(data.get("feature_mode_text") or ""),note=data.get("note"),created_at=data.get("created_at"),published_at=data.get("published_at"))

    def complete_domain(self,task_id:int,*,route:DomainRoute,prompt:PromptTemplateVersion|None,model:str,raw_output:str)->None:
        self._complete_domain(task_id,route=route,discard_reason=None,prompt=prompt,model=model,raw_output=raw_output,status="classified")

    def complete_domain_discard(self,task_id:int,*,prompt:PromptTemplateVersion|None,model:str,raw_output:str,discard_reason:str="non_crypto")->None:
        self._complete_domain(task_id,route=None,discard_reason=discard_reason,prompt=prompt,model=model,raw_output=raw_output,status="discarded")

    def _complete_domain(self,task_id:int,*,route:str|None,discard_reason:str|None,prompt,model:str,raw_output:str,status:str)->None:
        output={"route":route or "discard","raw_output":raw_output}
        if discard_reason: output["discard_reason"]=discard_reason
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE external_media_alert_pipeline SET domain_route=?,discard_reason=?,prompt_template_key=?,prompt_version_id=?,domain_model=?,domain_output=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",(route,discard_reason,prompt.template_key if prompt else None,prompt.id if prompt else None,model,_json(output),task_id)); self._set_status(conn,task_id,status); conn.commit()

    def _complete_result(self,task_id:int,column:str,result:dict[str,Any],status:str)->None:
        if column not in {"search_result","telegram_result"}: raise ValueError("invalid result column")
        with connect_sqlite(self.path) as conn: conn.execute(f"UPDATE external_media_alert_pipeline SET {column}=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",(_json(result),task_id)); self._set_status(conn,task_id,status); conn.commit()
    def complete_search_duplicate(self,task_id:int,*,result:dict[str,Any])->None: self._complete_result(task_id,"search_result",result,"duplicate")
    def complete_search_ready(self,task_id:int,*,result:dict[str,Any])->None: self._complete_result(task_id,"search_result",result,"deduped")
    def complete_notify(self,task_id:int,*,telegram_result:dict[str,Any])->None: self._complete_result(task_id,"telegram_result",telegram_result,"notified")

    def fail_task(self,task_id:int,*,stage:AlertStage,error:str,status:str|None=None)->None:
        with connect_sqlite(self.path) as conn: conn.execute("UPDATE external_media_alert_pipeline SET last_error=?,updated_at=CURRENT_TIMESTAMP WHERE task_id=?",(error[:2000],task_id)); self._set_status(conn,task_id,status or STAGE_SPECS[stage].failure_status); conn.commit()

    def list_odaily_reference_documents(self,*,since:datetime)->list[SearchDocument]:
        with connect_sqlite(self.path) as conn: rows=conn.execute("SELECT * FROM odaily_reference_items WHERE published_at IS NULL OR julianday(published_at)>=julianday(?) ORDER BY julianday(published_at) DESC,updated_at DESC",(utc_iso(since),)).fetchall()
        return [SearchDocument(doc_type="odaily_reference",doc_id=str(r["source_item_id"]),title=r["title"],content=str(r["content"]),source="odaily",source_url=r["source_url"],published_at=_dt(r["published_at"]),metadata=json.loads(r["metadata"] or "{}")) for r in rows]

    def list_notified_alert_documents(self,*,since:datetime|None=None)->list[SearchDocument]:
        sources=(*ALERT_NOTIFY_TASK_SOURCES,MAINSTREAM_MEDIA_TASK_SOURCE); params:list[Any]=list(sources); sql=f"SELECT * FROM tasks WHERE source IN ({','.join('?' for _ in sources)}) AND status IN ('notified','ready_review')"
        if since: sql+=" AND julianday(created_at)>=julianday(?)"; params.append(utc_iso(since))
        sql+=" ORDER BY julianday(created_at) DESC,id DESC"
        with connect_sqlite(self.path) as conn: rows=conn.execute(sql,params).fetchall()
        docs=[]
        for r in rows:
            metadata=json.loads(r["metadata"] or "{}"); docs.append(SearchDocument(doc_type="external_media_alert_history",doc_id=str(r["source_item_id"]),title=r["title"],content=str(r["content"]),source=str(metadata.get("source_kind") or r["source"] or ALERT_TASK_SOURCE),source_url=r["source_url"],task_id=int(r["id"]),published_at=_dt(r["published_at"]),metadata=metadata))
        return docs

    def record_worker_heartbeat(self,*,component:str,worker_id:str,status:str,success:bool,error:str|None=None,metadata:dict[str,Any]|None=None)->None:
        now=_now().isoformat()
        with connect_sqlite(self.path) as conn: conn.execute("INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata) VALUES (?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,last_success_at=COALESCE(excluded.last_success_at,pipeline_worker_heartbeats.last_success_at),last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(component,worker_id,status,now,now if success else None,error[:2000] if error else None,_json(metadata or {}))); conn.commit()

    @staticmethod
    def _set_status(conn,task_id:int,status:str)->None:
        conn.execute("UPDATE tasks SET status=?,locked_by=NULL,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(status,task_id))
