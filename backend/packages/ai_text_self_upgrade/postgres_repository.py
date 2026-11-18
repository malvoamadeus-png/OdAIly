from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_items (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, case_key text UNIQUE NOT NULL,
 source_type text NOT NULL DEFAULT 'text_auditor', auditor_check_id bigint NOT NULL,
 fact_check_id bigint, fact_policy_version text, fact_model text, fact_status text,
 fact_reason text NOT NULL DEFAULT '', fact_summary text NOT NULL DEFAULT '', fact_result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
 fact_original_text text NOT NULL DEFAULT '', fact_original_url text NOT NULL DEFAULT '', fact_completed_at timestamptz,
 source_item_id text NOT NULL, content_hash text NOT NULL, audit_prompt_version text, audit_model text,
 audited_title text, audited_content text NOT NULL, audit_result_json jsonb NOT NULL DEFAULT '{}'::jsonb,
 published_at timestamptz, source_url text, source_title text, source_content text NOT NULL, task_id bigint NOT NULL,
 writer_model text, prompt_template_key text, prompt_version_id bigint, draft_title text, draft_content text,
 final_title text, final_content text, analysis_status text NOT NULL DEFAULT 'pending', analysis_model text,
 analysis_prompt_version text, verdict text, confidence text, rationale text, evidence_json jsonb NOT NULL DEFAULT '[]'::jsonb,
 analysis_error text, attempt_count integer NOT NULL DEFAULT 0, human_decision text NOT NULL DEFAULT 'pending',
 decision_by text, decision_note text, decided_at timestamptz, human_resolution text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_ai_text_upgrade_queue ON ai_text_self_upgrade_items(analysis_status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_ai_text_upgrade_source_queue ON ai_text_self_upgrade_items(source_type,analysis_status,created_at,id);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_decisions(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,case_key text NOT NULL REFERENCES ai_text_self_upgrade_items(case_key),decision text NOT NULL,actor text NOT NULL,note text NOT NULL DEFAULT '',decided_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_resolutions(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,case_key text NOT NULL REFERENCES ai_text_self_upgrade_items(case_key),decision text NOT NULL,actor text NOT NULL,note text NOT NULL DEFAULT '',decided_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_state(state_key text PRIMARY KEY,state_value text NOT NULL,updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_runs(run_key text PRIMARY KEY,status text NOT NULL,started_at timestamptz NOT NULL,completed_at timestamptz,cases_found integer NOT NULL DEFAULT 0,cases_completed integer NOT NULL DEFAULT 0,cases_failed integer NOT NULL DEFAULT 0,cases_unlinked integer NOT NULL DEFAULT 0,error text);
CREATE INDEX IF NOT EXISTS idx_ai_text_upgrade_decisions ON ai_text_self_upgrade_decisions(case_key,id DESC);
"""
CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _item(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in tuple(result.items()):
        if isinstance(value, datetime): result[key] = value.isoformat()
    for source, target, default in (("audit_result_json", "audit_result", {}), ("fact_result_json", "fact_result", {}), ("evidence_json", "evidence", [])):
        result[target] = _decode(result.pop(source), default)
    if result.get("source_type") == "fact_auditor" and result.get("verdict") == "auditor_false_positive":
        result["verdict"] = "fact_auditor_error"
    if result.get("verdict") is None and result.get("analysis_status") == "completed":
        result["verdict"] = "inconclusive"
    return result


class PostgresAITextSelfUpgradeRepository:
    def __init__(self, *, schema: str = "system_observability", recover_inflight: bool = False) -> None:
        self.schema = schema
        self.initialize(recover_inflight=recover_inflight)

    def connect(self):
        return connect_postgres(schema=self.schema, application_name="odaily-ai-text-upgrade")

    @classmethod
    def initialize_schema(cls, *, schema: str = "system_observability") -> None:
        def install(conn):
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip(): conn.execute(statement)

        with connect_postgres(schema=schema, application_name="odaily-ai-text-upgrade-migrate") as conn:
            apply_schema_migrations(conn, schema=schema, component="ai_text_upgrade", migrations=(PostgresMigration(1, "AI text self-upgrade", CHECKSUM, install),))

    def initialize(self, *, recover_inflight: bool = True) -> None:
        self.initialize_schema(schema=self.schema)
        with self.connect() as conn:
            stamp = _now()
            conn.execute("INSERT INTO ai_text_self_upgrade_state(state_key,state_value,updated_at) VALUES('enabled_at',COALESCE((SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='fact_enabled_at'),%s),clock_timestamp()) ON CONFLICT(state_key) DO NOTHING", (stamp,))
            conn.execute("INSERT INTO ai_text_self_upgrade_state(state_key,state_value,updated_at) VALUES('fact_enabled_at',COALESCE((SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='enabled_at'),%s),clock_timestamp()) ON CONFLICT(state_key) DO NOTHING", (stamp,))
        if recover_inflight:
            self.recover_inflight()

    def recover_inflight(self) -> None:
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=COALESCE(analysis_error,'服务重启时原分析未完成'),updated_at=clock_timestamp() WHERE analysis_status='processing'")
                conn.execute("UPDATE ai_text_self_upgrade_runs SET status='failed',completed_at=clock_timestamp(),error=COALESCE(error,'服务重启时原批次未完成') WHERE status='running'")

    def close(self) -> None: return None
    def enabled_at(self) -> str:
        with self.connect() as conn: row = conn.execute("SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='enabled_at'").fetchone()
        if row is None: raise RuntimeError("AI文本自我审核启用时间未初始化")
        return str(row["state_value"])
    def fact_enabled_at(self) -> str:
        with self.connect() as conn: row = conn.execute("SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='fact_enabled_at'").fetchone()
        if row is None: raise RuntimeError("事实审查者自我审核启用时间未初始化")
        return str(row["state_value"])

    def case_exists(self, case_key: str) -> bool:
        with self.connect() as conn: return conn.execute("SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=%s", (case_key,)).fetchone() is not None

    def insert_case(self, case: dict[str, Any]) -> bool:
        columns = ("case_key,source_type,auditor_check_id,fact_check_id,fact_policy_version,fact_model,fact_status,fact_reason,fact_summary,fact_result_json,fact_original_text,fact_original_url,fact_completed_at,source_item_id,content_hash,audit_prompt_version,audit_model,audited_title,audited_content,audit_result_json,published_at,source_url,source_title,source_content,task_id,writer_model,prompt_template_key,prompt_version_id,draft_title,draft_content,final_title,final_content")
        values = (case["case_key"],case.get("source_type","fact_auditor"),case["auditor_check_id"],case.get("fact_check_id"),case.get("fact_policy_version"),case.get("fact_model"),case.get("fact_status"),case.get("fact_reason") or "",case.get("fact_summary") or "",_json(case.get("fact_result") or {}),case.get("fact_original_text") or "",case.get("fact_original_url") or "",case.get("fact_completed_at"),case["source_item_id"],case["content_hash"],case.get("audit_prompt_version"),case.get("audit_model"),case.get("audited_title"),case["audited_content"],_json(case.get("audit_result") or {}),case.get("published_at"),case.get("source_url"),case.get("source_title"),case["source_content"],case["task_id"],case.get("writer_model"),case.get("prompt_template_key"),case.get("prompt_version_id"),case.get("draft_title"),case.get("draft_content"),case.get("final_title"),case.get("final_content"))
        with self.connect() as conn:
            return bool(conn.execute(f"INSERT INTO ai_text_self_upgrade_items({columns}) VALUES({','.join(['%s']*len(values))}) ON CONFLICT(case_key) DO NOTHING", values).rowcount)

    def has_run(self, run_key: str) -> bool:
        with self.connect() as conn: return conn.execute("SELECT 1 FROM ai_text_self_upgrade_runs WHERE run_key=%s", (run_key,)).fetchone() is not None
    def start_run(self, run_key: str) -> bool:
        with self.connect() as conn: return bool(conn.execute("INSERT INTO ai_text_self_upgrade_runs(run_key,status,started_at) VALUES(%s,'running',clock_timestamp()) ON CONFLICT DO NOTHING", (run_key,)).rowcount)
    def finish_run(self, run_key: str, *, status: str, cases_found: int = 0, cases_completed: int = 0, cases_failed: int = 0, cases_unlinked: int = 0, error: str | None = None) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_text_self_upgrade_runs SET status=%s,completed_at=clock_timestamp(),cases_found=%s,cases_completed=%s,cases_failed=%s,cases_unlinked=%s,error=%s WHERE run_key=%s", (status,cases_found,cases_completed,cases_failed,cases_unlinked,error[:2000] if error else None,run_key))
    def recent_runs(self, limit: int = 12, *, since: str | None = None) -> list[dict[str, Any]]:
        where = "" if since is None else " WHERE started_at >= %s::timestamptz"
        params = (max(1, min(50, limit)),) if since is None else (since, max(1, min(50, limit)))
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM ai_text_self_upgrade_runs" + where + " ORDER BY started_at DESC LIMIT %s",
                params,
            ).fetchall()]

    def claim_next(self, *, excluded_case_keys: set[str] | None = None, included_case_keys: set[str] | None = None) -> dict[str, Any] | None:
        excluded, included = sorted(excluded_case_keys or ()), sorted(included_case_keys or ())
        if included_case_keys is not None and not included: return None
        clauses = ["analysis_status IN ('pending','failed')"]; params: list[Any] = []
        if included_case_keys is not None: clauses.append("case_key=ANY(%s)"); params.append(included)
        if excluded: clauses.append("NOT(case_key=ANY(%s))"); params.append(excluded)
        with self.connect() as conn:
            with conn.transaction():
                row = conn.execute("SELECT * FROM ai_text_self_upgrade_items WHERE " + " AND ".join(clauses) + " ORDER BY created_at,id LIMIT 1 FOR UPDATE SKIP LOCKED", params).fetchone()
                if row is None: return None
                conn.execute("UPDATE ai_text_self_upgrade_items SET analysis_status='processing',attempt_count=attempt_count+1,analysis_error=NULL,updated_at=clock_timestamp() WHERE case_key=%s", (row["case_key"],))
                return _item(conn.execute("SELECT * FROM ai_text_self_upgrade_items WHERE case_key=%s", (row["case_key"],)).fetchone())

    def complete_analysis(self, case_key: str, *, model: str, prompt_version: str, result: dict[str, Any]) -> None:
        verdict = None if result["verdict"] == "inconclusive" else result["verdict"]
        with self.connect() as conn: conn.execute("UPDATE ai_text_self_upgrade_items SET analysis_status='completed',analysis_model=%s,analysis_prompt_version=%s,verdict=%s,confidence=%s,rationale=%s,evidence_json=%s::jsonb,analysis_error=NULL,updated_at=clock_timestamp() WHERE case_key=%s", (model,prompt_version,verdict,result["confidence"],result["rationale"],_json(result["evidence"]),case_key))
    def fail_analysis(self, case_key: str, *, error: str) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=%s,updated_at=clock_timestamp() WHERE case_key=%s", (error[:2000],case_key))

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "pending"); clauses: list[str] = []; params: list[Any] = []
        if status != "all":
            if status not in {"pending","reviewer_correct","writer_correct","ignored","legacy"}: raise ValueError("不支持的案例状态")
            if status == "legacy": clauses.append("human_resolution IS NULL AND human_decision IN ('accepted','rejected','ignored')")
            elif status == "pending": clauses.append("human_resolution IS NULL AND human_decision='pending'")
            else: clauses.append("human_resolution=%s"); params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""; limit=max(1,min(100,int(payload.get("limit") or 30))); offset=max(0,int(payload.get("offset") or 0))
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) AS count FROM ai_text_self_upgrade_items" + where, params).fetchone()["count"])
            rows = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items" + where +
                " ORDER BY CASE WHEN human_resolution IS NULL AND human_decision='pending' THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            confirmed = int(conn.execute(
                "SELECT COUNT(*) AS count FROM ai_text_self_upgrade_items WHERE human_resolution IN ('reviewer_correct','writer_correct')"
            ).fetchone()["count"])
        runs=self.recent_runs(limit=1,since=self.enabled_at())
        return {"items":[_item(row) for row in rows],"total":total,"offset":offset,"limit":limit,"confirmed_count":confirmed,"latest_run":runs[0] if runs else None}

    def detail(self, case_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM ai_text_self_upgrade_items WHERE case_key=%s",(case_key,)).fetchone()
            if row is None: raise ValueError("自我审核案例不存在")
            result=_item(row); result["decisions"]=[dict(item) for item in conn.execute("SELECT decision,actor,note,decided_at,1 AS legacy FROM ai_text_self_upgrade_decisions WHERE case_key=%s UNION ALL SELECT decision,actor,note,decided_at,0 AS legacy FROM ai_text_self_upgrade_resolutions WHERE case_key=%s ORDER BY decided_at",(case_key,case_key)).fetchall()]; return result

    def decide(self, case_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"reviewer_correct","writer_correct","ignored"}: raise ValueError("不支持的人工决定")
        with self.connect() as conn:
            with conn.transaction():
                if not conn.execute("SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=%s FOR UPDATE",(case_key,)).fetchone(): raise ValueError("自我审核案例不存在")
                conn.execute("UPDATE ai_text_self_upgrade_items SET human_resolution=%s,decision_by=%s,decision_note=%s,decided_at=clock_timestamp(),updated_at=clock_timestamp() WHERE case_key=%s",(decision,actor[:200],note[:2000],case_key)); conn.execute("INSERT INTO ai_text_self_upgrade_resolutions(case_key,decision,actor,note,decided_at) VALUES(%s,%s,%s,%s,clock_timestamp())",(case_key,decision,actor[:200],note[:2000]))
        return self.detail(case_key)


__all__ = ["PostgresAITextSelfUpgradeRepository"]
