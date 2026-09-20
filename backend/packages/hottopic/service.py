"""Production-owned HotTopic worker and SQLite read/write boundary.

This module deliberately owns a separate database.  It never imports the
OdAIly X capture repository or writes to the main application database.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from packages.common.paths import ensure_runtime_dirs, get_paths

from .capture import AccountRow, ContentItem, scan_account
from .topic_aggregator import ModelBriefWriter, TopicAggregator


POLL_INTERVAL_SECONDS = 600
TRANSIENT_RETENTION_HOURS = 48
EVENT_RETENTION_DAYS = 5
MAINTENANCE_INTERVAL = timedelta(hours=1)
ACCOUNT_CLEANUP_BATCH_SIZE = 100
AGGREGATION_BATCH_SIZE = 100
BLACKLISTED_HANDLES = {
    "nikkei", "polymarketmoney", "cb_doge", "teamtrump", "skaas777",
    "pr0h0s", "fxtrader", "big_pharmai", "acboxliu", "notthreadguy",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS hottopic_meta (
  singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'global'),
  deployment_started_at TEXT NOT NULL,
  last_maintenance_at TEXT
);
CREATE TABLE IF NOT EXISTS hottopic_accounts (
  screen_name TEXT PRIMARY KEY,
  screen_name_lower TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  protected INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('followed','unfollowed','blacklisted')),
  next_due_at TEXT,
  last_polled_at TEXT,
  last_success_at TEXT,
  last_error TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_item_count INTEGER NOT NULL DEFAULT 0,
  cumulative_content_count INTEGER NOT NULL DEFAULT 0,
  cumulative_hot_topic_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hottopic_accounts_due
  ON hottopic_accounts(status, next_due_at);
CREATE TABLE IF NOT EXISTS hottopic_inbox (
  tweet_id TEXT PRIMARY KEY,
  screen_name_lower TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  collected_at TEXT NOT NULL,
  processed_at TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_hottopic_inbox_pending
  ON hottopic_inbox(processed_at, collected_at);
CREATE TABLE IF NOT EXISTS hottopic_account_cleanup (
  screen_name_lower TEXT PRIMARY KEY REFERENCES hottopic_accounts(screen_name_lower),
  queued_at TEXT NOT NULL,
  last_error TEXT
);
CREATE TABLE IF NOT EXISTS hottopic_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hottopic_events_at ON hottopic_events(at);
"""


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def normalize_handle(value: str) -> str:
    handle = value.strip().lstrip("@").split("/", 1)[0]
    if not handle or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in handle):
        raise ValueError("X 用户名格式无效")
    return handle


def open_worker_database(path: Path) -> sqlite3.Connection:
    """Open a long-lived WAL connection; common.connect_sqlite closes on `with`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


class HotTopicService:
    def __init__(self, database_path: Path | None = None, *, model: str | None = None, workers: int = 8) -> None:
        paths = get_paths()
        ensure_runtime_dirs(paths)
        self.path = database_path or paths.runtime_dir / "hottopic.sqlite"
        self.workers = max(1, workers)
        self.db = open_worker_database(self.path)
        self.db.executescript(SCHEMA)
        self._init_meta()
        writer = None
        configured_model = model or os.getenv("HOTTOPIC_MODEL", "")
        if configured_model:
            try:
                writer = ModelBriefWriter(configured_model)
            except RuntimeError:
                # The topic engine remains useful during credential incidents;
                # active topics stay visible with their working title.
                writer = None
        self.aggregator = TopicAggregator(self.path, brief_writer=writer)
        self.lock = threading.Lock()

    def close(self) -> None:
        self.aggregator.close()
        self.db.close()

    def _init_meta(self) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO hottopic_meta(singleton_key,deployment_started_at) VALUES('global',?)",
                (iso(utc_now()),),
            )

    def deployment_started_at(self) -> str:
        return str(self.db.execute("SELECT deployment_started_at FROM hottopic_meta WHERE singleton_key='global'").fetchone()[0])

    def event(self, kind: str, detail: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO hottopic_events(at,kind,detail_json) VALUES(?,?,?)",
                (iso(utc_now()), kind, json.dumps(detail, ensure_ascii=False)),
            )

    def seed_accounts(self, rows: Iterable[dict[str, str]]) -> int:
        now = iso(utc_now())
        inserted = 0
        with self.db:
            for row in rows:
                handle = normalize_handle(str(row.get("screen_name") or ""))
                lower = handle.lower()
                status = "blacklisted" if lower in BLACKLISTED_HANDLES else "followed"
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO hottopic_accounts("
                    "screen_name,screen_name_lower,display_name,status,next_due_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?)",
                    (handle, lower, str(row.get("display_name") or row.get("name") or ""), status,
                     now if status == "followed" else None, now, now),
                )
                inserted += cursor.rowcount
        return inserted

    def seed_from_csv(self, csv_path: Path) -> int:
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            return self.seed_accounts(csv.DictReader(source))

    def list_accounts(self, *, query: str = "", status: str = "all") -> list[dict[str, Any]]:
        clauses, params = [], []
        if status in {"followed", "unfollowed", "blacklisted"}:
            clauses.append("status=?")
            params.append(status)
        if query.strip():
            clauses.append("(screen_name_lower LIKE ? OR lower(display_name) LIKE ?)")
            value = f"%{query.strip().lower()}%"
            params.extend([value, value])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.execute(
            "SELECT screen_name,screen_name_lower,display_name,protected,status,last_polled_at,last_success_at,last_error,"
            "consecutive_failures,last_item_count,cumulative_content_count,cumulative_hot_topic_count "
            f"FROM hottopic_accounts{where} "
            "ORDER BY CASE status WHEN 'followed' THEN 0 WHEN 'unfollowed' THEN 1 ELSE 2 END, "
            "cumulative_hot_topic_count DESC,cumulative_content_count DESC,screen_name_lower",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def add_account(self, handle: str, display_name: str = "") -> dict[str, Any]:
        normalized = normalize_handle(handle)
        lower, now = normalized.lower(), iso(utc_now())
        seeded_as_blacklisted = False
        with self.db:
            existing = self.db.execute("SELECT status FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
            if existing and existing["status"] == "blacklisted":
                raise ValueError("账号已拉黑，请先解除拉黑")
            if existing is None and lower in BLACKLISTED_HANDLES:
                self.db.execute(
                    "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (normalized, lower, display_name.strip(), "blacklisted", now, now),
                )
                seeded_as_blacklisted = True
            if not seeded_as_blacklisted:
                self.db.execute(
                    "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(screen_name_lower) DO UPDATE SET "
                    "screen_name=excluded.screen_name,display_name=excluded.display_name,status='followed',next_due_at=excluded.next_due_at,updated_at=excluded.updated_at",
                    (normalized, lower, display_name.strip(), "followed", now, now, now),
                )
        if seeded_as_blacklisted:
            raise ValueError("账号已拉黑，请先解除拉黑")
        return self._account(lower)

    def set_account_status(self, handle: str, status: str) -> dict[str, Any]:
        if status not in {"followed", "unfollowed", "blacklisted"}:
            raise ValueError("不支持的账号状态")
        lower = normalize_handle(handle).lower()
        now = iso(utc_now())
        with self.db:
            row = self.db.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
            if row is None:
                raise ValueError("账号不存在")
            next_due = now if status == "followed" else None
            self.db.execute(
                "UPDATE hottopic_accounts SET status=?,next_due_at=?,updated_at=? WHERE screen_name_lower=?",
                (status, next_due, now, lower),
            )
            if status == "blacklisted":
                # Keep the console operation bounded.  The worker removes the
                # account's transient topic graph in small, resumable batches.
                self.db.execute("DELETE FROM hottopic_inbox WHERE screen_name_lower=?", (lower,))
                self.db.execute(
                    "INSERT INTO hottopic_account_cleanup(screen_name_lower,queued_at,last_error) VALUES(?,?,NULL) "
                    "ON CONFLICT(screen_name_lower) DO UPDATE SET queued_at=excluded.queued_at,last_error=NULL",
                    (lower, now),
                )
        return self._account(lower)

    def _account(self, lower: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
        assert row is not None
        return dict(row)

    def process_account_cleanup_jobs(self, *, job_limit: int = 1) -> dict[str, int]:
        """Process small, resumable blacklist cleanups outside console requests."""
        rows = self.db.execute(
            "SELECT screen_name_lower FROM hottopic_account_cleanup ORDER BY queued_at LIMIT ?",
            (max(1, job_limit),),
        ).fetchall()
        result = {"processed_jobs": 0, "deleted_content_items": 0, "deleted_claims": 0}
        for row in rows:
            lower = str(row["screen_name_lower"])
            try:
                deleted_content, deleted_claims, complete = self._discard_transient_account_material_batch(lower)
            except Exception as exc:
                with self.db:
                    self.db.execute(
                        "UPDATE hottopic_account_cleanup SET last_error=? WHERE screen_name_lower=?",
                        (f"{type(exc).__name__}: {exc}", lower),
                    )
                continue
            result["processed_jobs"] += 1
            result["deleted_content_items"] += deleted_content
            result["deleted_claims"] += deleted_claims
            if complete:
                with self.db:
                    self.db.execute("DELETE FROM hottopic_account_cleanup WHERE screen_name_lower=?", (lower,))
        return result

    def _discard_transient_account_material_batch(self, lower: str) -> tuple[int, int, bool]:
        """Delete at most one small batch while retaining permanent evidence."""
        state = self.aggregator.connection
        state.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                row[0]
                for row in state.execute(
                    "SELECT ci.content_item_id FROM content_items ci WHERE lower(ci.activity_account)=? AND NOT EXISTS ("
                    "SELECT 1 FROM claims cl JOIN memberships m ON m.claim_id=cl.claim_id JOIN topics t ON t.topic_id=m.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id AND m.superseded_by IS NULL AND t.retention_tier='permanent') "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM claims cl JOIN topic_evidence te ON te.claim_id=cl.claim_id JOIN topics t ON t.topic_id=te.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id AND t.retention_tier='permanent') "
                    "LIMIT ?",
                    (lower, ACCOUNT_CLEANUP_BATCH_SIZE),
                ).fetchall()
            ]
            if not ids:
                state.commit()
                return 0, 0, True
            marks = ",".join("?" for _ in ids)
            claim_ids = [
                row[0] for row in state.execute(
                    f"SELECT claim_id FROM claims WHERE content_item_id IN ({marks})", ids
                ).fetchall()
            ]
            if claim_ids:
                claim_marks = ",".join("?" for _ in claim_ids)
                state.execute(
                    f"DELETE FROM topic_evidence WHERE claim_id IN ({claim_marks}) "
                    "AND topic_id IN (SELECT topic_id FROM topics WHERE retention_tier!='permanent')",
                    claim_ids,
                )
                state.execute(f"DELETE FROM memberships WHERE claim_id IN ({claim_marks})", claim_ids)
                state.execute(f"DELETE FROM decision_audit WHERE claim_id IN ({claim_marks})", claim_ids)
                state.execute(f"DELETE FROM claims WHERE claim_id IN ({claim_marks})", claim_ids)
            state.execute(f"DELETE FROM content_items WHERE content_item_id IN ({marks})", ids)
            state.execute(
                "DELETE FROM topic_participations WHERE lower(activity_account)=? "
                "AND topic_id IN (SELECT topic_id FROM topics WHERE retention_tier!='permanent')",
                (lower,),
            )
            state.commit()
            return len(ids), len(claim_ids), len(ids) < ACCOUNT_CLEANUP_BATCH_SIZE
        except Exception:
            state.rollback()
            raise

    def _due_accounts(self) -> list[AccountRow]:
        rows = self.db.execute(
            "SELECT screen_name,display_name,protected FROM hottopic_accounts "
            "WHERE status='followed' AND (next_due_at IS NULL OR next_due_at<=?) "
            "ORDER BY COALESCE(next_due_at,''),screen_name_lower LIMIT ?",
            (iso(utc_now()), self.workers * 2),
        ).fetchall()
        return [AccountRow(screen_name=row["screen_name"], name=row["display_name"], description="", followers_count=0,
                           statuses_count=0, protected=bool(row["protected"]), url=f"https://x.com/{row['screen_name']}") for row in rows]

    def poll_once(self) -> dict[str, int]:
        accounts = self._due_accounts()
        if not accounts:
            self.aggregate()
            self.process_account_cleanup_jobs()
            self.maintain()
            return {"polled": 0, "accepted": 0}
        cutoff = datetime.fromisoformat(self.deployment_started_at())
        accepted = 0
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hottopic") as executor:
            futures = {executor.submit(scan_account, account, cutoff=cutoff, timeline_count=100, retries=1): account for account in accounts}
            for future in as_completed(futures):
                account = futures[future]
                now = iso(utc_now())
                try:
                    items, _latest, error = future.result()
                except Exception as exc:
                    items, error = [], {"kind": "exception", "error": str(exc)}
                added = self._store_poll(account, items, error, now)
                accepted += added
        self.aggregate()
        self.process_account_cleanup_jobs()
        self.maintain()
        return {"polled": len(accounts), "accepted": accepted}

    def _store_poll(self, account: AccountRow, items: list[ContentItem], error: dict[str, Any] | None, at: str) -> int:
        lower = account.screen_name.lower()
        deployment_started = datetime.fromisoformat(self.deployment_started_at())
        # scan_account applies this bound too.  Keep it at the persistence
        # boundary so a future collector cannot accidentally backfill history.
        items = [item for item in items if datetime.fromisoformat(item.created_at_iso) >= deployment_started]
        with self.db:
            current = self.db.execute("SELECT status,consecutive_failures,last_success_at FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
            if current is None or current["status"] != "followed":
                return 0
            accepted = 0
            for item in items:
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO hottopic_inbox(tweet_id,screen_name_lower,payload_json,collected_at) VALUES(?,?,?,?)",
                    (item.tweet_id, lower, json.dumps(item.__dict__, ensure_ascii=False), at),
                )
                accepted += cursor.rowcount
            failures = 0 if not error else int(current["consecutive_failures"] or 0) + 1
            self.db.execute(
                "UPDATE hottopic_accounts SET next_due_at=?,last_polled_at=?,last_success_at=?,last_error=?,"
                "consecutive_failures=?,last_item_count=?,cumulative_content_count=cumulative_content_count+?,updated_at=? "
                "WHERE screen_name_lower=?",
                (iso(utc_now() + timedelta(seconds=POLL_INTERVAL_SECONDS)), at, at if not error else current["last_success_at"],
                 json.dumps(error, ensure_ascii=False) if error else None, failures, len(items), accepted, at, lower),
            )
        if error:
            self.event("collect_error", {"account": account.screen_name, "error": error})
        return accepted

    def aggregate(self) -> None:
        rows = self.db.execute(
            "SELECT tweet_id,payload_json FROM hottopic_inbox WHERE processed_at IS NULL ORDER BY collected_at LIMIT ?",
            (AGGREGATION_BATCH_SIZE,),
        ).fetchall()
        if not rows:
            return
        try:
            payloads = [json.loads(row["payload_json"]) for row in rows]
            result = self.aggregator.process_batch(payloads, utc_now())
            with self.db:
                self.db.executemany("UPDATE hottopic_inbox SET processed_at=? WHERE tweet_id=?", [(iso(utc_now()), row["tweet_id"]) for row in rows])
            self._refresh_hot_topic_counts()
            self.event("aggregate_ok", result.get("metrics", {}))
        except Exception as exc:
            self.event("aggregate_error", {"error": str(exc)})

    def _refresh_hot_topic_counts(self) -> None:
        with self.db:
            self.db.execute(
                "UPDATE hottopic_accounts SET cumulative_hot_topic_count=("
                "SELECT COUNT(DISTINCT p.topic_id) FROM topic_participations p JOIN topics t ON t.topic_id=p.topic_id "
                "WHERE lower(p.activity_account)=hottopic_accounts.screen_name_lower AND t.retention_tier='permanent')"
            )

    def maintain(self, *, force: bool = False) -> dict[str, int] | None:
        row = self.db.execute("SELECT last_maintenance_at FROM hottopic_meta WHERE singleton_key='global'").fetchone()
        current = utc_now()
        if not force and row[0] and current - datetime.fromisoformat(row[0]) < MAINTENANCE_INTERVAL:
            return None
        reconciled = self.aggregator.reconcile_recent_topics(current)
        self._refresh_hot_topic_counts()
        state = self.aggregator.prune_transient_state(current, retention_hours=TRANSIENT_RETENTION_HOURS)
        inbox_cutoff = iso(current - timedelta(hours=TRANSIENT_RETENTION_HOURS))
        event_cutoff = iso(current - timedelta(days=EVENT_RETENTION_DAYS))
        with self.db:
            inbox = self.db.execute("DELETE FROM hottopic_inbox WHERE processed_at IS NOT NULL AND collected_at<?", (inbox_cutoff,)).rowcount
            events = self.db.execute("DELETE FROM hottopic_events WHERE at<?", (event_cutoff,)).rowcount
            self.db.execute("UPDATE hottopic_meta SET last_maintenance_at=? WHERE singleton_key='global'", (iso(current),))
        return {**state, "deleted_inbox": inbox, "deleted_events": events, "merged_topics": len(reconciled["topic_merges"])}

    def health(self) -> dict[str, Any]:
        counts = self.db.execute(
            "SELECT COUNT(*) total,SUM(status='followed') followed,SUM(status='unfollowed') unfollowed,SUM(status='blacklisted') blacklisted,"
            "SUM(last_error IS NOT NULL) errors FROM hottopic_accounts"
        ).fetchone()
        topic_counts = self.db.execute("SELECT matching_status,COUNT(*) FROM topics GROUP BY matching_status").fetchall()
        return {"deploymentStartedAt": self.deployment_started_at(), "accounts": dict(counts),
                "inboxPending": self.db.execute("SELECT COUNT(*) FROM hottopic_inbox WHERE processed_at IS NULL").fetchone()[0],
                "topics": dict(topic_counts), "databasePath": str(self.path)}

    def dashboard(self) -> dict[str, Any]:
        rows = self.db.execute(
            "SELECT t.topic_id,t.working_title,t.started_at,t.first_seen_at,t.last_evidence_at,t.last_participation_at,"
            "t.participant_count_1h,t.participant_count_6h,t.participant_count_24h,t.participant_velocity,t.hotness_score,"
            "b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
            "WHERE t.matching_status='active' AND t.visibility='visible' ORDER BY t.hotness_score DESC,t.last_evidence_at DESC LIMIT 100"
        ).fetchall()
        topics = [{"id": row["topic_id"], "title": row["title"] or row["working_title"], "brief": row["brief"] or "正文生成中",
                   "startedAt": row["started_at"], "firstSeenAt": row["first_seen_at"], "lastEvidenceAt": row["last_evidence_at"],
                   "lastParticipationAt": row["last_participation_at"], "hotness": row["hotness_score"],
                   "briefGeneratedAt": row["generated_at"], "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"], "twentyFourHours": row["participant_count_24h"], "velocity": row["participant_velocity"]}}
                  for row in rows]
        return {"generatedAt": iso(utc_now()), "health": self.health(), "topics": topics}

    def topic_detail(self, topic_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT t.*,b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) WHERE t.topic_id=?", (topic_id,)
        ).fetchone()
        if row is None:
            return None
        speakers = self.db.execute(
            "SELECT p.activity_account,p.last_participation_at,ci.source_url FROM topic_participations p "
            "LEFT JOIN content_items ci ON ci.content_item_id=p.last_content_item_id WHERE p.topic_id=? ORDER BY p.last_participation_at DESC", (topic_id,)
        ).fetchall()
        return {"id": row["topic_id"], "title": row["title"] or row["working_title"], "brief": row["brief"] or "正文生成中",
                "hotness": row["hotness_score"], "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"], "twentyFourHours": row["participant_count_24h"]},
                "speakers": [{"account": item["activity_account"], "lastParticipationAt": item["last_participation_at"], "sourceUrl": item["source_url"]} for item in speakers]}


def run_worker(*, database_path: Path | None = None, seed_path: Path | None = None, once: bool = False, model: str | None = None, workers: int = 8) -> int:
    service = HotTopicService(database_path, model=model, workers=workers)
    try:
        if seed_path and not service.list_accounts():
            count = service.seed_from_csv(seed_path)
            service.event("seeded_accounts", {"count": count, "seed_path": seed_path.name})
        if once:
            print(json.dumps(service.poll_once(), ensure_ascii=False))
            return 0
        while True:
            service.poll_once()
            time.sleep(2)
    finally:
        service.close()
