from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.x_processing.searcher import content_hash, normalize_for_embedding
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL, _dt, _json

from .events import EventAssignment, EventSourceRecord, NewsflashItemRecord, generate_event_id
from .fetchers import NewsflashItem
from .repository import parse_datetime


class SQLiteCompetitorMonitorRepository:
    def __init__(self,path:Path)->None:self.path=path;self.init_schema()
    def init_schema(self)->None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(SQLITE_SCHEMA_SQL+"""
            CREATE TABLE IF NOT EXISTS newsflash_items(id integer PRIMARY KEY AUTOINCREMENT,source text NOT NULL,source_item_id text NOT NULL,source_url text,title text,content text NOT NULL,content_hash text NOT NULL,published_at text,first_seen_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,raw_payload text NOT NULL DEFAULT '{}',metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(source,source_item_id));
            CREATE TABLE IF NOT EXISTS newsflash_events(event_id text PRIMARY KEY,representative_item_id integer,representative_title text,event_time text,first_source text,first_published_at text,source_count integer NOT NULL DEFAULT 0,competitor_source_count integer NOT NULL DEFAULT 0,has_odaily integer NOT NULL DEFAULT 0,status text NOT NULL DEFAULT 'active',needs_review integer NOT NULL DEFAULT 0,metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS newsflash_event_sources(id integer PRIMARY KEY AUTOINCREMENT,event_id text NOT NULL REFERENCES newsflash_events(event_id) ON DELETE CASCADE,item_id integer NOT NULL UNIQUE REFERENCES newsflash_items(id) ON DELETE CASCADE,source text NOT NULL,source_item_id text NOT NULL,role text NOT NULL,match_method text NOT NULL,similarity real,matched_item_id integer,ai_result text NOT NULL DEFAULT '{}',metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS newsflash_event_favorites(event_id text PRIMARY KEY REFERENCES newsflash_events(event_id) ON DELETE CASCADE,created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS newsflash_event_notes(event_id text PRIMARY KEY REFERENCES newsflash_events(event_id) ON DELETE CASCADE,note text NOT NULL,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS newsflash_item_notes(item_id integer PRIMARY KEY REFERENCES newsflash_items(id) ON DELETE CASCADE,note text NOT NULL,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_newsflash_items_source_time ON newsflash_items(source,published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_newsflash_event_sources_event ON newsflash_event_sources(event_id);
            DROP VIEW IF EXISTS newsflash_event_summary;
            CREATE VIEW newsflash_event_summary AS
            SELECT e.event_id,e.representative_title,e.event_time,e.first_source,e.first_published_at,e.source_count,e.competitor_source_count,e.has_odaily,e.status,e.needs_review,
                   CASE WHEN f.event_id IS NULL THEN 0 ELSE 1 END favorite,COALESCE(n.note,'') note,
                   COALESCE((SELECT json_group_array(json_object('id',s.id,'item_id',s.item_id,'source',s.source,'source_item_id',s.source_item_id,'role',s.role,'match_method',s.match_method,'similarity',s.similarity)) FROM newsflash_event_sources s WHERE s.event_id=e.event_id),'[]') sources
            FROM newsflash_events e LEFT JOIN newsflash_event_favorites f ON f.event_id=e.event_id LEFT JOIN newsflash_event_notes n ON n.event_id=e.event_id;
            """);conn.commit()
    def list_enabled_competitor_exclusion_terms(self)->list[str]:
        with connect_sqlite(self.path) as conn:
            try:rows=conn.execute("SELECT scopes,terms FROM source_exclusion_rule_groups WHERE enabled=1").fetchall()
            except Exception:return []
        terms=[]
        for r in rows:
            if "competitor" in json.loads(r["scopes"] or "[]"):terms.extend(str(x) for x in json.loads(r["terms"] or "[]") if str(x).strip())
        return sorted(set(terms),key=lambda x:(-len(x),x))
    def save_items(self,items:list[NewsflashItem])->tuple[int,int]:
        tasks,refs=self.save_items_for_pipeline(items);return len(tasks),refs
    def save_items_for_pipeline(self,items:list[NewsflashItem])->tuple[list[tuple[NewsflashItem,int]],int]:
        tasks=[];refs=0
        with connect_sqlite(self.path) as conn:
            for item in items:
                published=parse_datetime(item.published_at);published_text=published.isoformat() if published else None
                if item.source=="odaily":
                    previous=conn.execute("SELECT 1 FROM odaily_reference_items WHERE source_item_id=?",(item.source_item_id,)).fetchone()
                    conn.execute("INSERT INTO odaily_reference_items(source_item_id,source_url,title,content,published_at,raw_payload,metadata) VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_item_id) DO UPDATE SET source_url=excluded.source_url,title=excluded.title,content=excluded.content,published_at=excluded.published_at,raw_payload=excluded.raw_payload,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(item.source_item_id,item.source_url,item.title,item.content,published_text,_json(item.raw_payload),_json(item.metadata)))
                    if previous is None:refs+=1
                    continue
                conn.execute("INSERT INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES (?,?,?,?,?,?,?,?, 'pending') ON CONFLICT(source,source_item_id) DO UPDATE SET source_url=CASE WHEN excluded.source='jinse' THEN excluded.source_url ELSE COALESCE(excluded.source_url,tasks.source_url) END,raw_payload=excluded.raw_payload,updated_at=tasks.updated_at",(item.source,item.source_item_id,item.source_url,item.title,item.content,published_text,_json(item.raw_payload),_json({**item.metadata,"source_kind":"competitor"})))
                row=conn.execute("SELECT id FROM tasks WHERE source=? AND source_item_id=?",(item.source,item.source_item_id)).fetchone();tasks.append((item,int(row["id"])))
            conn.commit()
        return tasks,refs
    @staticmethod
    def _item(r)->NewsflashItemRecord:
        return NewsflashItemRecord(id=int(r["id"]),source=r["source"],source_item_id=r["source_item_id"],source_url=r["source_url"],title=r["title"],content=r["content"],published_at=_dt(r["published_at"]),first_seen_at=_dt(r["first_seen_at"]),metadata=json.loads(r["metadata"] or "{}"))
    def upsert_newsflash_items(self,items:list[NewsflashItem])->list[NewsflashItemRecord]:
        records=[]
        with connect_sqlite(self.path) as conn:
            for item in items:
                published=parse_datetime(item.published_at);digest=content_hash(normalize_for_embedding(title=item.title,content=item.content))
                conn.execute("INSERT INTO newsflash_items(source,source_item_id,source_url,title,content,content_hash,published_at,raw_payload,metadata) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_item_id) DO UPDATE SET source_url=excluded.source_url,title=excluded.title,content=excluded.content,content_hash=excluded.content_hash,published_at=excluded.published_at,raw_payload=excluded.raw_payload,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(item.source,item.source_item_id,item.source_url,item.title,item.content,digest,published.isoformat() if published else None,_json(item.raw_payload),_json(item.metadata)))
                records.append(self._item(conn.execute("SELECT * FROM newsflash_items WHERE source=? AND source_item_id=?",(item.source,item.source_item_id)).fetchone()))
            conn.commit()
        return records
    def _sources(self,where:str,params:tuple)->list[EventSourceRecord]:
        with connect_sqlite(self.path) as conn:rows=conn.execute("SELECT s.event_id,i.* FROM newsflash_event_sources s JOIN newsflash_items i ON i.id=s.item_id JOIN newsflash_events e ON e.event_id=s.event_id WHERE e.status='active' AND "+where+" ORDER BY COALESCE(i.published_at,i.first_seen_at) DESC,i.id DESC",params).fetchall()
        return [EventSourceRecord(event_id=r["event_id"],item=self._item(r)) for r in rows]
    def list_existing_event_sources(self,*,item_ids:set[int])->list[EventSourceRecord]:
        if not item_ids:return []
        return self._sources(f"i.id IN ({','.join('?' for _ in item_ids)})",tuple(item_ids))
    def list_recent_event_sources(self,*,since:datetime,exclude_item_ids:set[int])->list[EventSourceRecord]:
        where="COALESCE(i.published_at,i.first_seen_at)>=?";params=[since.isoformat()]
        if exclude_item_ids:where+=f" AND i.id NOT IN ({','.join('?' for _ in exclude_item_ids)})";params.extend(sorted(exclude_item_ids))
        return self._sources(where,tuple(params))
    def create_event_with_source(self,item:NewsflashItemRecord,*,needs_review:bool=False)->str:
        event_id=generate_event_id();event_time=item.published_at or item.first_seen_at
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE");existing=conn.execute("SELECT event_id FROM newsflash_event_sources WHERE item_id=?",(item.id,)).fetchone()
            if existing:conn.commit();return str(existing["event_id"])
            conn.execute("INSERT INTO newsflash_events(event_id,representative_item_id,representative_title,event_time,first_source,first_published_at,source_count,competitor_source_count,has_odaily,needs_review,metadata) VALUES (?,?,?,?,?,?,1,?,?,?,?)",(event_id,item.id,item.title,event_time.isoformat() if event_time else None,item.source,event_time.isoformat() if event_time else None,0 if item.source=="odaily" else 1,int(item.source=="odaily"),int(needs_review),_json({"created_from":{"source":item.source,"source_item_id":item.source_item_id}})))
            conn.execute("INSERT INTO newsflash_event_sources(event_id,item_id,source,source_item_id,role,match_method,metadata) VALUES (?,?,?,?, 'primary','new_event',?)",(event_id,item.id,item.source,item.source_item_id,_json({"needs_review":needs_review})));conn.commit()
        return event_id
    def assign_item_to_event(self,a:EventAssignment)->None:
        with connect_sqlite(self.path) as conn:
            row=conn.execute("SELECT source,source_item_id FROM newsflash_items WHERE id=?",(a.item_id,)).fetchone()
            if not row:raise ValueError(f"newsflash item not found: {a.item_id}")
            previous=conn.execute("SELECT event_id FROM newsflash_event_sources WHERE item_id=?",(a.item_id,)).fetchone()
            conn.execute("INSERT INTO newsflash_event_sources(event_id,item_id,source,source_item_id,role,match_method,similarity,matched_item_id,ai_result,metadata) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET event_id=excluded.event_id,role=excluded.role,match_method=excluded.match_method,similarity=excluded.similarity,matched_item_id=excluded.matched_item_id,ai_result=excluded.ai_result,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(a.event_id,a.item_id,row["source"],row["source_item_id"],a.role,a.match_method,a.similarity,a.matched_item_id,_json(a.ai_result),_json({"needs_review":a.needs_review})))
            if a.needs_review:conn.execute("UPDATE newsflash_events SET needs_review=1 WHERE event_id=?",(a.event_id,))
            if previous and previous["event_id"]!=a.event_id:conn.execute("DELETE FROM newsflash_events WHERE event_id=? AND NOT EXISTS(SELECT 1 FROM newsflash_event_sources WHERE event_id=?)",(previous["event_id"],previous["event_id"]))
            conn.commit()
        self.update_event_summaries({a.event_id,*([str(previous["event_id"])] if previous else [])})
    def update_event_summaries(self,event_ids:set[str])->None:
        with connect_sqlite(self.path) as conn:
            for eid in event_ids:
                rows=conn.execute("SELECT i.* FROM newsflash_event_sources s JOIN newsflash_items i ON i.id=s.item_id WHERE s.event_id=? ORDER BY COALESCE(i.published_at,i.first_seen_at),i.id",(eid,)).fetchall()
                if not rows:continue
                first=rows[0];representative=next((r for r in reversed(rows) if r["source"]=="odaily"),rows[-1]);times=[r["published_at"] or r["first_seen_at"] for r in rows if r["published_at"] or r["first_seen_at"]]
                conn.execute("UPDATE newsflash_events SET representative_item_id=?,representative_title=?,event_time=?,first_source=?,first_published_at=?,source_count=?,competitor_source_count=?,has_odaily=?,updated_at=CURRENT_TIMESTAMP WHERE event_id=?",(representative["id"],representative["title"],max(times) if times else None,first["source"],first["published_at"] or first["first_seen_at"],len(rows),sum(r["source"]!="odaily" for r in rows),int(any(r["source"]=="odaily" for r in rows)),eid))
            conn.commit()
    def prune_orphan_events(self)->int:
        with connect_sqlite(self.path) as conn:cur=conn.execute("DELETE FROM newsflash_events WHERE NOT EXISTS(SELECT 1 FROM newsflash_event_sources s WHERE s.event_id=newsflash_events.event_id)");conn.commit();return cur.rowcount
    def prune_excluded_event_sources(self,terms:list[str]|None=None)->dict[str,int]:
        terms=terms if terms is not None else self.list_enabled_competitor_exclusion_terms()
        if not terms:return {"matched_items":0,"removed_sources":0,"deleted_events":0,"updated_events":0}
        with connect_sqlite(self.path) as conn:
            rows=conn.execute("SELECT id FROM newsflash_items WHERE source!='odaily'").fetchall();ids=[]
            for r in rows:
                item=conn.execute("SELECT title,content FROM newsflash_items WHERE id=?",(r["id"],)).fetchone();text=f"{item['title'] or ''}\n{item['content']}".casefold()
                if any(t.casefold() in text for t in terms):ids.append(r["id"])
            sources=0
            if ids:sources=conn.execute(f"DELETE FROM newsflash_event_sources WHERE item_id IN ({','.join('?' for _ in ids)})",ids).rowcount;conn.execute(f"DELETE FROM newsflash_items WHERE id IN ({','.join('?' for _ in ids)})",ids)
            events=conn.execute("DELETE FROM newsflash_events WHERE NOT EXISTS(SELECT 1 FROM newsflash_event_sources s WHERE s.event_id=newsflash_events.event_id)").rowcount;conn.commit()
        return {"matched_items":len(ids),"removed_sources":sources,"deleted_events":events,"updated_events":0}
    def repair_newsflash_timestamps(self)->dict[str,int]:return {"updated_items":0,"updated_events":0}
    def record_worker_heartbeat(self,*,component:str,worker_id:str,status:str,success:bool,error:str|None=None,metadata:dict[str,Any]|None=None)->None:
        now=datetime.now(UTC).isoformat()
        with connect_sqlite(self.path) as conn:conn.execute("INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata) VALUES (?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,last_success_at=COALESCE(excluded.last_success_at,pipeline_worker_heartbeats.last_success_at),last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(component,worker_id,status,now,now if success else None,error[:2000] if error else None,_json(metadata or {})));conn.commit()
