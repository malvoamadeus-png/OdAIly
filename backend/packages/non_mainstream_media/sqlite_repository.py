from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.common.time_utils import utc_iso
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL, _dt, _json

from .models import DiscoveredPage, NonMainstreamMediaSettings, NonMainstreamMediaSource, ParsedArticle, SiteDefinition, SourceRunStats
from .repository import (
    alert_only_task_source, alert_only_task_source_for_target, source_group_label,
    write_flow_task_source, write_flow_task_source_for_target,
)


class SQLiteNonMainstreamMediaRepository:
    def __init__(self,path:Path)->None: self.path=path; self.init_schema()
    def init_schema(self)->None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(SQLITE_SCHEMA_SQL+"""
            CREATE TABLE IF NOT EXISTS non_mainstream_media_settings(singleton_key text PRIMARY KEY,global_interval_seconds integer NOT NULL DEFAULT 60,jitter_seconds integer NOT NULL DEFAULT 5,config_version integer NOT NULL DEFAULT 1,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS non_mainstream_media_sources(id integer PRIMARY KEY AUTOINCREMENT,site_key text NOT NULL UNIQUE,display_name text NOT NULL,homepage_url text NOT NULL,capture_method text NOT NULL,pipeline_mode text NOT NULL DEFAULT 'write_flow',source_group text NOT NULL DEFAULT 'external_media',discovery_mode text NOT NULL DEFAULT 'direct',interval_seconds integer,enabled integer NOT NULL DEFAULT 1,seeded_at text,last_polled_at text,last_success_at text,last_error text,created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS non_mainstream_media_seen_items(site_key text NOT NULL,source_item_id text NOT NULL,seeded integer NOT NULL DEFAULT 0,created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(site_key,source_item_id));
            """); conn.execute("INSERT OR IGNORE INTO non_mainstream_media_settings(singleton_key) VALUES ('global')"); conn.commit()
    @staticmethod
    def _source(r)->NonMainstreamMediaSource:
        return NonMainstreamMediaSource(id=int(r["id"]),site_key=r["site_key"],display_name=r["display_name"],homepage_url=r["homepage_url"],capture_method=r["capture_method"],pipeline_mode=r["pipeline_mode"],source_group=r["source_group"],discovery_mode=r["discovery_mode"],interval_seconds=r["interval_seconds"],enabled=bool(r["enabled"]),seeded_at=_dt(r["seeded_at"]),last_polled_at=_dt(r["last_polled_at"]),last_success_at=_dt(r["last_success_at"]),last_error=r["last_error"],created_at=_dt(r["created_at"]),updated_at=_dt(r["updated_at"]))
    def sync_sources(self,site_definitions:list[SiteDefinition])->None:
        with connect_sqlite(self.path) as conn:
            for s in site_definitions: conn.execute("INSERT INTO non_mainstream_media_sources(site_key,display_name,homepage_url,capture_method,pipeline_mode,source_group,discovery_mode,interval_seconds,enabled) VALUES (?,?,?,?,?,?,?,?,1) ON CONFLICT(site_key) DO UPDATE SET display_name=excluded.display_name,homepage_url=excluded.homepage_url,capture_method=excluded.capture_method,pipeline_mode=excluded.pipeline_mode,source_group=excluded.source_group,discovery_mode=excluded.discovery_mode,interval_seconds=excluded.interval_seconds,updated_at=CURRENT_TIMESTAMP",(s.site_key,s.display_name,s.homepage_url,s.capture_method,s.pipeline_mode,s.source_group,s.discovery_mode,s.interval_seconds)); conn.commit()
    def notify_config_changed(self)->None: return None
    def get_settings(self)->NonMainstreamMediaSettings:
        with connect_sqlite(self.path) as conn:r=conn.execute("SELECT * FROM non_mainstream_media_settings WHERE singleton_key='global'").fetchone()
        return NonMainstreamMediaSettings(global_interval_seconds=int(r["global_interval_seconds"]),jitter_seconds=int(r["jitter_seconds"]),updated_at=_dt(r["updated_at"]))
    def update_settings(self,*,global_interval_seconds:int|None=None,jitter_seconds:int|None=None)->NonMainstreamMediaSettings:
        c=self.get_settings()
        with connect_sqlite(self.path) as conn: conn.execute("UPDATE non_mainstream_media_settings SET global_interval_seconds=?,jitter_seconds=?,config_version=config_version+1,updated_at=CURRENT_TIMESTAMP WHERE singleton_key='global'",(global_interval_seconds if global_interval_seconds is not None else c.global_interval_seconds,jitter_seconds if jitter_seconds is not None else c.jitter_seconds)); conn.commit()
        return self.get_settings()
    def list_sources(self,*,include_disabled:bool=False)->list[NonMainstreamMediaSource]:
        with connect_sqlite(self.path) as conn:rows=conn.execute("SELECT * FROM non_mainstream_media_sources"+("" if include_disabled else " WHERE enabled=1")+" ORDER BY enabled DESC,pipeline_mode,display_name,site_key").fetchall()
        return [self._source(r) for r in rows]
    def update_source(self,source_id:int,*,enabled:bool|None=None)->NonMainstreamMediaSource:
        if enabled is None: raise ValueError("no fields to update")
        with connect_sqlite(self.path) as conn: conn.execute("UPDATE non_mainstream_media_sources SET enabled=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(int(enabled),source_id)); r=conn.execute("SELECT * FROM non_mainstream_media_sources WHERE id=?",(source_id,)).fetchone(); conn.commit()
        if not r: raise ValueError(f"non mainstream media source not found: {source_id}")
        return self._source(r)
    def mark_source_seeded(self,source:NonMainstreamMediaSource,source_item_ids:list[str])->None:
        with connect_sqlite(self.path) as conn: conn.executemany("INSERT OR IGNORE INTO non_mainstream_media_seen_items(site_key,source_item_id,seeded) VALUES (?,?,1)",((source.site_key,x) for x in source_item_ids)); conn.execute("UPDATE non_mainstream_media_sources SET seeded_at=COALESCE(seeded_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP WHERE id=?",(source.id,)); conn.commit()
    def mark_seen(self,source:NonMainstreamMediaSource,source_item_id:str,*,seeded:bool)->bool:
        with connect_sqlite(self.path) as conn:cur=conn.execute("INSERT OR IGNORE INTO non_mainstream_media_seen_items(site_key,source_item_id,seeded) VALUES (?,?,?)",(source.site_key,source_item_id,int(seeded)));conn.commit();return cur.rowcount>0
    def unseen_source_item_ids(self,site_key:str,source_item_ids:list[str])->set[str]:
        if not source_item_ids:return set()
        with connect_sqlite(self.path) as conn:rows=conn.execute(f"SELECT source_item_id FROM non_mainstream_media_seen_items WHERE site_key=? AND source_item_id IN ({','.join('?' for _ in source_item_ids)})",(site_key,*source_item_ids)).fetchall()
        return set(source_item_ids)-{r["source_item_id"] for r in rows}
    def _save(self,*,task_source:str,source_item_id:str,source_url:str|None,title:str|None,content:str,published_at:datetime|None,raw_payload:dict,metadata:dict)->int|None:
        with connect_sqlite(self.path) as conn: conn.execute("INSERT OR IGNORE INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status) VALUES (?,?,?,?,?,?,?,?, 'pending')",(task_source,source_item_id,source_url,title,content,utc_iso(published_at),_json(raw_payload),_json(metadata)));r=conn.execute("SELECT id FROM tasks WHERE source=? AND source_item_id=?",(task_source,source_item_id)).fetchone();conn.commit();return int(r["id"]) if r else None
    def save_task(self,source:NonMainstreamMediaSource,article:ParsedArticle,*,classified_target:str|None=None)->int|None:
        task_source=write_flow_task_source_for_target(classified_target) if classified_target in {"crypto","ai"} else write_flow_task_source(source); metadata={**article.metadata,"site_key":source.site_key,"site_display_name":source.display_name,"capture_method":source.capture_method,"pipeline_mode":source.pipeline_mode,"source_group":source.source_group,"discovery_mode":source.discovery_mode,"source_label":source_group_label(source.source_group),"content_format":article.content_format,"author_names":article.author_names,"tags":article.tags,"categories":article.categories,"excerpt":article.excerpt,"canonical_url":article.canonical_url,"source_kind":task_source}
        if classified_target in {"crypto","ai"}:metadata.update(origin_source_group=source.source_group,classified_target=classified_target)
        return self._save(task_source=task_source,source_item_id=article.canonical_url,source_url=article.canonical_url,title=article.title,content=article.content,published_at=article.published_at,raw_payload=article.raw_payload,metadata=metadata)
    def save_alert_task(self,source:NonMainstreamMediaSource,page:DiscoveredPage,*,classified_target:str|None=None,classification_metadata:dict[str,Any]|None=None)->int|None:
        task_source=alert_only_task_source_for_target(classified_target) if classified_target in {"crypto","ai"} else alert_only_task_source(source); metadata={"site_key":source.site_key,"site_display_name":source.display_name,"capture_method":source.capture_method,"pipeline_mode":source.pipeline_mode,"source_group":source.source_group,"discovery_mode":source.discovery_mode,"source_label":source_group_label(source.source_group),"excerpt":page.excerpt,"published_at_raw":page.published_at_raw,"source_kind":task_source};raw={"detail_url":page.detail_url,"title":page.title,"excerpt":page.excerpt,"published_at_raw":page.published_at_raw}
        if page.discovery_url and page.discovery_url!=page.detail_url:metadata["discovery_url"]=page.discovery_url;raw["discovery_url"]=page.discovery_url
        if classified_target in {"crypto","ai"}:metadata.update(origin_source_group=source.source_group,classified_target=classified_target)
        if classification_metadata:metadata.update(classification_metadata)
        return self._save(task_source=task_source,source_item_id=page.source_item_id,source_url=page.detail_url,title=page.title,content=(page.excerpt or page.title or page.detail_url).strip(),published_at=page.published_at,raw_payload=raw,metadata=metadata)
    def record_source_run(self,stats:SourceRunStats,*,started_at:datetime,finished_at:datetime)->None:
        with connect_sqlite(self.path) as conn:conn.execute("UPDATE non_mainstream_media_sources SET last_polled_at=?,last_success_at=CASE WHEN ?='success' THEN ? ELSE last_success_at END,last_error=CASE WHEN ?='success' THEN NULL ELSE ? END,updated_at=CURRENT_TIMESTAMP WHERE id=?",(finished_at.isoformat(),stats.status,finished_at.isoformat(),stats.status,stats.error,stats.source.id));conn.commit()
    def record_worker_heartbeat(self,*,component:str,worker_id:str,status:str,success:bool,error:str|None=None,metadata:dict[str,Any]|None=None)->None:
        now=datetime.now(UTC).isoformat()
        with connect_sqlite(self.path) as conn:conn.execute("INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata) VALUES (?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,last_success_at=COALESCE(excluded.last_success_at,pipeline_worker_heartbeats.last_success_at),last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP",(component,worker_id,status,now,now if success else None,error[:2000] if error else None,_json(metadata or {})));conn.commit()
