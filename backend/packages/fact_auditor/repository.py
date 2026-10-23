from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite

from .prompts import POLICY_VERSION


LABELS = {"unreviewed": "未审查", "pending": "待查", "processing": "审查中", "skipped": "无需审查", "exempt": "豁免",
          "passed": "未发现问题", "flagged": "发现疑点", "unverifiable": "无法确认", "failed": "执行失败"}
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fact_checks (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_item_id TEXT NOT NULL, content_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
 title TEXT, content TEXT NOT NULL, source_url TEXT, source_locator TEXT NOT NULL DEFAULT '', published_at TEXT,
 status TEXT NOT NULL DEFAULT 'pending', model TEXT NOT NULL DEFAULT 'gpt-6-luna',
 reason TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
 original_text TEXT NOT NULL DEFAULT '', original_url TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
 locked_by TEXT, locked_until TEXT, next_attempt_at TEXT, last_error TEXT,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_item_id,content_hash,policy_version)
);
CREATE INDEX IF NOT EXISTS idx_fact_checks_source ON fact_checks(source_item_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_fact_checks_due ON fact_checks(status,next_attempt_at,locked_until);
CREATE TABLE IF NOT EXISTS fact_check_runs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, check_id INTEGER NOT NULL REFERENCES fact_checks(id),
 status TEXT NOT NULL, summary TEXT NOT NULL, result_json TEXT NOT NULL,
 completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_check_runs_check ON fact_check_runs(check_id,id DESC);
"""


def locator_sql(alias: str = "r") -> str:
    return (f"COALESCE(NULLIF(CASE WHEN json_valid({alias}.raw_payload) THEN "
            f"json_extract({alias}.raw_payload,'$.sourceUrl') END,''),{alias}.source_url,'')")


def current_join() -> str:
    return f"""LEFT JOIN fact_checks fc ON fc.id=(SELECT c.id FROM fact_checks c
        WHERE c.source_item_id=r.source_item_id AND c.title IS r.title AND c.content=r.content
        AND c.source_locator={locator_sql()} AND c.policy_version='{POLICY_VERSION}' ORDER BY c.id DESC LIMIT 1)"""


def summary_from_row(row: dict[str, Any]) -> dict[str, Any]:
    status = row.get("fact_status") or "unreviewed"
    return {"id": row.get("fact_check_id"), "status": status, "label": LABELS.get(status, status),
            "reason": row.get("fact_reason") or "", "summary": row.get("fact_summary") or "",
            "updated_at": row.get("fact_updated_at")}


def decode_check(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["result"] = json.loads(result.pop("result_json") or "{}")
    result["label"] = LABELS.get(result["status"], result["status"])
    return result


class FactReviewRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        with connect_sqlite(path) as conn:
            conn.executescript(SCHEMA_SQL)

    def claim(self, *, worker_id: str, lookback_minutes: int, max_attempts: int, start_after: str = "") -> dict[str, Any] | None:
        now = datetime.now(UTC)
        now_iso = now.isoformat()
        cutoff = (now - timedelta(minutes=lookback_minutes)).isoformat()
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""UPDATE fact_checks SET status='failed',last_error='Worker lease expired; retry limit reached',
                summary='执行中断且已达到重试上限',locked_by=NULL,locked_until=NULL,updated_at=?
                WHERE status='processing' AND attempts>=? AND julianday(locked_until)<julianday(?)""", (now_iso, max_attempts, now_iso))
            # A changed title/body/source gets a new immutable review snapshot.
            row = conn.execute(f"""SELECT r.*, {locator_sql()} AS source_locator FROM odaily_reference_items r
                WHERE r.content<>'' AND julianday(r.published_at)>=julianday(?)
                AND julianday(r.published_at)<=julianday(?)
                AND (?='' OR julianday(r.published_at)>=julianday(?))
                AND NOT EXISTS(SELECT 1 FROM fact_checks c WHERE c.source_item_id=r.source_item_id
                  AND c.title IS r.title AND c.content=r.content AND c.source_locator={locator_sql()}
                  AND c.policy_version=?)
                ORDER BY julianday(r.published_at) DESC LIMIT 1""",
                (cutoff, now_iso, start_after, start_after, POLICY_VERSION)).fetchone()
            if row:
                digest = hashlib.sha256(json.dumps([row["title"], row["content"], row["source_locator"]], ensure_ascii=False).encode()).hexdigest()
                conn.execute("""INSERT OR IGNORE INTO fact_checks
                    (source_item_id,content_hash,policy_version,title,content,source_url,source_locator,published_at)
                    VALUES(?,?,?,?,?,?,?,?)""", (row["source_item_id"], digest, POLICY_VERSION, row["title"], row["content"], row["source_url"], row["source_locator"], row["published_at"]))
            # Ignore obsolete pending/failed versions once the published copy changes.
            selected = conn.execute(f"""SELECT fc.* FROM odaily_reference_items r {current_join()}
                WHERE fc.attempts<?
                  AND (fc.status='pending' OR (fc.status='failed' AND julianday(fc.next_attempt_at)<=julianday(?))
                    OR (fc.status='processing' AND julianday(fc.locked_until)<julianday(?)))
                ORDER BY CASE WHEN fc.status='pending' THEN 0 ELSE 1 END, julianday(fc.published_at) DESC LIMIT 1""",
                (max_attempts, now_iso, now_iso)).fetchone()
            if selected is None:
                conn.commit()
                return None
            lock_until = (now + timedelta(minutes=10)).isoformat()
            conn.execute("UPDATE fact_checks SET status='processing',locked_by=?,locked_until=?,attempts=attempts+1,updated_at=? WHERE id=?",
                         (worker_id, lock_until, now_iso, selected["id"]))
            selected = conn.execute("SELECT * FROM fact_checks WHERE id=?", (selected["id"],)).fetchone()
            conn.commit()
            return decode_check(selected)

    def save(self, task: dict[str, Any], result: dict[str, Any], *, worker_id: str) -> None:
        now = datetime.now(UTC)
        retry_at = (now + timedelta(seconds=min(900, 60 * 2 ** task["attempts"]))).isoformat()
        with connect_sqlite(self.path) as conn:
            updated = conn.execute("""UPDATE fact_checks SET status=?,reason=?,summary=?,original_text=?,original_url=?,result_json=?,
                model=?,last_error=?,next_attempt_at=?,locked_by=NULL,locked_until=NULL,updated_at=? WHERE id=? AND locked_by=?""",
                (result["status"], result.get("reason", ""), result.get("summary", ""), result.get("original_text", ""),
                 result.get("original_url", ""), json.dumps(result, ensure_ascii=False), result.get("model", "gpt-6-luna"),
                 result.get("error"), retry_at if result["status"] == "failed" else None, now.isoformat(), task["id"], worker_id))
            if updated.rowcount:
                conn.execute("INSERT INTO fact_check_runs(check_id,status,summary,result_json,completed_at) VALUES(?,?,?,?,?)",
                    (task["id"], result["status"], result.get("summary", ""), json.dumps(result, ensure_ascii=False), now.isoformat()))
            conn.commit()

    def retry(self, check_id: int) -> dict[str, Any]:
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(f"SELECT fc.id,fc.status,fc.source_item_id FROM odaily_reference_items r {current_join()} WHERE fc.id=?", (check_id,)).fetchone()
            if row is None:
                raise ValueError("只能重新审查当前稿件版本")
            if row["status"] in {"pending", "processing"}:
                raise ValueError("该稿件已在等待或执行审查")
            conn.execute("""UPDATE fact_checks SET status='pending',attempts=0,next_attempt_at=NULL,last_error=NULL,
                locked_by=NULL,locked_until=NULL,result_json='{}',summary='已请求重新审查',updated_at=? WHERE id=?""",
                (datetime.now(UTC).isoformat(), check_id))
            conn.commit()
            return {"check_id": check_id, "source_item_id": row["source_item_id"], "status": "pending"}

    def detail(self, source_item_id: str, check_id: int | None = None) -> dict[str, Any]:
        with connect_sqlite(self.path) as conn:
            current = conn.execute(f"SELECT r.*,fc.id AS current_check_id FROM odaily_reference_items r {current_join()} WHERE r.source_item_id=?", (source_item_id,)).fetchone()
            if current is None:
                raise ValueError("newsflash not found")
            target = check_id if check_id is not None else current["current_check_id"]
            row = conn.execute("SELECT * FROM fact_checks WHERE id=? AND source_item_id=?", (target, source_item_id)).fetchone() if target else None
            if check_id is not None and row is None:
                raise ValueError("fact review not found")
            history = [dict(x) for x in conn.execute("SELECT id,status,summary,updated_at FROM fact_checks WHERE source_item_id=? ORDER BY id DESC LIMIT 20", (source_item_id,))]
            runs = [dict(x) for x in conn.execute("SELECT id,status,summary,completed_at FROM fact_check_runs WHERE check_id=? ORDER BY id DESC LIMIT 20", (target,))] if target else []
        return {"check": decode_check(row) if row else None,
                "published": {key: current[key] for key in ("source_item_id", "title", "content", "published_at")},
                "is_current": bool(row and row["id"] == current["current_check_id"]), "history": history, "runs": runs}
