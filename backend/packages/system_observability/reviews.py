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

from packages.x_processing.ai_client import extract_chat_completion_text, extract_response_text, parse_openai_response_payload
from .store import SystemInformationStore, timestamp_ms


BEIJING = ZoneInfo("Asia/Shanghai")
DAILY_CALL_LIMIT = 100
DAILY_TOKEN_LIMIT = 1_000_000
MAX_ATTEMPTS = 3

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
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def upsert_issue(self, issue: dict[str, Any]) -> str:
        key = str(issue.get("item_key") or f"{issue['task_kind']}:{issue['category']}:{_digest(issue.get('identity') or issue['title'])[:24]}")
        now = _now()
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
                        status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'pending' ELSE system_review_items.status END,
                        decision_by=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_by END,
                        decision_note=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN NULL ELSE system_review_items.decision_note END,
                        analysis_attempts=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash THEN 0 ELSE system_review_items.analysis_attempts END,
                        evidence_hash=excluded.evidence_hash,recovered_at=NULL,
                        analysis_status=CASE WHEN system_review_items.evidence_hash<>excluded.evidence_hash OR system_review_items.recovered_at IS NOT NULL THEN 'queued' ELSE system_review_items.analysis_status END""",
                (*values[:14], now, now, values[14], values[15]),
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
                "SELECT * FROM system_review_items WHERE analysis_status IN ('queued','failed','budget_paused') AND analysis_attempts<? ORDER BY last_seen_at,item_key LIMIT ?",
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
        clauses: list[str] = []
        params: list[Any] = []
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
            for source, target in (("scope_json", "scope"), ("evidence_json", "evidence"), ("counter_evidence_json", "counter_evidence")):
                result[target] = json.loads(result.pop(source) or "{}")
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

    def finish_run(self, run_key: str, *, status: str, items_created: int = 0, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE system_review_runs SET status=?,completed_at=?,items_created=?,error=? WHERE run_key=?",
                (status, _now(), items_created, error[:1000] if error else None, run_key),
            )

    def recent_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
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
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _channel_outages(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        bucket = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0).isoformat()
        issues = []
        heartbeats: dict[tuple[str, str], dict[str, Any]] = {}
        for path in (self.primary_database_path, self.competitor_database_path):
            if path is None or not path.exists():
                continue
            try:
                with self._connect_path(path) as conn:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_worker_heartbeats'").fetchone()
                    if not exists:
                        continue
                    for row in conn.execute("SELECT component,worker_id,status,last_seen_at,last_success_at,last_error,metadata FROM pipeline_worker_heartbeats"):
                        value = dict(row)
                        key = (str(value["component"]), str(value["worker_id"]))
                        previous = heartbeats.get(key)
                        if previous is None or (timestamp_ms(value.get("last_seen_at")) or 0) > (timestamp_ms(previous.get("last_seen_at")) or 0):
                            heartbeats[key] = value
            except sqlite3.Error:
                continue

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

        expected = self._expected_components()
        for key, config in expected.items():
            if not config["enabled"]:
                continue
            worker_id = "worker" if key == "binance_square" else key
            heartbeats.setdefault((key, worker_id), {"component": key, "worker_id": worker_id, "status": "missing",
                "last_seen_at": None, "last_success_at": None, "last_error": "未发现心跳记录", "metadata": "{}"})

        for (component, worker_id), row in heartbeats.items():
            expected_config = expected.get(component)
            if expected_config is not None and not expected_config["enabled"]:
                continue
            if expected_config is None and not self._component_expected(component):
                continue
            interval_minutes = expected_config["interval_minutes"] if expected_config else self._component_interval_minutes(component)
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

    def _component_expected(self, component: str) -> bool:
        config = self._expected_components().get(component)
        return bool(config["enabled"]) if config is not None else True

    def _component_interval_minutes(self, component: str) -> int:
        table_column = {
            "x_capture": ("x_capture_settings", "global_interval_seconds"),
            "jin10_monitor": ("jin10_settings", "interval_seconds"),
        }.get(component)
        if table_column is None:
            return 5
        table, column = table_column
        try:
            with self._connect() as conn:
                row = conn.execute(f"SELECT {column} FROM {table} LIMIT 1").fetchone()
            return max(1, int(row[0] or 60) // 60)
        except (sqlite3.Error, TypeError, ValueError):
            return 5

    def _coverage_gaps(self) -> list[dict[str, Any]]:
        issues = []
        if self.competitor_database_path is None or not self.competitor_database_path.exists():
            return issues
        with self._connect_path(self.competitor_database_path) as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "newsflash_events" not in tables:
                return []
            rows = conn.execute("SELECT event_id,representative_title,first_source,first_published_at,source_count,competitor_source_count,has_odaily,needs_review,metadata,updated_at FROM newsflash_events WHERE competitor_source_count>0 AND (has_odaily=0 OR needs_review=1) AND julianday(first_published_at)>=julianday('now','-3 day') ORDER BY updated_at DESC LIMIT 100").fetchall()
            for row in rows:
                if int(row["needs_review"] or 0) == 0 and int(row["has_odaily"] or 0) == 1:
                    continue
                evidence = conn.execute("SELECT source,source_item_id,source_url,title,created_at FROM newsflash_event_sources WHERE event_id=? ORDER BY created_at LIMIT 12", (row["event_id"],)).fetchall() if "newsflash_event_sources" in tables else []
                event = dict(row)
                competitor_count = int(event.get("competitor_source_count") or 0)
                issues.append({"identity": {"event_id": event["event_id"]}, "category": "competitor_coverage_gap" if not event.get("has_odaily") else "event_needs_review",
                    "title": str(event.get("representative_title") or "竞品事件待复核"),
                    "summary": "竞品先发或多家竞品事件未关联到Odaily；该事件关联状态只表示需逐条追踪，未发现历史稿件不等于漏闻。",
                    "severity": "medium", "certainty": "low", "expected": "检查订阅资格、采集、轻决定、写作、后台回执与公开关联证据",
                    "actual": f"first_source={event.get('first_source')} competitor_sources={competitor_count} has_odaily={event.get('has_odaily')}",
                    "scope": {"event_id": event["event_id"], "first_source": event.get("first_source")}, "affected_count": max(1, competitor_count),
                    "evidence": [dict(item) for item in evidence], "counter_evidence": [{"metadata": event.get("metadata")}],
                    "suggestion": "按原帖ID与URL逐项核对当时启用配置、采集结果、停止原因、稿件和回执；证据不足保持无法判断",
                    "validation": "每个原始事件均关联到明确终点，或标出缺失证据与无法确认项"})
        return issues

    def _audit_findings(self) -> list[dict[str, Any]]:
        issues = []
        findings = []
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
        return issues

    def _rule_conflicts(self) -> list[dict[str, Any]]:
        issues = []
        requirements = ("固定开头", "必须后台", "必须挂后台", "固定格式", "严格json", "严格json输出")
        prohibitions = ("不得固定开头", "禁止固定开头", "不得挂后台", "禁止挂后台", "不要后台", "禁止严格json")
        with self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='prompt_template_versions'").fetchone()
            if not exists:
                return []
            active_rows = conn.execute(
                "SELECT pt.template_key,pt.active_version_id,pt.feature_mode_enabled,pt.feature_mode_text,pv.version_number,pv.content "
                "FROM prompt_templates pt LEFT JOIN prompt_template_versions pv ON pv.id=pt.active_version_id"
            ).fetchall()
            attached = conn.execute("SELECT singleton_key,config_json,prompt_text,updated_at FROM publisher_rule_config").fetchall() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='publisher_rule_config'"
            ).fetchone() else []
        for row in active_rows:
            if not row["active_version_id"]:
                continue
            additions = [
                ("publisher_rule_config", str(config["singleton_key"]), str(config["prompt_text"] or config["config_json"] or ""))
                for config in attached if str(config["prompt_text"] or config["config_json"] or "").strip()
            ]
            for origin, key, addition in additions:
                base = str(row["content"] or "") + ("\n" + str(row["feature_mode_text"] or "") if row["feature_mode_enabled"] else "")
                if not addition or not any(term in base for term in requirements) or not any(term in addition for term in prohibitions + requirements):
                    continue
                positive = next((line.strip() for line in base.splitlines() if any(term in line for term in requirements)), "")
                opposite = next((line.strip() for line in addition.splitlines() if any(term in line for term in prohibitions + requirements)), "")
                identity = {"template_key": str(row["template_key"]), "version": int(row["version_number"]), "attachment": origin, "attachment_key": key, "positive_sha": _digest(positive), "other_sha": _digest(opposite)}
                issues.append({"identity": identity, "category": "possible_rule_conflict", "title": f"模板 {row['template_key']} 与附加规则需要核对",
                    "summary": "同一有效模板和附加规则分别出现可能相反的约束；适用场景仍需人工核实。",
                    "severity": "medium", "certainty": "low", "expected": "同一场景有效规则组合应一致，不同场景可保留不同要求",
                    "actual": f"模板句：{positive[:500]}\n附加规则句：{opposite[:500]}", "scope": identity, "affected_count": 1,
                    "evidence": [{"template_key": row["template_key"], "version": row["version_number"], "feature_mode": row["feature_mode_text"], "template_line": positive, "attachment": origin, "attachment_key": key, "attachment_line": opposite}],
                    "counter_evidence": [], "suggestion": "确认两条规则的来源范围、优先级和适用场景，再仅修复真实重叠的组合",
                    "validation": "对同场景组合执行正反用例，确认不影响其他模板路径"})
        for row in active_rows:
            if not row["active_version_id"]:
                continue
            content = str(row["content"] or "")
            if row["feature_mode_enabled"]:
                content += "\n" + str(row["feature_mode_text"] or "")
            lines = [line.strip() for line in content.splitlines() if line.strip()]
            for required in requirements:
                if required in content and any(prohibition in content for prohibition in prohibitions):
                    contradictory = next((line for line in lines if required in line), required)
                    opposite = next((line for line in lines if any(term in line for term in prohibitions)), "存在方向相反的规则句")
                    if contradictory == opposite:
                        continue
                    identity = {"template_key": str(row["template_key"]), "version": int(row["version_number"]), "required": required, "opposite": opposite}
                    issues.append({"identity": identity, "category": "possible_rule_conflict", "title": f"模板 {row['template_key']} v{row['version_number']} 有规则方向冲突",
                        "summary": "确定性词句扫描找到同一模板中的正向要求和相反限制；是否适用同一业务场景仍需复核。",
                        "severity": "medium", "certainty": "low", "expected": "同一场景的生效规则应一致；不同场景差异可并存",
                        "actual": f"正向句：{contradictory[:500]}\n相反句：{opposite[:500]}", "scope": identity,
                        "affected_count": 1, "evidence": [{"template_key": row["template_key"], "version": row["version_number"], "required_line": contradictory, "opposite_line": opposite}],
                        "counter_evidence": [], "suggestion": "核对适用场景、附加规则和版本组合；仅修正真实重叠的规则",
                        "validation": "用同场景正反案例分别执行确定性校验并检查输出"})
        return issues

    def _failure_clusters(self) -> list[dict[str, Any]]:
        issues = []
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        with self._connect() as conn:
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "x_processing_attempts" in tables:
                rows = conn.execute(
                    "SELECT a.id,a.task_id,a.stage,a.attempt_no,a.status,a.error_code,a.error_message,a.started_at,a.finished_at,t.source "
                    "FROM x_processing_attempts a JOIN tasks t ON t.id=a.task_id WHERE a.status IN ('failed','stopped') ORDER BY a.id DESC LIMIT 3000"
                ).fetchall()
                for row in rows:
                    value = dict(row)
                    message = re.sub(r"\b\d{3,}\b", "#", str(value.get("error_message") or "unknown"))[:240]
                    key = (str(value.get("source") or "unknown"), str(value.get("stage") or "unknown"), str(value.get("error_code") or "unknown") + ":" + message)
                    groups.setdefault(key, []).append(value)
            if "tasks" in tables and "x_task_pipeline" in tables:
                rows = conn.execute("SELECT t.id,t.source,t.status,t.updated_at,p.last_error FROM tasks t JOIN x_task_pipeline p ON p.task_id=t.id WHERE (t.status LIKE '%failed' OR p.last_error IS NOT NULL) ORDER BY t.updated_at DESC LIMIT 1500").fetchall()
                for row in rows:
                    value = dict(row)
                    message = re.sub(r"\b\d{3,}\b", "#", str(value.get("last_error") or "unknown"))[:240]
                    key = (str(value.get("source") or "unknown"), "current_status", message)
                    groups.setdefault(key, []).append(value)
        if self.local_pipeline_database_path and self.local_pipeline_database_path.exists():
            try:
                with self._connect_path(self.local_pipeline_database_path) as conn:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_pipeline_jobs'").fetchone():
                        for row in conn.execute("SELECT id,source,status,attempts,last_error,updated_at FROM local_pipeline_jobs WHERE last_error IS NOT NULL ORDER BY updated_at DESC LIMIT 1000"):
                            value = dict(row)
                            key = (str(value.get("source") or "unknown"), "local_pipeline", str(value.get("last_error") or "unknown")[:240])
                            groups.setdefault(key, []).append(value)
            except sqlite3.Error:
                pass
        for (source, stage, message), rows_in_cluster in groups.items():
            ids = {row.get("task_id", row.get("id")) for row in rows_in_cluster}
            affected = len(ids)
            succeeded_task_ids = set()
            for row in rows_in_cluster:
                task_id = row.get("task_id")
                if task_id is not None:
                    try:
                        if str(row.get("status")) == "failed":
                            with self._connect() as conn:
                                state = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
                            if state and not str(state["status"]).endswith("failed"):
                                succeeded_task_ids.add(task_id)
                    except sqlite3.Error:
                        continue
            recovered = len(succeeded_task_ids)
            identity = {"source": source, "stage": stage, "error_signature": message}
            issues.append({"identity": identity, "category": "failure_recovery_cluster", "title": f"{source} · {stage} 错误聚合",
                "summary": f"影响记录 {affected} 条；观察到重试后恢复 {recovered} 条；仍失败状态 {max(0, affected - recovered)} 条。无法确认的根因保持未知。",
                "severity": "high" if affected >= 10 else "medium", "certainty": "medium",
                "expected": "保留每次失败和后续重试结果，并汇总恢复与仍失败数量",
                "actual": f"错误码与摘要：{message}；观察记录数：{len(rows_in_cluster)}", "scope": identity, "affected_count": affected,
                "evidence": rows_in_cluster[:30], "counter_evidence": [{"recovered_count": recovered}],
                "suggestion": "按事件时间核实失败尝试后是否出现成功记录；不要仅凭当前last_error推断责任方",
                "validation": "聚合的影响数、恢复数和仍失败数与原始尝试及任务当前状态相符"})
        return issues

    def analyze_pending(self, limit: int = 12) -> dict[str, int]:
        analyzed = failed = deferred = 0
        for item in self.repository.pending_analysis(limit):
            material = {key: item.get(key) for key in ("task_kind", "category", "title", "summary", "scope_json", "expected", "actual", "evidence_json", "counter_evidence_json", "suggestion", "validation")}
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
                result = self._analyze(item, attempt_id=attempt_id, on_usage=settle_usage)
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
        prompt = _json({"expected": item.get("expected"), "actual": item.get("actual"), "evidence": item.get("evidence_json"), "counter_evidence": item.get("counter_evidence_json"), "deterministic_context": item.get("summary"), "initial_suggestion": item.get("suggestion"), "task_kind": item.get("task_kind")})
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
                compacted = self.service.information_store.cleanup_expired_evidence(dry_run=False, limit=200)
                orphaned = self.service.information_store.prune_orphan_evidence(dry_run=False, limit=500)
                self.service.repository.finish_run(run_key, status="completed", items_created=compacted["compacted"] + orphaned["files"])
                self.service.repository.set_state("last_storage_maintenance_date", now.date().isoformat())
                results["storage_retention"] = {"compacted": compacted["compacted"], "orphaned": orphaned["files"]}
            except Exception as exc:
                self.service.repository.finish_run(run_key, status="failed", error=f"{type(exc).__name__}: {exc}")
                raise
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.service.repository.set_state("worker_last_error", f"{type(exc).__name__}: {exc}"[:1000])
                print(f"[odaily] system review round failed: {type(exc).__name__}: {exc}")
            self._stop.wait(self.interval_seconds)
