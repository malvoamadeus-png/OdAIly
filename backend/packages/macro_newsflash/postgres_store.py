from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.common.time_utils import SHANGHAI_TZ
from .models import MacroUpdate

SCHEMA_SQL="""
CREATE TABLE IF NOT EXISTS macro_metric_state(metric_id text PRIMARY KEY,last_reference_period text NOT NULL,last_event_key text NOT NULL,last_observed_at text NOT NULL,last_source_name text NOT NULL,last_source_url text NOT NULL,last_raw_json text NOT NULL);
CREATE TABLE IF NOT EXISTS macro_newsflash_events(event_key text PRIMARY KEY,metric_id text NOT NULL,reference_period text NOT NULL,source_name text NOT NULL,source_url text NOT NULL,actual text NOT NULL,previous text NOT NULL,title text NOT NULL,content text NOT NULL,status text NOT NULL,raw_json text NOT NULL,error text,created_at text NOT NULL,delivery_started_at text,published_at text);
CREATE INDEX IF NOT EXISTS idx_macro_newsflash_events_created ON macro_newsflash_events(created_at DESC);
CREATE TABLE IF NOT EXISTS macro_release_schedule(release_id text PRIMARY KEY,source_name text NOT NULL,title text NOT NULL,metric_ids_json text NOT NULL,scheduled_at text NOT NULL,source_url text NOT NULL,is_estimate boolean NOT NULL,refreshed_at text NOT NULL);
CREATE INDEX IF NOT EXISTS idx_macro_release_schedule_time ON macro_release_schedule(scheduled_at);
CREATE TABLE IF NOT EXISTS macro_newsflash_meta(meta_key text PRIMARY KEY,meta_value text NOT NULL);
"""
CHECKSUM=hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()

class PostgresMacroNewsflashStore:
    def __init__(self,path,*,initialize=True):self.path=path; self.initialize() if initialize else None
    @staticmethod
    def initialize_schema():
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-migrate') as c:apply_schema_migrations(c,schema='macro_newsflash',component='macro_newsflash',migrations=(PostgresMigration(1,'macro newsflash persistence',CHECKSUM,lambda x:x.execute(SCHEMA_SQL)),))
    def initialize(self):self.initialize_schema()
    def schedule_refresh_due(self,*,max_age:timedelta=timedelta(hours=6))->bool:
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro') as c:meta={r['meta_key']:r['meta_value'] for r in c.execute('SELECT * FROM macro_newsflash_meta').fetchall()}
        raw=meta.get('schedule_refreshed_at')
        if not raw:return True
        try:stamp=datetime.fromisoformat(raw)
        except ValueError:return True
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=UTC)
        return datetime.now(UTC)-stamp.astimezone(UTC)>= (timedelta(minutes=15) if json.loads(meta.get('schedule_errors') or '{}') else max_age)
    def replace_release_schedule(self,releases:list[Any],*,refreshed_sources:set[str])->None:
        stamp=datetime.now(UTC).isoformat(timespec='seconds')
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-schedule') as c:
            with c.transaction():
                if refreshed_sources:c.execute('DELETE FROM macro_release_schedule WHERE source_name=ANY(%s)',(list(refreshed_sources),))
                for r in releases:
                    old=c.execute('SELECT source_name,is_estimate FROM macro_release_schedule WHERE release_id=%s FOR UPDATE',(r.release_id,)).fetchone()
                    if old and not old['is_estimate'] and r.is_estimate:continue
                    if old and old['source_name']=='Census' and r.source_name.startswith('TradingView'):continue
                    c.execute('INSERT INTO macro_release_schedule VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(release_id) DO UPDATE SET source_name=excluded.source_name,title=excluded.title,metric_ids_json=excluded.metric_ids_json,scheduled_at=excluded.scheduled_at,source_url=excluded.source_url,is_estimate=excluded.is_estimate,refreshed_at=excluded.refreshed_at',(r.release_id,r.source_name,r.title,json.dumps(r.metric_ids,ensure_ascii=False),r.scheduled_at,r.source_url,r.is_estimate,stamp))
    def _meta(self,values):
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro') as c:
            with c.transaction():
                for k,v in values.items():c.execute('INSERT INTO macro_newsflash_meta VALUES(%s,%s) ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value',(k,v))
    def record_schedule_refresh(self,*,errors:dict[str,str])->None:self._meta({'schedule_refreshed_at':datetime.now(UTC).isoformat(timespec='seconds'),'schedule_errors':json.dumps(errors,ensure_ascii=False)})
    def record_source_refresh(self,*,checked_sources:set[str],errors:dict[str,str])->None:
        if not checked_sources:return
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro') as c:
            with c.transaction():
                row=c.execute("SELECT meta_value FROM macro_newsflash_meta WHERE meta_key='source_errors' FOR UPDATE").fetchone();existing=json.loads(row['meta_value']) if row else {}
                for source in checked_sources:existing.pop(source,None)
                existing.update(errors)
                for k,v in {'source_checked_at':datetime.now(UTC).isoformat(timespec='seconds'),'source_errors':json.dumps(existing,ensure_ascii=False)}.items():c.execute('INSERT INTO macro_newsflash_meta VALUES(%s,%s) ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value',(k,v))
    def _release(self,row):
        item=dict(row);item['metric_ids']=json.loads(item.pop('metric_ids_json'));item['is_estimate']=bool(item['is_estimate']);dt=datetime.fromisoformat(item['scheduled_at']);dt=dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt;item['scheduled_at_eastern']=dt.astimezone(ZoneInfo('America/New_York')).isoformat(timespec='minutes');item['scheduled_at_beijing']=dt.astimezone(SHANGHAI_TZ).isoformat(timespec='minutes');return item
    def upcoming_releases(self,*,limit=30):
        cutoff=(datetime.now(UTC)-timedelta(hours=4)).isoformat(timespec='seconds')
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro') as c:rows=c.execute('SELECT * FROM macro_release_schedule WHERE scheduled_at >= %s ORDER BY scheduled_at LIMIT %s',(cutoff,limit)).fetchall()
        return [self._release(r) for r in rows]
    def observe_update(self,update:MacroUpdate,*,title:str,content:str,publish_latest:bool)->str:
        now=datetime.now(UTC).isoformat(timespec='seconds');raw={**update.raw_payload,'reported_actual':update.actual,'reported_previous':update.previous,'rate_action':update.rate_action};raw_json=json.dumps(raw,ensure_ascii=False,separators=(',',':'))
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-observe') as c:
            with c.transaction():
                c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (update.metric_id,))
                state=c.execute('SELECT last_reference_period FROM macro_metric_state WHERE metric_id=%s FOR UPDATE',(update.metric_id,)).fetchone()
                if state is None:
                    c.execute('INSERT INTO macro_metric_state VALUES(%s,%s,%s,%s,%s,%s,%s)',(update.metric_id,update.reference_period,update.event_key,update.observed_at,update.source_name,update.source_url,raw_json))
                    if not publish_latest:return 'baselined'
                elif update.reference_period<=state['last_reference_period']:return 'unchanged'
                else:c.execute('UPDATE macro_metric_state SET last_reference_period=%s,last_event_key=%s,last_observed_at=%s,last_source_name=%s,last_source_url=%s,last_raw_json=%s WHERE metric_id=%s',(update.reference_period,update.event_key,update.observed_at,update.source_name,update.source_url,raw_json,update.metric_id))
                c.execute("INSERT INTO macro_newsflash_events(event_key,metric_id,reference_period,source_name,source_url,actual,previous,title,content,status,raw_json,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s) ON CONFLICT(event_key) DO NOTHING",(update.event_key,update.metric_id,update.reference_period,update.source_name,update.source_url,update.actual,update.previous,title,content,raw_json,now))
        return 'queued'
    def claim_pending(self,*,limit=50):
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-delivery') as c:
            with c.transaction():
                rows=c.execute("SELECT event_key FROM macro_newsflash_events WHERE status='pending' ORDER BY created_at,event_key LIMIT %s FOR UPDATE SKIP LOCKED",(limit,)).fetchall();keys=[r['event_key'] for r in rows]
                if not keys:return []
                now=datetime.now(UTC).isoformat(timespec='seconds');return [dict(r) for r in c.execute("UPDATE macro_newsflash_events SET status='publishing',delivery_started_at=%s WHERE event_key=ANY(%s) RETURNING *",(now,keys)).fetchall()]
    def finish_delivery(self,*,event_key:str,ok:bool,error:str|None=None):
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-delivery') as c:c.execute("UPDATE macro_newsflash_events SET status=%s,error=%s,published_at=%s WHERE event_key=%s AND status='publishing'",('published' if ok else 'uncertain',None if ok else (error or 'delivery result is unknown'),datetime.now(UTC).isoformat(timespec='seconds') if ok else None,event_key))
    def recover_inflight(self):
        stale=(datetime.now(UTC)-timedelta(seconds=120)).isoformat(timespec='seconds')
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-recovery') as c:return c.execute("UPDATE macro_newsflash_events SET status='uncertain',error='worker stopped during delivery; check the feed before manual recovery' WHERE status='publishing' AND delivery_started_at<%s",(stale,)).rowcount
    def dashboard(self,*,limit=50):
        with connect_postgres(schema='macro_newsflash',application_name='odaily-macro-dashboard') as c:
            states=c.execute('SELECT * FROM macro_metric_state ORDER BY metric_id').fetchall();events=c.execute('SELECT event_key,metric_id,reference_period,actual,previous,title,status,error,created_at,published_at FROM macro_newsflash_events ORDER BY created_at DESC LIMIT %s',(limit,)).fetchall();meta={r['meta_key']:r['meta_value'] for r in c.execute('SELECT * FROM macro_newsflash_meta').fetchall()};cut=(datetime.now(UTC)-timedelta(hours=4)).isoformat(timespec='seconds');releases=c.execute('SELECT * FROM macro_release_schedule WHERE scheduled_at >= %s ORDER BY scheduled_at LIMIT 30',(cut,)).fetchall()
        metrics=[]
        for r in states:
            try:snap=json.loads(r['last_raw_json'])
            except (TypeError,ValueError):snap={}
            metrics.append({'metric_id':r['metric_id'],'last_reference_period':r['last_reference_period'],'last_observed_at':r['last_observed_at'],'source_name':r['last_source_name'],'source_url':r['last_source_url'],'actual':snap.get('reported_actual'),'previous':snap.get('reported_previous'),'rate_action':snap.get('rate_action')})
        return {'available':True,'database_path':f'postgres:{self.path}','metrics':metrics,'events':[dict(r) for r in events],'scheduled_releases':[self._release(r) for r in releases],'schedule_refreshed_at':meta.get('schedule_refreshed_at'),'schedule_errors':json.loads(meta.get('schedule_errors') or '{}'),'source_checked_at':meta.get('source_checked_at'),'source_errors':json.loads(meta.get('source_errors') or '{}')}

__all__=['PostgresMacroNewsflashStore','SCHEMA_SQL']
