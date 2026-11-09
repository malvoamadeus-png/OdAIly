from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import requests
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly
from packages.x_processing.ai_client import extract_chat_completion_text, extract_response_text, parse_openai_response_payload
from .store import SQLITE_BUSY_TIMEOUT_MS, SystemInformationStore, timestamp_ms
from .error_policy import day_start_ms, expire_external_errors, external_error, failure_category

CURRENT_ERROR_SCOPE = "NOT (julianday(last_seen_at)<julianday('now','+8 hours','start of day','-8 hours') AND odaily_external_error(scope_json,evidence_json,actual))"


BEIJING = ZoneInfo("Asia/Shanghai")
DAILY_CALL_LIMIT = 100
DAILY_TOKEN_LIMIT = 1_000_000
MAX_ATTEMPTS = 3
FAILURE_HISTORY_DAYS = 7
PERSISTENT_FAILURE_MIN_DAYS = 3
MAX_PROCESSING_ATTEMPT_ROWS = 50_000
MAX_CAPTURE_ATTEMPT_ROWS = 20_000


def is_actionable_internal_failure(message: Any) -> bool:
    category = failure_category(message)
    if category == "local_storage":
        return True
    if category != "program_or_unknown":
        return False
    text = str(message or "").casefold()
    return any(term in text for term in (
        "writer output", "active prompt not found", "output validation", "schema validation",
        "unsupported", "invalid field", "invalid json", "missing required", "traceback",
        "attributeerror", "keyerror", "typeerror", "valueerror", "nameerror", "runtimeerror",
        "assertionerror", "operationalerror", "importerror", "modulenotfounderror", "jsondecodeerror",
    ))

REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS system_review_items (
    item_key TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    category TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected','deferred')),
    analysis_status TEXT NOT NULL DEFAULT 'queued' CHECK(analysis_status IN ('queued','completed','failed','budget_paused','evidence_insufficient')),
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'unknown',
    scope_json TEXT NOT NULL DEFAULT '{}',
    expected TEXT NOT NULL DEFAULT '',
    actual TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    counter_evidence_json TEXT NOT NULL DEFAULT '[]',
    certainty TEXT NOT NULL DEFAULT 'unknown',
    suggestion TEXT NOT NULL DEFAULT '',
    validation TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    recovered_at TEXT,
    affected_count INTEGER NOT NULL DEFAULT 1,
    analysis_attempts INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    analysis_error TEXT,
    last_reminded_at TEXT,
    decision_by TEXT,
    decision_note TEXT,
    evidence_hash TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_system_review_queue ON system_review_items(analysis_status,analysis_attempts,last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_review_status ON system_review_items(status,last_seen_at DESC);
CREATE TABLE IF NOT EXISTS system_review_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    note TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_review_decisions_item ON system_review_decisions(item_key,id DESC);
CREATE TABLE IF NOT EXISTS system_review_usage (
    usage_date TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_review_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_review_runs (
    run_key TEXT PRIMARY KEY,
    task_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    items_created INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS system_review_analysis_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_key TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    request_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    raw_response_ref TEXT
);
CREATE INDEX IF NOT EXISTS idx_system_review_analysis_attempts_item ON system_review_analysis_attempts(item_key,id DESC);
CREATE INDEX IF NOT EXISTS idx_system_review_analysis_response ON system_review_analysis_attempts(raw_response_ref);
CREATE TABLE IF NOT EXISTS system_review_channel_observations (
    component TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    last_bucket TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(component,worker_id)
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or "null"))
    except (TypeError, ValueError):
        return value


def _sample_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        key = value.get("sample_key")
        if key:
            found.append(str(key))
        for item in value.values():
            found.extend(_sample_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_sample_keys(item))
    return list(dict.fromkeys(found))


def _stream_item_keys(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        key = value.get("stream_item_id")
        if key:
            found.append(str(key))
        for item in value.values():
            found.extend(_stream_item_keys(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_stream_item_keys(item))
    return list(dict.fromkeys(found))


def _expand_sample_evidence(value: Any, details: dict[str, dict[str, Any]]) -> Any:
    if isinstance(value, dict):
        key = str(value.get("sample_key") or value.get("stream_item_id") or "")
        expanded = {name: _expand_sample_evidence(item, details) for name, item in value.items()}
        if key and key in details:
            return {**expanded, **details[key]}
        return expanded
    if isinstance(value, list):
        return [_expand_sample_evidence(item, details) for item in value]
    return value


def _without_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_timestamps(item)
            for key, item in value.items()
            if not str(key).casefold().endswith(("_at", "_ms", "_timestamp"))
        }
    if isinstance(value, list):
        return [_without_timestamps(item) for item in value]
    return value


class SystemReviewRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.information_store = SystemInformationStore(path)
        self.information_store.initialize()
        with self.connect() as conn:
            conn.executescript(REVIEW_SCHEMA)
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(system_review_items)")}
            if "evidence_hash" not in columns:
                conn.execute("ALTER TABLE system_review_items ADD COLUMN evidence_hash TEXT NOT NULL DEFAULT ''")

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000, factory=ClosingSQLiteConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.create_function('odaily_external_error', 3, lambda scope, evidence, actual: int(external_error({'scope': scope, 'evidence': evidence, 'status': 'failed', 'actual': actual})), deterministic=True)
        conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        return conn

    def upsert_issue(self, issue: dict[str, Any]) -> str:
        key = str(issue.get("item_key") or f"{issue['task_kind']}:{issue['category']}:{_digest(issue.get('identity') or issue['title'])[:24]}")
        now = _now()
        first_seen = str(issue.get("first_seen_at") or now)
        evidence_hash = _digest({
            "identity": issue.get("identity"),
            "scope": issue.get("scope"),
            "expected": issue.get("expected"),
            "evidence": _without_timestamps(issue.get("evidence") or []),
            "counter_evidence": _without_timestamps(issue.get("counter_evidence") or []),
            "affected_count": issue.get("affected_count"),
        })
        values = (
            key, issue["task_kind"], issue["category"], issue["title"][:300], issue.get("summary", "")[:2000],
            issue.get("severity", "unknown"), _json(issue.get("scope") or {}), issue.get("expected", "")[:2000],
            issue.get("actual", "")[:2000], _json(issue.get("evidence") or []),
            _json(issue.get("counter_evidence") or []), issue.get("certainty", "unknown"),
            issue.get("suggestion", "")[:2000], issue.get("validation", "")[:2000], int(issue.get("affected_count") or 1), evidence_hash,
        )
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO system_review_items(item_key,task_kind,category,title,summary,severity,scope_json,
                    expected,actual,evidence_json,counter_evidence_json,certainty,suggestion,validation,first_seen_at,last_seen_at,affected_count,evidence_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?)
                    ON CONFLICT(item_key) DO UPDATE SET
                        title=excluded.title,summary=excluded.summary,severity=excluded.severity,scope_json=excluded.scope_json,
                        expected=excluded.expected,actual=excluded.actual,evidence_json=excluded.evidence_json,
                        counter_evidence_json=excluded.counter_evidence_json,certainty=excluded.certainty,
                        suggestion=excluded.suggestion,validation=excluded.validation,last_seen_at=excluded.last_seen_at,
                        affected_count=excluded.affected_count,
                        first_seen_at=CASE WHEN system_review_items.recovered_at IS NOT NULL THEN excluded.first_seen_at ELSE system_review_items.first_seen_at END,
                        status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'pending' ELSE system_review_items.status END,
                        decision_by=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_by END,
                        decision_note=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_note END,
                        analysis_attempts=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash THEN 0 ELSE system_review_items.analysis_attempts END,
                        evidence_hash=excluded.evidence_hash,recovered_at=NULL,
                        analysis_status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'queued' ELSE system_review_items.analysis_status END""",
                (*values[:14], first_seen, now, values[14], values[15]),
            )
        return key

    def resolve_unseen(self, task_kind: str, active_keys: set[str]) -> None:
        with self.connect() as conn:
            rows = conn.execute("SELECT item_key FROM system_review_items WHERE task_kind=? AND recovered_at IS NULL", (task_kind,)).fetchall()
            for row in rows:
                key = str(row["item_key"])
                if key not in active_keys:
                    conn.execute("UPDATE system_review_items SET recovered_at=? WHERE item_key=?", (_now(), key))

    def pending_analysis(self, limit: int = 12) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM system_review_items WHERE {CURRENT_ERROR_SCOPE} AND (task_kind!='failure_recovery' OR (category='persistent_internal_failure' AND recovered_at IS NULL)) AND analysis_status IN ('queued','failed','budget_paused') AND analysis_attempts<? ORDER BY last_seen_at,item_key LIMIT ?",
                (MAX_ATTEMPTS, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def reserve(self, usage_date: str, token_estimate: int) -> bool:
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO system_review_usage(usage_date,updated_at) VALUES(?,?)", (usage_date, now))
            row = conn.execute("SELECT calls,input_tokens,reserved_tokens FROM system_review_usage WHERE usage_date=?", (usage_date,)).fetchone()
            if int(row["calls"]) >= DAILY_CALL_LIMIT or int(row["input_tokens"]) + int(row["reserved_tokens"]) + token_estimate > DAILY_TOKEN_LIMIT:
                return False
            conn.execute("UPDATE system_review_usage SET calls=calls+1,reserved_tokens=reserved_tokens+?,updated_at=? WHERE usage_date=?", (token_estimate, now, usage_date))
        return True

    def settle(self, usage_date: str, reserved: int, input_tokens: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE system_review_usage SET reserved_tokens=max(0,reserved_tokens-?),input_tokens=input_tokens+?,updated_at=? WHERE usage_date=?", (reserved, input_tokens, _now(), usage_date))

    def release(self, usage_date: str, reserved: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE system_review_usage SET reserved_tokens=max(0,reserved_tokens-?),updated_at=? WHERE usage_date=?", (reserved, _now(), usage_date))

    def complete_analysis(self, item_key: str, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_review_items SET analysis_status='completed',summary=?,severity=?,certainty=?,suggestion=?,validation=?,input_tokens=?,output_tokens=?,analysis_attempts=analysis_attempts+1,analysis_error=NULL WHERE item_key=?",
                (result["summary"][:2000], result["severity"], result["certainty"], result["suggestion"][:2000], result["validation"][:2000], result["input_tokens"], result["output_tokens"], item_key),
            )

    def fail_analysis(self, item_key: str, error: str, *, budget_paused: bool = False) -> None:
        with self.connect() as conn:
            if budget_paused:
                conn.execute("UPDATE system_review_items SET analysis_status='budget_paused',analysis_error=? WHERE item_key=?", (error[:1000], item_key))
            else:
                conn.execute("UPDATE system_review_items SET analysis_status='failed',analysis_attempts=analysis_attempts+1,analysis_error=? WHERE item_key=?", (error[:1000], item_key))

    def start_analysis_attempt(self, item_key: str) -> int:
        with self.connect() as conn:
            attempt_no = int(conn.execute("SELECT analysis_attempts FROM system_review_items WHERE item_key=?", (item_key,)).fetchone()[0]) + 1
            cursor = conn.execute(
                "INSERT INTO system_review_analysis_attempts(item_key,attempt_no,started_at,status) VALUES(?,?,?,'running')",
                (item_key, attempt_no, _now()),
            )
            return int(cursor.lastrowid)

    def record_analysis_response(self, attempt_id: int, raw_response: str, request_id: str | None, input_tokens: int) -> str:
        evidence_ref = self.information_store.save_evidence({"raw_model_response": raw_response})
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_review_analysis_attempts SET request_id=?,input_tokens=?,raw_response_ref=? WHERE id=?",
                (request_id[:300] if request_id else None, max(0, input_tokens), evidence_ref, attempt_id),
            )
        return evidence_ref

    def finish_analysis_attempt(self, attempt_id: int, *, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_review_analysis_attempts SET status=?,finished_at=?,error=? WHERE id=?",
                (status, _now(), error[:1000] if error else None, attempt_id),
            )

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "pending")
        task_kind = str(payload.get("task_kind") or "all")
        clauses: list[str] = [CURRENT_ERROR_SCOPE]
        params: list[Any] = []
        if status == "pending":
            clauses.append("(task_kind!='failure_recovery' OR (category='persistent_internal_failure' AND recovered_at IS NULL))")
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if task_kind != "all":
            clauses.append("task_kind=?")
            params.append(task_kind)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM system_review_items{where}", params).fetchone()[0])
            rows = conn.execute(f"SELECT * FROM system_review_items{where} ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,last_seen_at DESC LIMIT ? OFFSET ?", (*params, max(1, min(100, int(payload.get("limit") or 30))), max(0, int(payload.get("offset") or 0)))).fetchall()
            usage = conn.execute("SELECT * FROM system_review_usage WHERE usage_date=?", (datetime.now(BEIJING).date().isoformat(),)).fetchone()
        items = []
        for row in rows:
            item = dict(row)
            item["scope"] = json.loads(item.pop("scope_json") or "{}")
            item["evidence"] = json.loads(item.pop("evidence_json") or "[]")
            item["counter_evidence"] = json.loads(item.pop("counter_evidence_json") or "[]")
            items.append(item)
        return {"items": items, "total": total, "offset": max(0, int(payload.get("offset") or 0)), "limit": max(1, min(100, int(payload.get("limit") or 30))),
                "usage": dict(usage) if usage else {"calls": 0, "input_tokens": 0, "reserved_tokens": 0},
                "analyses_incomplete": [item["item_key"] for item in items if item["analysis_status"] not in {"completed", "evidence_insufficient"}]}

    def decide(self, item_key: str, decision: str, actor: str, note: str) -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "deferred"}:
            raise ValueError("不支持的人工决定")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT item_key FROM system_review_items WHERE item_key=?", (item_key,)).fetchone()
            if not row:
                raise ValueError("巡检问题不存在")
            now = _now()
            conn.execute("UPDATE system_review_items SET status=?,decision_by=?,decision_note=? WHERE item_key=?", (decision, actor, note[:2000], item_key))
            conn.execute("INSERT INTO system_review_decisions(item_key,decision,actor,note,decided_at) VALUES(?,?,?,?,?)", (item_key, decision, actor, note[:2000], now))
            return self.detail(item_key, conn=conn)

    def detail(self, item_key: str, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
        owned = conn is None
        connection = conn or self.connect()
        try:
            row = connection.execute("SELECT * FROM system_review_items WHERE item_key=?", (item_key,)).fetchone()
            if not row:
                raise ValueError("巡检问题不存在")
            result = dict(row)
            if (timestamp_ms(result.get('last_seen_at')) or 0) < day_start_ms() and external_error(result):
                raise ValueError('外部错误明细已超过当天留存期，请查看每日错误次数统计')
            for source, target in (("scope_json", "scope"), ("evidence_json", "evidence"), ("counter_evidence_json", "counter_evidence")):
                decoded = json.loads(result.pop(source) or "{}")
                result[target] = self.expand_sample_references(decoded) if target in {"evidence", "counter_evidence"} else decoded
            result["decisions"] = [dict(value) for value in connection.execute("SELECT decision,actor,note,decided_at FROM system_review_decisions WHERE item_key=? ORDER BY id", (item_key,)).fetchall()]
            result["analysis_history"] = []
            for attempt in connection.execute(
                "SELECT id,attempt_no,started_at,finished_at,status,request_id,input_tokens,error,raw_response_ref "
                "FROM system_review_analysis_attempts WHERE item_key=? ORDER BY id", (item_key,),
            ).fetchall():
                value = dict(attempt)
                response = self.information_store.load_evidence(value.pop("raw_response_ref"))
                value["raw_response"] = (
                    response.get("raw_model_response") or response.get("raw_model_response_excerpt")
                    if isinstance(response, dict) else None
                )
                result["analysis_history"].append(value)
            return result
        finally:
            if owned:
                connection.close()

    def expand_sample_references(self, value: Any) -> Any:
        sample_keys = _sample_keys(value)[:30]
        stream_item_ids = _stream_item_keys(value)[:30]
        details = {
            str(item["sample_key"]): item
            for item in self.information_store.decision_sample_details(sample_keys)
        }
        details.update({
            str(item["stream_item_id"]): item
            for item in self.information_store.stream_item_evidence_details(stream_item_ids)
        })
        return _expand_sample_evidence(value, details)

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT state_value FROM system_review_state WHERE state_key=?", (key,)).fetchone()
        return str(row["state_value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO system_review_state(state_key,state_value,updated_at) VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at", (key, value, _now()))

    def start_run(self, run_key: str, task_kind: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO system_review_runs(run_key,task_kind,status,started_at) VALUES(?,?,?,?) "
                "ON CONFLICT(run_key) DO UPDATE SET status='running',started_at=excluded.started_at,completed_at=NULL,error=NULL",
                (run_key, task_kind, "running", _now()),
            )

    def recover_interrupted_runs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE system_review_runs SET status='failed',completed_at=?, "
                "error=COALESCE(error,'系统巡检进程重启，原运行未完成') WHERE status='running'",
                (_now(),),
            )
            return int(cursor.rowcount)

    def finish_run(self, run_key: str, *, status: str, items_created: int = 0, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_review_runs SET status=?,completed_at=?,items_created=?,error=? WHERE run_key=?",
                (status, _now(), items_created, error[:1000] if error else None, run_key),
            )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [expire_external_errors(dict(row), timestamp_ms(row["completed_at"] or row["started_at"])) for row in conn.execute(
                "SELECT run_key,task_kind,status,started_at,completed_at,items_created,error "
                "FROM system_review_runs ORDER BY started_at DESC LIMIT ?", (max(1, min(50, limit)),)
            ).fetchall()]

    def record_channel_observation(self, component: str, worker_id: str, *, failed: bool, bucket: str) -> int:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT last_bucket,consecutive_failures FROM system_review_channel_observations WHERE component=? AND worker_id=?",
                (component, worker_id),
            ).fetchone()
            if row and row["last_bucket"] == bucket:
                return int(row["consecutive_failures"])
            previous_ms = timestamp_ms(row["last_bucket"]) if row else None
            current_ms = timestamp_ms(bucket)
            adjacent = previous_ms is not None and current_ms is not None and current_ms - previous_ms <= 5 * 60_000
            failures = int(row["consecutive_failures"]) + 1 if failed and adjacent else (1 if failed else 0)
            conn.execute(
                "INSERT INTO system_review_channel_observations(component,worker_id,last_bucket,consecutive_failures) VALUES(?,?,?,?) "
                "ON CONFLICT(component,worker_id) DO UPDATE SET last_bucket=excluded.last_bucket,consecutive_failures=excluded.consecutive_failures",
                (component, worker_id, bucket, failures),
            )
            return failures


class SystemReviewService:
    def __init__(self, primary_database_path: Path, repository: SystemReviewRepository, *, model_settings: Any = None, competitor_database_path: Path | None = None, evidence_database_path: Path | None = None, local_pipeline_database_path: Path | None = None) -> None:
        self.primary_database_path = primary_database_path
        self.model_settings = model_settings
        self.competitor_database_path = competitor_database_path
        self.evidence_database_path = evidence_database_path or primary_database_path
        self.local_pipeline_database_path = local_pipeline_database_path
        self.repository = repository
        self.information_store = SystemInformationStore(repository.path)
        self._worker_lock = threading.Lock()

    def run_deterministic_checks(self) -> dict[str, int]:
        active_by_kind: dict[str, set[str]] = {key: set() for key in ("channel_outage", "missed_coverage", "output_audit", "rule_conflict", "failure_recovery")}
        detectors = (
            ("channel_outage", self._channel_outages), ("missed_coverage", self._coverage_gaps),
            ("output_audit", self._audit_findings), ("rule_conflict", self._rule_conflicts),
            ("failure_recovery", self._failure_clusters),
        )
        counts: dict[str, int] = {}
        for task_kind, detector in detectors:
            try:
                issues = detector()
            except Exception as exc:
                issues = [{"task_kind": task_kind, "category": "inspection_failed", "title": f"{task_kind}巡检执行失败",
                           "summary": f"{type(exc).__name__}: {exc}", "severity": "unknown", "certainty": "high",
                           "expected": "巡检应完成并记录覆盖范围", "actual": "巡检逻辑异常中断", "suggestion": "检查巡检器及其只读数据接口",
                           "validation": "修复后重新运行并确认该类别巡检状态恢复", "evidence": []}]
            for issue in issues:
                issue["task_kind"] = task_kind
                key = self.repository.upsert_issue(issue)
                active_by_kind[task_kind].add(key)
            self.repository.resolve_unseen(task_kind, active_by_kind[task_kind])
            counts[task_kind] = len(issues)
        return counts

    def run_channel_check(self) -> int:
        issues = self._channel_outages()
        keys: set[str] = set()
        for issue in issues:
            if issue.get("consecutive_check_required"):
                failures = self.repository.record_channel_observation(
                    issue["scope"]["component"], issue["scope"]["worker_id"],
                    failed=True, bucket=str(issue["observation_bucket"]),
                )
                if failures < 3:
                    continue
                issue["summary"] += f"；已連續 {failures} 次五分钟巡检观察到失败"
            else:
                self.repository.record_channel_observation(
                    issue["scope"]["component"], issue["scope"]["worker_id"],
                    failed=True, bucket=str(issue["observation_bucket"]),
                )
            issue.pop("consecutive_check_required", None)
            issue.pop("observation_bucket", None)
            keys.add(self.repository.upsert_issue({**issue, "task_kind": "channel_outage"}))
        self.repository.resolve_unseen("channel_outage", keys)
        return len(keys)

    def last_daily_run_date(self) -> str | None:
        return self.repository.get_state("last_daily_run_date")

    def set_daily_run_date(self, value: str) -> None:
        self.repository.set_state("last_daily_run_date", value)

    def _connect(self) -> sqlite3.Connection:
        return self._connect_path(self.primary_database_path)

    @staticmethod
    def _connect_path(path: Path) -> sqlite3.Connection:
        return connect_sqlite_readonly(path, timeout_seconds=10)

    def _channel_outages(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0).isoformat()
        issues = []
        heartbeats: dict[tuple[str, str], dict[str, Any]] = {}
        expected = self._expected_components()
        enabled_components = tuple(sorted(key for key, config in expected.items() if config["enabled"]))
        latest_by_component: dict[str, dict[str, Any]] = {}
        for path in dict.fromkeys((self.primary_database_path, self.competitor_database_path)):
            if path is None or not path.exists():
                continue
            try:
                with self._connect_path(path) as conn:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_worker_heartbeats'").fetchone()
                    if not exists or not enabled_components:
                        continue
                    placeholders = ",".join("?" for _ in enabled_components)
                    rows = conn.execute(
                        "SELECT component,worker_id,status,last_seen_at,last_success_at,last_error,metadata "
                        f"FROM pipeline_worker_heartbeats WHERE component IN ({placeholders})",
                        enabled_components,
                    )
                    for row in rows:
                        value = dict(row)
                        component = str(value["component"])
                        previous = latest_by_component.get(component)
                        if previous is None or (timestamp_ms(value.get("last_seen_at")) or 0) > (timestamp_ms(previous.get("last_seen_at")) or 0):
                            latest_by_component[component] = value
            except sqlite3.Error:
                continue

        for component, row in latest_by_component.items():
            heartbeats[(component, str(row["worker_id"]))] = row

        if self.primary_database_path.exists():
            try:
                with self._connect_path(self.primary_database_path) as conn:
                    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if {"binance_square_settings", "binance_square_accounts"} <= tables:
                        settings = conn.execute(
                            "SELECT enabled,interval_seconds,worker_status,worker_last_seen_at FROM binance_square_settings WHERE singleton_key='global'"
                        ).fetchone()
                        if settings and settings["enabled"]:
                            interval_minutes = max(1, int(settings["interval_seconds"] or 180) // 60)
                            heartbeats[("binance_square", "worker")] = {
                                "component": "binance_square", "worker_id": "worker",
                                "status": settings["worker_status"], "last_seen_at": settings["worker_last_seen_at"],
                                "last_success_at": settings["worker_last_seen_at"], "last_error": None, "metadata": "{}",
                            }
                            for row in conn.execute(
                                "SELECT slug_lower,last_polled_at,last_success_at,last_error FROM binance_square_accounts WHERE enabled=1"
                            ):
                                heartbeats[("binance_square", f"account:{row['slug_lower']}")] = {
                                    "component": "binance_square", "worker_id": f"account:{row['slug_lower']}",
                                    "status": "failed" if row["last_error"] else "ok",
                                    "last_seen_at": row["last_polled_at"], "last_success_at": row["last_success_at"],
                                    "last_error": row["last_error"], "metadata": "{}",
                                }
            except (sqlite3.Error, TypeError, ValueError):
                pass

        for key, config in expected.items():
            if not config["enabled"]:
                continue
            worker_id = "worker" if key == "binance_square" else key
            if not any(component == key for component, _instance_id in heartbeats):
                heartbeats[(key, worker_id)] = {"component": key, "worker_id": worker_id, "status": "missing",
                    "last_seen_at": None, "last_success_at": None, "last_error": "未发现心跳记录", "metadata": "{}"}

        for (component, worker_id), row in heartbeats.items():
            expected_config = expected.get(component)
            if expected_config is None or not expected_config["enabled"]:
                continue
            interval_minutes = expected_config["interval_minutes"]
            window = max(15, interval_minutes * 3)
            cutoff_ms = int((now - timedelta(minutes=window)).timestamp() * 1000)
            latest_ms = timestamp_ms(row.get("last_seen_at"))
            success_ms = timestamp_ms(row.get("last_success_at"))
            seen_recently = latest_ms is not None and latest_ms >= cutoff_ms
            success_recently = success_ms is not None and success_ms >= cutoff_ms
            status = str(row.get("status") or "").casefold()
            if seen_recently and success_recently and status not in {"stopped", "missing"}:
                self.repository.record_channel_observation(component, worker_id, failed=False, bucket=bucket)
                continue
            if seen_recently and status in {"rate_limited", "cooldown", "degraded"} and success_recently:
                self.repository.record_channel_observation(component, worker_id, failed=False, bucket=bucket)
                continue
            identity = {"component": component, "worker_id": worker_id}
            consecutive_check_required = seen_recently and status in {"failed", "error", "stopped"}
            issues.append({"identity": identity, "category": "channel_interruption", "title": f"{component}采集或处理心跳中断",
                "summary": f"最近心跳 {row.get('last_seen_at') or '未知'}；最近成功 {row.get('last_success_at') or '未知'}；最近错误 {row.get('last_error') or '未记录'}",
                "severity": "high", "certainty": "high", "expected": f"启用中的计划任务应在 {window} 分钟内有运行成功或明确降级记录",
                "actual": f"worker={worker_id} status={row.get('status')} last_seen={row.get('last_seen_at')} last_success={row.get('last_success_at')}",
                "scope": identity, "affected_count": 1, "evidence": [row], "counter_evidence": [],
                "suggestion": "检查worker心跳、上游错误和队列积压；确认订阅仍启用后再调整恢复策略",
                "validation": "连续三次计划运行成功，或最近成功时间恢复到配置窗口内",
                "consecutive_check_required": consecutive_check_required, "observation_bucket": bucket})
        return issues

    def _expected_components(self) -> dict[str, dict[str, Any]]:
        components: dict[str, dict[str, Any]] = {}
        configs = (
            ("x_capture", self.primary_database_path, "SELECT COUNT(*) FROM x_capture_accounts WHERE enabled=1", "SELECT global_interval_seconds FROM x_capture_settings LIMIT 1"),
            ("non_mainstream_media", self.competitor_database_path, "SELECT COUNT(*) FROM non_mainstream_media_sources WHERE enabled=1", "SELECT global_interval_seconds FROM non_mainstream_media_settings LIMIT 1"),
            ("jin10_monitor", self.primary_database_path, "SELECT COUNT(*) FROM jin10_settings WHERE enabled=1", "SELECT interval_seconds FROM jin10_settings LIMIT 1"),
            ("whale_watch", self.primary_database_path, "SELECT COUNT(*) FROM whale_watch_addresses WHERE enabled=1", None),
            ("whale_watch_hyperliquid", self.primary_database_path, "SELECT COUNT(*) FROM whale_watch_hyperliquid_addresses WHERE enabled=1", None),
            ("binance_square", self.primary_database_path, "SELECT COUNT(*) FROM binance_square_accounts WHERE enabled=1 AND (SELECT enabled FROM binance_square_settings WHERE singleton_key='global')=1", "SELECT interval_seconds FROM binance_square_settings WHERE singleton_key='global'"),
        )
        for component, path, enabled_query, interval_query in configs:
            enabled = False
            interval_minutes = 5
            if path is not None and path.exists():
                try:
                    with self._connect_path(path) as conn:
                        enabled = bool(conn.execute(enabled_query).fetchone()[0])
                        if interval_query:
                            value = conn.execute(interval_query).fetchone()
                            if value:
                                interval_minutes = max(1, int(value[0] or 60) // 60)
                except (sqlite3.Error, TypeError, ValueError):
                    enabled = False
            components[component] = {"enabled": enabled, "interval_minutes": interval_minutes}

        if self.local_pipeline_database_path and self.local_pipeline_database_path.exists():
            try:
                with self._connect_path(self.local_pipeline_database_path) as conn:
                    enabled = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_pipeline_jobs'").fetchone())
                components["local_pipeline"] = {"enabled": enabled, "interval_minutes": 1}
            except sqlite3.Error:
                components["local_pipeline"] = {"enabled": False, "interval_minutes": 1}
        return components

    def _coverage_gaps(self) -> list[dict[str, Any]]:
        issues = []
        if self.competitor_database_path is None or not self.competitor_database_path.exists():
            return issues
        with self._connect_path(self.competitor_database_path) as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"newsflash_events", "newsflash_event_sources", "newsflash_items"} <= tables:
                return []
            rows = conn.execute("SELECT event_id,representative_title,first_source,first_published_at,source_count,competitor_source_count,has_odaily,needs_review,metadata,updated_at FROM newsflash_events WHERE competitor_source_count>0 AND (has_odaily=0 OR needs_review=1) AND julianday(first_published_at)>=julianday('now','-3 day') ORDER BY updated_at DESC LIMIT 100").fetchall()
            for row in rows:
                if int(row["needs_review"] or 0) == 0 and int(row["has_odaily"] or 0) == 1:
                    continue
                evidence = [dict(item) for item in conn.execute(
                    "SELECT s.source,s.source_item_id,i.id AS item_id,i.source_url,i.title,s.role,s.match_method,s.created_at "
                    "FROM newsflash_event_sources s JOIN newsflash_items i ON i.id=s.item_id "
                    "WHERE s.event_id=? ORDER BY s.created_at LIMIT 12", (row["event_id"],),
                ).fetchall()]
                event = dict(row)
                competitor_count = int(event.get("competitor_source_count") or 0)
                source_pairs = [
                    (str(source.get("source") or ""), str(source.get("source_item_id") or ""))
                    for source in evidence if source.get("source") != "odaily" and source.get("source_item_id")
                ]
                event_ms = timestamp_ms(event.get("first_published_at")) or int(datetime.now(UTC).timestamp() * 1000)
                decisions = self.repository.information_store.decision_samples_for_sources(
                    source_pairs, through_ms=event_ms,
                )
                source_keys = [source for source, _source_id in source_pairs]
                config_changes = self.repository.information_store.configuration_changes_for_sources(
                    source_keys, through_ms=event_ms,
                )
                official_publications = [
                    {key: source.get(key) for key in ("source_item_id", "item_id", "source_url", "title", "created_at")}
                    for source in evidence if source.get("source") == "odaily"
                ]
                task_evidence: list[dict[str, Any]] = []
                if "tasks" in tables:
                    task_columns = {str(column["name"]) for column in conn.execute("PRAGMA table_info(tasks)")}
                    if {"source", "source_item_id", "id", "status"} <= task_columns:
                        has_pipeline = "x_task_pipeline" in tables
                        for source in evidence:
                            if source["source"] == "odaily":
                                continue
                            task_query = (
                                "SELECT t.id,t.source,t.source_item_id,t.status,t.updated_at"
                                + (",p.publisher_reason_code,p.publisher_output,p.push_result,p.last_error,p.final_title,p.final_content" if has_pipeline else "")
                                + " FROM tasks t"
                                + (" LEFT JOIN x_task_pipeline p ON p.task_id=t.id" if has_pipeline else "")
                                + " WHERE t.source=? AND t.source_item_id=? ORDER BY t.id DESC LIMIT 1"
                            )
                            task = conn.execute(task_query, (source["source"], source["source_item_id"])).fetchone()
                            source_key = (str(source["source"]), str(source["source_item_id"]))
                            config_history = config_changes.get(str(source["source"]), [])
                            task_evidence.append({
                                "source": source["source"],
                                "source_item_id": source["source_item_id"],
                                "configuration_history": config_history or None,
                                "configuration_history_status": "observed_changes" if config_history else "unknown",
                                "decision_samples": decisions.get(source_key, []),
                                "task": dict(task) if task else None,
                            })
                issues.append({"identity": {"event_id": event["event_id"]}, "category": "competitor_coverage_gap" if not event.get("has_odaily") else "event_needs_review",
                    "title": str(event.get("representative_title") or "竞品事件待复核"),
                    "summary": "竞品事件需要逐条核对。已附来源原链接、精确来源ID对应的公开稿映射、任务/投递与无候选样本，并附可取得的订阅变更；没有历史配置记录时保持未知。",
                    "severity": "medium", "certainty": "low", "expected": "检查订阅资格、采集、轻决定、写作、后台回执与公开关联证据",
                    "actual": f"first_source={event.get('first_source')} competitor_sources={competitor_count} has_odaily={event.get('has_odaily')} exact_task_links={sum(item['task'] is not None for item in task_evidence)}",
                    "scope": {"event_id": event["event_id"], "first_source": event.get("first_source")}, "affected_count": max(1, competitor_count),
                    "evidence": [{"event_sources": evidence, "official_publications": official_publications, "source_tasks": task_evidence}],
                    "counter_evidence": [{"metadata": event.get("metadata"), "has_odaily": event.get("has_odaily")}],
                    "suggestion": "按原帖ID逐项核对当时订阅变更、轻决定、任务终点、稿件、后台回执和公开稿关联；缺少历史证据时标为无法判断",
                    "validation": "每个原始事件都能回查来源链接、可用配置变更、精确轻决定样本及任务；缺失部分明确标为未知"})
        return issues

    def _audit_findings(self) -> list[dict[str, Any]]:
        issues = []
        findings = []
        sampling = self.information_store.record_decision_samples()
        with self._connect() as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table, status_col, content_cols, kind in (
                ("auditor_checks", "status", ("title", "content", "audit_result", "raw_output", "last_error", "prompt_version", "source_item_id"), "text_review"),
                ("fact_checks", "status", ("title", "content", "result_json", "summary", "last_error", "policy_version", "source_item_id", "original_text", "original_url"), "fact_review"),
            ):
                if table not in tables:
                    continue
                selected = ",".join(content_cols)
                rows = conn.execute(f"SELECT id,{status_col} AS status,{selected},created_at,updated_at FROM {table} WHERE {status_col} IN ('flagged','failed','unverifiable') AND julianday(updated_at)>=julianday('now','-1 day') ORDER BY updated_at DESC LIMIT 100").fetchall()
                for row in rows:
                    value = dict(row)
                    source = str(value.get("source_item_id") or "")
                    identity = {"table": table, "source_item_id": source, "version": value.get("content_hash") or value.get("prompt_version") or value.get("policy_version"), "status": value["status"]}
                    findings.append({"identity": identity, "category": kind,
                        "title": f"{kind}结果需要核对 · {source or value['id']}",
                        "summary": str(value.get("summary") or value.get("last_error") or value.get("audit_result") or value.get("result_json") or "审核记录需要对齐稿件版本和证据"),
                        "severity": "high" if value["status"] == "flagged" else "medium", "certainty": "medium",
                        "expected": "先对齐原始稿、定稿、提交稿、镜像和审查版本，再确定问题来源",
                        "actual": f"审查状态={value['status']}；稿件版本键={identity.get('version') or '未保存'}",
                        "scope": identity, "affected_count": 1, "evidence": [{k: value.get(k) for k in content_cols}], "counter_evidence": [],
                        "suggestion": "根据原始证据区分来源矛盾、写作问题、组装、稿件版本变化和审核误报；不要无版本证据归责",
                        "validation": "给出稿件版本、来源证据与审核结论的逐项对照"})
                if status_col == "status":
                    samples = conn.execute(
                        f"SELECT id,{status_col} AS status,{selected},created_at,updated_at FROM {table} WHERE {status_col} IN ('passed','completed','clean') AND julianday(updated_at)>=julianday('now','-1 day') AND id % 17 = 0 ORDER BY updated_at DESC LIMIT 30"
                    ).fetchall()
                    for row in samples:
                        value = dict(row)
                        source = str(value.get("source_item_id") or "")
                        identity = {"table": table, "source_item_id": source, "version": value.get("content_hash") or value.get("prompt_version") or value.get("policy_version"), "status": value["status"], "sample": True}
                        findings.append({"identity": identity, "category": f"{kind}_sample",
                            "title": f"抽查已通过的{kind} · {source or value['id']}",
                            "summary": "按稳定ID取模抽样，需先对齐稿件及审查版本；审核无标记不等于事实正确。",
                            "severity": "unknown", "certainty": "low", "expected": "抽查输入、生成稿、提交稿、镜像和审核对象版本是否一致",
                            "actual": f"审查状态={value['status']}；稿件版本键={identity.get('version') or '未保存'}",
                            "scope": identity, "affected_count": 1, "evidence": [{k: value.get(k) for k in content_cols}], "counter_evidence": [],
                            "suggestion": "检查样本中的事实与版本对应关系；没有原始材料时标为无法判断",
                            "validation": "给出所查版本和每条可验证证据，不以审核状态替代事实核对"})
        if findings:
            issues.append({
                "identity": {"review_window": "rolling_24h", "finding_keys": sorted(_digest(item["identity"])[:16] for item in findings)},
                "category": "review_and_output_audit_batch", "title": "产出与审核复盘",
                "summary": f"有 {len(findings)} 项审核标记或抽样结果等待按稿件版本和来源证据核对。没有保存的版本关系保持未知。",
                "severity": "unknown", "certainty": "low", "expected": "对齐原始稿、定稿、提交稿、镜像和审核对象，并说明归因依据",
                "actual": f"候选复核 {len(findings)} 项；确定性证据不足时不判断责任方。", "affected_count": len(findings),
                "scope": {"window": "rolling_24h", "items": len(findings)}, "evidence": findings[:30], "counter_evidence": [],
                "suggestion": "按样本逐项核对来源、稿件各版本与审核证据；仅指出能够定位的修改位置",
                "validation": "核对每项输入、稿件版本和审核对象；缺少版本证据的项标记无法判断",
            })
        if sampling["sampled"]:
            sample_date = datetime.now(UTC).date().isoformat()
            samples = self.information_store.decision_samples(sample_date=sample_date, limit=30)
            if samples:
                sample_keys = [str(row["sample_key"]) for row in samples]
                counts: dict[str, int] = {}
                for row in samples:
                    business = str(row["business_key"])
                    counts[business] = counts.get(business, 0) + 1
                issues.append({
                    "identity": {"sample_date": sample_date, "sample_keys": sample_keys},
                    "category": "no_candidate_sample_review",
                    "title": "无候选决定抽样复核",
                    "summary": f"从 {sampling['sampled']} 条分层留存样本中抽查 {len(samples)} 条；抽样不代表总体错误率。",
                    "severity": "unknown", "certainty": "low",
                    "expected": "对照当时原文、轻决定理由和规则版本，判断停止理由是否有证据支持；材料不足时保持无法判断",
                    "actual": "各业务样本数：" + "、".join(f"{key} {value} 条" for key, value in sorted(counts.items())),
                    "scope": {"sample_date": sample_date, "business_counts": counts},
                    "affected_count": len(samples),
                    "evidence": [
                        {key: row.get(key) for key in (
                            "sample_key", "item_id", "business_key", "source", "source_id", "stage",
                            "status", "result", "reason_summary", "rule_version", "evidence_ref",
                        )}
                        for row in samples
                    ],
                    "counter_evidence": [],
                    "suggestion": "只对证据明确显示错误的样本定位规则或阶段；截断或缺失原文的样本不得用来判定语义错误",
                    "validation": "打开样本证据确认原文可用性、当时规则版本及决定理由；不能由抽样外推总体准确率",
                })
        output_samples = self.information_store.output_review_samples(limit=30)
        if output_samples:
            sample_date = datetime.now(UTC).date().isoformat()
            counts: dict[str, int] = {}
            for row in output_samples:
                business = str(row["business_key"])
                counts[business] = counts.get(business, 0) + 1
            stream_item_ids = [str(row["item_id"]) for row in output_samples]
            issues.append({
                "identity": {"sample_date": sample_date, "stream_item_ids": stream_item_ids},
                "category": "successful_output_sample_review",
                "title": "已完成产出抽样复核",
                "summary": f"从最近一天已完成的快讯、Meme与X Agent结果中分层抽查 {len(output_samples)} 项；样本不代表总体错误率。",
                "severity": "unknown", "certainty": "low",
                "expected": "对齐输入材料、产出版本、业务终点和可用审查证据；内部研究完成不视作漏发",
                "actual": "各业务样本数：" + "、".join(f"{key} {value} 条" for key, value in sorted(counts.items())),
                "scope": {"sample_date": sample_date, "business_counts": counts},
                "affected_count": len(output_samples),
                "evidence": [
                    {"stream_item_id": row["item_id"], **{key: row.get(key) for key in (
                        "business_key", "source", "source_id", "title", "summary", "source_url",
                        "stage", "status", "result", "updated_at_ms",
                    )}}
                    for row in output_samples
                ],
                "counter_evidence": [],
                "suggestion": "先核对每项的输入、输出和预期业务终点；版本或来源证据缺失时标为无法判断",
                "validation": "打开证据并核对相同业务身份的稿件/研究结果；不得将抽样结论外推为总体质量率",
            })
        return issues

    def _rule_conflicts(self) -> list[dict[str, Any]]:
        issues = []
        requirements = ("固定开头", "必须后台", "必须挂后台", "固定格式", "严格json", "严格json输出")
        prohibitions = ("不得固定开头", "禁止固定开头", "不得挂后台", "禁止挂后台", "不要后台", "禁止严格json")
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_template_versions'").fetchone()
            active_rows = conn.execute(
                "SELECT pt.template_key,pt.active_version_id,pt.feature_mode_enabled,pt.feature_mode_text,pv.version_number,pv.content "
                "FROM prompt_templates pt LEFT JOIN prompt_template_versions pv ON pv.id=pt.active_version_id"
            ).fetchall() if exists else []
        for row in active_rows:
            if not row["active_version_id"]:
                continue
            base_lines = [line.strip() for line in str(row["content"] or "").splitlines() if line.strip()]
            feature_lines = [line.strip() for line in str(row["feature_mode_text"] or "").splitlines() if line.strip()] if row["feature_mode_enabled"] else []
            lines = base_lines + feature_lines
            for required in requirements:
                positive = next((line for line in lines if required.casefold() in line.casefold()), None)
                opposite = next((line for line in lines if any(term.casefold() in line.casefold() for term in prohibitions)), None)
                if not positive or not opposite or positive == opposite:
                    continue
                identity = {"template_key": str(row["template_key"]), "version": int(row["version_number"]), "required": required, "positive_sha": _digest(positive), "opposite_sha": _digest(opposite)}
                issues.append({"identity": identity, "category": "possible_rule_conflict", "title": f"模板 {row['template_key']} v{row['version_number']} 有规则方向冲突",
                    "summary": "确定性词句扫描在同一生效模板组合中找到可能相反的约束；仍需核对具体场景。",
                    "severity": "medium", "certainty": "low", "expected": "同一场景有效模板与特色规则应一致；不同场景差异可并存",
                    "actual": f"要求：{positive[:500]}\n限制：{opposite[:500]}", "scope": identity,
                    "affected_count": 1, "evidence": [{"template_key": row["template_key"], "version": row["version_number"], "feature_mode_enabled": bool(row["feature_mode_enabled"]), "required_line": positive, "opposite_line": opposite}],
                    "counter_evidence": [], "suggestion": "核对适用场景、模板版本和特色模式后，仅修正确实同时生效的矛盾",
                    "validation": "用同一模板组合的正反例验证，并确认其他模板不受影响"})
        issues.extend(self._rule_execution_findings())
        return issues

    def _rule_execution_findings(self) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        with self._connect() as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"tasks", "x_task_pipeline"} <= tables:
                return issues
            task_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(tasks)")}
            pipeline_columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(x_task_pipeline)")}
            required_task_columns = {"id", "source", "source_item_id", "source_url", "title", "status", "metadata", "raw_payload", "updated_at"}
            required_pipeline_columns = {"task_id", "prompt_template_key", "prompt_version_id", "draft_title", "draft_content", "final_title", "final_content"}
            if not required_task_columns <= task_columns or not required_pipeline_columns <= pipeline_columns:
                return issues
            rows = conn.execute(
                "SELECT t.id,t.source,t.source_item_id,t.source_url,t.title,t.status,t.metadata,t.raw_payload,t.updated_at,"
                "p.prompt_template_key,p.prompt_version_id,p.draft_title,p.draft_content,p.final_title,p.final_content "
                "FROM tasks t JOIN x_task_pipeline p ON p.task_id=t.id "
                "WHERE julianday(t.updated_at)>=julianday('now','-1 day') AND p.final_content IS NOT NULL "
                "ORDER BY t.updated_at DESC LIMIT 500"
            ).fetchall()
        from packages.hyperliquid_news.copy import ODAILY_PREFIX, PREFIX as HL_PREFIX, normalize_display_numbers
        from packages.x_processing.prompt_rules import validate_x_onchain_attribution

        for row in rows:
            value = dict(row)
            metadata = _json_dict(value.get("metadata"))
            final_title = str(value.get("final_title") or "")
            final_content = str(value.get("final_content") or "")
            violations: list[str] = []
            if value["source"] == "x" and value.get("prompt_template_key") == "x_onchain_writer":
                speaker = str(metadata.get("effective_author_name") or metadata.get("author_display_name") or metadata.get("author_username") or "").strip()
                if speaker:
                    try:
                        validate_x_onchain_attribution(str(value.get("draft_content") or final_content), speaker)
                    except ValueError:
                        violations.append(f"写作稿未按链上模板以“据{speaker}监测，”开头")
            is_preview = bool(metadata.get("is_preview") or metadata.get("mode") == "preview_only")
            if value["source"] == "hyperliquid_news" and not is_preview and str(value.get("status") or "").casefold() in {"published", "auto_published"}:
                violations.append("新闻场景稿件已直发；该场景要求强制进入后台审核")
            if value["source"] == "hyperliquid_news" and not is_preview and final_content:
                if not final_content.startswith(ODAILY_PREFIX + HL_PREFIX):
                    violations.append("定稿缺少 Hyperliquid 稿件规定的 Odaily 固定刊头")
                if normalize_display_numbers(final_title) != final_title or normalize_display_numbers(final_content) != final_content:
                    violations.append("定稿仍含可由现有金额/比例格式化规则规范的数字")
                if re.search(r"样本(?:截至|时间|日期)\s*[:：]?\s*20\d{2}", final_title + "\n" + final_content):
                    violations.append("定稿保留了规则要求移除的样本时间")
            if not violations:
                continue
            identity = {"task_id": int(value["id"]), "source": value["source"], "source_item_id": value["source_item_id"], "template_key": value.get("prompt_template_key"), "prompt_version_id": value.get("prompt_version_id"), "violations": violations}
            issues.append({"identity": identity, "category": "rule_execution_mismatch", "title": f"{value['source']}稿件未符合已声明格式或投递要求",
                "summary": "定稿或任务状态与当前代码/结构化规则不一致；问题只按可验证的不变量生成。",
                "severity": "high", "certainty": "high", "expected": "遵守已记录的固定开头、后台投递与金额/比例/时间格式要求",
                "actual": "；".join(violations), "scope": identity, "affected_count": 1,
                "evidence": [{"source_url": value.get("source_url"), "source_item_id": value.get("source_item_id"), "status": value.get("status"), "prompt_template_key": value.get("prompt_template_key"), "prompt_version_id": value.get("prompt_version_id"), "draft_title": value.get("draft_title"), "draft_content": str(value.get("draft_content") or "")[:5000], "final_title": final_title, "final_content": final_content[:5000], "metadata": metadata}],
                "counter_evidence": [], "suggestion": "检查该稿件所用版本及对应格式化/发布代码，确认是否存在绕过已定义校验的路径",
                "validation": "对同一来源构造违规和合规样本，违规样本可被确定性巡检定位，合规路径不告警"})
        return issues

    def _failure_clusters(self) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        successful_attempts: dict[tuple[int, str], int] = {}
        capture_successes: dict[str, list[int]] = {}
        binance_successes: dict[str, list[int]] = {}

        def error_signature(value: Any) -> str:
            return re.sub(r"\b\d{3,}\b", "#", str(value or "unknown"))[:240]

        def latest_id_floor(conn: sqlite3.Connection, table: str, limit: int) -> int:
            latest_id = int(conn.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0] or 0)
            return max(0, latest_id - limit)

        def add_failure(source: Any, stage: str, error_code: Any, error: Any, version: Any, row: dict[str, Any], *, evidence_level: str) -> None:
            category = failure_category(error)
            if not is_actionable_internal_failure(error):
                return
            occurred = timestamp_ms(row.get('finished_at') or row.get('updated_at') or row.get('started_at'))
            if occurred is None:
                return
            message = error_signature(error)
            version_key = str(version or "unknown")[:160]
            key = (str(category or "program_or_unknown"), message)
            groups.setdefault(key, []).append({
                **row,
                "source": str(source or "unknown"),
                "stage": stage,
                "error_code": str(error_code or "unknown"),
                "evidence_level": evidence_level,
                "error_signature": message,
                "rule_version": version_key,
                "failure_category": category,
                "occurred_at_ms": occurred,
                "failure_day": datetime.fromtimestamp(occurred / 1000, UTC).astimezone(BEIJING).date().isoformat(),
            })

        with self._connect() as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "x_processing_attempts" in tables:
                attempt_id_floor = latest_id_floor(conn, "x_processing_attempts", MAX_PROCESSING_ATTEMPT_ROWS)
                attempt_rows = conn.execute(
                    "SELECT a.id,a.task_id,a.stage,a.attempt_no,a.executor,a.status,a.error_code,a.error_message,"
                    "a.started_at,a.finished_at,a.rule_json,t.source,t.status AS task_status,"
                    "p.prompt_template_key,p.prompt_version_id "
                    "FROM x_processing_attempts a JOIN tasks t ON t.id=a.task_id "
                    "LEFT JOIN x_task_pipeline p ON p.task_id=t.id "
                    f"WHERE a.id>? AND julianday(a.started_at)>=julianday('now','-{FAILURE_HISTORY_DAYS} day') "
                    "AND a.status IN ('failed','succeeded') ORDER BY a.id DESC LIMIT ?",
                    (attempt_id_floor, MAX_PROCESSING_ATTEMPT_ROWS),
                ).fetchall()
                for row in attempt_rows:
                    value = dict(row)
                    stage = str(value.get("stage") or "unknown")
                    if value["status"] == "succeeded":
                        key = (int(value["task_id"]), stage)
                        successful_attempts[key] = max(successful_attempts.get(key, 0), int(value["attempt_no"] or 0))
                        continue
                    rule = _json_dict(value.get("rule_json"))
                    version = rule.get("prompt_version_id") or value.get("prompt_version_id")
                    if value.get("prompt_template_key"):
                        version = f"{value['prompt_template_key']}:{version or 'unknown'}"
                    add_failure(value.get("source"), stage, value.get("error_code"), value.get("error_message"), version, value, evidence_level="per_stage_attempt")
            if "tasks" in tables and "x_task_pipeline" in tables:
                rows = conn.execute(
                    "SELECT t.id AS task_id,t.source,t.source_item_id,t.status,t.updated_at,p.last_error,p.prompt_template_key,p.prompt_version_id "
                    "FROM tasks t JOIN x_task_pipeline p ON p.task_id=t.id "
                    "WHERE t.status IN ('judge_failed','search_failed','write_failed','format_failed','publisher_failed') AND p.last_error IS NOT NULL "
                    f"AND julianday(t.updated_at)>=julianday('now','-{FAILURE_HISTORY_DAYS} day') ORDER BY t.updated_at DESC LIMIT 10000"
                ).fetchall()
                for row in rows:
                    value = dict(row)
                    if not value.get('last_error'):
                        continue
                    version = f"{value.get('prompt_template_key') or 'unknown'}:{value.get('prompt_version_id') or 'unknown'}"
                    add_failure(value.get("source"), "current_status", "unknown", value.get("last_error") or value.get("status"), version, value, evidence_level="current_task_snapshot")
            if "x_capture_attempts" in tables:
                capture_id_floor = latest_id_floor(conn, "x_capture_attempts", MAX_CAPTURE_ATTEMPT_ROWS)
                rows = conn.execute(
                    "SELECT id,account_id,username_lower,status,source,error,started_at,finished_at,candidate_count,new_count,saved_count "
                    f"FROM x_capture_attempts WHERE id>? AND julianday(started_at)>=julianday('now','-{FAILURE_HISTORY_DAYS} day') ORDER BY id DESC LIMIT ?",
                    (capture_id_floor, MAX_CAPTURE_ATTEMPT_ROWS),
                ).fetchall()
                for row in rows:
                    value = dict(row)
                    account = str(value.get("account_id") or value.get("username_lower") or "unknown")
                    if str(value.get("status") or "").casefold() in {"success", "ok", "completed"}:
                        capture_successes.setdefault(account, []).append(timestamp_ms(value.get("finished_at")) or 0)
                    elif str(value.get("status") or "").casefold() in {"failed", "error"}:
                        add_failure(f"x_capture:{value.get('source') or 'unknown'}", "capture", "unknown", value.get("error") or value.get("status"), "capture_attempt", value, evidence_level="per_source_attempt")
            if "binance_square_attempts" in tables:
                square_id_floor = latest_id_floor(conn, "binance_square_attempts", MAX_CAPTURE_ATTEMPT_ROWS)
                rows = conn.execute(
                    "SELECT id,account_id,slug_lower,status,error,started_at,finished_at,candidate_count,new_count,saved_count "
                    f"FROM binance_square_attempts WHERE id>? AND julianday(started_at)>=julianday('now','-{FAILURE_HISTORY_DAYS} day') ORDER BY id DESC LIMIT ?",
                    (square_id_floor, MAX_CAPTURE_ATTEMPT_ROWS),
                ).fetchall()
                for row in rows:
                    value = dict(row)
                    account = str(value.get("account_id") or value.get("slug_lower") or "unknown")
                    if str(value.get("status") or "").casefold() in {"success", "ok", "completed"}:
                        binance_successes.setdefault(account, []).append(timestamp_ms(value.get("finished_at")) or 0)
                    elif str(value.get("status") or "").casefold() in {"failed", "error"}:
                        add_failure("binance_square", "capture", "unknown", value.get("error") or value.get("status"), "capture_attempt", value, evidence_level="per_account_attempt")
        issues: list[dict[str, Any]] = []
        for (category, message), rows_in_cluster in groups.items():
            subjects: dict[str, dict[str, Any]] = {}
            latest_failure_at: dict[str, int] = {}
            latest_failed_attempt: dict[tuple[int, str], int] = {}
            capture_failure_at: dict[str, int] = {}
            recovered_subjects: set[str] = set()
            recovery_evidence: list[dict[str, Any]] = []
            for row in rows_in_cluster:
                task_id = row.get("task_id")
                account_id = row.get("account_id") or row.get("username_lower") or row.get("slug_lower")
                source = str(row.get("source") or "unknown")
                subject_id = f"task:{task_id}" if task_id is not None else f"account:{source}:{account_id}" if account_id is not None else f"row:{source}:{row.get('id')}"
                occurred = int(row["occurred_at_ms"])
                if subject_id not in subjects or occurred > latest_failure_at[subject_id]:
                    subjects[subject_id] = row
                    latest_failure_at[subject_id] = occurred
                if task_id is not None:
                    task_stage = (int(task_id), str(row.get("stage") or "unknown"))
                    latest_failed_attempt[task_stage] = max(latest_failed_attempt.get(task_stage, 0), int(row.get("attempt_no") or 0))
                elif account_id is not None:
                    capture_failure_at[subject_id] = max(capture_failure_at.get(subject_id, 0), occurred)

            for subject_id, row in subjects.items():
                task_id = row.get("task_id")
                if task_id is not None:
                    task_status = row.get("task_status")
                    if task_status is None and str(row.get("stage") or "") == "current_status":
                        task_status = row.get("status")
                    normalized_task_status = str(task_status or "").casefold()
                    if normalized_task_status and not (normalized_task_status == "failed" or normalized_task_status.endswith("_failed")):
                        recovered_subjects.add(subject_id)
                        recovery_evidence.append({"subject_id": subject_id, "current_status": normalized_task_status})
                        continue
                    stage = str(row.get("stage") or "unknown")
                    failed_attempt = latest_failed_attempt.get((int(task_id), stage), 0)
                    success_attempt = successful_attempts.get((int(task_id), stage), 0)
                    if success_attempt > failed_attempt:
                        recovered_subjects.add(subject_id)
                        recovery_evidence.append({"subject_id": subject_id, "recovered_by_attempt_no": success_attempt, "stage": stage})
                elif row.get("account_id") is not None or row.get("username_lower") or row.get("slug_lower"):
                    account = str(row.get("account_id") or row.get("username_lower") or row.get("slug_lower"))
                    source = str(row.get("source") or "")
                    successful = capture_successes.get(account, []) if source.startswith("x_capture:") else binance_successes.get(account, [])
                    failed_at = capture_failure_at.get(subject_id, latest_failure_at[subject_id])
                    if any(success_at > failed_at for success_at in successful):
                        recovered_subjects.add(subject_id)
                        recovery_evidence.append({"subject_id": subject_id, "recovered_after_ms": failed_at, "source": source})

            affected = len(subjects)
            recovered = len(recovered_subjects)
            failed = affected - recovered
            failure_days = sorted({str(row["failure_day"]) for row in rows_in_cluster})
            if len(failure_days) < PERSISTENT_FAILURE_MIN_DAYS or failed == 0:
                continue

            sources = sorted({str(row.get("source") or "unknown") for row in rows_in_cluster})
            stages = sorted({str(row.get("stage") or "unknown") for row in rows_in_cluster})
            versions = sorted({str(row.get("rule_version") or "unknown") for row in rows_in_cluster})
            first_seen = min(int(row["occurred_at_ms"]) for row in rows_in_cluster)
            first_seen_at = datetime.fromtimestamp(first_seen / 1000, UTC).isoformat()
            identity = {"failure_category": category, "error_signature": message}
            issue_title = {
                "writer output repeats title in content": "写作校验持续失败：标题重复在正文",
                "writer output feature mode fields are invalid": "写作校验持续失败：特色模式字段无效",
                "active prompt not found: msx_notice_writer": "写作配置持续失败：MSX 活动 Prompt 缺失",
                "writer output contains unsupported title_strategy": "写作校验持续失败：标题策略不受支持",
                "database is locked": "本地数据库持续写入锁冲突",
            }.get(message, f"内部故障持续出现：{message[:120]}")
            source_labels = {
                "x": "X", "panews": "PANews", "jinse": "金十", "blockbeats": "律动",
                "msx": "MSX", "non_mainstream_media": "非主流媒体", "hyperliquid_news": "Hyperliquid",
                "ai_source": "AI 信源", "binance_square": "币安广场",
            }
            display_sources = [source_labels.get(source, source) for source in sources]
            source_detail = "、".join(display_sources[:8]) + (f" 等 {len(sources)} 个来源" if len(sources) > 8 else "")
            evidence_by_subject = sorted(subjects.values(), key=lambda row: int(row["occurred_at_ms"]), reverse=True)
            evidence = [{
                **row,
                "latest_failure_day": row["failure_day"],
                "source": row.get("source"),
                "stage": row.get("stage"),
            } for row in evidence_by_subject[:30]]
            issues.append({
                "identity": identity,
                "category": "persistent_internal_failure",
                "title": issue_title,
                "summary": f"过去 {FAILURE_HISTORY_DAYS} 天在 {len(failure_days)} 个自然日反复出现；涉及 {affected} 个唯一任务/账号，当前仍有 {failed} 个对象未恢复。覆盖来源：{source_detail}。",
                "severity": "high" if failed >= 10 else "medium",
                "certainty": "high",
                "expected": "系统巡检只将可由内部修复、跨日反复出现且仍未恢复的故障作为持续问题展示。",
                "actual": f"近 {FAILURE_HISTORY_DAYS} 天出现日期：{', '.join(failure_days)}；来源：{source_detail}；阶段：{'、'.join(stages)}；规则版本：{'、'.join(versions[:8])}{' 等' if len(versions) > 8 else ''}；根因：{message}",
                "scope": identity,
                "affected_count": affected,
                "first_seen_at": first_seen_at,
                "evidence": evidence,
                "counter_evidence": recovery_evidence[:30],
                "suggestion": "检查上述来源和阶段的共同校验或流程配置，修复同一根因后观察后续处理是否恢复。",
                "validation": "修复后连续 3 天不再出现该根因，并确认受影响任务恢复或已明确停止。",
            })
        return sorted(issues, key=lambda issue: (-int(issue["affected_count"]), str(issue["title"])))

    def analyze_pending(self, limit: int = 12) -> dict[str, int]:
        analyzed = failed = deferred = 0
        for item in self.repository.pending_analysis(limit):
            analysis_item = dict(item)
            for key in ("evidence_json", "counter_evidence_json"):
                try:
                    decoded = json.loads(str(analysis_item.get(key) or "[]"))
                except ValueError:
                    decoded = []
                analysis_item[key] = _json(self.repository.expand_sample_references(decoded))
            material = {key: analysis_item.get(key) for key in ("task_kind", "category", "title", "summary", "scope_json", "expected", "actual", "evidence_json", "counter_evidence_json", "suggestion", "validation")}
            encoded = _json(material)
            reserved = min(DAILY_TOKEN_LIMIT + 1, max(1, len(encoded) + 3000))
            day = datetime.now(BEIJING).date().isoformat()
            if not self.repository.reserve(day, reserved):
                self.repository.fail_analysis(item["item_key"], "达到每日100次请求或100万输入tokens预算；保留积压顺延", budget_paused=True)
                deferred += 1
                continue
            attempt_id = self.repository.start_analysis_attempt(item["item_key"])
            usage_settled = False

            def settle_usage(input_tokens: int) -> None:
                nonlocal usage_settled
                if usage_settled:
                    return
                self.repository.settle(day, reserved, max(0, input_tokens))
                usage_settled = True

            try:
                result = self._analyze(analysis_item, attempt_id=attempt_id, on_usage=settle_usage)
                if not usage_settled:
                    settle_usage(result["input_tokens"])
                self.repository.complete_analysis(item["item_key"], result)
                self.repository.finish_analysis_attempt(attempt_id, status="completed")
                analyzed += 1
            except Exception as exc:
                if not usage_settled:
                    self.repository.release(day, reserved)
                self.repository.fail_analysis(item["item_key"], f"{type(exc).__name__}: {exc}")
                self.repository.finish_analysis_attempt(attempt_id, status="failed", error=f"{type(exc).__name__}: {exc}")
                failed += 1
        return {"analyzed": analyzed, "failed": failed, "budget_paused": deferred}

    def _analyze(self, item: dict[str, Any], *, attempt_id: int | None = None, on_usage: Any = None) -> dict[str, Any]:
        settings = self.model_settings
        base = str(
            getattr(settings, "search_ai_review_openai_base_url", None)
            or getattr(settings, "openai_base_url", None)
            or os.getenv("X_AGENT_OPENAI_BASE_URL")
            or os.getenv("ODAILY_LLM_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or ""
        ).rstrip("/")
        key = str(
            getattr(settings, "search_ai_review_openai_api_key", None)
            or getattr(settings, "openai_api_key", None)
            or os.getenv("X_AGENT_OPENAI_API_KEY")
            or os.getenv("ODAILY_LLM_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or ""
        )
        if not base or not key:
            raise RuntimeError("AI巡检模型路由未配置，待审任务保留并显示失败状态")
        model = str(os.getenv("SYSTEM_REVIEW_MODEL") or getattr(settings, "search_ai_review_model", None) or "gpt-6-luna")
        api_style = str(
            getattr(settings, "search_ai_review_openai_api_style", None)
            or getattr(settings, "openai_api_style", None)
            or "responses"
        )
        system = (
            "你是OdAIly内部质量审阅者。只使用输入证据，不推断缺失的历史、规则或发布状态。优先识别会误导读者交易决策或错误损害个人/机构名誉的问题。"
            "明确区分证据、反证、确定性和无法判断；不得建议自动改Prompt、加信源或更改发布资格。只返回JSON："
            "{\"summary\":\"具体结论\",\"severity\":\"low|medium|high|critical\",\"certainty\":\"low|medium|high\",\"suggestion\":\"修改位置和建议\",\"validation\":\"可执行的验证方法\"}"
        )
        prompt = _json({"expected": item.get("expected"), "actual": item.get("actual"), "evidence": _json_value(item.get("evidence_json")), "counter_evidence": _json_value(item.get("counter_evidence_json")), "deterministic_context": item.get("summary"), "initial_suggestion": item.get("suggestion"), "task_kind": item.get("task_kind")})
        timeout = float(os.getenv("SYSTEM_REVIEW_TIMEOUT_SECONDS") or getattr(settings, "request_timeout_seconds", 60) or 60)
        if api_style == "responses":
            url = base if base.endswith("/responses") else f"{base}/responses"
            request_body = {"model": model, "instructions": system, "input": prompt, "max_output_tokens": 1200}
            extractor = extract_response_text
        elif api_style == "chat_completions":
            url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
            request_body = {"model": model, "temperature": 0.1, "max_tokens": 1200, "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
            extractor = extract_chat_completion_text
        else:
            raise ValueError(f"不支持的AI巡检API类型：{api_style}")
        try:
            response = requests.post(url, json=request_body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=timeout)
        except requests.RequestException:
            if on_usage:
                on_usage(len(prompt))
            raise
        raw_response = response.text
        try:
            raw_payload = json.loads(raw_response)
        except ValueError:
            raw_payload = {}
        usage = raw_payload.get("usage") if isinstance(raw_payload, dict) and isinstance(raw_payload.get("usage"), dict) else {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or max(1, len(prompt)))
        if on_usage:
            on_usage(input_tokens)
        if attempt_id is not None:
            request_id = response.headers.get("x-request-id") or response.headers.get("request-id")
            self.repository.record_analysis_response(attempt_id, raw_response, request_id, input_tokens)
        if not response.ok:
            raise RuntimeError(f"AI巡检上游HTTP {response.status_code}")
        payload = parse_openai_response_payload(response)
        content = extractor(payload)
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise ValueError("AI巡检未返回JSON")
        output = json.loads(match.group())
        return {"summary": str(output.get("summary") or "需要人工复核")[:2000],
                "severity": output.get("severity") if output.get("severity") in {"low", "medium", "high", "critical"} else "unknown",
                "certainty": output.get("certainty") if output.get("certainty") in {"low", "medium", "high"} else "low",
                "suggestion": str(output.get("suggestion") or item.get("suggestion") or "")[:2000],
                "validation": str(output.get("validation") or item.get("validation") or "")[:2000],
                "input_tokens": input_tokens,
                "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or max(1, len(content) // 4))}


class SystemReviewWorker:
    def __init__(self, service: SystemReviewService, *, interval_seconds: int = 60) -> None:
        self.service = service
        self.interval_seconds = max(30, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        recovered = self.service.repository.recover_interrupted_runs()
        if recovered:
            print(f"[odaily] system review recovered interrupted runs count={recovered}")
        self._thread = threading.Thread(target=self._run, name="system-review-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_once(self) -> dict[str, Any]:
        now = datetime.now(BEIJING)
        self.service.repository.set_state("worker_last_seen_at", now.isoformat())
        self.service.repository.set_state("worker_last_error", "")
        results: dict[str, Any] = {"analysis": self.service.analyze_pending()}
        channel_bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0).isoformat()
        if self.service.repository.get_state("last_channel_check_bucket") != channel_bucket:
            run_key = f"channel_outage:{channel_bucket}"
            self.service.repository.start_run(run_key, "channel_outage")
            try:
                count = self.service.run_channel_check()
                self.service.repository.finish_run(run_key, status="completed", items_created=count)
                self.service.repository.set_state("last_channel_check_bucket", channel_bucket)
                results["channel_outage"] = count
            except Exception as exc:
                self.service.repository.finish_run(run_key, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
        if now.hour >= 6 and self.service.last_daily_run_date() != now.date().isoformat():
            run_key = f"daily:{now.date().isoformat()}:v1"
            self.service.repository.start_run(run_key, "daily_quality")
            try:
                counts = self.service.run_deterministic_checks()
                self.service.repository.finish_run(run_key, status="completed", items_created=sum(counts.values()))
                self.service.set_daily_run_date(now.date().isoformat())
                results["daily"] = counts
            except Exception as exc:
                self.service.repository.finish_run(run_key, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
        if now.hour >= 6 and self.service.repository.get_state("last_storage_maintenance_date") != now.date().isoformat():
            run_key = f"storage_retention:{now.date().isoformat()}:v1"
            self.service.repository.start_run(run_key, "storage_retention")
            try:
                self.service.information_store.storage_health(sample=True)
                self.service.information_store.expire_decision_samples()
                orphaned = self.service.information_store.prune_orphan_evidence(dry_run=False, limit=500)
                self.service.repository.finish_run(run_key, status="completed", items_created=orphaned["files"])
                self.service.repository.set_state("last_storage_maintenance_date", now.date().isoformat())
                results["storage_retention"] = {"orphaned": orphaned["files"]}
            except Exception as exc:
                self.service.repository.finish_run(run_key, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                try:
                    self.service.repository.set_state("worker_last_error", f"{type(exc).__name__}: {exc}"[:1000])
                except Exception as state_error:
                    print(f"[odaily] system review error state persistence failed: {type(state_error).__name__}: {state_error}")
                print(f"[odaily] system review round failed: {type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_seconds)
