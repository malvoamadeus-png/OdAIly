from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings
from packages.whale_watch.sqlite_repository import record_heartbeat

from .models import ContextResult, OdailyReference, Writer3Candidate, Writer3Task
from .repository import WRITER3_CURRENT_SOURCE, _candidate_to_dict


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    if not value: return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed).astimezone(UTC)


class SQLiteWriter3Repository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS odaily_reference_items (
                    source_item_id text PRIMARY KEY,source_url text,title text,content text NOT NULL,published_at text,
                    raw_payload text NOT NULL DEFAULT '{}',metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS writer3_contexts (
                    id integer PRIMARY KEY AUTOINCREMENT,task_id integer,current_source text NOT NULL,current_source_item_id text NOT NULL,
                    current_source_url text,current_title text,current_content text NOT NULL,current_published_at text,status text NOT NULL DEFAULT 'pending',
                    analysis_model text,writer_model text,writer_reasoning_effort text,analysis_result text NOT NULL DEFAULT '{}',candidates text NOT NULL DEFAULT '[]',
                    context_text text,evidence_source_item_ids text NOT NULL DEFAULT '[]',telegram_text text,telegram_result text NOT NULL DEFAULT '{}',sent_at text,
                    locked_by text,locked_until text,attempt_count integer NOT NULL DEFAULT 0,last_error text,skip_reason text,metadata text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(current_source,current_source_item_id));
                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL,worker_id text NOT NULL,status text NOT NULL,last_seen_at text NOT NULL,last_success_at text,last_error text,
                    metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component,worker_id));
            """); conn.commit()

    def claim_task(self, *, worker_id: str, start_after: datetime, freshness_window_seconds: int, lock_seconds: int = 300) -> Writer3Task | None:
        self.init_schema(); now=datetime.now(UTC); cutoff=max(start_after,now-timedelta(seconds=freshness_window_seconds)); lock_until=_iso(now+timedelta(seconds=lock_seconds))
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row=conn.execute("""SELECT r.*,w.id context_id,w.status context_status,w.locked_until context_locked_until FROM odaily_reference_items r
                LEFT JOIN writer3_contexts w ON w.current_source=? AND w.current_source_item_id=r.source_item_id
                WHERE r.published_at IS NOT NULL AND r.content<>''
                AND julianday(r.published_at)>=julianday(?) AND julianday(r.published_at)<=julianday(?)
                AND (w.id IS NULL OR (w.status IN ('pending','processing') AND (w.locked_until IS NULL OR julianday(w.locked_until)<julianday(?))))
                ORDER BY julianday(r.published_at),r.source_item_id LIMIT 1""",(WRITER3_CURRENT_SOURCE,_iso(cutoff),_iso(now),_iso(now))).fetchone()
            if row is None: conn.commit(); return None
            final_content=str(row["content"])
            conn.execute("""INSERT INTO writer3_contexts(task_id,current_source,current_source_item_id,current_source_url,current_title,current_content,current_published_at,status,locked_by,locked_until,attempt_count)
                VALUES(NULL,?,?,?,?,?,?,'processing',?,?,1) ON CONFLICT(current_source,current_source_item_id) DO UPDATE SET
                current_source_url=excluded.current_source_url,current_title=excluded.current_title,current_content=excluded.current_content,current_published_at=excluded.current_published_at,
                status='processing',locked_by=excluded.locked_by,locked_until=excluded.locked_until,attempt_count=writer3_contexts.attempt_count+1,last_error=NULL,updated_at=CURRENT_TIMESTAMP""",
                (WRITER3_CURRENT_SOURCE,row["source_item_id"],row["source_url"],row["title"],final_content,row["published_at"],worker_id,lock_until))
            context=conn.execute("SELECT id FROM writer3_contexts WHERE current_source=? AND current_source_item_id=?",(WRITER3_CURRENT_SOURCE,row["source_item_id"])).fetchone(); conn.commit()
        return Writer3Task(task_id=None,source=WRITER3_CURRENT_SOURCE,source_item_id=str(row["source_item_id"]),source_url=row["source_url"],title=row["title"],
            content=str(row["content"]),final_content=final_content,published_at=_dt(row["published_at"]),updated_at=_dt(row["updated_at"]),
            metadata=json.loads(row["metadata"] or "{}"),context_id=int(context["id"]))

    def list_odaily_references(self, *, since: datetime) -> list[OdailyReference]:
        self.init_schema()
        with connect_sqlite(self.path) as conn: rows=conn.execute("SELECT * FROM odaily_reference_items WHERE julianday(published_at)>=julianday(?) ORDER BY julianday(published_at) DESC,updated_at DESC",(_iso(since),)).fetchall()
        return [OdailyReference(source_item_id=r["source_item_id"],source_url=r["source_url"],title=r["title"],content=r["content"],published_at=_dt(r["published_at"]),
            metadata=json.loads(r["metadata"] or "{}"),raw_payload=json.loads(r["raw_payload"] or "{}")) for r in rows]

    def upsert_odaily_references(self, references: list[OdailyReference]) -> int:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            for item in references: conn.execute("""INSERT INTO odaily_reference_items(source_item_id,source_url,title,content,published_at,raw_payload,metadata) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(source_item_id) DO UPDATE SET source_url=excluded.source_url,title=excluded.title,content=excluded.content,published_at=excluded.published_at,
                raw_payload=excluded.raw_payload,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP""",
                (item.source_item_id,item.source_url,item.title,item.content,_iso(item.published_at),json.dumps(item.raw_payload,ensure_ascii=False),json.dumps(item.metadata,ensure_ascii=False)))
            conn.commit()
        return len(references)

    def complete_skipped(self, task: Writer3Task, *, reason: str, metadata: dict[str, Any] | None = None) -> None:
        self._update(task,"status='skipped',skip_reason=?,metadata=?",(reason,json.dumps(metadata or {},ensure_ascii=False)))

    def complete_sent(self, task: Writer3Task, *, analysis: dict[str, Any], candidates: list[Writer3Candidate], context: ContextResult,
                      telegram_text: str, telegram_result: dict[str, Any], analysis_model: str, writer_model: str, writer_reasoning_effort: str) -> None:
        self._update(task,"""status='sent',analysis_model=?,writer_model=?,writer_reasoning_effort=?,analysis_result=?,candidates=?,context_text=?,
            evidence_source_item_ids=?,telegram_text=?,telegram_result=?,sent_at=CURRENT_TIMESTAMP""",
            (analysis_model,writer_model,writer_reasoning_effort,json.dumps(analysis,ensure_ascii=False),json.dumps([_candidate_to_dict(c) for c in candidates],ensure_ascii=False),
             context.context_text,json.dumps(context.evidence_source_item_ids,ensure_ascii=False),telegram_text,json.dumps(telegram_result,ensure_ascii=False)))

    def complete_failed(self, task: Writer3Task, *, error: str) -> None:
        self._update(task,"status='failed',last_error=?",(error[:2000],))

    def _update(self, task: Writer3Task, fields: str, values: tuple[Any,...]) -> None:
        if task.context_id is None: raise ValueError("missing writer3 context_id")
        with connect_sqlite(self.path) as conn:
            conn.execute(f"UPDATE writer3_contexts SET {fields},locked_by=NULL,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?",(*values,task.context_id)); conn.commit()

    def reset_task(self, task_id: int) -> bool:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            cursor=conn.execute("UPDATE writer3_contexts SET status='pending',locked_by=NULL,locked_until=NULL,last_error=NULL,skip_reason=NULL,updated_at=CURRENT_TIMESTAMP WHERE (task_id=? OR id=?) AND status IN ('failed','skipped','processing')",(task_id,task_id)); conn.commit()
        return bool(cursor.rowcount)

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.init_schema(); record_heartbeat(self.path,component=component,worker_id=worker_id,status=status,success=success,error=error,metadata=metadata)


def create_writer3_repository(database_url: str | None = None) -> SQLiteWriter3Repository:
    del database_url
    return SQLiteWriter3Repository(load_storage_settings().sqlite_path)
