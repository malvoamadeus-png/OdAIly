from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime, timedelta
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
DELETED_CASES_SQL = "CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_deleted_cases(case_key text PRIMARY KEY,deleted_by text NOT NULL,deleted_at timestamptz NOT NULL DEFAULT clock_timestamp())"
DELETED_CASES_CHECKSUM = hashlib.sha256(DELETED_CASES_SQL.encode()).hexdigest()
MANUAL_REJECTIONS_SQL = """CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_manual_rejections (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 auditor_check_id bigint NOT NULL UNIQUE,
 actor text NOT NULL,
 status text NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','completed','deleted')),
 attempt_count integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 case_key text,
 last_error text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_ai_text_upgrade_manual_rejections_due
 ON ai_text_self_upgrade_manual_rejections(status,next_attempt_at,id)"""
MANUAL_REJECTIONS_CHECKSUM = hashlib.sha256(MANUAL_REJECTIONS_SQL.encode()).hexdigest()
NULLABLE_TASK_ID_SQL = "ALTER TABLE ai_text_self_upgrade_items ALTER COLUMN task_id DROP NOT NULL"
NULLABLE_TASK_ID_CHECKSUM = hashlib.sha256(NULLABLE_TASK_ID_SQL.encode()).hexdigest()


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
            apply_schema_migrations(conn, schema=schema, component="ai_text_upgrade", migrations=(
                PostgresMigration(1, "AI text self-upgrade", CHECKSUM, install),
                PostgresMigration(2, "AI text self-upgrade deletion tombstones", DELETED_CASES_CHECKSUM, lambda connection: connection.execute(DELETED_CASES_SQL)),
                PostgresMigration(3, "AI text self-upgrade manual rejection queue", MANUAL_REJECTIONS_CHECKSUM, lambda connection: [connection.execute(statement) for statement in MANUAL_REJECTIONS_SQL.split(";") if statement.strip()]),
                PostgresMigration(4, "AI text self-upgrade independent manual snapshots", NULLABLE_TASK_ID_CHECKSUM, lambda connection: connection.execute(NULLABLE_TASK_ID_SQL)),
            ))

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

    def recover_manual_rejections(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute("UPDATE ai_text_self_upgrade_manual_rejections SET status='pending',next_attempt_at=clock_timestamp(),updated_at=clock_timestamp() WHERE status='processing'")
            return int(cursor.rowcount)

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
        values = (case["case_key"],case.get("source_type","fact_auditor"),case["auditor_check_id"],case.get("fact_check_id"),case.get("fact_policy_version"),case.get("fact_model"),case.get("fact_status"),case.get("fact_reason") or "",case.get("fact_summary") or "",_json(case.get("fact_result") or {}),case.get("fact_original_text") or "",case.get("fact_original_url") or "",case.get("fact_completed_at"),case["source_item_id"],case["content_hash"],case.get("audit_prompt_version"),case.get("audit_model"),case.get("audited_title"),case["audited_content"],_json(case.get("audit_result") or {}),case.get("published_at"),case.get("source_url"),case.get("source_title"),case.get("source_content") or "",case.get("task_id"),case.get("writer_model"),case.get("prompt_template_key"),case.get("prompt_version_id"),case.get("draft_title"),case.get("draft_content"),case.get("final_title"),case.get("final_content"))
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("ai-text-upgrade-case:" + str(case["case_key"]),))
                if conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=%s FOR KEY SHARE", (case["case_key"],)).fetchone():
                    return False
                return bool(conn.execute(f"INSERT INTO ai_text_self_upgrade_items({columns}) VALUES({','.join(['%s']*len(values))}) ON CONFLICT(case_key) DO NOTHING", values).rowcount)

    def queue_manual_rejection(self, auditor_check_id: int, *, actor: str) -> str | None:
        """Persist a manual rejection for background exact-link resolution."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO ai_text_self_upgrade_manual_rejections(auditor_check_id,actor)
                   VALUES(%s,%s) ON CONFLICT(auditor_check_id) DO UPDATE SET actor=excluded.actor,
                   status=CASE WHEN ai_text_self_upgrade_manual_rejections.status IN ('completed','deleted')
                               THEN ai_text_self_upgrade_manual_rejections.status ELSE 'pending' END,
                   next_attempt_at=CASE WHEN ai_text_self_upgrade_manual_rejections.status IN ('completed','deleted')
                                        THEN ai_text_self_upgrade_manual_rejections.next_attempt_at ELSE clock_timestamp() END,
                   updated_at=clock_timestamp()""",
                (auditor_check_id, actor[:200]),
            )
        return f"manual-rejection:{auditor_check_id}"

    def manual_rejection_status(self, auditor_check_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM ai_text_self_upgrade_manual_rejections WHERE auditor_check_id=%s", (auditor_check_id,)).fetchone()
            return str(row["status"]) if row else None

    def process_manual_rejection_queue(self, *, limit: int = 10) -> dict[str, int]:
        with self.connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    """SELECT * FROM ai_text_self_upgrade_manual_rejections WHERE status='pending'
                       AND next_attempt_at<=clock_timestamp() ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED""",
                    (max(1, min(int(limit), 100)),),
                ).fetchall()
                for row in rows:
                    conn.execute("UPDATE ai_text_self_upgrade_manual_rejections SET status='processing',attempt_count=attempt_count+1,updated_at=clock_timestamp() WHERE id=%s", (row["id"],))
        result = {"processed": 0, "completed": 0, "retrying": 0, "deleted": 0}
        for row in rows:
            result["processed"] += 1
            try:
                case_key = self._create_manual_rejection_case(int(row["auditor_check_id"]), actor=str(row["actor"]))
                if case_key is None:
                    status, error = "pending", "精确发布关联尚未就绪"
                    result["retrying"] += 1
                elif case_key == "__deleted__":
                    status, error = "deleted", None
                    result["deleted"] += 1
                else:
                    status, error = "completed", None
                    result["completed"] += 1
                attempts = int(row["attempt_count"]) + 1
                delay = min(6 * 60 * 60, 30 * (2 ** min(attempts - 1, 10)))
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE ai_text_self_upgrade_manual_rejections SET status=%s,case_key=%s,last_error=%s,next_attempt_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp() WHERE id=%s",
                        (status, None if case_key in (None, "__deleted__") else case_key, error, delay, row["id"]),
                    )
            except Exception as exc:
                result["retrying"] += 1
                attempts = int(row["attempt_count"]) + 1
                delay = min(6 * 60 * 60, 30 * (2 ** min(attempts - 1, 10)))
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE ai_text_self_upgrade_manual_rejections SET status='pending',last_error=%s,next_attempt_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp() WHERE id=%s",
                        (f"{type(exc).__name__}: {exc}"[:2000], delay, row["id"]),
                    )
        return result

    def _create_manual_rejection_case(self, auditor_check_id: int, *, actor: str) -> str | None:
        """Snapshot a rejection only when its published ID maps to one exact Writer1 version."""
        from packages.auditor.repository import calculate_content_hash

        with connect_postgres(schema="main", application_name="odaily-ai-text-upgrade-manual-rejection") as source:
            with source.transaction():
                row = source.execute("SELECT * FROM auditor_checks WHERE id=%s FOR SHARE", (auditor_check_id,)).fetchone()
                if row is None:
                    return "__deleted__"
                if row["status"] != "flagged":
                    return "__deleted__"
                expected_hash = calculate_content_hash(row["title"], str(row["content"] or ""))
                hash_matches = str(row["content_hash"] or "") == expected_hash
                source_item_id = str(row["source_item_id"] or f"auditor-check:{auditor_check_id}")
                rejected_case_key = hashlib.sha256(f"{source_item_id}\n{expected_hash}".encode()).hexdigest()
                with self.connect() as review_conn:
                    if review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=%s", (rejected_case_key,)).fetchone():
                        return "__deleted__"
                metadata_columns = {str(item["column_name"]) for item in source.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name='newsflash_production_metadata'"
                ).fetchall()}
                publication_rows = []
                if "odaily_id" in metadata_columns:
                    publication_rows = source.execute(
                        "SELECT * FROM newsflash_production_metadata WHERE odaily_id=%s",
                        (str(row["source_item_id"]),),
                    ).fetchall()
                explicit_categories = {
                    str(item["outlet_category"] or "") for item in publication_rows
                    if "outlet_category" in item.keys() and item["outlet_category"]
                }
                explicit_non_manual = bool(explicit_categories - {"manual"})
                ambiguous_writer1_metadata = any(
                    ("id_match_status" in item.keys() and item["id_match_status"] == "ambiguous")
                    or ("delivery_key" in item.keys() and str(item["delivery_key"] or "").startswith("odaily-task-"))
                    for item in publication_rows
                )
                manual_rows = [item for item in publication_rows
                               if item["outlet_category"] == "manual"] if "outlet_category" in metadata_columns else []
                manual_origin = any(
                    "origin_key" in item.keys() and item["origin_key"] == "manual"
                    for item in publication_rows
                )
                publisher_kind = None
                fact_columns = {str(item["column_name"]) for item in source.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema='main' AND table_name='newsflash_operation_facts'"
                ).fetchall()}
                if "publisher_kind" in fact_columns:
                    fact = source.execute(
                        "SELECT publisher_kind FROM newsflash_operation_facts WHERE source_item_id=%s",
                        (str(row["source_item_id"]),),
                    ).fetchone()
                    publisher_kind = str(fact["publisher_kind"] or "") if fact else None
                manual_publication = not explicit_non_manual and (
                    bool(manual_rows) or manual_origin or publisher_kind in {"human", "human_unmapped"}
                )
                unknown_authoring_basis = (
                    "explicit_non_manual_metadata" if explicit_non_manual
                    else "ambiguous_writer1_mapping" if ambiguous_writer1_metadata and not manual_publication
                    else None
                )
                task = None
                task_tables = source.execute(
                    "SELECT to_regclass('main.x_task_pipeline') AS pipeline,to_regclass('main.tasks') AS tasks"
                ).fetchone()
                writer_columns = {str(item["table_name"]): set() for item in source.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_name IN ('x_task_pipeline','tasks')"
                ).fetchall()}
                for item in source.execute(
                    "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='main' AND table_name IN ('x_task_pipeline','tasks')"
                ).fetchall():
                    writer_columns[str(item["table_name"])].add(str(item["column_name"]))
                if manual_publication:
                    metadata = manual_rows[0] if manual_rows else next(
                        (item for item in publication_rows if "title" in item.keys()), None
                    )
                    task = {
                        "task_id": None, "writer_model": None, "prompt_template_key": None, "prompt_version_id": None,
                        "draft_title": None, "draft_content": None, "final_title": str(row["title"] or ""),
                        "final_content": str(row["content"] or ""), "source_url": row["source_url"],
                        "source_title": str(metadata["title"] or "") if metadata is not None and "title" in metadata.keys() else "", "source_content": "",
                        "authoring_path": "manual_publication",
                    }
                elif (hash_matches and {"delivery_key", "odaily_id", "id_match_status"} <= metadata_columns
                      and task_tables["pipeline"] is not None and task_tables["tasks"] is not None
                      and {"task_id", "writer_model", "prompt_template_key", "prompt_version_id", "draft_title", "draft_content", "final_title", "final_content", "write_completed_at", "publish_completed_at"} <= writer_columns.get("x_task_pipeline", set())
                      and {"id", "source_url", "title", "content"} <= writer_columns.get("tasks", set())):
                    task_rows = source.execute(
                    """SELECT p.task_id,p.writer_model,p.prompt_template_key,p.prompt_version_id,
                              p.draft_title,p.draft_content,p.final_title,p.final_content,
                              t.source_url,t.title AS source_title,t.content AS source_content
                       FROM newsflash_production_metadata m
                       JOIN x_task_pipeline p ON m.delivery_key='odaily-task-' || p.task_id::text
                       JOIN tasks t ON t.id=p.task_id
                       WHERE m.odaily_id=%s AND m.id_match_status='matched'
                         AND p.writer_model IS NOT NULL AND p.write_completed_at IS NOT NULL
                         AND p.publish_completed_at IS NOT NULL""",
                        (str(row["source_item_id"]),),
                    ).fetchall()
                    if len(task_rows) == 1:
                        candidate = task_rows[0]
                        if ((candidate["draft_title"] or candidate["draft_content"])
                                and calculate_content_hash(candidate["final_title"], str(candidate["final_content"] or "")) == expected_hash):
                            task = candidate
                if task is None:
                    task = {
                        "task_id": None, "writer_model": None, "prompt_template_key": None, "prompt_version_id": None,
                        "draft_title": None, "draft_content": None, "final_title": str(row["title"] or ""),
                        "final_content": str(row["content"] or ""), "source_url": row["source_url"],
                        "source_title": "", "source_content": "",
                        "authoring_path": "unknown_publication" if unknown_authoring_basis else "manual_publication",
                    }
                audit_result = _decode(row["audit_result"], {})
                if not isinstance(audit_result, dict):
                    audit_result = {}
                authoring_path = task["authoring_path"] if "authoring_path" in task.keys() else None
                audit_result["manual_rejection"] = {
                    "actor": actor[:200], "rejected_at": datetime.now(UTC).isoformat(),
                    **({"authoring_path": authoring_path, "writer1_snapshot": "unavailable"}
                       if authoring_path in {"manual_publication", "unknown_publication"} else {}),
                    **({"authoring_basis": unknown_authoring_basis} if unknown_authoring_basis else {}),
                    **({"audit_snapshot_integrity": "content_hash_mismatch"} if not hash_matches else {}),
                }
                key = rejected_case_key
                case = {
                    "case_key": key, "source_type": "text_auditor", "auditor_check_id": auditor_check_id,
                    "source_item_id": source_item_id, "content_hash": expected_hash,
                    "audit_prompt_version": row["prompt_version"], "audit_model": row["model"],
                    "audited_title": row["title"], "audited_content": str(row["content"] or ""),
                    "audit_result": audit_result, "published_at": row["published_at"],
                    "source_url": task["source_url"] or row["source_url"],
                    "source_title": task["source_title"],
                    "source_content": str(task["source_content"] or ""), "task_id": int(task["task_id"]) if task["task_id"] is not None else None,
                    "writer_model": task["writer_model"],
                    "prompt_template_key": task["prompt_template_key"],
                    "prompt_version_id": task["prompt_version_id"],
                    "draft_title": task["draft_title"], "draft_content": task["draft_content"],
                    "final_title": task["final_title"], "final_content": task["final_content"],
                }
                inserted = self.insert_case(case)
                if not inserted:
                    with self.connect() as review_conn:
                        tombstone = review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=%s", (key,)).fetchone()
                        if tombstone:
                            return "__deleted__"
                        review_conn.execute(
                            "UPDATE ai_text_self_upgrade_items SET audit_result_json=jsonb_set(audit_result_json,'{manual_rejection}',%s::jsonb,true),updated_at=clock_timestamp() WHERE case_key=%s",
                            (_json(audit_result["manual_rejection"]), key),
                        )
                        exists = review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=%s", (key,)).fetchone()
                    if not exists:
                        return None
                return key

    def has_run(self, run_key: str) -> bool:
        with self.connect() as conn: return conn.execute("SELECT 1 FROM ai_text_self_upgrade_runs WHERE run_key=%s", (run_key,)).fetchone() is not None
    def run_status(self, run_key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM ai_text_self_upgrade_runs WHERE run_key=%s", (run_key,)).fetchone()
            return str(row["status"]) if row else None
    def oldest_unfinished_storage_retention_run(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_key FROM ai_text_self_upgrade_runs WHERE run_key LIKE 'storage_retention:%' AND status IN ('running','failed') ORDER BY started_at,run_key LIMIT 1"
            ).fetchone()
            return str(row["run_key"]) if row else None
    def start_run(self, run_key: str) -> bool:
        with self.connect() as conn: return bool(conn.execute("INSERT INTO ai_text_self_upgrade_runs(run_key,status,started_at) VALUES(%s,'running',clock_timestamp()) ON CONFLICT DO NOTHING", (run_key,)).rowcount)
    def retry_failed_run(self, run_key: str, *, retry_after_seconds: int = 300) -> bool:
        with self.connect() as conn:
            return bool(conn.execute(
                "UPDATE ai_text_self_upgrade_runs SET status='running',started_at=clock_timestamp(),completed_at=NULL,error=NULL WHERE run_key=%s AND status='failed' AND completed_at<=clock_timestamp()-(%s * interval '1 second')",
                (run_key, max(0, int(retry_after_seconds))),
            ).rowcount)
    def recover_storage_retention_runs(self) -> int:
        with self.connect() as conn:
            return int(conn.execute(
                "UPDATE ai_text_self_upgrade_runs SET status='failed',completed_at=clock_timestamp(),error=COALESCE(error,'worker restarted during storage retention') WHERE status='running' AND run_key LIKE 'storage_retention:%'"
            ).rowcount)
    def finish_run(self, run_key: str, *, status: str, cases_found: int = 0, cases_completed: int = 0, cases_failed: int = 0, cases_unlinked: int = 0, error: str | None = None) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_text_self_upgrade_runs SET status=%s,completed_at=CASE WHEN %s='running' THEN NULL ELSE clock_timestamp() END,cases_found=%s,cases_completed=%s,cases_failed=%s,cases_unlinked=%s,error=%s WHERE run_key=%s", (status,status,cases_found,cases_completed,cases_failed,cases_unlinked,error[:2000] if error else None,run_key))
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
        status = str(payload.get("status") or "pending")
        scope = str(payload.get("scope") or "all")
        if scope not in {"all", "manual_recent"}: raise ValueError("不支持的案例范围")
        scope_clauses: list[str] = []; scope_params: list[Any] = []
        if scope == "manual_recent":
            scope_clauses = ["source_type='text_auditor'",
                "audit_result_json->'manual_rejection' IS NOT NULL",
                "audit_result_json->'manual_rejection' NOT IN ('null'::jsonb,'false'::jsonb,'0'::jsonb,'\"\"'::jsonb)",
                "COALESCE(audit_result_json->'manual_rejection'->>'authoring_basis','') NOT IN ('explicit_non_manual_metadata','ambiguous_writer1_mapping')",
                "COALESCE(published_at,created_at)>=%s"]
            scope_params = [datetime.now(UTC) - timedelta(hours=72)]
        clauses = list(scope_clauses); params: list[Any] = list(scope_params)
        if status != "all":
            if status not in {"pending","reviewer_correct","writer_correct","ignored","legacy","confirmed"}: raise ValueError("不支持的案例状态")
            if status == "confirmed": clauses.append("human_resolution IN ('reviewer_correct','writer_correct')")
            elif status == "legacy": clauses.append("human_resolution IS NULL AND human_decision IN ('accepted','rejected','ignored')")
            elif status == "pending": clauses.append("human_resolution IS NULL AND human_decision='pending'")
            else: clauses.append("human_resolution=%s"); params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""; limit=max(1,min(100,int(payload.get("limit") or 30))); offset=max(0,int(payload.get("offset") or 0))
        columns = "*"
        if scope == "manual_recent":
            columns = ("id,case_key,source_type,audited_title,published_at,created_at,analysis_status,verdict,"
                       "human_decision,human_resolution,"
                       "jsonb_build_object('summary',audit_result_json->'summary','manual_rejection',jsonb_build_object("
                       "'authoring_path',audit_result_json->'manual_rejection'->'authoring_path',"
                       "'authoring_basis',audit_result_json->'manual_rejection'->'authoring_basis',"
                       "'writer1_snapshot',audit_result_json->'manual_rejection'->'writer1_snapshot')) AS audit_result_json,"
                       "'{}'::jsonb AS fact_result_json,'[]'::jsonb AS evidence_json")
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) AS count FROM ai_text_self_upgrade_items" + where, params).fetchone()["count"])
            rows = conn.execute(
                "SELECT " + columns + " FROM ai_text_self_upgrade_items" + where +
                " ORDER BY CASE WHEN human_resolution IS NULL AND human_decision='pending' THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            confirmed_where = " AND ".join([*scope_clauses, "human_resolution IN ('reviewer_correct','writer_correct')"])
            confirmed = int(conn.execute(
                "SELECT COUNT(*) AS count FROM ai_text_self_upgrade_items WHERE " + confirmed_where, scope_params
            ).fetchone()["count"])
        runs=self.recent_runs(limit=1,since=self.enabled_at())
        return {"items":[_item(row) for row in rows],"total":total,"offset":offset,"limit":limit,"confirmed_count":confirmed,"latest_run":runs[0] if runs else None}

    def detail(self, case_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row=conn.execute("SELECT * FROM ai_text_self_upgrade_items WHERE case_key=%s",(case_key,)).fetchone()
            if row is None: raise ValueError("自我审核案例不存在")
            result=_item(row); result["decisions"]=[dict(item) for item in conn.execute("SELECT decision,actor,note,decided_at,1 AS legacy FROM ai_text_self_upgrade_decisions WHERE case_key=%s UNION ALL SELECT decision,actor,note,decided_at,0 AS legacy FROM ai_text_self_upgrade_resolutions WHERE case_key=%s ORDER BY decided_at",(case_key,case_key)).fetchall()]; return result

    def delete_case(self, case_key: str, *, actor: str) -> bool:
        """Delete the snapshot and leave a tombstone atomically against discovery."""
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("ai-text-upgrade-case:" + case_key,))
                conn.execute("INSERT INTO ai_text_self_upgrade_deleted_cases(case_key,deleted_by) VALUES(%s,%s) ON CONFLICT(case_key) DO NOTHING", (case_key, actor[:200]))
                conn.execute("DELETE FROM ai_text_self_upgrade_decisions WHERE case_key=%s", (case_key,))
                conn.execute("DELETE FROM ai_text_self_upgrade_resolutions WHERE case_key=%s", (case_key,))
                deleted = conn.execute("DELETE FROM ai_text_self_upgrade_items WHERE case_key=%s", (case_key,)).rowcount
                return bool(deleted)

    def decide(self, case_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"reviewer_correct","writer_correct","ignored"}: raise ValueError("不支持的人工决定")
        with self.connect() as conn:
            with conn.transaction():
                if not conn.execute("SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=%s FOR UPDATE",(case_key,)).fetchone(): raise ValueError("自我审核案例不存在")
                conn.execute("UPDATE ai_text_self_upgrade_items SET human_resolution=%s,decision_by=%s,decision_note=%s,decided_at=clock_timestamp(),updated_at=clock_timestamp() WHERE case_key=%s",(decision,actor[:200],note[:2000],case_key)); conn.execute("INSERT INTO ai_text_self_upgrade_resolutions(case_key,decision,actor,note,decided_at) VALUES(%s,%s,%s,%s,clock_timestamp())",(case_key,decision,actor[:200],note[:2000]))
        return self.detail(case_key)


__all__ = ["PostgresAITextSelfUpgradeRepository"]
