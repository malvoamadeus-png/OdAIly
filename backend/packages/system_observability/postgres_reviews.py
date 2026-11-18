from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from .error_policy import day_start_ms, external_error, expire_external_errors, failure_category
from .postgres_store import PostgresSystemInformationStore, _now_ms
from .reviews import (
    BEIJING,
    DAILY_CALL_LIMIT,
    DAILY_TOKEN_LIMIT,
    FAILURE_HISTORY_DAYS,
    MAX_ATTEMPTS,
    PERSISTENT_FAILURE_MIN_DAYS,
    RETIRED_FAILURE_SIGNATURES,
    SystemReviewService,
    _digest,
    _expand_sample_evidence,
    _json,
    _json_dict,
    _now,
    _sample_keys,
    _stream_item_keys,
    is_actionable_internal_failure,
)
from .store import timestamp_ms


REVIEW_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_review_items (
 item_key text PRIMARY KEY,task_kind text NOT NULL,category text NOT NULL,status text NOT NULL DEFAULT 'pending',
 analysis_status text NOT NULL DEFAULT 'queued',title text NOT NULL,summary text NOT NULL DEFAULT '',severity text NOT NULL DEFAULT 'unknown',
 scope_json text NOT NULL DEFAULT '{}',expected text NOT NULL DEFAULT '',actual text NOT NULL DEFAULT '',evidence_json text NOT NULL DEFAULT '[]',
 counter_evidence_json text NOT NULL DEFAULT '[]',certainty text NOT NULL DEFAULT 'unknown',suggestion text NOT NULL DEFAULT '',
 validation text NOT NULL DEFAULT '',first_seen_at text NOT NULL,last_seen_at text NOT NULL,recovered_at text,affected_count integer NOT NULL DEFAULT 1,
 analysis_attempts integer NOT NULL DEFAULT 0,input_tokens integer NOT NULL DEFAULT 0,output_tokens integer NOT NULL DEFAULT 0,analysis_error text,
 last_reminded_at text,decision_by text,decision_note text,evidence_hash text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_system_review_queue ON system_review_items(analysis_status,analysis_attempts,last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_review_status ON system_review_items(status,last_seen_at DESC);
CREATE TABLE IF NOT EXISTS system_review_decisions(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,item_key text NOT NULL,decision text NOT NULL,actor text NOT NULL,note text NOT NULL,decided_at text NOT NULL);
CREATE INDEX IF NOT EXISTS idx_system_review_decisions_item ON system_review_decisions(item_key,id DESC);
CREATE TABLE IF NOT EXISTS system_review_usage(usage_date text PRIMARY KEY,calls integer NOT NULL DEFAULT 0,input_tokens integer NOT NULL DEFAULT 0,reserved_tokens integer NOT NULL DEFAULT 0,updated_at text NOT NULL);
CREATE TABLE IF NOT EXISTS system_review_state(state_key text PRIMARY KEY,state_value text NOT NULL,updated_at text NOT NULL);
CREATE TABLE IF NOT EXISTS system_review_runs(run_key text PRIMARY KEY,task_kind text NOT NULL,status text NOT NULL,started_at text NOT NULL,completed_at text,items_created integer NOT NULL DEFAULT 0,error text);
CREATE TABLE IF NOT EXISTS system_review_analysis_attempts(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,item_key text NOT NULL,attempt_no integer NOT NULL,started_at text NOT NULL,finished_at text,status text NOT NULL,request_id text,input_tokens integer NOT NULL DEFAULT 0,error text,raw_response_ref text);
CREATE INDEX IF NOT EXISTS idx_system_review_analysis_attempts_item ON system_review_analysis_attempts(item_key,id DESC);
CREATE INDEX IF NOT EXISTS idx_system_review_analysis_response ON system_review_analysis_attempts(raw_response_ref);
CREATE TABLE IF NOT EXISTS system_review_channel_observations(component text NOT NULL,worker_id text NOT NULL,last_bucket text NOT NULL,consecutive_failures integer NOT NULL DEFAULT 0,PRIMARY KEY(component,worker_id));
"""
REVIEW_CHECKSUM = hashlib.sha256(REVIEW_SCHEMA_SQL.encode()).hexdigest()


def _install_review_schema(conn) -> None:
    for statement in REVIEW_SCHEMA_SQL.split(";"):
        if statement.strip(): conn.execute(statement)


class PostgresSystemReviewRepository:
    def __init__(self, path: Path | None = None, *, information_store: PostgresSystemInformationStore | None = None, schema: str = "system_observability") -> None:
        self.path=path
        self.schema=schema
        self.information_store=information_store or PostgresSystemInformationStore(path,schema=schema)

    def connect(self):
        return connect_postgres(schema=self.schema,application_name="odaily-system-review")

    @staticmethod
    def initialize_schema(*, schema: str = "system_observability") -> None:
        with connect_postgres(schema=schema, application_name="odaily-system-review-migrate") as conn:
            apply_schema_migrations(
                conn,
                schema=schema,
                component="system_review",
                migrations=(PostgresMigration(1, "system review tables", REVIEW_CHECKSUM, _install_review_schema),),
            )

    def initialize(self) -> None:
        self.information_store.initialize()
        self.initialize_schema(schema=self.schema)

    def upsert_issue(self, issue:dict[str,Any])->str:
        key=str(issue.get("item_key") or f"{issue['task_kind']}:{issue['category']}:{_digest(issue.get('identity') or issue['title'])[:24]}")
        now=_now(); first=str(issue.get("first_seen_at") or now)
        evidence_hash=_digest({"identity":issue.get("identity"),"scope":issue.get("scope"),"expected":issue.get("expected"),"evidence":issue.get("evidence") or [],"counter_evidence":issue.get("counter_evidence") or [],"affected_count":issue.get("affected_count")})
        vals=(key,issue["task_kind"],issue["category"],issue["title"][:300],issue.get("summary","")[:2000],issue.get("severity","unknown"),_json(issue.get("scope") or {}),issue.get("expected","")[:2000],issue.get("actual","")[:2000],_json(issue.get("evidence") or []),_json(issue.get("counter_evidence") or []),issue.get("certainty","unknown"),issue.get("suggestion","")[:2000],issue.get("validation","")[:2000],first,now,int(issue.get("affected_count") or 1),evidence_hash)
        with self.connect() as conn:
            conn.execute("""INSERT INTO system_review_items(item_key,task_kind,category,title,summary,severity,scope_json,expected,actual,evidence_json,counter_evidence_json,certainty,suggestion,validation,first_seen_at,last_seen_at,affected_count,evidence_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(item_key) DO UPDATE SET title=excluded.title,summary=excluded.summary,severity=excluded.severity,scope_json=excluded.scope_json,expected=excluded.expected,actual=excluded.actual,evidence_json=excluded.evidence_json,counter_evidence_json=excluded.counter_evidence_json,certainty=excluded.certainty,suggestion=excluded.suggestion,validation=excluded.validation,last_seen_at=excluded.last_seen_at,affected_count=excluded.affected_count,
                first_seen_at=CASE WHEN system_review_items.recovered_at IS NOT NULL THEN excluded.first_seen_at ELSE system_review_items.first_seen_at END,
                status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'pending' ELSE system_review_items.status END,
                decision_by=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_by END,
                decision_note=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_note END,
                analysis_attempts=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash THEN 0 ELSE system_review_items.analysis_attempts END,
                evidence_hash=excluded.evidence_hash,recovered_at=NULL,analysis_status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'queued' ELSE system_review_items.analysis_status END""",vals)
        return key

    def resolve_unseen(self,task_kind:str,active_keys:set[str])->None:
        with self.connect() as conn:
            conn.execute("UPDATE system_review_items SET recovered_at=%s WHERE task_kind=%s AND recovered_at IS NULL AND NOT(item_key=ANY(%s))",(_now(),task_kind,list(active_keys)))

    @staticmethod
    def _current_error(row:dict[str,Any])->bool:
        if (timestamp_ms(row.get("last_seen_at")) or 0)<day_start_ms() and external_error({"scope":row.get("scope_json"),"evidence":row.get("evidence_json"),"status":"failed","actual":row.get("actual")}): return False
        return True

    def pending_analysis(self,limit:int=12)->list[dict[str,Any]]:
        with self.connect() as conn:
            rows=conn.execute("SELECT * FROM system_review_items WHERE (task_kind!='failure_recovery' OR (category='persistent_internal_failure' AND recovered_at IS NULL)) AND analysis_status=ANY(%s) AND analysis_attempts<%s ORDER BY last_seen_at,item_key LIMIT %s",(["queued","failed","budget_paused"],MAX_ATTEMPTS,limit)).fetchall()
        return [dict(row) for row in rows if self._current_error(dict(row))]

    def reserve(self,usage_date:str,token_estimate:int)->bool:
        now=_now()
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("INSERT INTO system_review_usage(usage_date,updated_at) VALUES(%s,%s) ON CONFLICT(usage_date) DO NOTHING",(usage_date,now))
                row=conn.execute("SELECT calls,input_tokens,reserved_tokens FROM system_review_usage WHERE usage_date=%s FOR UPDATE",(usage_date,)).fetchone()
                if row["calls"]>=DAILY_CALL_LIMIT or row["input_tokens"]+row["reserved_tokens"]+token_estimate>DAILY_TOKEN_LIMIT:return False
                conn.execute("UPDATE system_review_usage SET calls=calls+1,reserved_tokens=reserved_tokens+%s,updated_at=%s WHERE usage_date=%s",(token_estimate,now,usage_date))
        return True

    def settle(self,usage_date:str,reserved:int,input_tokens:int)->None:
        with self.connect() as c:c.execute("UPDATE system_review_usage SET reserved_tokens=GREATEST(0,reserved_tokens-%s),input_tokens=input_tokens+%s,updated_at=%s WHERE usage_date=%s",(reserved,input_tokens,_now(),usage_date))

    def release(self,usage_date:str,reserved:int)->None:
        with self.connect() as c:c.execute("UPDATE system_review_usage SET reserved_tokens=GREATEST(0,reserved_tokens-%s),updated_at=%s WHERE usage_date=%s",(reserved,_now(),usage_date))

    def complete_analysis(self,item_key:str,result:dict[str,Any])->None:
        with self.connect() as c:c.execute("UPDATE system_review_items SET analysis_status='completed',summary=%s,severity=%s,certainty=%s,suggestion=%s,validation=%s,input_tokens=%s,output_tokens=%s,analysis_attempts=analysis_attempts+1,analysis_error=NULL WHERE item_key=%s",(result["summary"][:2000],result["severity"],result["certainty"],result["suggestion"][:2000],result["validation"][:2000],result["input_tokens"],result["output_tokens"],item_key))

    def fail_analysis(self,item_key:str,error:str,*,budget_paused:bool=False)->None:
        with self.connect() as c:
            if budget_paused:c.execute("UPDATE system_review_items SET analysis_status='budget_paused',analysis_error=%s WHERE item_key=%s",(error[:1000],item_key))
            else:c.execute("UPDATE system_review_items SET analysis_status='failed',analysis_attempts=analysis_attempts+1,analysis_error=%s WHERE item_key=%s",(error[:1000],item_key))

    def start_analysis_attempt(self,item_key:str)->int:
        with self.connect() as c:
            with c.transaction():
                row=c.execute("SELECT analysis_attempts FROM system_review_items WHERE item_key=%s FOR UPDATE",(item_key,)).fetchone()
                attempt_no=int(row["analysis_attempts"])+1
                return int(c.execute("INSERT INTO system_review_analysis_attempts(item_key,attempt_no,started_at,status) VALUES(%s,%s,%s,'running') RETURNING id",(item_key,attempt_no,_now())).fetchone()["id"])

    def record_analysis_response(self,attempt_id:int,raw_response:str,request_id:str|None,input_tokens:int)->str:
        with self.connect() as c:
            with c.transaction():
                ref=self.information_store._put_evidence(c,{"raw_model_response":raw_response})
                cursor=c.execute("UPDATE system_review_analysis_attempts SET request_id=%s,input_tokens=%s,raw_response_ref=%s WHERE id=%s",(request_id[:300] if request_id else None,max(0,input_tokens),ref,attempt_id))
                if not cursor.rowcount:
                    raise ValueError("系统巡检分析尝试不存在")
        return str(ref)

    def finish_analysis_attempt(self,attempt_id:int,*,status:str,error:str|None=None)->None:
        with self.connect() as c:c.execute("UPDATE system_review_analysis_attempts SET status=%s,finished_at=%s,error=%s WHERE id=%s",(status,_now(),error[:1000] if error else None,attempt_id))

    def list_items(self,payload:dict[str,Any])->dict[str,Any]:
        status=str(payload.get("status") or "pending"); kind=str(payload.get("task_kind") or "all"); clauses=[]; params=[]
        if status=="pending":clauses.append("(task_kind!='failure_recovery' OR (category='persistent_internal_failure' AND recovered_at IS NULL))")
        if status!="all":clauses.append("status=%s");params.append(status)
        if kind!="all":clauses.append("task_kind=%s");params.append(kind)
        where=" WHERE "+" AND ".join(clauses) if clauses else ""
        with self.connect() as c:
            rows=c.execute(f"SELECT * FROM system_review_items{where} ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,last_seen_at DESC LIMIT %s OFFSET %s",(*params,max(1,min(100,int(payload.get("limit") or 30))),max(0,int(payload.get("offset") or 0)))).fetchall()
            total=int(c.execute(f"SELECT COUNT(*) AS count FROM system_review_items{where}",params).fetchone()["count"])
            usage=c.execute("SELECT * FROM system_review_usage WHERE usage_date=%s",(datetime.now(UTC).date().isoformat(),)).fetchone()
        items=[]
        for r in rows:
            item=dict(r);item["scope"]=json.loads(item.pop("scope_json") or "{}");item["evidence"]=json.loads(item.pop("evidence_json") or "[]");item["counter_evidence"]=json.loads(item.pop("counter_evidence_json") or "[]");items.append(item)
        return {"items":items,"total":total,"offset":max(0,int(payload.get("offset") or 0)),"limit":max(1,min(100,int(payload.get("limit") or 30))),"usage":dict(usage) if usage else {"calls":0,"input_tokens":0,"reserved_tokens":0},"analyses_incomplete":[i["item_key"] for i in items if i["analysis_status"] not in {"completed","evidence_insufficient"}]}

    def decide(self,item_key:str,decision:str,actor:str,note:str)->dict[str,Any]:
        if decision not in {"accepted","rejected","deferred"}:raise ValueError("不支持的人工决定")
        with self.connect() as c:
            with c.transaction():
                if not c.execute("SELECT 1 FROM system_review_items WHERE item_key=%s FOR UPDATE",(item_key,)).fetchone():raise ValueError("巡检问题不存在")
                now=_now();c.execute("UPDATE system_review_items SET status=%s,decision_by=%s,decision_note=%s WHERE item_key=%s",(decision,actor,note[:2000],item_key));c.execute("INSERT INTO system_review_decisions(item_key,decision,actor,note,decided_at) VALUES(%s,%s,%s,%s,%s)",(item_key,decision,actor,note[:2000],now))
        return self.detail(item_key)

    def detail(self,item_key:str,*,conn=None)->dict[str,Any]:
        if conn is None:
            with self.connect() as connection:
                return self.detail(item_key,conn=connection)
        c=conn
        row=c.execute("SELECT * FROM system_review_items WHERE item_key=%s",(item_key,)).fetchone()
        if not row:raise ValueError("巡检问题不存在")
        result=dict(row)
        if (timestamp_ms(result.get("last_seen_at")) or 0)<day_start_ms() and external_error({"scope":result.get("scope_json"),"evidence":result.get("evidence_json"),"status":"failed","actual":result.get("actual")}):raise ValueError("外部错误明细已超过当天留存期，请查看每日错误次数统计")
        for source,target in (("scope_json","scope"),("evidence_json","evidence"),("counter_evidence_json","counter_evidence")):
            decoded=json.loads(result.pop(source) or "{}");result[target]=self.expand_sample_references(decoded,conn=c) if target in {"evidence","counter_evidence"} else decoded
        result["decisions"]=[dict(v) for v in c.execute("SELECT decision,actor,note,decided_at FROM system_review_decisions WHERE item_key=%s ORDER BY id",(item_key,)).fetchall()]
        result["analysis_history"]=[]
        for attempt in c.execute("SELECT id,attempt_no,started_at,finished_at,status,request_id,input_tokens,error,raw_response_ref FROM system_review_analysis_attempts WHERE item_key=%s ORDER BY id",(item_key,)).fetchall():
            value=dict(attempt);ref=value.pop("raw_response_ref");response=self.information_store._load_evidence_in_connection(c,ref) if ref else None
            value["raw_response"]=(response.get("raw_model_response") or response.get("raw_model_response_excerpt")) if isinstance(response,dict) else None;result["analysis_history"].append(value)
        return result

    def expand_sample_references(self,value:Any,conn=None)->Any:
        samples=_sample_keys(value)[:30];streams=_stream_item_keys(value)[:30]
        if conn is None:
            details={str(x["sample_key"]):x for x in self.information_store.decision_sample_details(samples)}
            details.update({str(x["stream_item_id"]):x for x in self.information_store.stream_item_evidence_details(streams)})
        else:
            details={}
            if samples:
                for row in conn.execute("SELECT sample_key,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref FROM system_information_decision_samples WHERE sample_key=ANY(%s)",(samples,)).fetchall():
                    item=self.information_store._evidence_excerpt(dict(row),conn=conn);details[str(item["sample_key"])]=item
            if streams:
                for row in conn.execute("SELECT item_id,business_key,source,source_id,stage,status,result,evidence_ref FROM system_information_items WHERE item_id=ANY(%s)",(streams,)).fetchall():
                    item=dict(row);item["stream_item_id"]=item["item_id"];item=self.information_store._evidence_excerpt(item,conn=conn);details[str(item["stream_item_id"])]=item
        return _expand_sample_evidence(value,details)

    def get_state(self,key:str)->str|None:
        with self.connect() as c:
            row=c.execute("SELECT state_value FROM system_review_state WHERE state_key=%s",(key,)).fetchone()
        return str(row["state_value"]) if row else None

    def set_state(self,key:str,value:str)->None:
        with self.connect() as c:c.execute("INSERT INTO system_review_state(state_key,state_value,updated_at) VALUES(%s,%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",(key,value,_now()))

    def start_run(self,run_key:str,task_kind:str)->None:
        with self.connect() as c:c.execute("INSERT INTO system_review_runs(run_key,task_kind,status,started_at) VALUES(%s,%s,'running',%s) ON CONFLICT(run_key) DO UPDATE SET status='running',started_at=excluded.started_at,completed_at=NULL,error=NULL",(run_key,task_kind,_now()))

    def recover_interrupted_runs(self)->int:
        with self.connect() as c:return int(c.execute("UPDATE system_review_runs SET status='failed',completed_at=%s,error=COALESCE(error,'系统巡检进程重启，原运行未完成') WHERE status='running'",(_now(),)).rowcount)

    def finish_run(self,run_key:str,*,status:str,items_created:int=0,error:str|None=None)->None:
        with self.connect() as c:c.execute("UPDATE system_review_runs SET status=%s,completed_at=%s,items_created=%s,error=%s WHERE run_key=%s",(status,_now(),items_created,error[:1000] if error else None,run_key))

    def recent_runs(self,limit:int=20)->list[dict[str,Any]]:
        with self.connect() as c:rows=c.execute("SELECT run_key,task_kind,status,started_at,completed_at,items_created,error FROM system_review_runs ORDER BY started_at DESC LIMIT %s",(max(1,min(50,limit)),)).fetchall()
        return [expire_external_errors(dict(row),timestamp_ms(row["completed_at"] or row["started_at"])) for row in rows]

    def record_channel_observation(self,component:str,worker_id:str,*,failed:bool,bucket:str)->int:
        with self.connect() as c:
            with c.transaction():
                row=c.execute("SELECT last_bucket,consecutive_failures FROM system_review_channel_observations WHERE component=%s AND worker_id=%s FOR UPDATE",(component,worker_id)).fetchone()
                if row and row["last_bucket"]==bucket:return int(row["consecutive_failures"])
                prev=timestamp_ms(row["last_bucket"]) if row else None;current=timestamp_ms(bucket);adjacent=prev is not None and current is not None and current-prev<=5*60_000
                failures=int(row["consecutive_failures"])+1 if failed and adjacent else (1 if failed else 0)
                c.execute("INSERT INTO system_review_channel_observations(component,worker_id,last_bucket,consecutive_failures) VALUES(%s,%s,%s,%s) ON CONFLICT(component,worker_id) DO UPDATE SET last_bucket=excluded.last_bucket,consecutive_failures=excluded.consecutive_failures",(component,worker_id,bucket,failures));return failures


class PostgresSystemReviewService(SystemReviewService):
    """Run deterministic review checks against native PostgreSQL source tables."""

    def __init__(self, repository: PostgresSystemReviewRepository, *, model_settings: Any = None) -> None:
        root = Path("/")
        self.logical_schema_map = {"main": "main", "hyperliquid": "hyperliquid_news", "hot": "hottopic", "meme": "meme", "gate": "gate", "stock": "stock", "queue": "local_pipeline", "business": "business_news"}
        super().__init__(root, repository, model_settings=model_settings, competitor_database_path=root,
                         evidence_database_path=root, local_pipeline_database_path=root)

    def _table_exists(self, table: str) -> bool:
        with self.repository.connect() as conn:
            return any(conn.execute("SELECT to_regclass(%s) AS relation", (f"{schema}.{table}",)).fetchone()["relation"] is not None
                       for schema in dict.fromkeys(self.logical_schema_map.values()))

    def _columns(self, table: str) -> set[str]:
        with self.repository.connect() as conn:
            for schema in dict.fromkeys(self.logical_schema_map.values()):
                rows = conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                                    (schema, table)).fetchall()
                if rows:
                    return {str(row["column_name"]) for row in rows}
            return set()

    def _schema_table_exists(self, schema: str, table: str) -> bool:
        physical = self.logical_schema_map.get(schema, schema)
        with self.repository.connect() as conn:
            return conn.execute("SELECT to_regclass(%s) AS relation", (f"{physical}.{table}",)).fetchone()["relation"] is not None

    def _expected_components(self) -> dict[str, dict[str, Any]]:
        configs = {
            "x_capture": ("x_capture_accounts", "SELECT global_interval_seconds AS interval FROM main.x_capture_settings LIMIT 1"),
            "non_mainstream_media": ("non_mainstream_media_sources", "SELECT global_interval_seconds AS interval FROM main.non_mainstream_media_settings LIMIT 1"),
            "jin10_monitor": ("jin10_settings", "SELECT interval_seconds AS interval FROM main.jin10_settings LIMIT 1"),
            "whale_watch": ("whale_watch_addresses", None),
            "whale_watch_hyperliquid": ("whale_watch_hyperliquid_addresses", None),
            "binance_square": ("binance_square_accounts", "SELECT interval_seconds AS interval,enabled FROM main.binance_square_settings WHERE singleton_key='global'"),
        }
        result: dict[str, dict[str, Any]] = {}
        with self.repository.connect() as conn:
            for component, (table, interval_sql) in configs.items():
                enabled = False
                interval = 5
                try:
                    count = conn.execute(f"SELECT COUNT(*) AS count FROM main.{table} WHERE enabled=true").fetchone()
                    enabled = int(count["count"]) > 0
                    if interval_sql:
                        row = conn.execute(interval_sql).fetchone()
                        interval = max(1, int(row["interval"] or 60) // 60) if row else 5
                        if component == "binance_square":
                            enabled = enabled and bool(row and row["enabled"])
                except Exception:
                    pass
                result[component] = {"enabled": enabled, "interval_minutes": interval}
            result["local_pipeline"] = {"enabled": conn.execute("SELECT to_regclass('local_pipeline.local_pipeline_jobs') AS relation").fetchone()["relation"] is not None, "interval_minutes": 1}
        return result

    def _channel_outages(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0).isoformat()
        expected = self._expected_components()
        enabled = {key for key, value in expected.items() if value["enabled"]}
        if not enabled or not self._schema_table_exists("main", "pipeline_worker_heartbeats"):
            return []
        issues = []
        with self.repository.connect() as conn:
            rows = conn.execute("SELECT component,worker_id,status,last_seen_at,last_success_at,last_error,metadata FROM main.pipeline_worker_heartbeats WHERE component=ANY(%s)", (list(enabled),)).fetchall()
            by_component: dict[str, dict[str, Any]] = {}
            for record in rows:
                row = dict(record)
                old = by_component.get(str(row["component"]))
                if old is None or (timestamp_ms(row.get("last_seen_at")) or 0) > (timestamp_ms(old.get("last_seen_at")) or 0):
                    by_component[str(row["component"])] = row
            if "binance_square" in enabled:
                settings = conn.execute("SELECT enabled,interval_seconds,worker_status,worker_last_seen_at FROM main.binance_square_settings WHERE singleton_key='global'").fetchone()
                if settings and settings["enabled"]:
                    by_component["binance_square"] = {"component":"binance_square","worker_id":"worker","status":settings["worker_status"],"last_seen_at":settings["worker_last_seen_at"],"last_success_at":settings["worker_last_seen_at"],"last_error":None,"metadata":{}}
                    for record in conn.execute("SELECT slug_lower,last_polled_at,last_success_at,last_error FROM main.binance_square_accounts WHERE enabled=true").fetchall():
                        account=dict(record);worker_id=f"account:{account['slug_lower']}"
                        by_component[worker_id]={"component":"binance_square","worker_id":worker_id,"status":"failed" if account["last_error"] else "ok","last_seen_at":account["last_polled_at"],"last_success_at":account["last_success_at"],"last_error":account["last_error"],"metadata":{}}
        for component in enabled:
            config=expected[component]
            candidates=[row for row in by_component.values() if row.get("component")==component]
            if not candidates:
                candidates=[{"component":component,"worker_id":"worker","status":"missing","last_seen_at":None,"last_success_at":None,"last_error":"未发现心跳记录","metadata":{}}]
            for row in candidates:
                self._append_channel_issue(issues, component, config, row, now, bucket)
        return issues

    @staticmethod
    def _append_channel_issue(issues, component, config, row, now, bucket):
            window=max(15,int(config["interval_minutes"])*3)
            cutoff=int((now-timedelta(minutes=window)).timestamp()*1000)
            recent=(timestamp_ms(row.get("last_seen_at")) or 0)>=cutoff
            success=(timestamp_ms(row.get("last_success_at")) or 0)>=cutoff
            status=str(row.get("status") or "").casefold()
            if recent and success and status not in {"stopped","missing"}: return
            if recent and status in {"rate_limited","cooldown","degraded"} and success: return
            identity={"component":component,"worker_id":str(row.get("worker_id") or "worker")}
            issues.append({"identity":identity,"category":"channel_interruption","title":f"{component}采集或处理心跳中断",
                "summary":f"最近心跳 {row.get('last_seen_at') or '未知'}；最近成功 {row.get('last_success_at') or '未知'}；最近错误 {row.get('last_error') or '未记录'}",
                "severity":"high","certainty":"high","expected":f"启用中的计划任务应在 {window} 分钟内有运行成功或明确降级记录",
                "actual":f"worker={identity['worker_id']} status={row.get('status')} last_seen={row.get('last_seen_at')} last_success={row.get('last_success_at')}",
                "scope":identity,"affected_count":1,"evidence":[row],"counter_evidence":[],"suggestion":"检查 worker 心跳、上游错误和队列积压；确认订阅仍启用后再调整恢复策略",
                "validation":"连续三次计划运行成功，或最近成功时间恢复到配置窗口内",
                "consecutive_check_required":recent and status in {"failed","error","stopped"},"observation_bucket":bucket})

    def _coverage_gaps(self) -> list[dict[str, Any]]:
        if not all(self._table_exists(name) for name in ("newsflash_events","newsflash_event_sources","newsflash_items")):
            return []
        with self.repository.connect() as conn:
            news_schema = "main"
            events=conn.execute(f"SELECT event_id,representative_title,first_source,first_published_at,competitor_source_count,has_odaily,needs_review,metadata FROM {news_schema}.newsflash_events WHERE competitor_source_count>0 AND (has_odaily=false OR needs_review=true) AND first_published_at>=now()-interval '3 days' ORDER BY updated_at DESC LIMIT 100").fetchall()
            issues=[]
            for event_row in events:
                event=dict(event_row)
                sources=conn.execute(f"SELECT s.source,s.source_item_id,i.id AS item_id,i.source_url,i.title,s.role,s.match_method,s.created_at FROM {news_schema}.newsflash_event_sources s JOIN {news_schema}.newsflash_items i ON i.id=s.item_id WHERE s.event_id=%s ORDER BY s.created_at LIMIT 12",(event["event_id"],)).fetchall()
                source_evidence=[dict(row) for row in sources]
                pairs=[(str(row.get("source") or ""),str(row.get("source_item_id") or "")) for row in source_evidence if row.get("source")!="odaily" and row.get("source_item_id")]
                through=timestamp_ms(event.get("first_published_at")) or _now_ms()
                decisions=self.repository.information_store.decision_samples_for_sources(pairs,through_ms=through)
                configs=self.repository.information_store.configuration_changes_for_sources([source for source,_ in pairs],through_ms=through)
                official_publications=[{key:source.get(key) for key in ("source_item_id","item_id","source_url","title","created_at")} for source in source_evidence if source.get("source")=="odaily"]
                task_evidence=[]
                if conn.execute("SELECT to_regclass('main.tasks') AS relation").fetchone()["relation"]:
                    task_columns={str(row["column_name"]) for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name='tasks'").fetchall()}
                    pipeline_exists=conn.execute("SELECT to_regclass('main.x_task_pipeline') AS relation").fetchone()["relation"] is not None
                    pipeline_columns={str(row["column_name"]) for row in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name='x_task_pipeline'").fetchall()} if pipeline_exists else set()
                    task_fields=[name for name in ("id","source","source_item_id","status","updated_at") if name in task_columns]
                    pipeline_fields=[name for name in ("publisher_reason_code","publisher_output","push_result","last_error","final_title","final_content") if name in pipeline_columns]
                    task_select=",".join(f"t.{name}" for name in task_fields)
                    pipeline_select=","+",".join(f"p.{name}" for name in pipeline_fields) if pipeline_fields else ""
                    for source,source_id in pairs:
                        task=conn.execute(f"SELECT {task_select}{pipeline_select} FROM main.tasks t"+(" LEFT JOIN main.x_task_pipeline p ON p.task_id=t.id" if pipeline_exists else "")+" WHERE t.source=%s AND t.source_item_id=%s ORDER BY t.id DESC LIMIT 1",(source,source_id)).fetchone() if {"id","source","source_item_id","status"} <= task_columns else None
                        task_evidence.append({"source":source,"source_item_id":source_id,"configuration_history":configs.get(source) or None,"configuration_history_status":"observed_changes" if configs.get(source) else "unknown","decision_samples":decisions.get((source,source_id),[]),"task":dict(task) if task else None})
                competitor_count=int(event.get("competitor_source_count") or 0)
                issues.append({"identity":{"event_id":event["event_id"]},"category":"competitor_coverage_gap" if not event.get("has_odaily") else "event_needs_review",
                    "title":str(event.get("representative_title") or "竞品事件待复核"),"summary":"竞品事件需要逐条核对。已附精确来源 ID 对应的配置、轻决定及任务证据；缺失历史证据显示为未知。",
                    "severity":"medium","certainty":"low","expected":"检查订阅资格、采集、轻决定、写作、后台回执与公开关联证据",
                    "actual":f"first_source={event.get('first_source')} competitor_sources={competitor_count} has_odaily={event.get('has_odaily')}",
                    "scope":{"event_id":event["event_id"],"first_source":event.get("first_source")},"affected_count":max(1,competitor_count),
                    "evidence":[{"event_sources":source_evidence,"official_publications":official_publications,"source_tasks":task_evidence}],"counter_evidence":[{"metadata":event.get("metadata"),"has_odaily":event.get("has_odaily")}],
                    "suggestion":"按原帖 ID 核对历史配置、轻决定、任务终点、投递回执和公开稿关联；缺失部分标为未知","validation":"每个原始事件可回查精确来源和已有处理证据"})
        return issues

    def _audit_findings(self) -> list[dict[str, Any]]:
        issues=[]
        findings=[]
        sampling=self.information_store.record_decision_samples()
        for table,status_col,content_cols,kind in (
            ("auditor_checks","status",("title","content","audit_result","raw_output","last_error","prompt_version","source_item_id"),"text_review"),
            ("fact_checks","status",("title","content","result_json","summary","last_error","policy_version","source_item_id","original_text","original_url"),"fact_review"),
        ):
            columns=self._columns(table)
            if not {"id",status_col,"updated_at"}<=columns: continue
            selected=[name for name in content_cols if name in columns]
            projection=",".join(selected)
            projection=","+projection if projection else ""
            with self.repository.connect() as conn:
                rows=conn.execute(f"SELECT id,{status_col} AS status{projection},created_at,updated_at FROM main.{table} WHERE {status_col}=ANY(%s) AND updated_at>=now()-interval '1 day' ORDER BY updated_at DESC LIMIT 100",(["flagged","failed","unverifiable"],)).fetchall()
            for row in rows:
                value=dict(row);source=str(value.get("source_item_id") or "")
                identity={"table":table,"source_item_id":source,"version":value.get("content_hash") or value.get("prompt_version") or value.get("policy_version"),"status":value["status"]}
                findings.append({"identity":identity,"category":kind,"title":f"{kind}结果需要核对 · {source or value['id']}",
                    "summary":str(value.get("summary") or value.get("last_error") or value.get("audit_result") or value.get("result_json") or "审核记录需要对齐稿件版本和证据"),
                    "severity":"high" if value["status"]=="flagged" else "medium","certainty":"medium","expected":"先对齐原始稿、定稿、提交稿、镜像和审查版本",
                    "actual":f"审查状态={value['status']}；稿件版本键={identity.get('version') or '未保存'}","scope":identity,"affected_count":1,
                    "evidence":[{key:value.get(key) for key in selected}],"counter_evidence":[],"suggestion":"根据原始证据区分来源矛盾、写作问题、组装、稿件版本变化和审核误报；不要无版本证据归责","validation":"给出稿件版本、来源证据与审核结论的逐项对照"})
            with self.repository.connect() as conn:
                samples=conn.execute(f"SELECT id,{status_col} AS status{projection},created_at,updated_at FROM main.{table} WHERE {status_col}=ANY(%s) AND updated_at>=now()-interval '1 day' AND mod(id,17)=0 ORDER BY updated_at DESC LIMIT 30",(["passed","completed","clean"],)).fetchall()
            for row in samples:
                value=dict(row);source=str(value.get("source_item_id") or "")
                identity={"table":table,"source_item_id":source,"version":value.get("content_hash") or value.get("prompt_version") or value.get("policy_version"),"status":value["status"],"sample":True}
                findings.append({"identity":identity,"category":f"{kind}_sample","title":f"抽查已通过的{kind} · {source or value['id']}","summary":"按稳定ID取模抽样，需先对齐稿件及审查版本；审核无标记不等于事实正确。","severity":"unknown","certainty":"low","expected":"抽查输入、生成稿、提交稿、镜像和审核对象版本是否一致","actual":f"审查状态={value['status']}；稿件版本键={identity.get('version') or '未保存'}","scope":identity,"affected_count":1,"evidence":[{key:value.get(key) for key in selected}],"counter_evidence":[],"suggestion":"检查样本中的事实与版本对应关系；没有原始材料时标为无法判断","validation":"给出所查版本和每条可验证证据，不以审核状态替代事实核对"})
        if findings:
            issues.append({"identity":{"review_window":"rolling_24h","finding_keys":sorted(_digest(item["identity"])[:16] for item in findings)},"category":"review_and_output_audit_batch","title":"产出与审核复盘","summary":f"有 {len(findings)} 项审核标记或抽样结果等待按稿件版本和来源证据核对。没有保存的版本关系保持未知。","severity":"unknown","certainty":"low","expected":"对齐原始稿、定稿、提交稿、镜像和审核对象，并说明归因依据","actual":f"候选复核 {len(findings)} 项；确定性证据不足时不判断责任方。","affected_count":len(findings),"scope":{"window":"rolling_24h","items":len(findings)},"evidence":findings[:30],"counter_evidence":[],"suggestion":"按样本逐项核对来源、稿件各版本与审核证据；仅指出能够定位的修改位置","validation":"核对每项输入、稿件版本和审核对象；缺少版本证据的项标记无法判断"})
        if sampling["sampled"]:
            date=datetime.now(UTC).date().isoformat();samples=self.information_store.decision_samples(sample_date=date,limit=30)
            if samples:
                counts={}
                for row in samples:counts[str(row["business_key"])]=counts.get(str(row["business_key"]),0)+1
                issues.append({"identity":{"sample_date":date,"sample_keys":[str(row["sample_key"]) for row in samples]},"category":"no_candidate_sample_review","title":"无候选决定抽样复核","summary":f"从 {sampling['sampled']} 条分层留存样本中抽查 {len(samples)} 条；抽样不代表总体错误率。","severity":"unknown","certainty":"low","expected":"对照当时原文、轻决定理由和规则版本，判断停止理由是否有证据支持；材料不足时保持无法判断","actual":"各业务样本数："+"、".join(f"{key} {value} 条" for key,value in sorted(counts.items())),"scope":{"sample_date":date,"business_counts":counts},"affected_count":len(samples),"evidence":[{key:row.get(key) for key in ("sample_key","item_id","business_key","source","source_id","stage","status","result","reason_summary","rule_version","evidence_ref")} for row in samples],"counter_evidence":[],"suggestion":"只对证据明确显示错误的样本定位规则或阶段；截断或缺失原文的样本不得用来判定语义错误","validation":"打开样本证据确认原文可用性、当时规则版本及决定理由；不能由抽样外推总体准确率"})
        output=self.information_store.output_review_samples(limit=30)
        if output:
            date=datetime.now(UTC).date().isoformat();counts={}
            for row in output:counts[str(row["business_key"])]=counts.get(str(row["business_key"]),0)+1
            issues.append({"identity":{"sample_date":date,"stream_item_ids":[str(row["item_id"]) for row in output]},"category":"successful_output_sample_review","title":"已完成产出抽样复核","summary":f"从最近一天已完成的快讯、Meme与X Agent结果中分层抽查 {len(output)} 项；样本不代表总体错误率。","severity":"unknown","certainty":"low","expected":"对齐输入材料、产出版本、业务终点和可用审查证据；内部研究完成不视作漏发","actual":"各业务样本数："+"、".join(f"{key} {value} 条" for key,value in sorted(counts.items())),"scope":{"sample_date":date,"business_counts":counts},"affected_count":len(output),"evidence":[{"stream_item_id":row["item_id"],**{key:row.get(key) for key in ("business_key","source","source_id","title","summary","source_url","stage","status","result","updated_at_ms")}} for row in output],"counter_evidence":[],"suggestion":"先核对每项的输入、输出和预期业务终点；版本或来源证据缺失时标为未知","validation":"打开证据并核对相同业务身份的稿件/研究结果；不得将抽样结论外推为总体质量率"})
        return issues

    def _rule_conflicts(self) -> list[dict[str, Any]]:
        if not self._table_exists("prompt_templates"):
            return []
        issues=[]
        with self.repository.connect() as conn:
            rows=conn.execute("SELECT pt.template_key,pt.active_version_id,pt.feature_mode_enabled,pt.feature_mode_text,pv.version_number,pv.content FROM main.prompt_templates pt LEFT JOIN main.prompt_template_versions pv ON pv.id=pt.active_version_id").fetchall() if self._table_exists("prompt_template_versions") else []
        requirements=("固定开头","必须后台","必须挂后台","固定格式","严格json","严格json输出")
        prohibitions=("不得固定开头","禁止固定开头","不得挂后台","禁止挂后台","不要后台","禁止严格json")
        for row in rows:
            if not row["active_version_id"]: continue
            lines=[line.strip() for line in str(row["content"] or "").splitlines() if line.strip()]
            if row["feature_mode_enabled"]: lines.extend(line.strip() for line in str(row["feature_mode_text"] or "").splitlines() if line.strip())
            for required in requirements:
                positive=next((line for line in lines if required.casefold() in line.casefold()),None)
                opposite=next((line for line in lines if any(term.casefold() in line.casefold() for term in prohibitions)),None)
                if not positive or not opposite or positive==opposite: continue
                identity={"template_key":str(row["template_key"]),"version":int(row["version_number"]),"required":required,"positive_sha":_digest(positive),"opposite_sha":_digest(opposite)}
                issues.append({"identity":identity,"category":"possible_rule_conflict","title":f"模板 {row['template_key']} v{row['version_number']} 有规则方向冲突","summary":"确定性词句扫描找到可能相反的约束；仍需核对具体场景。","severity":"medium","certainty":"low","expected":"同一场景有效模板与特色规则应一致","actual":f"要求：{positive[:500]}\n限制：{opposite[:500]}","scope":identity,"affected_count":1,"evidence":[{"template_key":row["template_key"],"version":row["version_number"],"required_line":positive,"opposite_line":opposite}],"counter_evidence":[],"suggestion":"核对适用场景、模板版本和特色模式后修正同时生效的矛盾","validation":"用同一模板组合的正反例验证"})
        issues.extend(self._rule_execution_findings())
        return issues

    def _rule_execution_findings(self) -> list[dict[str, Any]]:
        if not self._table_exists("tasks") or not self._table_exists("x_task_pipeline"): return []
        from packages.hyperliquid_news.copy import ODAILY_PREFIX, PREFIX as HL_PREFIX, normalize_display_numbers
        from packages.x_processing.prompt_rules import validate_x_onchain_attribution
        with self.repository.connect() as conn:
            rows=conn.execute("SELECT t.id,t.source,t.source_item_id,t.source_url,t.title,t.status,t.metadata,p.prompt_template_key,p.prompt_version_id,p.draft_title,p.draft_content,p.final_title,p.final_content FROM main.tasks t JOIN main.x_task_pipeline p ON p.task_id=t.id WHERE t.updated_at>=now()-interval '1 day' AND p.final_content IS NOT NULL ORDER BY t.updated_at DESC LIMIT 500").fetchall()
        issues=[]
        for row in rows:
            value=dict(row);metadata=_json_dict(value.get("metadata"));title=str(value.get("final_title") or "");content=str(value.get("final_content") or "");violations=[]
            if value["source"]=="x" and value.get("prompt_template_key")=="x_onchain_writer":
                speaker=str(metadata.get("effective_author_name") or metadata.get("author_display_name") or metadata.get("author_username") or "").strip()
                if speaker:
                    try:validate_x_onchain_attribution(str(value.get("draft_content") or content),speaker)
                    except ValueError:violations.append(f"写作稿未按链上模板以“据{speaker}监测，”开头")
            preview=bool(metadata.get("is_preview") or metadata.get("mode")=="preview_only")
            if value["source"]=="hyperliquid_news" and not preview and str(value.get("status") or "").casefold() in {"published","auto_published"}:violations.append("新闻场景稿件已直发；该场景要求强制进入后台审核")
            if value["source"]=="hyperliquid_news" and not preview and content:
                if not content.startswith(ODAILY_PREFIX+HL_PREFIX):violations.append("定稿缺少 Hyperliquid 稿件规定的 Odaily 固定刊头")
                if normalize_display_numbers(title)!=title or normalize_display_numbers(content)!=content:violations.append("定稿仍含可规范的金额/比例格式")
                if re.search(r"样本(?:截至|时间|日期)\s*[:：]?\s*20\d{2}",title+"\n"+content):violations.append("定稿保留了规则要求移除的样本时间")
            if not violations:continue
            identity={"task_id":int(value["id"]),"source":value["source"],"source_item_id":value["source_item_id"],"template_key":value.get("prompt_template_key"),"prompt_version_id":value.get("prompt_version_id"),"violations":violations}
            issues.append({"identity":identity,"category":"rule_execution_mismatch","title":f"{value['source']}稿件未符合已声明格式或投递要求","summary":"定稿或任务状态与已记录规则不一致。","severity":"high","certainty":"high","expected":"遵守已记录的固定开头、后台投递和格式要求","actual":"；".join(violations),"scope":identity,"affected_count":1,"evidence":[{"source_url":value.get("source_url"),"source_item_id":value.get("source_item_id"),"status":value.get("status"),"prompt_template_key":value.get("prompt_template_key"),"prompt_version_id":value.get("prompt_version_id"),"draft_title":value.get("draft_title"),"draft_content":str(value.get("draft_content") or "")[:5000],"final_title":title,"final_content":content[:5000],"metadata":metadata}],"counter_evidence":[],"suggestion":"检查该稿件使用的版本及格式化/发布代码","validation":"违规样本可定位，合规路径不告警"})
        return issues

    def _failure_clusters(self) -> list[dict[str, Any]]:
        if not self._table_exists("x_processing_attempts") or not self._table_exists("tasks"): return []
        from collections import defaultdict
        groups=defaultdict(list)
        with self.repository.connect() as conn:
            rows=conn.execute("SELECT a.id,a.task_id,a.stage,a.attempt_no,a.status,a.error_code,a.error_message,a.started_at,a.finished_at,t.source,t.status AS task_status,p.prompt_template_key,p.prompt_version_id FROM main.x_processing_attempts a JOIN main.tasks t ON t.id=a.task_id LEFT JOIN main.x_task_pipeline p ON p.task_id=t.id WHERE a.started_at>=now()-(%s * interval '1 day') AND a.status='failed' ORDER BY a.id DESC LIMIT 50000",(FAILURE_HISTORY_DAYS,)).fetchall()
        for row in rows:
            value=dict(row);error=str(value.get("error_message") or "")
            if not error or error in RETIRED_FAILURE_SIGNATURES or not is_actionable_internal_failure(error):continue
            if not str(value.get("task_status") or "").casefold().endswith("failed"):continue
            occurred=timestamp_ms(value.get("finished_at") or value.get("started_at"))
            if occurred is None:continue
            signature=re.sub(r"\b\d{3,}\b","#",error)[:240];category=failure_category(error) or "program_or_unknown"
            row_key=(category,signature);value.update({"source":str(value.get("source") or "unknown"),"occurred_at_ms":occurred,"failure_day":datetime.fromtimestamp(occurred/1000,UTC).astimezone(BEIJING).date().isoformat(),"error_signature":signature,"failure_category":category})
            groups[row_key].append(value)
        issues=[]
        for (category,signature),rows in groups.items():
            days=sorted({row["failure_day"] for row in rows});subjects={int(row["task_id"]):row for row in rows}
            if len(days)<PERSISTENT_FAILURE_MIN_DAYS or not subjects:continue
            sources=sorted({row["source"] for row in rows});stages=sorted({str(row.get("stage") or "unknown") for row in rows});first=min(row["occurred_at_ms"] for row in rows)
            identity={"failure_category":category,"error_signature":signature}
            issues.append({"identity":identity,"category":"persistent_internal_failure","title":f"内部故障持续出现：{signature[:120]}","summary":f"过去 {FAILURE_HISTORY_DAYS} 天在 {len(days)} 个自然日反复出现；涉及 {len(subjects)} 个当前仍失败的任务。","severity":"high" if len(subjects)>=10 else "medium","certainty":"high","expected":"只展示跨日反复且仍未恢复的内部故障","actual":f"出现日期：{', '.join(days)}；来源：{'、'.join(sources[:8])}；阶段：{'、'.join(stages)}；根因：{signature}","scope":identity,"affected_count":len(subjects),"first_seen_at":datetime.fromtimestamp(first/1000,UTC).isoformat(),"evidence":[{key:row.get(key) for key in ("task_id","source","stage","error_code","error_message","started_at","finished_at","attempt_no")} for row in sorted(subjects.values(),key=lambda row:row["occurred_at_ms"],reverse=True)[:30]],"counter_evidence":[],"suggestion":"检查上述来源和阶段的共同校验或流程配置","validation":"修复后连续 3 天不再出现该根因"})
        return sorted(issues,key=lambda issue:(-int(issue["affected_count"]),str(issue["title"])))
