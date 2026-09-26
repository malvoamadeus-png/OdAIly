"""Production-owned HotTopic worker and SQLite read/write boundary.

This module deliberately owns a separate database.  It never imports the
OdAIly X capture repository or writes to the main application database.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

from packages.common.paths import ensure_runtime_dirs, get_paths
from packages.x_agent import XAgentAnalyzer, is_relevant

from .capture import AccountRow, ContentItem, scan_account
from .topic_aggregator import ModelBriefWriter, TopicAggregator


POLL_INTERVAL_SECONDS = 600
DEFAULT_COLLECT_MIN_REQUEST_INTERVAL_SECONDS = 2.0
DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS = 300.0
MAX_RATE_LIMIT_ACCOUNT_BACKOFF_SECONDS = 6 * 60 * 60
TRANSIENT_RETENTION_HOURS = 48
EVENT_RETENTION_DAYS = 5
MAINTENANCE_INTERVAL = timedelta(hours=1)
ACCOUNT_CLEANUP_BATCH_SIZE = 100
AGGREGATION_BATCH_SIZE = 100
X_AGENT_ANALYSIS_BATCH_SIZE = 12
X_AGENT_ANALYSIS_MAX_ATTEMPTS = 3
X_AGENT_RETRY_DELAY = timedelta(minutes=15)
X_AGENT_PROCESSING_LEASE = timedelta(minutes=5)
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
  hot_topic_enabled INTEGER NOT NULL DEFAULT 1,
  market_sentiment_enabled INTEGER NOT NULL DEFAULT 0,
  project_promotion_enabled INTEGER NOT NULL DEFAULT 0,
  last_x_agent_analyzed_at TEXT,
  last_x_agent_error TEXT,
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

X_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS x_agent_analysis_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT NOT NULL,
  module TEXT NOT NULL CHECK (module IN ('market_sentiment','project_promotion')),
  status TEXT NOT NULL CHECK (status IN ('pending','processing','succeeded','ignored','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  actual_model TEXT,
  fallback_reason TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(tweet_id, module)
);
CREATE INDEX IF NOT EXISTS idx_x_agent_jobs_pending
  ON x_agent_analysis_jobs(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS x_agent_sentiment_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_id TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  instrument_name TEXT NOT NULL,
  ticker TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL CHECK (scope IN ('大盘','Crypto具体标的','美股具体标的')),
  sentiment TEXT NOT NULL CHECK (sentiment IN ('极度狂热','偏多/乐观','中性/分歧','偏空/谨慎','极度恐慌','证据不足')),
  reason TEXT NOT NULL,
  account_lower TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_text TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  actual_model TEXT NOT NULL,
  fallback_reason TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(tweet_id, instrument_key)
);
CREATE INDEX IF NOT EXISTS idx_x_agent_sentiment_window
  ON x_agent_sentiment_results(posted_at DESC, instrument_key);
CREATE TABLE IF NOT EXISTS x_agent_project_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  identity_key TEXT NOT NULL,
  project_name TEXT NOT NULL,
  ticker TEXT NOT NULL DEFAULT '',
  chain_name TEXT NOT NULL DEFAULT '',
  contract_address TEXT NOT NULL DEFAULT '',
  official_url TEXT NOT NULL DEFAULT '',
  logic TEXT NOT NULL,
  logic_hash TEXT NOT NULL,
  account_lower TEXT NOT NULL,
  source_tweet_id TEXT NOT NULL,
  source_url TEXT NOT NULL,
  source_text TEXT NOT NULL,
  first_mentioned_at TEXT NOT NULL,
  last_mentioned_at TEXT NOT NULL,
  actual_model TEXT NOT NULL,
  fallback_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(identity_key, account_lower, logic_hash)
);
CREATE INDEX IF NOT EXISTS idx_x_agent_projects_window
  ON x_agent_project_observations(last_mentioned_at DESC, identity_key);
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


def configured_seconds(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name) or default)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


class CollectRequestPacer:
    """Space FXTwitter requests across all poll threads in one worker process."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        # Waiters do not reserve a future slot.  A 429 can therefore move the
        # shared deadline while other poll threads are asleep, preventing the
        # rest of the batch from continuing into an upstream cooldown.
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next_request_at - now
                if delay <= 0:
                    self._next_request_at = now + self.min_interval_seconds
                    return
            time.sleep(min(delay, 0.5))

    def defer(self, seconds: float) -> None:
        with self._lock:
            self._next_request_at = max(self._next_request_at, time.monotonic() + max(0.0, seconds))


def _serialized(method: Any) -> Any:
    """Serialize access to the long-lived SQLite connection in the console process."""
    @wraps(method)
    def wrapped(self: "HotTopicService", *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class HotTopicService:
    def __init__(self, database_path: Path | None = None, *, model: str | None = None, workers: int = 8) -> None:
        paths = get_paths()
        ensure_runtime_dirs(paths)
        self.path = database_path or paths.runtime_dir / "hottopic.sqlite"
        self.workers = max(1, workers)
        self.db = open_worker_database(self.path)
        self.db.executescript(SCHEMA)
        self._migrate_x_agent_schema()
        self._init_meta()
        writer = None
        configured_model = model or os.getenv("HOTTOPIC_MODEL") or "gpt-5.6-luna"
        configured_fallback_model = os.getenv("HOTTOPIC_FALLBACK_MODEL") or "gpt-5.6-terra"
        if configured_model:
            try:
                writer = ModelBriefWriter(configured_model, fallback_model=configured_fallback_model)
            except RuntimeError:
                # The topic engine remains useful during credential incidents;
                # active topics stay visible with their working title.
                writer = None
        self.aggregator = TopicAggregator(self.path, brief_writer=writer)
        # The console HTTP server is threaded while this service intentionally
        # owns one long-lived SQLite connection. All exposed read/write methods
        # therefore use this re-entrant lock around their SQLite work.
        self.lock = threading.RLock()
        self.collect_request_pacer = CollectRequestPacer(
            configured_seconds(
                "HOTTOPIC_MIN_REQUEST_INTERVAL_SECONDS",
                DEFAULT_COLLECT_MIN_REQUEST_INTERVAL_SECONDS,
                minimum=0.0,
                maximum=60.0,
            )
        )
        self.rate_limit_cooldown_seconds = configured_seconds(
            "HOTTOPIC_RATE_LIMIT_COOLDOWN_SECONDS",
            DEFAULT_RATE_LIMIT_COOLDOWN_SECONDS,
            minimum=1.0,
            maximum=MAX_RATE_LIMIT_ACCOUNT_BACKOFF_SECONDS,
        )
        self.analysis_workers = max(1, int(os.getenv("X_AGENT_ANALYSIS_WORKERS") or "2"))
        self.x_agent_analyzer_error: str | None = None
        try:
            self.x_agent_analyzer: XAgentAnalyzer | None = XAgentAnalyzer()
        except RuntimeError as exc:
            # Collection and HotTopic aggregation remain available when the
            # low-cost X Agent model route is temporarily unavailable. Pending
            # jobs become visible failures when the worker next sees them.
            self.x_agent_analyzer = None
            self.x_agent_analyzer_error = f"{type(exc).__name__}: {exc}"

    def close(self) -> None:
        self.aggregator.close()
        self.db.close()

    def _init_meta(self) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO hottopic_meta(singleton_key,deployment_started_at) VALUES('global',?)",
                (iso(utc_now()),),
            )

    def _migrate_x_agent_schema(self) -> None:
        """Apply additive migrations to the independently deployed SQLite file."""
        columns = {
            row["name"] for row in self.db.execute("PRAGMA table_info(hottopic_accounts)").fetchall()
        }
        additions = {
            "hot_topic_enabled": "INTEGER NOT NULL DEFAULT 1",
            "market_sentiment_enabled": "INTEGER NOT NULL DEFAULT 0",
            "project_promotion_enabled": "INTEGER NOT NULL DEFAULT 0",
            "last_x_agent_analyzed_at": "TEXT",
            "last_x_agent_error": "TEXT",
        }
        already_had_subscriptions = all(name in columns for name in (
            "hot_topic_enabled",
            "market_sentiment_enabled",
            "project_promotion_enabled",
        ))
        with self.db:
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f"ALTER TABLE hottopic_accounts ADD COLUMN {name} {definition}")
            self.db.executescript(X_AGENT_SCHEMA)
            # A former HotTopic blacklist is not an X Agent account state.
            # During the first migration all such rows receive the normal
            # account-directory default. If a partial prior deployment already
            # added three explicit switches and left every one off, retain that
            # all-off choice while dropping the retired hidden status.
            legacy_rows = self.db.execute(
                "SELECT screen_name_lower,hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled "
                "FROM hottopic_accounts WHERE status='blacklisted'"
            ).fetchall()
            if legacy_rows:
                now = iso(utc_now())
                restored: list[str] = []
                retained_disabled: list[str] = []
                for row in legacy_rows:
                    handle = str(row["screen_name_lower"])
                    all_switches_off = not any(
                        bool(row[name])
                        for name in ("hot_topic_enabled", "market_sentiment_enabled", "project_promotion_enabled")
                    )
                    if already_had_subscriptions and all_switches_off:
                        retained_disabled.append(handle)
                    else:
                        restored.append(handle)
                if restored:
                    marks = ",".join("?" for _ in restored)
                    self.db.execute(
                        "UPDATE hottopic_accounts SET status='followed',hot_topic_enabled=1,"
                        "next_due_at=COALESCE(next_due_at,?),updated_at=? "
                        f"WHERE screen_name_lower IN ({marks})",
                        [now, now, *restored],
                    )
                if retained_disabled:
                    marks = ",".join("?" for _ in retained_disabled)
                    self.db.execute(
                        "UPDATE hottopic_accounts SET status='unfollowed',next_due_at=NULL,updated_at=? "
                        f"WHERE screen_name_lower IN ({marks})",
                        [now, *retained_disabled],
                    )
                # A queued legacy cleanup must not delete a newly restored
                # account's historical topic state on the next worker pass.
                normalized = [str(row["screen_name_lower"]) for row in legacy_rows]
                marks = ",".join("?" for _ in normalized)
                self.db.execute(f"DELETE FROM hottopic_account_cleanup WHERE screen_name_lower IN ({marks})", normalized)

    @_serialized
    def deployment_started_at(self) -> str:
        return str(self.db.execute("SELECT deployment_started_at FROM hottopic_meta WHERE singleton_key='global'").fetchone()[0])

    @_serialized
    def event(self, kind: str, detail: dict[str, Any]) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO hottopic_events(at,kind,detail_json) VALUES(?,?,?)",
                (iso(utc_now()), kind, json.dumps(detail, ensure_ascii=False)),
            )

    @_serialized
    def seed_accounts(self, rows: Iterable[dict[str, str]]) -> int:
        now = iso(utc_now())
        inserted = 0
        with self.db:
            for row in rows:
                handle = normalize_handle(str(row.get("screen_name") or ""))
                lower = handle.lower()
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO hottopic_accounts("
                    "screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                    "market_sentiment_enabled,project_promotion_enabled,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (handle, lower, str(row.get("display_name") or row.get("name") or ""), "followed",
                     now, 1, 0, 0, now, now),
                )
                inserted += cursor.rowcount
        return inserted

    def seed_from_csv(self, csv_path: Path) -> int:
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            return self.seed_accounts(csv.DictReader(source))

    @_serialized
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
            "consecutive_failures,last_item_count,cumulative_content_count,cumulative_hot_topic_count,"
            "hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,last_x_agent_analyzed_at,last_x_agent_error "
            f"FROM hottopic_accounts{where} "
            "ORDER BY CASE status WHEN 'followed' THEN 0 WHEN 'unfollowed' THEN 1 ELSE 2 END, "
            "cumulative_hot_topic_count DESC,cumulative_content_count DESC,screen_name_lower",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def add_account(self, handle: str, display_name: str = "") -> dict[str, Any]:
        normalized = normalize_handle(handle)
        lower, now = normalized.lower(), iso(utc_now())
        with self.db:
            self.db.execute(
                "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                "market_sentiment_enabled,project_promotion_enabled,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(screen_name_lower) DO UPDATE SET "
                "screen_name=excluded.screen_name,display_name=excluded.display_name,status='followed',next_due_at=excluded.next_due_at,"
                "hot_topic_enabled=1,updated_at=excluded.updated_at",
                (normalized, lower, display_name.strip(), "followed", now, 1, 0, 0, now, now),
            )
        return self._account(lower)

    @_serialized
    def set_account_status(self, handle: str, status: str) -> dict[str, Any]:
        if status not in {"followed", "unfollowed"}:
            raise ValueError("不支持的账号状态")
        lower = normalize_handle(handle).lower()
        now = iso(utc_now())
        with self.db:
            row = self.db.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
            if row is None:
                raise ValueError("账号不存在")
            # Compatibility only: map the retired status operation onto the
            # X Agent subscription contract.
            if status == "followed":
                hot_topic_enabled = 1
                market_sentiment_enabled = int(row["market_sentiment_enabled"])
                project_promotion_enabled = int(row["project_promotion_enabled"])
            else:
                hot_topic_enabled, market_sentiment_enabled, project_promotion_enabled = 0, 0, 0
            next_due = now if status == "followed" else None
            self.db.execute(
                "UPDATE hottopic_accounts SET status=?,next_due_at=?,hot_topic_enabled=?,market_sentiment_enabled=?,"
                "project_promotion_enabled=?,updated_at=? WHERE screen_name_lower=?",
                (status, next_due, hot_topic_enabled, market_sentiment_enabled, project_promotion_enabled, now, lower),
            )
        return self._account(lower)

    def _account(self, lower: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
        assert row is not None
        return dict(row)

    @_serialized
    def add_x_agent_account(self, handle: str, display_name: str = "") -> dict[str, Any]:
        """Add an account to the shared X Agent directory with HotTopic enabled."""
        normalized = normalize_handle(handle)
        lower, now = normalized.lower(), iso(utc_now())
        with self.db:
            existing = self.db.execute(
                "SELECT screen_name_lower FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO hottopic_accounts("
                    "screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                    "market_sentiment_enabled,project_promotion_enabled,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (normalized, lower, display_name.strip(), "followed", now, 1, 0, 0, now, now),
                )
            elif display_name.strip():
                self.db.execute(
                    "UPDATE hottopic_accounts SET display_name=?,updated_at=? WHERE screen_name_lower=?",
                    (display_name.strip(), now, lower),
                )
        return self._x_agent_account_payload(self._account(lower))

    @_serialized
    def list_x_agent_accounts(
        self,
        *,
        query: str = "",
        module: str = "all",
        enabled: str | bool = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        column_by_module = {
            "hot_topic": "hot_topic_enabled",
            "market_sentiment": "market_sentiment_enabled",
            "project_promotion": "project_promotion_enabled",
        }
        clauses: list[str] = []
        params: list[Any] = []
        column = column_by_module.get(module)
        if module != "all" and column is None:
            raise ValueError("不支持的 X Agent 模块筛选")
        if query.strip():
            value = f"%{query.strip().lower()}%"
            clauses.append("(screen_name_lower LIKE ? OR lower(display_name) LIKE ?)")
            params.extend([value, value])
        if column and enabled in {True, False, "true", "false"}:
            clauses.append(f"{column}=?")
            params.append(1 if enabled in {True, "true"} else 0)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        safe_limit = min(100, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        total = int(self.db.execute(f"SELECT COUNT(*) FROM hottopic_accounts{where}", params).fetchone()[0])
        rows = self.db.execute(
            "SELECT screen_name,screen_name_lower,display_name,status,hot_topic_enabled,market_sentiment_enabled,"
            "project_promotion_enabled,last_polled_at,last_success_at,last_error,last_x_agent_analyzed_at,last_x_agent_error "
            f"FROM hottopic_accounts{where} "
            "ORDER BY hot_topic_enabled DESC,market_sentiment_enabled DESC,project_promotion_enabled DESC,screen_name_lower "
            "LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [self._x_agent_account_payload(dict(row)) for row in rows], "total": total}

    @_serialized
    def set_x_agent_subscriptions(self, handles: Iterable[str], patch: dict[str, Any]) -> list[dict[str, Any]]:
        allowed = {"hot_topic_enabled", "market_sentiment_enabled", "project_promotion_enabled"}
        changes = {key: value for key, value in patch.items() if key in allowed}
        if not changes:
            raise ValueError("至少提供一个订阅开关")
        if any(not isinstance(value, bool) for value in changes.values()):
            raise ValueError("订阅开关必须是布尔值")
        lowered: list[str] = []
        for handle in handles:
            lower = normalize_handle(str(handle)).lower()
            if lower not in lowered:
                lowered.append(lower)
        if not lowered:
            raise ValueError("至少选择一个账号")
        if len(lowered) > 500:
            raise ValueError("单次最多更新 500 个账号")
        now = iso(utc_now())
        updated: list[dict[str, Any]] = []
        with self.db:
            marks = ",".join("?" for _ in lowered)
            rows = self.db.execute(
                f"SELECT * FROM hottopic_accounts WHERE screen_name_lower IN ({marks})", lowered
            ).fetchall()
            found = {str(row["screen_name_lower"]) for row in rows}
            if missing := [item for item in lowered if item not in found]:
                raise ValueError(f"账号不存在：@{missing[0]}")
            for row in rows:
                values = dict(row)
                values.update(changes)
                any_enabled = any(bool(values[key]) for key in allowed)
                status = "followed" if any_enabled else "unfollowed"
                next_due = now if any_enabled else None
                self.db.execute(
                    "UPDATE hottopic_accounts SET hot_topic_enabled=?,market_sentiment_enabled=?,"
                    "project_promotion_enabled=?,status=?,next_due_at=?,updated_at=? WHERE screen_name_lower=?",
                    (
                        int(bool(values["hot_topic_enabled"])),
                        int(bool(values["market_sentiment_enabled"])),
                        int(bool(values["project_promotion_enabled"])),
                        status,
                        next_due,
                        now,
                        values["screen_name_lower"],
                    ),
                )
                if bool(row["hot_topic_enabled"]) and not bool(values["hot_topic_enabled"]):
                    # A subscription change applies from this point forward.
                    # Do not revive stale inbox rows when HotTopic is turned
                    # back on later; X Agent jobs still retain their source.
                    self.db.execute(
                        "UPDATE hottopic_inbox SET processed_at=? WHERE screen_name_lower=? AND processed_at IS NULL",
                        (now, values["screen_name_lower"]),
                    )
                for module, column in (
                    ("market_sentiment", "market_sentiment_enabled"),
                    ("project_promotion", "project_promotion_enabled"),
                ):
                    if bool(row[column]) and not bool(values[column]):
                        # Work belongs to the former subscription. A currently
                        # executing request is left for _finish_x_agent_job(),
                        # which checks the switch again before saving a result.
                        self.db.execute(
                            "UPDATE x_agent_analysis_jobs SET status='ignored',next_attempt_at=NULL,last_error=NULL,"
                            "completed_at=?,updated_at=? WHERE module=? AND status IN ('pending','failed') "
                            "AND tweet_id IN (SELECT tweet_id FROM hottopic_inbox WHERE screen_name_lower=?)",
                            (now, now, module, values["screen_name_lower"]),
                        )
                # An ignored former subscription no longer represents a live
                # analysis failure. Preserve an error only while another
                # enabled module still has unresolved failed work.
                self._update_x_agent_account_state(values["screen_name_lower"], None, None)
                updated.append(self._x_agent_account_payload(self._account(values["screen_name_lower"])))
        return updated

    @staticmethod
    def _x_agent_account_payload(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "screenName": row["screen_name"],
            "displayName": row.get("display_name") or "",
            "profileUrl": f"https://x.com/{row['screen_name']}",
            "status": str(row.get("status") or "unfollowed"),
            "hotTopicEnabled": bool(row.get("hot_topic_enabled")),
            "marketSentimentEnabled": bool(row.get("market_sentiment_enabled")),
            "projectPromotionEnabled": bool(row.get("project_promotion_enabled")),
            "lastPolledAt": row.get("last_polled_at"),
            "lastSuccessAt": row.get("last_success_at"),
            "lastError": row.get("last_error"),
            "lastAnalyzedAt": row.get("last_x_agent_analyzed_at"),
            "lastAnalysisError": row.get("last_x_agent_error"),
        }

    @_serialized
    def import_x_agent_screening_report(self, report_path: Path, *, apply_suggestions: bool = False) -> dict[str, int]:
        """Create the independent account directory from a local review report.

        Suggestions only overwrite switches when explicitly requested.  Normal
        worker runs never revisit this report, so later manual corrections stay
        authoritative.
        """
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        rows = payload.get("accounts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("筛选报告缺少 accounts 数组")
        now = utc_now()
        inserted = updated = skipped = 0
        with self.db:
            for index, raw in enumerate(rows):
                if not isinstance(raw, dict):
                    skipped += 1
                    continue
                try:
                    handle = normalize_handle(str(raw.get("username") or raw.get("screen_name") or ""))
                except ValueError:
                    skipped += 1
                    continue
                lower = handle.lower()
                judgment = raw.get("judgment") if isinstance(raw.get("judgment"), dict) else {}
                market = int(judgment.get("market_sentiment", {}).get("decision") == "include") if isinstance(judgment.get("market_sentiment"), dict) else 0
                project = int(judgment.get("project_promotion", {}).get("decision") == "include") if isinstance(judgment.get("project_promotion"), dict) else 0
                current = self.db.execute("SELECT screen_name_lower FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
                due = iso(now + timedelta(seconds=index % POLL_INTERVAL_SECONDS))
                if current is None:
                    self.db.execute(
                        "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,"
                        "hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (handle, lower, str(raw.get("display_name") or ""), "followed", due, 1, market, project, iso(now), iso(now)),
                    )
                    inserted += 1
                elif apply_suggestions:
                    self.db.execute(
                        "UPDATE hottopic_accounts SET hot_topic_enabled=1,market_sentiment_enabled=?,project_promotion_enabled=?,"
                        "status='followed',next_due_at=?,updated_at=? WHERE screen_name_lower=?",
                        (market, project, due, iso(now), lower),
                    )
                    updated += 1
                else:
                    skipped += 1
        return {"inserted": inserted, "updated": updated, "skipped": skipped}

    @_serialized
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

    @_serialized
    def _due_accounts(self) -> list[AccountRow]:
        rows = self.db.execute(
            "SELECT screen_name,display_name,protected FROM hottopic_accounts "
            "WHERE status='followed' AND (hot_topic_enabled=1 OR market_sentiment_enabled=1 OR project_promotion_enabled=1) "
            "AND (next_due_at IS NULL OR next_due_at<=?) "
            "ORDER BY COALESCE(next_due_at,''),screen_name_lower LIMIT ?",
            (iso(utc_now()), self.workers * 2),
        ).fetchall()
        return [AccountRow(screen_name=row["screen_name"], name=row["display_name"], description="", followers_count=0,
                           statuses_count=0, protected=bool(row["protected"]), url=f"https://x.com/{row['screen_name']}") for row in rows]

    def poll_once(self) -> dict[str, int]:
        accounts = self._due_accounts()
        if not accounts:
            self.aggregate()
            analyzed = self.process_x_agent_jobs()
            self.process_account_cleanup_jobs()
            self.maintain()
            return {"polled": 0, "accepted": 0, "analyzed": analyzed}
        cutoff = datetime.fromisoformat(self.deployment_started_at())
        accepted = 0
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hottopic") as executor:
            futures = {
                executor.submit(
                    scan_account,
                    account,
                    cutoff=cutoff,
                    timeline_count=100,
                    retries=1,
                    before_request=self.collect_request_pacer.wait,
                    on_rate_limited=lambda retry_after: self.collect_request_pacer.defer(
                        max(self.rate_limit_cooldown_seconds, retry_after)
                    ),
                ): account
                for account in accounts
            }
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
        analyzed = self.process_x_agent_jobs()
        self.process_account_cleanup_jobs()
        self.maintain()
        return {"polled": len(accounts), "accepted": accepted, "analyzed": analyzed}

    @_serialized
    def _store_poll(self, account: AccountRow, items: list[ContentItem], error: dict[str, Any] | None, at: str) -> int:
        lower = account.screen_name.lower()
        deployment_started = datetime.fromisoformat(self.deployment_started_at())
        # scan_account applies this bound too.  Keep it at the persistence
        # boundary so a future collector cannot accidentally backfill history.
        items = [item for item in items if datetime.fromisoformat(item.created_at_iso) >= deployment_started]
        with self.db:
            current = self.db.execute(
                "SELECT status,consecutive_failures,last_success_at,hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled "
                "FROM hottopic_accounts WHERE screen_name_lower=?",
                (lower,),
            ).fetchone()
            if current is None or current["status"] != "followed":
                return 0
            accepted = 0
            for item in items:
                processed_at = None if current["hot_topic_enabled"] else at
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO hottopic_inbox(tweet_id,screen_name_lower,payload_json,collected_at,processed_at) VALUES(?,?,?,?,?)",
                    (item.tweet_id, lower, json.dumps(item.__dict__, ensure_ascii=False), at, processed_at),
                )
                accepted += cursor.rowcount
                if cursor.rowcount:
                    for module, enabled in (
                        ("market_sentiment", current["market_sentiment_enabled"]),
                        ("project_promotion", current["project_promotion_enabled"]),
                    ):
                        if enabled:
                            self.db.execute(
                                "INSERT OR IGNORE INTO x_agent_analysis_jobs(tweet_id,module,status,created_at,updated_at) VALUES(?,?,?, ?, ?)",
                                (item.tweet_id, module, "pending", at, at),
                            )
            failures = 0 if not error else int(current["consecutive_failures"] or 0) + 1
            delay_seconds = self._next_poll_delay_seconds(error, failures)
            if error and self._is_rate_limited(error):
                self.collect_request_pacer.defer(
                    max(self.rate_limit_cooldown_seconds, self._retry_after_seconds(error))
                )
            self.db.execute(
                "UPDATE hottopic_accounts SET next_due_at=?,last_polled_at=?,last_success_at=?,last_error=?,"
                "consecutive_failures=?,last_item_count=?,cumulative_content_count=cumulative_content_count+?,updated_at=? "
                "WHERE screen_name_lower=?",
                (iso(utc_now() + timedelta(seconds=delay_seconds)), at, at if not error else current["last_success_at"],
                 json.dumps(error, ensure_ascii=False) if error else None, failures, len(items), accepted, at, lower),
            )
        if error:
            self.event("collect_error", {"account": account.screen_name, "error": error})
        return accepted

    @staticmethod
    def _is_rate_limited(error: dict[str, Any]) -> bool:
        if str(error.get("kind") or "") == "rate_limited":
            return True
        return "429" in str(error.get("error") or "") or "rate limit" in str(error.get("error") or "").lower()

    @staticmethod
    def _retry_after_seconds(error: dict[str, Any]) -> float:
        try:
            return max(0.0, float(error.get("retry_after_seconds") or 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _next_poll_delay_seconds(self, error: dict[str, Any] | None, failures: int) -> float:
        if not error:
            return float(POLL_INTERVAL_SECONDS)
        # Every repeated collection failure is isolated to the affected
        # account. A 429 also incorporates the server's cooldown; successful
        # polls reset `failures` to zero in _store_poll.
        base = float(POLL_INTERVAL_SECONDS)
        if self._is_rate_limited(error):
            base = max(base, self.rate_limit_cooldown_seconds, self._retry_after_seconds(error))
        backoff = base * (2 ** min(max(failures - 1, 0), 5))
        return min(
            MAX_RATE_LIMIT_ACCOUNT_BACKOFF_SECONDS,
            max(float(POLL_INTERVAL_SECONDS), backoff),
        )

    def aggregate(self) -> None:
        with self.lock:
            rows = self.db.execute(
                "SELECT i.tweet_id,i.payload_json FROM hottopic_inbox i JOIN hottopic_accounts a "
                "ON a.screen_name_lower=i.screen_name_lower WHERE i.processed_at IS NULL AND a.hot_topic_enabled=1 "
                "ORDER BY i.collected_at LIMIT ?",
                (AGGREGATION_BATCH_SIZE,),
            ).fetchall()
        if not rows:
            return
        try:
            payloads = [json.loads(row["payload_json"]) for row in rows]
            result = self.aggregator.process_batch(payloads, utc_now())
            with self.lock:
                with self.db:
                    self.db.executemany("UPDATE hottopic_inbox SET processed_at=? WHERE tweet_id=?", [(iso(utc_now()), row["tweet_id"]) for row in rows])
                self._refresh_hot_topic_counts()
            self.event("aggregate_ok", result.get("metrics", {}))
        except Exception as exc:
            self._record_worker_failure("aggregate_error", exc)

    @staticmethod
    def _is_sqlite_lock_error(error: BaseException) -> bool:
        return isinstance(error, sqlite3.OperationalError) and any(
            marker in str(error).lower() for marker in ("locked", "busy")
        )

    def _record_worker_failure(self, kind: str, error: BaseException) -> None:
        """Best-effort worker diagnostics that do not turn a lock race fatal."""
        try:
            self.event(kind, {"error": f"{type(error).__name__}: {error}"})
        except sqlite3.OperationalError as event_error:
            if not self._is_sqlite_lock_error(event_error):
                raise

    def process_x_agent_jobs(self) -> int:
        """Claim a bounded batch, call models outside SQLite, then persist results."""
        analyzer = self.x_agent_analyzer
        now = iso(utc_now())
        with self.lock:
            # Do this before checking model availability.  Otherwise a worker
            # restart while the route is unavailable leaves leased jobs stuck
            # in `processing` forever.
            with self.db:
                self._reclaim_stale_x_agent_jobs(now)
            if analyzer is None:
                return self._fail_unavailable_x_agent_jobs(now)
            with self.db:
                rows = self.db.execute(
                    "SELECT j.id,j.tweet_id,j.module,j.attempts,i.payload_json,a.market_sentiment_enabled,a.project_promotion_enabled "
                    "FROM x_agent_analysis_jobs j JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower "
                    "WHERE j.status='pending' AND j.attempts<? AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=?) "
                    "ORDER BY j.created_at LIMIT ?",
                    (X_AGENT_ANALYSIS_MAX_ATTEMPTS, now, X_AGENT_ANALYSIS_BATCH_SIZE),
                ).fetchall()
                claimed: list[dict[str, Any]] = []
                for row in rows:
                    item = dict(row)
                    enabled = bool(item["market_sentiment_enabled"] if item["module"] == "market_sentiment" else item["project_promotion_enabled"])
                    if not enabled:
                        self.db.execute(
                            "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=?,updated_at=? WHERE id=?",
                            (now, now, item["id"]),
                        )
                        continue
                    cursor = self.db.execute(
                        "UPDATE x_agent_analysis_jobs SET status='processing',attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'",
                        (now, item["id"]),
                    )
                    if cursor.rowcount:
                        item["attempts"] = int(item["attempts"]) + 1
                        claimed.append(item)
        if not claimed:
            return 0

        def analyze_job(job: dict[str, Any]) -> tuple[dict[str, Any], Any, Exception | None]:
            try:
                source = json.loads(job["payload_json"])
                # Quoted text supplies context for HotTopic, but X Agent must
                # only attribute the tracked account's own words to it.
                own_text = str(source.get("text") or "")
                if not own_text and source.get("activity_type") == "quote":
                    return job, None, None
                text = own_text or str(source.get("expanded_text") or "")
                if not is_relevant(str(job["module"]), text):
                    return job, None, None
                return job, analyzer.analyze(str(job["module"]), source), None
            except Exception as exc:  # A single source must not hold up the batch.
                return job, None, exc

        completed = 0
        with ThreadPoolExecutor(max_workers=self.analysis_workers, thread_name_prefix="x-agent") as executor:
            futures = [executor.submit(analyze_job, job) for job in claimed]
            for future in as_completed(futures):
                job, result, error = future.result()
                self._finish_x_agent_job(job, result, error)
                completed += 1
        return completed

    def _reclaim_stale_x_agent_jobs(self, now: str) -> None:
        """Return abandoned worker claims to the queue without resetting attempts."""
        cutoff = iso(datetime.fromisoformat(now) - X_AGENT_PROCESSING_LEASE)
        self.db.execute(
            "UPDATE x_agent_analysis_jobs SET status='failed',next_attempt_at=NULL,"
            "last_error=COALESCE(last_error,'分析 worker 租约过期'),completed_at=?,updated_at=? "
            "WHERE status='processing' AND attempts>=? AND updated_at<?",
            (now, now, X_AGENT_ANALYSIS_MAX_ATTEMPTS, cutoff),
        )
        self.db.execute(
            "UPDATE x_agent_analysis_jobs SET status='pending',next_attempt_at=NULL,"
            "last_error=COALESCE(last_error,'分析 worker 租约过期，已重新排队'),completed_at=NULL,updated_at=? "
            "WHERE status='processing' AND attempts<? AND updated_at<?",
            (now, X_AGENT_ANALYSIS_MAX_ATTEMPTS, cutoff),
        )

    def _fail_unavailable_x_agent_jobs(self, now: str) -> int:
        """Expose a missing model route as bounded failed work instead of a silent pending queue."""
        reason = self.x_agent_analyzer_error or "X Agent 模型路由不可用"
        with self.db:
            rows = self.db.execute(
                "SELECT j.id,j.module,a.screen_name_lower,a.market_sentiment_enabled,a.project_promotion_enabled "
                "FROM x_agent_analysis_jobs j "
                "JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower "
                "WHERE j.status='pending' ORDER BY j.created_at LIMIT ?",
                (X_AGENT_ANALYSIS_BATCH_SIZE,),
            ).fetchall()
            for row in rows:
                enabled = bool(
                    row["market_sentiment_enabled"]
                    if row["module"] == "market_sentiment"
                    else row["project_promotion_enabled"]
                )
                if enabled:
                    self.db.execute(
                        "UPDATE x_agent_analysis_jobs SET status='failed',next_attempt_at=NULL,last_error=?,"
                        "completed_at=?,updated_at=? WHERE id=?",
                        (reason, now, now, row["id"]),
                    )
                    self._update_x_agent_account_state(str(row["screen_name_lower"]), None, reason)
                else:
                    self.db.execute(
                        "UPDATE x_agent_analysis_jobs SET status='ignored',next_attempt_at=NULL,last_error=NULL,"
                        "completed_at=?,updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
        return len(rows)

    @_serialized
    def _finish_x_agent_job(self, job: dict[str, Any], result: Any, error: Exception | None) -> None:
        at = iso(utc_now())
        source = json.loads(job["payload_json"])
        account = str(source.get("account_screen_name") or "").lower()
        with self.db:
            current = self.db.execute(
                "SELECT j.status,a.market_sentiment_enabled,a.project_promotion_enabled "
                "FROM x_agent_analysis_jobs j JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower WHERE j.id=?",
                (job["id"],),
            ).fetchone()
            if current is None or current["status"] != "processing":
                return
            enabled = bool(
                current["market_sentiment_enabled"]
                if job["module"] == "market_sentiment"
                else current["project_promotion_enabled"]
            )
            if not enabled:
                self.db.execute(
                    "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=?,updated_at=?,last_error=NULL WHERE id=?",
                    (at, at, job["id"]),
                )
                return
            if error is None and result is None:
                self.db.execute(
                    "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=?,updated_at=?,last_error=NULL WHERE id=?",
                    (at, at, job["id"]),
                )
                self._update_x_agent_account_state(account, at, None)
                return
            if error is None:
                assert result is not None
                if job["module"] == "market_sentiment":
                    self._save_sentiment_results(source, result, at)
                else:
                    self._save_project_results(source, result, at)
                self.db.execute(
                    "UPDATE x_agent_analysis_jobs SET status='succeeded',actual_model=?,fallback_reason=?,last_error=NULL,"
                    "completed_at=?,updated_at=? WHERE id=?",
                    (result.actual_model, result.fallback_reason, at, at, job["id"]),
                )
                self._update_x_agent_account_state(account, at, None)
                return
            message = f"{type(error).__name__}: {error}"
            exhausted = int(job["attempts"]) >= X_AGENT_ANALYSIS_MAX_ATTEMPTS
            self.db.execute(
                "UPDATE x_agent_analysis_jobs SET status=?,next_attempt_at=?,last_error=?,updated_at=?,completed_at=? WHERE id=?",
                (
                    "failed" if exhausted else "pending",
                    None if exhausted else iso(utc_now() + X_AGENT_RETRY_DELAY),
                    message,
                    at,
                    at if exhausted else None,
                    job["id"],
                ),
            )
            self._update_x_agent_account_state(account, None, message)

    def _update_x_agent_account_state(self, account: str, analyzed_at: str | None, error: str | None) -> None:
        if not account:
            return
        if error is None:
            unresolved = self.db.execute(
                "SELECT 1 FROM x_agent_analysis_jobs j JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                "WHERE i.screen_name_lower=? AND j.status IN ('pending','processing','failed') "
                "AND j.last_error IS NOT NULL LIMIT 1",
                (account,),
            ).fetchone()
            if unresolved:
                self.db.execute(
                    "UPDATE hottopic_accounts SET last_x_agent_analyzed_at=COALESCE(?,last_x_agent_analyzed_at) "
                    "WHERE screen_name_lower=?",
                    (analyzed_at, account),
                )
                return
        self.db.execute(
            "UPDATE hottopic_accounts SET last_x_agent_analyzed_at=COALESCE(?,last_x_agent_analyzed_at),"
            "last_x_agent_error=? WHERE screen_name_lower=?",
            (analyzed_at, error, account),
        )

    def _save_sentiment_results(self, source: dict[str, Any], result: Any, at: str) -> None:
        account = str(source.get("account_screen_name") or "").lower()
        tweet_id = str(source.get("tweet_id") or "")
        source_url = str(source.get("url") or f"https://x.com/{account}/status/{tweet_id}")
        source_text = str(source.get("text") or source.get("expanded_text") or "")
        posted_at = str(source.get("created_at_iso") or at)
        for item in result.items:
            label = str(item["ticker"] or item["instrument_name"]).strip()
            key = f"{item['scope']}:{' '.join(label.lower().split())}"
            self.db.execute(
                "INSERT OR IGNORE INTO x_agent_sentiment_results("
                "tweet_id,instrument_key,instrument_name,ticker,scope,sentiment,reason,account_lower,source_url,source_text,"
                "posted_at,actual_model,fallback_reason,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tweet_id, key, item["instrument_name"], item["ticker"], item["scope"], item["sentiment"],
                    item["reason"], account, source_url, source_text, posted_at, result.actual_model, result.fallback_reason, at,
                ),
            )

    def _save_project_results(self, source: dict[str, Any], result: Any, at: str) -> None:
        account = str(source.get("account_screen_name") or "").lower()
        tweet_id = str(source.get("tweet_id") or "")
        source_url = str(source.get("url") or f"https://x.com/{account}/status/{tweet_id}")
        source_text = str(source.get("text") or source.get("expanded_text") or "")
        posted_at = str(source.get("created_at_iso") or at)
        for item in result.items:
            identity = self._project_identity(item, tweet_id)
            logic_hash = hashlib.sha256(" ".join(str(item["logic"]).lower().split()).encode("utf-8")).hexdigest()
            self.db.execute(
                "INSERT INTO x_agent_project_observations("
                "identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,logic_hash,account_lower,"
                "source_tweet_id,source_url,source_text,first_mentioned_at,last_mentioned_at,actual_model,fallback_reason,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_key,account_lower,logic_hash) DO UPDATE SET "
                "source_tweet_id=excluded.source_tweet_id,source_url=excluded.source_url,source_text=excluded.source_text,"
                "last_mentioned_at=excluded.last_mentioned_at,actual_model=excluded.actual_model,fallback_reason=excluded.fallback_reason,updated_at=excluded.updated_at",
                (
                    identity, item["project_name"], item["ticker"], item["chain"], item["contract_address"], item["official_url"],
                    item["logic"], logic_hash, account, tweet_id, source_url, source_text, posted_at, posted_at,
                    result.actual_model, result.fallback_reason, at, at,
                ),
            )

    @staticmethod
    def _project_identity(item: dict[str, Any], tweet_id: str) -> str:
        contract = str(item.get("contract_address") or "").strip().lower()
        chain = str(item.get("chain") or "").strip().lower()
        if contract:
            return f"contract:{chain or 'unknown'}:{contract}"
        url = str(item.get("official_url") or "").strip().lower().rstrip("/")
        if url:
            return f"url:{url}"
        # No stable identity means no cross-post merge; that is intentional.
        return f"tweet:{tweet_id}"

    @_serialized
    def retry_failed_x_agent_jobs(self, *, module: str = "", limit: int = 100) -> int:
        if module and module not in {"market_sentiment", "project_promotion"}:
            raise ValueError("不支持的 X Agent 模块")
        safe_limit = min(500, max(1, int(limit)))
        where = "WHERE status='failed'" + (" AND module=?" if module else "")
        params: list[Any] = [module] if module else []
        ids = [row[0] for row in self.db.execute(f"SELECT id FROM x_agent_analysis_jobs {where} ORDER BY updated_at LIMIT ?", [*params, safe_limit]).fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        with self.db:
            self.db.execute(
                f"UPDATE x_agent_analysis_jobs SET status='pending',attempts=0,next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE id IN ({marks})",
                [iso(utc_now()), *ids],
            )
        return len(ids)

    @_serialized
    def x_agent_dashboard(self) -> dict[str, Any]:
        accounts = self.db.execute(
            "SELECT COUNT(*) total,SUM(hot_topic_enabled=1) hot_topic_enabled,"
            "SUM(market_sentiment_enabled=1) market_sentiment_enabled,"
            "SUM(project_promotion_enabled=1) project_promotion_enabled,"
            "SUM(last_error IS NOT NULL OR last_x_agent_error IS NOT NULL) errors "
            "FROM hottopic_accounts"
        ).fetchone()
        jobs = self.db.execute(
            "SELECT SUM(status IN ('pending','processing')) pending,SUM(status='failed') failed FROM x_agent_analysis_jobs"
        ).fetchone()
        return {
            "generatedAt": iso(utc_now()),
            "accounts": {
                "total": int(accounts["total"] or 0),
                "hotTopicEnabled": int(accounts["hot_topic_enabled"] or 0),
                "marketSentimentEnabled": int(accounts["market_sentiment_enabled"] or 0),
                "projectPromotionEnabled": int(accounts["project_promotion_enabled"] or 0),
                "errors": int(accounts["errors"] or 0),
            },
            "jobs": {"pending": int(jobs["pending"] or 0), "failed": int(jobs["failed"] or 0)},
        }

    @staticmethod
    def _window_cutoff(window: str, allowed: dict[str, timedelta]) -> str:
        if window not in allowed:
            raise ValueError("不支持的时间窗口")
        return iso(utc_now() - allowed[window])

    @staticmethod
    def _page(offset: int, limit: int) -> tuple[int, int]:
        return max(0, int(offset)), min(100, max(1, int(limit)))

    @_serialized
    def list_market_sentiment(
        self,
        *,
        window: str = "24h",
        query: str = "",
        sentiment: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        cutoff = self._window_cutoff(window, {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)})
        safe_offset, safe_limit = self._page(offset, limit)
        filters: list[str] = []
        params: list[Any] = [cutoff]
        if query.strip():
            filters.append("LOWER(instrument_name || ' ' || ticker || ' ' || scope) LIKE ?")
            params.append(f"%{query.strip().lower()}%")
        if sentiment != "all":
            filters.append("snapshot=?")
            params.append(sentiment)
        where = f" WHERE {' AND '.join(filters)}" if filters else ""
        cte = """
            WITH filtered AS (
              SELECT id,instrument_key,instrument_name,ticker,scope,sentiment,reason,account_lower,posted_at,
                CASE sentiment
                  WHEN '极度恐慌' THEN -2 WHEN '偏空/谨慎' THEN -1 WHEN '中性/分歧' THEN 0
                  WHEN '偏多/乐观' THEN 1 WHEN '极度狂热' THEN 2
                END AS score
              FROM x_agent_sentiment_results
              WHERE posted_at>=? AND sentiment<>'证据不足'
            ), grouped AS (
              SELECT instrument_key,COUNT(*) AS source_count,COUNT(DISTINCT account_lower) AS account_count,
                MAX(posted_at) AS latest_at,MIN(score) AS min_score,MAX(score) AS max_score,AVG(score) AS average_score
              FROM filtered GROUP BY instrument_key
            ), snapshots AS (
              SELECT *,CASE
                WHEN min_score<0 AND max_score>0 THEN '中性/分歧'
                WHEN average_score>=1.5 THEN '极度狂热'
                WHEN average_score>0.25 THEN '偏多/乐观'
                WHEN average_score<=-1.5 THEN '极度恐慌'
                WHEN average_score<-0.25 THEN '偏空/谨慎'
                ELSE '中性/分歧'
              END AS snapshot
              FROM grouped
            ), latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY instrument_key ORDER BY posted_at DESC,id DESC) AS row_number
              FROM filtered
            ), result_rows AS (
              SELECT l.instrument_key,l.instrument_name,l.ticker,l.scope,s.snapshot,s.source_count,s.account_count,s.latest_at,
                CASE WHEN l.sentiment=s.snapshot THEN l.reason ELSE '来源态度综合为' || s.snapshot || '，展开查看各来源。' END AS reason
              FROM snapshots s JOIN latest l ON l.instrument_key=s.instrument_key AND l.row_number=1
            )
        """
        total = int(self.db.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()[0])
        rows = self.db.execute(
            f"{cte} SELECT instrument_key,instrument_name,ticker,scope,snapshot,source_count,account_count,latest_at,reason "
            f"FROM result_rows{where} ORDER BY latest_at DESC LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [{
            "instrumentKey": row["instrument_key"], "instrumentName": row["instrument_name"], "ticker": row["ticker"],
            "scope": row["scope"], "sentiment": row["snapshot"], "latestAt": row["latest_at"], "reason": row["reason"],
        } for row in rows], "total": total}

    @staticmethod
    def _snapshot_sentiment(values: list[str]) -> str:
        scores = {"极度恐慌": -2, "偏空/谨慎": -1, "中性/分歧": 0, "偏多/乐观": 1, "极度狂热": 2}
        numeric = [scores[value] for value in values if value in scores]
        if not numeric:
            return "中性/分歧"
        if min(numeric) < 0 < max(numeric):
            return "中性/分歧"
        average = sum(numeric) / len(numeric)
        if average >= 1.5:
            return "极度狂热"
        if average > 0.25:
            return "偏多/乐观"
        if average <= -1.5:
            return "极度恐慌"
        if average < -0.25:
            return "偏空/谨慎"
        return "中性/分歧"

    @_serialized
    def market_sentiment_detail(
        self,
        instrument_key: str,
        *,
        window: str = "24h",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not instrument_key.strip():
            raise ValueError("instrument_key 不能为空")
        cutoff = self._window_cutoff(window, {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)})
        safe_offset, safe_limit = self._page(offset, limit)
        total = int(self.db.execute(
            "SELECT COUNT(*) FROM x_agent_sentiment_results WHERE instrument_key=? AND posted_at>=?",
            (instrument_key, cutoff),
        ).fetchone()[0])
        rows = self.db.execute(
            "SELECT tweet_id,account_lower,source_url,source_text,posted_at,sentiment,reason,actual_model,fallback_reason "
            "FROM x_agent_sentiment_results WHERE instrument_key=? AND posted_at>=? ORDER BY posted_at DESC LIMIT ? OFFSET ?",
            (instrument_key, cutoff, safe_limit, safe_offset),
        ).fetchall()
        return {"items": [{
            "tweetId": row["tweet_id"], "account": row["account_lower"], "sourceUrl": row["source_url"],
            "sourceText": row["source_text"], "postedAt": row["posted_at"], "sentiment": row["sentiment"],
            "reason": row["reason"], "actualModel": row["actual_model"], "fallbackReason": row["fallback_reason"],
        } for row in rows], "total": total}

    @_serialized
    def list_project_promotions(
        self,
        *,
        window: str = "24h",
        query: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        cutoff = self._window_cutoff(window, {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)})
        safe_offset, safe_limit = self._page(offset, limit)
        params: list[Any] = [cutoff]
        where = ""
        if query.strip():
            where = " WHERE LOWER(project_name || ' ' || ticker || ' ' || logic) LIKE ?"
            params.append(f"%{query.strip().lower()}%")
        cte = """
            WITH filtered AS (
              SELECT id,identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,account_lower,last_mentioned_at
              FROM x_agent_project_observations WHERE last_mentioned_at>=?
            ), grouped AS (
              SELECT identity_key,COUNT(*) AS source_count,COUNT(DISTINCT account_lower) AS account_count,
                MAX(last_mentioned_at) AS last_mentioned_at
              FROM filtered GROUP BY identity_key
            ), latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered
            ), result_rows AS (
              SELECT l.identity_key,l.project_name,l.ticker,l.chain_name,l.contract_address,l.official_url,l.logic,
                g.account_count,g.source_count,g.last_mentioned_at
              FROM grouped g JOIN latest l ON l.identity_key=g.identity_key AND l.row_number=1
            )
        """
        total = int(self.db.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()[0])
        rows = self.db.execute(
            f"{cte} SELECT identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,account_count,source_count,last_mentioned_at "
            f"FROM result_rows{where} ORDER BY last_mentioned_at DESC LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [{
            "identityKey": row["identity_key"], "projectName": row["project_name"], "ticker": row["ticker"],
            "chainName": row["chain_name"], "contractAddress": row["contract_address"], "officialUrl": row["official_url"],
            "logic": row["logic"],
            "lastMentionedAt": row["last_mentioned_at"],
        } for row in rows], "total": total}

    @_serialized
    def project_promotion_detail(
        self,
        identity_key: str,
        *,
        window: str = "24h",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not identity_key.strip():
            raise ValueError("identity_key 不能为空")
        cutoff = self._window_cutoff(window, {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)})
        safe_offset, safe_limit = self._page(offset, limit)
        total = int(self.db.execute(
            "SELECT COUNT(*) FROM x_agent_project_observations WHERE identity_key=? AND last_mentioned_at>=?",
            (identity_key, cutoff),
        ).fetchone()[0])
        rows = self.db.execute(
            "SELECT id,account_lower,source_url,source_text,source_tweet_id,logic,last_mentioned_at,actual_model,fallback_reason "
            "FROM x_agent_project_observations WHERE identity_key=? AND last_mentioned_at>=? ORDER BY last_mentioned_at DESC LIMIT ? OFFSET ?",
            (identity_key, cutoff, safe_limit, safe_offset),
        ).fetchall()
        return {"items": [{
            "id": row["id"], "account": row["account_lower"], "sourceUrl": row["source_url"],
            "sourceText": row["source_text"], "sourceTweetId": row["source_tweet_id"], "logic": row["logic"],
            "lastMentionedAt": row["last_mentioned_at"], "actualModel": row["actual_model"],
            "fallbackReason": row["fallback_reason"],
        } for row in rows], "total": total}

    @_serialized
    def _refresh_hot_topic_counts(self) -> None:
        with self.db:
            self.db.execute(
                "UPDATE hottopic_accounts SET cumulative_hot_topic_count=("
                "SELECT COUNT(DISTINCT p.topic_id) FROM topic_participations p JOIN topics t ON t.topic_id=p.topic_id "
                "WHERE lower(p.activity_account)=hottopic_accounts.screen_name_lower AND t.retention_tier='permanent')"
            )

    def maintain(self, *, force: bool = False) -> dict[str, int] | None:
        try:
            return self._maintain(force=force)
        except sqlite3.OperationalError as exc:
            if not self._is_sqlite_lock_error(exc):
                raise
            self._record_worker_failure("maintain_busy", exc)
            return None

    def _maintain(self, *, force: bool) -> dict[str, int] | None:
        with self.lock:
            row = self.db.execute("SELECT last_maintenance_at FROM hottopic_meta WHERE singleton_key='global'").fetchone()
        current = utc_now()
        if not force and row[0] and current - datetime.fromisoformat(row[0]) < MAINTENANCE_INTERVAL:
            return None
        reconciled = self.aggregator.reconcile_recent_topics(current)
        self._refresh_hot_topic_counts()
        state = self.aggregator.prune_transient_state(current, retention_hours=TRANSIENT_RETENTION_HOURS)
        inbox_cutoff = iso(current - timedelta(hours=TRANSIENT_RETENTION_HOURS))
        event_cutoff = iso(current - timedelta(days=EVENT_RETENTION_DAYS))
        with self.lock:
            with self.db:
                inbox = self.db.execute(
                    "DELETE FROM hottopic_inbox WHERE processed_at IS NOT NULL AND collected_at<? AND NOT EXISTS ("
                    "SELECT 1 FROM x_agent_analysis_jobs j WHERE j.tweet_id=hottopic_inbox.tweet_id "
                    "AND j.status IN ('pending','processing','failed')"
                    ")",
                    (inbox_cutoff,),
                ).rowcount
                events = self.db.execute("DELETE FROM hottopic_events WHERE at<?", (event_cutoff,)).rowcount
                self.db.execute("UPDATE hottopic_meta SET last_maintenance_at=? WHERE singleton_key='global'", (iso(current),))
        return {**state, "deleted_inbox": inbox, "deleted_events": events, "merged_topics": len(reconciled["topic_merges"])}

    @_serialized
    def health(self) -> dict[str, Any]:
        counts = self.db.execute(
            "SELECT COUNT(*) total,SUM(status='followed') followed,SUM(status='unfollowed') unfollowed,SUM(status='blacklisted') blacklisted,"
            "SUM(last_error IS NOT NULL) errors FROM hottopic_accounts"
        ).fetchone()
        topic_counts = self.db.execute("SELECT matching_status,COUNT(*) FROM topics GROUP BY matching_status").fetchall()
        return {"deploymentStartedAt": self.deployment_started_at(), "accounts": dict(counts),
                "inboxPending": self.db.execute("SELECT COUNT(*) FROM hottopic_inbox WHERE processed_at IS NULL").fetchone()[0],
                "topics": dict(topic_counts), "databasePath": str(self.path)}

    @_serialized
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

    @_serialized
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
    # Systemd supplies EnvironmentFile in production; loading locally keeps
    # the standalone command consistent with the console API process.
    from dotenv import load_dotenv

    load_dotenv()
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
