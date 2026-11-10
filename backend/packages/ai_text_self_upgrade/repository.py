from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL DEFAULT 'text_auditor',
    auditor_check_id INTEGER NOT NULL,
    fact_check_id INTEGER,
    fact_policy_version TEXT,
    fact_model TEXT,
    fact_status TEXT,
    fact_reason TEXT NOT NULL DEFAULT '',
    fact_summary TEXT NOT NULL DEFAULT '',
    fact_result_json TEXT NOT NULL DEFAULT '{}',
    fact_original_text TEXT NOT NULL DEFAULT '',
    fact_original_url TEXT NOT NULL DEFAULT '',
    fact_completed_at TEXT,
    source_item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    audit_prompt_version TEXT,
    audit_model TEXT,
    audited_title TEXT,
    audited_content TEXT NOT NULL,
    audit_result_json TEXT NOT NULL DEFAULT '{}',
    published_at TEXT,
    source_url TEXT,
    source_title TEXT,
    source_content TEXT NOT NULL,
    task_id INTEGER NOT NULL,
    writer_model TEXT,
    prompt_template_key TEXT,
    prompt_version_id INTEGER,
    draft_title TEXT,
    draft_content TEXT,
    final_title TEXT,
    final_content TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending' CHECK(analysis_status IN ('pending','processing','completed','failed')),
    analysis_model TEXT,
    verdict TEXT CHECK(verdict IS NULL OR verdict IN ('writer1_error','auditor_false_positive')),
    confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low','medium','high')),
    rationale TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    analysis_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    human_decision TEXT NOT NULL DEFAULT 'pending' CHECK(human_decision IN ('pending','accepted','rejected','ignored')),
    decision_by TEXT,
    decision_note TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_queue
ON ai_text_self_upgrade_items(analysis_status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_decisions
ON ai_text_self_upgrade_items(human_decision,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL REFERENCES ai_text_self_upgrade_items(case_key),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected','ignored')),
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_decision_history
ON ai_text_self_upgrade_decisions(case_key,id DESC);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_runs (
    run_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    cases_found INTEGER NOT NULL DEFAULT 0,
    cases_completed INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0,
    cases_unlinked INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, json.JSONDecodeError):
        return default


def _item(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for source, target, default in (
        ("audit_result_json", "audit_result", {}),
        ("fact_result_json", "fact_result", {}),
        ("evidence_json", "evidence", []),
    ):
        result[target] = _decode(result.pop(source), default)
    if result.get("source_type") == "fact_auditor":
        if result.get("verdict") == "auditor_false_positive":
            result["verdict"] = "fact_auditor_error"
        elif result.get("verdict") is None and result.get("analysis_status") == "completed":
            result["verdict"] = "inconclusive"
    return result


class AITextSelfUpgradeRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ai_text_self_upgrade_items)")}
            migrations = {
                "source_type": "TEXT NOT NULL DEFAULT 'text_auditor'",
                "fact_check_id": "INTEGER",
                "fact_policy_version": "TEXT",
                "fact_model": "TEXT",
                "fact_status": "TEXT",
                "fact_reason": "TEXT NOT NULL DEFAULT ''",
                "fact_summary": "TEXT NOT NULL DEFAULT ''",
                "fact_result_json": "TEXT NOT NULL DEFAULT '{}'",
                "fact_original_text": "TEXT NOT NULL DEFAULT ''",
                "fact_original_url": "TEXT NOT NULL DEFAULT ''",
                "fact_completed_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE ai_text_self_upgrade_items ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_source_queue "
                "ON ai_text_self_upgrade_items(source_type,analysis_status,created_at,id)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO ai_text_self_upgrade_state(state_key,state_value,updated_at) VALUES('fact_enabled_at',?,?)",
                (_now(), _now()),
            )
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=COALESCE(analysis_error,'服务重启时原分析未完成'),updated_at=? WHERE analysis_status='processing'",
                (_now(),),
            )
            conn.execute(
                "UPDATE ai_text_self_upgrade_runs SET status='failed',completed_at=?,error=COALESCE(error,'服务重启时原批次未完成') WHERE status='running'",
                (_now(),),
            )

    def fact_enabled_at(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='fact_enabled_at'"
            ).fetchone()
        if row is None:
            raise RuntimeError("事实审核自我辩论启用时间未初始化")
        return str(row["state_value"])

    def case_exists(self, case_key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=?", (case_key,)
            ).fetchone() is not None

    def insert_case(self, case: dict[str, Any]) -> bool:
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO ai_text_self_upgrade_items(
                    case_key,source_type,auditor_check_id,fact_check_id,fact_policy_version,fact_model,fact_status,
                    fact_reason,fact_summary,fact_result_json,fact_original_text,fact_original_url,fact_completed_at,
                    source_item_id,content_hash,audit_prompt_version,audit_model,
                    audited_title,audited_content,audit_result_json,published_at,source_url,source_title,
                    source_content,task_id,writer_model,prompt_template_key,prompt_version_id,draft_title,
                    draft_content,final_title,final_content,created_at,updated_at
                ) VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                (
                    case["case_key"], case.get("source_type", "fact_auditor"), case["auditor_check_id"],
                    case.get("fact_check_id"), case.get("fact_policy_version"), case.get("fact_model"),
                    case.get("fact_status"), case.get("fact_reason") or "", case.get("fact_summary") or "",
                    _json(case.get("fact_result") or {}), case.get("fact_original_text") or "",
                    case.get("fact_original_url") or "", case.get("fact_completed_at"),
                    case["source_item_id"], case["content_hash"],
                    case.get("audit_prompt_version"), case.get("audit_model"), case.get("audited_title"),
                    case["audited_content"], _json(case.get("audit_result") or {}), case.get("published_at"),
                    case.get("source_url"), case.get("source_title"), case["source_content"], case["task_id"],
                    case.get("writer_model"), case.get("prompt_template_key"), case.get("prompt_version_id"),
                    case.get("draft_title"), case.get("draft_content"), case.get("final_title"),
                    case.get("final_content"), now, now,
                ),
            )
            return cursor.rowcount == 1

    def has_run(self, run_key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_runs WHERE run_key=?", (run_key,)
            ).fetchone() is not None

    def start_run(self, run_key: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO ai_text_self_upgrade_runs(run_key,status,started_at) VALUES(?,'running',?)",
                (run_key, _now()),
            )
            return cursor.rowcount == 1

    def finish_run(
        self,
        run_key: str,
        *,
        status: str,
        cases_found: int = 0,
        cases_completed: int = 0,
        cases_failed: int = 0,
        cases_unlinked: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE ai_text_self_upgrade_runs SET status=?,completed_at=?,cases_found=?,cases_completed=?,
                    cases_failed=?,cases_unlinked=?,error=? WHERE run_key=?""",
                (status, _now(), cases_found, cases_completed, cases_failed, cases_unlinked,
                 error[:2000] if error else None, run_key),
            )

    def recent_runs(self, limit: int = 12, *, since: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM ai_text_self_upgrade_runs WHERE (? IS NULL OR julianday(started_at)>=julianday(?)) "
                "ORDER BY started_at DESC LIMIT ?",
                (since, since, max(1, min(50, limit))),
            ).fetchall()]

    def claim_next(self, *, excluded_case_keys: set[str] | None = None) -> dict[str, Any] | None:
        excluded = sorted(excluded_case_keys or ())
        exclusion_sql = f" AND case_key NOT IN ({','.join('?' for _ in excluded)})" if excluded else ""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE source_type='fact_auditor' "
                "AND analysis_status IN ('pending','failed') "
                + exclusion_sql + " ORDER BY created_at,id LIMIT 1",
                excluded,
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET analysis_status='processing',attempt_count=attempt_count+1,analysis_error=NULL,updated_at=? WHERE case_key=?",
                (_now(), row["case_key"]),
            )
            claimed = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE case_key=?", (row["case_key"],)
            ).fetchone()
            conn.commit()
        return _item(claimed)

    def complete_analysis(self, case_key: str, *, model: str, result: dict[str, Any]) -> None:
        stored_verdict = {
            "fact_auditor_error": "auditor_false_positive",
            "inconclusive": None,
        }.get(result["verdict"], result["verdict"])
        with self.connect() as conn:
            conn.execute(
                """UPDATE ai_text_self_upgrade_items SET analysis_status='completed',analysis_model=?,verdict=?,
                    confidence=?,rationale=?,evidence_json=?,analysis_error=NULL,updated_at=? WHERE case_key=?""",
                (model, stored_verdict, result["confidence"], result["rationale"], _json(result["evidence"]),
                 _now(), case_key),
            )

    def fail_analysis(self, case_key: str, *, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=?,updated_at=? WHERE case_key=?",
                (error[:2000], _now(), case_key),
            )

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "pending")
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            if status not in {"pending", "accepted", "rejected", "ignored"}:
                raise ValueError("不支持的案例状态")
            clauses.append("human_decision=?")
            params.append(status)
        clauses.insert(0, "source_type='fact_auditor'")
        where = " WHERE " + " AND ".join(clauses)
        limit = max(1, min(100, int(payload.get("limit") or 30)))
        offset = max(0, int(payload.get("offset") or 0))
        with self.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM ai_text_self_upgrade_items{where}", params
            ).fetchone()[0])
            rows = conn.execute(
                f"SELECT * FROM ai_text_self_upgrade_items{where} "
                "ORDER BY CASE WHEN human_decision='pending' THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            accepted_count = int(conn.execute(
                "SELECT COUNT(*) FROM ai_text_self_upgrade_items WHERE source_type='fact_auditor' AND human_decision='accepted'"
            ).fetchone()[0])
        runs = self.recent_runs(limit=1, since=self.fact_enabled_at())
        return {
            "items": [_item(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "accepted_count": accepted_count,
            "latest_run": runs[0] if runs else None,
        }

    def detail(self, case_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE case_key=? AND source_type='fact_auditor'", (case_key,)
            ).fetchone()
            if row is None:
                raise ValueError("自我审核案例不存在")
            result = _item(row)
            result["decisions"] = [dict(decision) for decision in conn.execute(
                "SELECT decision,actor,note,decided_at FROM ai_text_self_upgrade_decisions WHERE case_key=? ORDER BY id",
                (case_key,),
            ).fetchall()]
            return result

    def decide(self, case_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "ignored"}:
            raise ValueError("不支持的人工决定")
        stamp = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=? AND source_type='fact_auditor'", (case_key,)
            ).fetchone():
                raise ValueError("自我审核案例不存在")
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET human_decision=?,decision_by=?,decision_note=?,decided_at=?,updated_at=? WHERE case_key=?",
                (decision, actor[:200], note[:2000], stamp, stamp, case_key),
            )
            conn.execute(
                "INSERT INTO ai_text_self_upgrade_decisions(case_key,decision,actor,note,decided_at) VALUES(?,?,?,?,?)",
                (case_key, decision, actor[:200], note[:2000], stamp),
            )
        return self.detail(case_key)
