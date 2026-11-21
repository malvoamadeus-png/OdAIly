"""Production-owned HotTopic worker and SQLite read/write boundary.

This module deliberately owns a separate database.  It never imports the
OdAIly X capture repository or writes to the main application database.
"""
from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import heapq
import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Iterable

from packages.common.paths import ensure_runtime_dirs, get_paths
from packages.common.storage import connect_sqlite_readonly, load_postgres_settings, load_storage_settings
from packages.common.pg_native import connect_postgres
from packages.x_agent import XAgentAnalyzer, is_relevant
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.investment_leads.schema import ensure_schema as ensure_investment_leads_schema
from packages.investment_leads.service import (
    InvestmentLeadService,
    cancel_ineligible_jobs,
    persist_capture_materials,
)
from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository

from .capture import AccountRow, ContentItem, fetch_account_identity_snapshot, scan_account
from .event_tracking import EventTracker, EventTrackingAI, EventTrackingTaskDispatcher
from .topic_aggregator import ModelBriefWriter, TopicCandidateResolver, TopicClaimReviewer, TopicVisibleMergeReviewer, TopicAggregator
from .yesterday_hot_discussion import YesterdayHotDiscussion
from .postgres_repository import HotTopicPostgresRepository
from .postgres_event_tracking import PostgresEventTrackingRepository
from .collector_spool import CollectorSpool, SpoolFullError, SpoolRecordTooLarge


POLL_INTERVAL_SECONDS = 600
DEFAULT_COLLECT_MIN_REQUEST_INTERVAL_SECONDS = 0.15
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
X_AGENT_SUSPECTED_INVALID_404_THRESHOLD = 3
COLLECT_RETRY_DELAY_SECONDS = 0.8
SCHEMA = """
CREATE TABLE IF NOT EXISTS hottopic_meta (
  singleton_key TEXT PRIMARY KEY CHECK (singleton_key = 'global'),
  deployment_started_at TEXT NOT NULL,
  last_maintenance_at TEXT,
  retention_prune_pending INTEGER NOT NULL DEFAULT 0
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
  consecutive_not_found INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS x_agent_sentiment_snapshots (
  instrument_key TEXT NOT NULL,
  snapshot_at TEXT NOT NULL,
  instrument_name TEXT NOT NULL,
  ticker TEXT NOT NULL DEFAULT '',
  sentiment TEXT NOT NULL CHECK (sentiment IN ('极度狂热','偏多/乐观','中性/分歧','偏空/谨慎','极度恐慌')),
  score INTEGER NOT NULL CHECK (score BETWEEN -100 AND 100),
  mention_count INTEGER NOT NULL,
  source_count INTEGER NOT NULL,
  PRIMARY KEY (instrument_key, snapshot_at)
);
CREATE INDEX IF NOT EXISTS idx_x_agent_sentiment_snapshots_history
  ON x_agent_sentiment_snapshots(instrument_key, snapshot_at DESC);
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
  direct_promotion_json TEXT,
  operation_advice_json TEXT,
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
    from packages.common.storage import MonitoredPersistentSQLiteConnection
    connection = sqlite3.connect(path, timeout=30.0, check_same_thread=False, factory=MonitoredPersistentSQLiteConnection)
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


def configured_integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        return default
    return min(maximum, max(minimum, value))


def configured_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


class CollectRequestPacer:
    """Space FXTwitter requests across all poll threads in one worker process."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, min_interval_seconds)
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        # Smooth request starts without letting one account pause other polls.
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
    def __init__(
        self,
        database_path: Path | None = None,
        *,
        model: str | None = None,
        workers: int = 8,
        primary_database_path: Path | None = None,
        event_tracking_ai: EventTrackingAI | None = None,
        event_tracking_dispatcher: EventTrackingTaskDispatcher | None = None,
        semantic_reviewer: Any | None = None,
        postgres_repository: HotTopicPostgresRepository | None = None,
    ) -> None:
        paths = get_paths()
        if postgres_repository is None and load_storage_settings().backend == "postgres":
            postgres_repository = HotTopicPostgresRepository(settings=load_postgres_settings())
        if postgres_repository is None:
            ensure_runtime_dirs(paths)
        self.path = database_path or paths.runtime_dir / "hottopic.sqlite"
        self.workers = max(1, workers)
        self.postgres_repository = postgres_repository
        self.collector_spool = None
        if self.postgres_repository is not None:
            spool_directory = Path(os.getenv("ODAILY_COLLECT_SPOOL_DIR") or "/data/OdAIly/collector-spool")
            spool_directory = spool_directory.expanduser().resolve()
            if not spool_directory.is_relative_to(Path("/data")):
                raise ValueError("ODAILY_COLLECT_SPOOL_DIR must be an absolute path under /data")
            self.collector_spool = CollectorSpool(
                spool_directory,
                max_bytes=int(os.getenv("ODAILY_COLLECT_SPOOL_MAX_BYTES") or str(512 * 1024 * 1024)),
                max_record_bytes=int(os.getenv("ODAILY_COLLECT_SPOOL_MAX_RECORD_BYTES") or str(4 * 1024 * 1024)),
            )
        if self.postgres_repository is not None:
            self.postgres_repository.initialize()
            self.db = None
        else:
            self.db = open_worker_database(self.path)
            self.db.executescript(SCHEMA)
            ensure_investment_leads_schema(self.db, capture_started_at=iso(utc_now()))
            self._migrate_x_agent_schema()
        investment_repository = None
        if self.postgres_repository is not None:
            investment_repository = InvestmentLeadPostgresRepository(
                schema=self.postgres_repository.schema, settings=self.postgres_repository.settings,
            )
        self.investment_lead_queries = InvestmentLeadService(
            self.path, connection=self.db if investment_repository is None else None,
            reader_only=True, postgres_repository=investment_repository,
        )
        if self.db is not None:
            self._init_meta()
        writer = None
        configured_model = model or os.getenv("HOTTOPIC_MODEL") or "gpt-6-luna"
        configured_fallback_model = os.getenv("HOTTOPIC_FALLBACK_MODEL") or "gpt-6-luna"
        if configured_model:
            try:
                writer = ModelBriefWriter(configured_model, fallback_model=configured_fallback_model)
            except RuntimeError:
                # The topic engine remains useful during credential incidents;
                # active topics stay visible with their working title.
                writer = None
        self.semantic_reviewer_error: str | None = None
        if semantic_reviewer is None and configured_bool("HOTTOPIC_CLAIM_REVIEW_ENABLED", True):
            configured_review_model = os.getenv("HOTTOPIC_CLAIM_REVIEW_MODEL") or "gpt-6-luna"
            configured_review_fallback = os.getenv("HOTTOPIC_CLAIM_REVIEW_FALLBACK_MODEL") or "gpt-6-luna"
            configured_review_effort = os.getenv("HOTTOPIC_CLAIM_REVIEW_REASONING_EFFORT") or "high"
            try:
                semantic_reviewer = TopicClaimReviewer(
                    configured_review_model,
                    fallback_model=configured_review_fallback,
                    reasoning_effort=configured_review_effort,
                    timeout=float(os.getenv("HOTTOPIC_CLAIM_REVIEW_TIMEOUT_SECONDS") or "90"),
                )
            except (RuntimeError, ValueError) as exc:
                # Deterministic aggregation remains available during a model
                # credential or route incident; the review is fail-open and
                # is enabled automatically on the next worker restart.
                semantic_reviewer = None
                self.semantic_reviewer_error = f"{type(exc).__name__}: {exc}"
        candidate_resolver = None
        self.candidate_resolver_error: str | None = None
        if configured_bool("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", True):
            try:
                candidate_resolver = TopicCandidateResolver(
                    "gpt-6-luna",
                    fallback_model="gpt-6-luna",
                    reasoning_effort="none",
                    timeout=float(os.getenv("HOTTOPIC_CANDIDATE_RESOLVER_TIMEOUT_SECONDS") or "30"),
                )
            except (RuntimeError, ValueError) as exc:
                self.candidate_resolver_error = f"{type(exc).__name__}: {exc}"
        visible_merge_reviewer = None
        self.topic_merge_reviewer_error: str | None = None
        if configured_bool("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", True):
            try:
                visible_merge_reviewer = TopicVisibleMergeReviewer(
                    "gpt-6-luna",
                    fallback_model="gpt-6-luna",
                    reasoning_effort="high",
                    timeout=float(os.getenv("HOTTOPIC_TOPIC_MERGE_TIMEOUT_SECONDS") or "300"),
                )
            except (RuntimeError, ValueError) as exc:
                self.topic_merge_reviewer_error = f"{type(exc).__name__}: {exc}"
                class UnavailableVisibleMergeReviewer:
                    def propose(_self, _cards):
                        raise RuntimeError(self.topic_merge_reviewer_error or "visible topic reviewer unavailable")

                visible_merge_reviewer = UnavailableVisibleMergeReviewer()
        else:
            class DisabledVisibleMergeReviewer:
                def propose(_self, _cards):
                    return []

            visible_merge_reviewer = DisabledVisibleMergeReviewer()
        if self.db is None:
            self.aggregator = TopicAggregator(
                self.path, postgres_repository=self.postgres_repository, brief_writer=writer,
                resolver=candidate_resolver, semantic_reviewer=semantic_reviewer,
                visible_merge_reviewer=visible_merge_reviewer,
                semantic_review_batch_size=configured_integer("HOTTOPIC_CLAIM_REVIEW_BATCH_SIZE", 40, minimum=1, maximum=100),
                semantic_review_backfill_limit=configured_integer("HOTTOPIC_CLAIM_REVIEW_BACKFILL_LIMIT", 120, minimum=0, maximum=1000),
            )
        else:
            self.aggregator = TopicAggregator(
                self.path,
                brief_writer=writer,
                resolver=candidate_resolver,
                semantic_reviewer=semantic_reviewer,
                visible_merge_reviewer=visible_merge_reviewer,
                semantic_review_batch_size=configured_integer(
                    "HOTTOPIC_CLAIM_REVIEW_BATCH_SIZE", 40, minimum=1, maximum=100
                ),
                semantic_review_backfill_limit=configured_integer(
                    "HOTTOPIC_CLAIM_REVIEW_BACKFILL_LIMIT", 120, minimum=0, maximum=1000
                ),
            )
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
        # Event tracking owns its own tables and temporary account directory;
        # it never joins the baseline HotTopic/X Agent account pool.
        event_primary_database = primary_database_path
        if event_primary_database is None:
            event_primary_database = paths.primary_database_path if database_path is None else self.path.parent / "odaily.sqlite"
        if self.db is None:
            event_repository = PostgresEventTrackingRepository(
                schema=self.postgres_repository.schema, settings=self.postgres_repository.settings,
            )
            self.event_tracker = EventTracker(
                None, primary_database_path=event_primary_database,
                ai=event_tracking_ai, dispatcher=event_tracking_dispatcher,
                feed_writer=LocalEditorPluginFeedWriter(),
                account_identity_fetcher=self._fetch_event_account_identity,
                postgres_repository=event_repository,
            )
            from packages.ai_review import AIReviewService

            self.ai_review = AIReviewService(
                database_path=paths.runtime_dir / "ai_review.sqlite",
                primary_database_path=event_primary_database,
                hottopic_database_path=self.path,
            )
            self.yesterday_hot_discussion = YesterdayHotDiscussion(
                postgres_repository=self.postgres_repository,
            )
        else:
            self.event_tracker = EventTracker(
                open_worker_database(self.path),
                primary_database_path=event_primary_database,
                ai=event_tracking_ai,
                dispatcher=event_tracking_dispatcher,
                feed_writer=LocalEditorPluginFeedWriter(),
                account_identity_fetcher=self._fetch_event_account_identity,
                owns_connection=True,
            )
            from packages.ai_review import AIReviewService

            self.ai_review = AIReviewService(
                database_path=paths.runtime_dir / "ai_review.sqlite",
                primary_database_path=event_primary_database,
                hottopic_database_path=self.path,
            )
            self.yesterday_hot_discussion = YesterdayHotDiscussion(self.db)
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
        if self.ai_review is not None:
            self.ai_review.close()
        self.investment_lead_queries.close()
        if self.event_tracker is not None:
            self.event_tracker.close()
        if self.aggregator is not None:
            self.aggregator.close()
        if self.db is not None:
            self.db.close()

    def _init_meta(self) -> None:
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(hottopic_meta)")}
        if "retention_prune_pending" not in columns:
            with self.db:
                self.db.execute(
                    "ALTER TABLE hottopic_meta ADD COLUMN retention_prune_pending INTEGER NOT NULL DEFAULT 0"
                )
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
            "consecutive_not_found": "INTEGER NOT NULL DEFAULT 0",
            "hot_topic_enabled": "INTEGER NOT NULL DEFAULT 1",
            "market_sentiment_enabled": "INTEGER NOT NULL DEFAULT 0",
            "project_promotion_enabled": "INTEGER NOT NULL DEFAULT 0",
            "investment_leads_enabled": "INTEGER NOT NULL DEFAULT 0",
            "investment_leads_enabled_at": "TEXT",
            "last_x_agent_analyzed_at": "TEXT",
            "last_x_agent_error": "TEXT",
        }
        had_consecutive_not_found = "consecutive_not_found" in columns
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
            project_columns = {
                row["name"] for row in self.db.execute("PRAGMA table_info(x_agent_project_observations)").fetchall()
            }
            for name in ("direct_promotion_json", "operation_advice_json"):
                if name not in project_columns:
                    self.db.execute(f"ALTER TABLE x_agent_project_observations ADD COLUMN {name} TEXT")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS hottopic_migrations(name TEXT PRIMARY KEY,applied_at TEXT NOT NULL)"
            )
            if not had_consecutive_not_found:
                self.db.execute(
                    "UPDATE hottopic_accounts SET consecutive_not_found=1 "
                    "WHERE last_error IS NOT NULL AND (last_error LIKE '%HTTP Error 404%' "
                    "OR last_error LIKE '%\"http_status\": 404%' "
                    "OR last_error LIKE '%\"kind\": \"not_found\"%')"
                )
            migration = self.db.execute(
                "INSERT OR IGNORE INTO hottopic_migrations(name,applied_at) VALUES(?,?)",
                ("clear_resolved_x_agent_account_errors_v1", iso(utc_now())),
            )
            if migration.rowcount:
                self.db.execute(
                    "UPDATE hottopic_accounts SET last_x_agent_error=NULL "
                    "WHERE last_x_agent_error IS NOT NULL AND EXISTS ("
                    "SELECT 1 FROM x_agent_analysis_jobs succeeded "
                    "JOIN hottopic_inbox i ON i.tweet_id=succeeded.tweet_id "
                    "WHERE i.screen_name_lower=hottopic_accounts.screen_name_lower "
                    "AND succeeded.status='succeeded' AND NOT EXISTS ("
                    "SELECT 1 FROM x_agent_analysis_jobs unresolved "
                    "JOIN hottopic_inbox ui ON ui.tweet_id=unresolved.tweet_id "
                    "WHERE ui.screen_name_lower=hottopic_accounts.screen_name_lower "
                    "AND unresolved.status IN ('pending','processing','failed') "
                    "AND unresolved.last_error IS NOT NULL AND ("
                    "unresolved.updated_at>succeeded.updated_at OR "
                    "(unresolved.updated_at=succeeded.updated_at AND unresolved.id>succeeded.id)"
                    ")))"
                )
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
            migration = "x_agent_investment_leads_enabled_v1"
            applied = self.db.execute("SELECT 1 FROM hottopic_migrations WHERE name=?", (migration,)).fetchone()
            if applied is None:
                now = iso(utc_now())
                # The initial release enables this module once for every existing
                # directory entry, including accounts with the other switches off.
                self.db.execute(
                    "UPDATE hottopic_accounts SET investment_leads_enabled=1,status='followed',"
                    "next_due_at=?,updated_at=?",
                    (now, now),
                )
                self.db.execute(
                    "INSERT INTO hottopic_migrations(name,applied_at) VALUES(?,?)", (migration, now)
                )
            capture_start_migration = "x_agent_investment_leads_capture_start_v1"
            capture_start_applied = self.db.execute(
                "SELECT 1 FROM hottopic_migrations WHERE name=?", (capture_start_migration,)
            ).fetchone()
            if capture_start_applied is None:
                capture_row = self.db.execute(
                    "SELECT capture_started_at FROM investment_leads_meta WHERE singleton_key='global'"
                ).fetchone()
                capture_started_at = str(capture_row[0]) if capture_row else iso(utc_now())
                self.db.execute(
                    "UPDATE hottopic_accounts SET investment_leads_enabled_at=? "
                    "WHERE investment_leads_enabled=1 AND investment_leads_enabled_at IS NULL",
                    (capture_started_at,),
                )
                self.db.execute(
                    "INSERT INTO hottopic_migrations(name,applied_at) VALUES(?,?)",
                    (capture_start_migration, iso(utc_now())),
                )

    @_serialized
    def deployment_started_at(self, database: sqlite3.Connection | None = None) -> str:
        if self.postgres_repository is not None and database is None:
            return self.postgres_repository.deployment_started_at().isoformat()
        db = database if database is not None else self.db
        return str(db.execute("SELECT deployment_started_at FROM hottopic_meta WHERE singleton_key='global'").fetchone()[0])

    @_serialized
    def event(self, kind: str, detail: dict[str, Any], database: sqlite3.Connection | None = None) -> None:
        if self.postgres_repository is not None and database is None:
            self.postgres_repository.log_event(kind, detail, iso(utc_now()))
            return
        db = database if database is not None else self.db
        with db:
            db.execute(
                "INSERT INTO hottopic_events(at,kind,detail_json) VALUES(?,?,?)",
                (iso(utc_now()), kind, json.dumps(detail, ensure_ascii=False)),
            )

    @_serialized
    def seed_accounts(self, rows: Iterable[dict[str, str]]) -> int:
        now = iso(utc_now())
        if self.postgres_repository is not None:
            return self.postgres_repository.seed_accounts(rows, now=now)
        inserted = 0
        with self.db:
            for row in rows:
                handle = normalize_handle(str(row.get("screen_name") or ""))
                lower = handle.lower()
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO hottopic_accounts("
                    "screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                    "market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,investment_leads_enabled_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (handle, lower, str(row.get("display_name") or row.get("name") or ""), "followed",
                     now, 1, 0, 0, 1, now, now, now),
                )
                inserted += cursor.rowcount
        return inserted

    def seed_from_csv(self, csv_path: Path) -> int:
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            return self.seed_accounts(csv.DictReader(source))

    @_serialized
    def list_accounts(self, *, query: str = "", status: str = "all") -> list[dict[str, Any]]:
        if self.postgres_repository is not None:
            return self.postgres_repository.list_accounts(query=query, status=status)
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
            "hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,"
            "last_x_agent_analyzed_at,last_x_agent_error "
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
        if self.postgres_repository is not None:
            return self.postgres_repository.add_account(normalized, display_name, now=now)
        with self.db:
            self.db.execute(
                "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                "market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,investment_leads_enabled_at,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(screen_name_lower) DO UPDATE SET "
                "screen_name=excluded.screen_name,display_name=excluded.display_name,status='followed',next_due_at=excluded.next_due_at,"
                "hot_topic_enabled=1,updated_at=excluded.updated_at",
                (normalized, lower, display_name.strip(), "followed", now, 1, 0, 0, 1, now, now, now),
            )
        return self._account(lower)

    @_serialized
    def set_account_status(self, handle: str, status: str) -> dict[str, Any]:
        if status not in {"followed", "unfollowed"}:
            raise ValueError("不支持的账号状态")
        lower = normalize_handle(handle).lower()
        now = iso(utc_now())
        if self.postgres_repository is not None:
            row = self.postgres_repository.set_account_status(lower, status, now=now)
            if row is None:
                raise ValueError("账号不存在")
            return row
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
                investment_leads_enabled = int(row["investment_leads_enabled"])
            else:
                hot_topic_enabled, market_sentiment_enabled, project_promotion_enabled, investment_leads_enabled = 0, 0, 0, 0
            next_due = now if status == "followed" else None
            self.db.execute(
                "UPDATE hottopic_accounts SET status=?,next_due_at=?,hot_topic_enabled=?,market_sentiment_enabled=?,"
                "project_promotion_enabled=?,investment_leads_enabled=?,investment_leads_enabled_at=?,updated_at=? WHERE screen_name_lower=?",
                (status, next_due, hot_topic_enabled, market_sentiment_enabled, project_promotion_enabled,
                 investment_leads_enabled,
                 row["investment_leads_enabled_at"] if status == "followed" else None, now, lower),
            )
            if status == "unfollowed":
                cancel_ineligible_jobs(self.db, account_lower=lower, now=now)
        return self._account(lower)

    def _account(self, lower: str) -> dict[str, Any]:
        if self.postgres_repository is not None:
            row = self.postgres_repository.get_account(lower)
            assert row is not None
            return row
        row = self.db.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)).fetchone()
        assert row is not None
        return dict(row)

    @_serialized
    def add_x_agent_account(self, handle: str, display_name: str = "") -> dict[str, Any]:
        """Add an account to the shared X Agent directory with HotTopic enabled."""
        normalized = normalize_handle(handle)
        lower, now = normalized.lower(), iso(utc_now())
        if self.postgres_repository is not None:
            row = self.postgres_repository.add_account(normalized, display_name, now=now)
            return self._x_agent_account_payload(row)
        with self.db:
            existing = self.db.execute(
                "SELECT screen_name_lower FROM hottopic_accounts WHERE screen_name_lower=?", (lower,)
            ).fetchone()
            if existing is None:
                self.db.execute(
                    "INSERT INTO hottopic_accounts("
                    "screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
                        "market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,investment_leads_enabled_at,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (normalized, lower, display_name.strip(), "followed", now, 1, 0, 0, 1, now, now, now),
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
        availability: str = "all",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self.postgres_repository is not None:
            result = self.postgres_repository.list_x_agent_accounts(
                query=query, module=module, enabled=enabled, availability=availability, offset=offset, limit=limit,
            )
            result["items"] = [self._x_agent_account_payload(row) for row in result["items"]]
            return result
        column_by_module = {
            "hot_topic": "hot_topic_enabled",
            "market_sentiment": "market_sentiment_enabled",
            "project_promotion": "project_promotion_enabled",
            "investment_leads": "investment_leads_enabled",
        }
        clauses: list[str] = []
        params: list[Any] = []
        column = column_by_module.get(module)
        if module != "all" and column is None:
            raise ValueError("不支持的 X Agent 模块筛选")
        if availability not in {"all", "suspected_invalid"}:
            raise ValueError("不支持的 X Agent 账号状态筛选")
        if availability == "suspected_invalid":
            clauses.append("consecutive_not_found>=?")
            params.append(X_AGENT_SUSPECTED_INVALID_404_THRESHOLD)
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
            "project_promotion_enabled,investment_leads_enabled,cumulative_content_count,cumulative_hot_topic_count,"
            "last_polled_at,last_success_at,last_error,consecutive_not_found,last_x_agent_analyzed_at,last_x_agent_error "
            f"FROM hottopic_accounts{where} "
            "ORDER BY hot_topic_enabled DESC,market_sentiment_enabled DESC,project_promotion_enabled DESC,"
            "investment_leads_enabled DESC,screen_name_lower "
            "LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [self._x_agent_account_payload(dict(row)) for row in rows], "total": total}

    @_serialized
    def set_x_agent_subscriptions(self, handles: Iterable[str], patch: dict[str, Any]) -> list[dict[str, Any]]:
        allowed = {"hot_topic_enabled", "market_sentiment_enabled", "project_promotion_enabled", "investment_leads_enabled"}
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
        if self.postgres_repository is not None:
            rows = self.postgres_repository.set_x_agent_subscriptions(lowered, changes, now=now)
            return [self._x_agent_account_payload(row) for row in rows]
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
                investment_enabled_at = (
                    (row["investment_leads_enabled_at"] or now)
                    if values["investment_leads_enabled"] else None
                )
                self.db.execute(
                    "UPDATE hottopic_accounts SET hot_topic_enabled=?,market_sentiment_enabled=?,"
                    "project_promotion_enabled=?,investment_leads_enabled=?,investment_leads_enabled_at=?,"
                    "status=?,next_due_at=?,updated_at=? WHERE screen_name_lower=?",
                    (
                        int(bool(values["hot_topic_enabled"])),
                        int(bool(values["market_sentiment_enabled"])),
                        int(bool(values["project_promotion_enabled"])),
                        int(bool(values["investment_leads_enabled"])),
                        investment_enabled_at,
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
                if bool(row["investment_leads_enabled"]) and not bool(values["investment_leads_enabled"]):
                    cancel_ineligible_jobs(
                        self.db, account_lower=str(values["screen_name_lower"]), now=now
                    )
                # Ignored work no longer represents the account's latest analysis result.
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
            "investmentLeadsEnabled": bool(row.get("investment_leads_enabled")),
            "cumulativeContentCount": int(row.get("cumulative_content_count") or 0),
            "cumulativeHotTopicCount": int(row.get("cumulative_hot_topic_count") or 0),
            "lastPolledAt": row.get("last_polled_at"),
            "lastSuccessAt": row.get("last_success_at"),
            "lastError": row.get("last_error"),
            "consecutiveNotFoundCount": int(row.get("consecutive_not_found") or 0),
            "suspectedInvalid": int(row.get("consecutive_not_found") or 0) >= X_AGENT_SUSPECTED_INVALID_404_THRESHOLD,
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
        if self.postgres_repository is not None:
            return self.postgres_repository.import_x_agent_screening_rows(rows, apply_suggestions=apply_suggestions, now=now)
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
                        "hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,"
                        "investment_leads_enabled_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (handle, lower, str(raw.get("display_name") or ""), "followed", due, 1, market, project, 1,
                         iso(now), iso(now), iso(now)),
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

    @staticmethod
    def _x_agent_analysis_text(source: dict[str, Any]) -> str | None:
        """Return only the tracked account's own text for X Agent analysis."""
        own_text = str(source.get("text") or "")
        if not own_text and source.get("activity_type") == "quote":
            return None
        return own_text or str(source.get("expanded_text") or "")

    @_serialized
    def backfill_x_agent_inbox(
        self,
        *,
        module: str,
        since: str,
        limit: int = 100,
        apply: bool = False,
    ) -> dict[str, int]:
        """Queue a bounded, explicit replay of eligible existing inbox rows.

        This never changes subscriptions or calls a model. The normal worker
        consumes the standard pending jobs after an operator chooses --apply.
        """
        column_by_module = {
            "market_sentiment": "market_sentiment_enabled",
            "project_promotion": "project_promotion_enabled",
        }
        column = column_by_module.get(module)
        if column is None:
            raise ValueError("不支持的 X Agent 模块")
        try:
            parsed_since = datetime.fromisoformat(since.strip().replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("--since 必须是带时区的 ISO 时间") from exc
        if parsed_since.tzinfo is None:
            raise ValueError("--since 必须包含时区")
        safe_since = iso(parsed_since)
        safe_limit = min(500, max(1, int(limit)))
        if self.postgres_repository is not None:
            return self.postgres_repository.backfill_x_agent_inbox(
                module=module, since=parsed_since, limit=safe_limit, apply=apply,
            )
        rows = self.db.execute(
            "SELECT i.tweet_id,i.payload_json FROM hottopic_inbox i "
            "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower "
            f"WHERE a.status='followed' AND a.{column}=1 AND i.collected_at>=? "
            "AND NOT EXISTS(SELECT 1 FROM x_agent_analysis_jobs j WHERE j.tweet_id=i.tweet_id AND j.module=?) "
            "ORDER BY i.collected_at,i.tweet_id",
            (safe_since, module),
        ).fetchall()
        considered = len(rows)
        relevant_rows: list[tuple[str, str]] = []
        for row in rows:
            try:
                source = json.loads(str(row["payload_json"]))
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(source, dict):
                continue
            text = self._x_agent_analysis_text(source)
            if text is not None and is_relevant(module, text):
                relevant_rows.append((str(row["tweet_id"]), str(row["payload_json"])))
        relevant = len(relevant_rows)
        enqueued = 0
        if apply:
            now = iso(utc_now())
            with self.db:
                for tweet_id, _payload in relevant_rows[:safe_limit]:
                    cursor = self.db.execute(
                        "INSERT OR IGNORE INTO x_agent_analysis_jobs(tweet_id,module,status,created_at,updated_at) "
                        "VALUES(?,?,?, ?, ?)",
                        (tweet_id, module, "pending", now, now),
                    )
                    enqueued += cursor.rowcount
        return {"considered": considered, "relevant": relevant, "enqueued": enqueued}

    @_serialized
    def process_account_cleanup_jobs(self, *, job_limit: int = 1) -> dict[str, int]:
        """Process small, resumable blacklist cleanups outside console requests."""
        if self.postgres_repository is not None:
            return self.postgres_repository.process_account_cleanup_jobs(job_limit=job_limit)
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
                    "SELECT 1 FROM claims cl INDEXED BY idx_claim_item "
                    "CROSS JOIN topic_evidence te INDEXED BY idx_topic_evidence_claim CROSS JOIN topics t "
                    "WHERE cl.content_item_id=ci.content_item_id AND te.claim_id=cl.claim_id "
                    "AND t.topic_id=te.topic_id AND t.retention_tier='permanent') "
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
    def _due_accounts(
        self,
        database: sqlite3.Connection | None = None,
        *,
        limit: int | None = None,
    ) -> list[AccountRow]:
        if self.postgres_repository is not None and database is None:
            rows = self.postgres_repository.due_accounts(at=utc_now(), limit=limit)
            return [AccountRow(
                screen_name=row["screen_name"], name=row.get("display_name") or "", description="",
                followers_count=0, statuses_count=0, protected=bool(row.get("protected")),
                url=f"https://x.com/{row['screen_name']}",
            ) for row in rows]
        db = database if database is not None else self.db
        query = (
            "SELECT screen_name,display_name,protected FROM hottopic_accounts "
            "WHERE status='followed' AND (hot_topic_enabled=1 OR market_sentiment_enabled=1 OR "
            "project_promotion_enabled=1 OR investment_leads_enabled=1) "
            "AND (next_due_at IS NULL OR next_due_at<=?) "
            "ORDER BY COALESCE(next_due_at,''),screen_name_lower"
        )
        parameters: tuple[Any, ...] = (iso(utc_now()),)
        if limit is not None:
            query += " LIMIT ?"
            parameters += (max(1, limit),)
        rows = db.execute(query, parameters).fetchall()
        return [AccountRow(screen_name=row["screen_name"], name=row["display_name"], description="", followers_count=0,
                           statuses_count=0, protected=bool(row["protected"]), url=f"https://x.com/{row['screen_name']}") for row in rows]

    def _scan_event_account(self, account: AccountRow, cutoff: datetime) -> tuple[list[ContentItem], Any, dict[str, Any] | None]:
        items, latest, error = scan_account(
            account,
            cutoff=cutoff,
            timeline_count=100,
            retries=1,
            before_request=self.collect_request_pacer.wait,
            include_x_articles=True,
        )
        if error and self._is_rate_limited(error):
            error["retry_after_seconds"] = max(
                self.rate_limit_cooldown_seconds, self._retry_after_seconds(error)
            )
        return items, latest, error

    def _fetch_event_account_identity(self, screen_name: str) -> dict[str, Any]:
        return fetch_account_identity_snapshot(
            screen_name,
            post_count=4,
            retries=1,
            before_request=self.collect_request_pacer.wait,
        )

    def _advance_event_tracking(self, *, poll_accounts: bool) -> dict[str, Any]:
        if self.event_tracker is None:
            return {"discovery": {"discovered": 0, "failed": 0, "bound": 0},
                    "polling": {"polled": 0, "accepted": 0, "errors": 0},
                    "updates": {"classified": 0, "material": 0, "failed": 0},
                    "outbox": {"submitted": 0, "failed": 0, "duplicate": 0, "published": 0},
                    "lifecycle": {"ended": 0}}
        discovery = self.event_tracker.discover_official_accounts()
        polling = self.event_tracker.poll_due_accounts(self._scan_event_account, workers=self.workers) if poll_accounts else {
            "polled": 0, "accepted": 0, "errors": 0,
        }
        updates = self.event_tracker.classify_official_updates()
        outbox = self.event_tracker.dispatch_publications()
        lifecycle = self.event_tracker.maintain()
        return {"discovery": discovery, "polling": polling, "updates": updates, "outbox": outbox, "lifecycle": lifecycle}

    def poll_due_accounts_once(
        self,
        database: sqlite3.Connection | None = None,
        *,
        limit: int | None = None,
    ) -> dict[str, int]:
        self._replay_collector_spool()
        db = database if database is not None else self.db
        accounts = self._due_accounts(db, limit=limit)
        if not accounts:
            return {"polled": 0, "accepted": 0, "errors": 0}

        cutoff = datetime.fromisoformat(self.deployment_started_at(db))
        accepted = errors = 0
        polled_accounts: set[str] = set()
        reservation_deferred = False
        spool_failure: str | None = None
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="hottopic") as executor, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hottopic-spool",
        ) as persister:
            account_iter = iter(accounts)
            futures = {}
            persistence_futures: dict[Any, tuple[str, dict[str, Any]]] = {}
            delayed_retries: list[tuple[float, int, AccountRow, int]] = []
            retry_sequence = 0
            max_in_flight = self.workers * 2

            def submit(account: AccountRow, retry_count: int) -> bool:
                reservation = None
                if self.postgres_repository is not None and self.collector_spool is not None:
                    reservation = self.collector_spool.reserve()
                    if reservation is None:
                        return False
                future = executor.submit(
                    scan_account,
                    account,
                    cutoff=cutoff,
                    timeline_count=100,
                    retries=0,
                    before_request=self.collect_request_pacer.wait,
                    include_investment_materials=True,
                )
                futures[future] = (account, retry_count, reservation)
                return True

            def submit_new_accounts() -> None:
                while len(futures) < max_in_flight:
                    try:
                        account = next(account_iter)
                    except StopIteration:
                        return
                    if not submit(account, 0):
                        return

            submit_new_accounts()
            while futures or delayed_retries or persistence_futures:
                for persist_future in [future for future in persistence_futures if future.done()]:
                    reservation, record = persistence_futures.pop(persist_future)
                    try:
                        persist_future.result()
                    except SpoolRecordTooLarge as exc:
                        spool_failure = str(exc)
                        errors += 1
                        reservation_deferred = True
                    except Exception:
                        time.sleep(COLLECT_RETRY_DELAY_SECONDS)
                        persistence_futures[persister.submit(
                            self.collector_spool.append_reserved_retrying,
                            reservation, record, attempts=8, delay_seconds=0.1,
                        )] = (reservation, record)

                now_monotonic = time.monotonic()
                while delayed_retries and delayed_retries[0][0] <= now_monotonic and len(futures) < max_in_flight:
                    ready_at, sequence, account, retry_count = heapq.heappop(delayed_retries)
                    if not submit(account, retry_count):
                        reservation_deferred = True
                        delayed_retries.clear()
                        break
                if not reservation_deferred:
                    submit_new_accounts()

                if not futures:
                    if delayed_retries:
                        if reservation_deferred:
                            break
                        time.sleep(max(0.0, delayed_retries[0][0] - time.monotonic()))
                    elif persistence_futures:
                        wait(persistence_futures, timeout=0.1, return_when=FIRST_COMPLETED)
                    continue

                timeout = None
                if delayed_retries and len(futures) < max_in_flight:
                    timeout = max(0.0, delayed_retries[0][0] - time.monotonic())
                completed, _pending = wait(futures, timeout=timeout, return_when=FIRST_COMPLETED)
                if not completed:
                    continue

                for future in completed:
                    account, retry_count, reservation = futures.pop(future)
                    polled_accounts.add(account.screen_name.lower())
                    try:
                        items, _latest, error = future.result()
                    except Exception as exc:
                        items, error = [], {"kind": "exception", "error": str(exc)}

                    if error and error.get("retryable") and retry_count < 1:
                        if reservation and self.collector_spool is not None:
                            self.collector_spool.release_reservation(reservation)
                        retry_sequence += 1
                        heapq.heappush(
                            delayed_retries,
                            (
                                time.monotonic() + COLLECT_RETRY_DELAY_SECONDS * (retry_count + 1),
                                retry_sequence,
                                account,
                                retry_count + 1,
                            ),
                        )
                        continue

                    now = iso(utc_now())
                    if self.postgres_repository is not None and self.collector_spool is not None:
                        if reservation is None:
                            raise RuntimeError("PostgreSQL collection result has no spool reservation")
                        record = {
                                "spool_id": f"{account.screen_name.lower()}:{now}:{uuid.uuid4().hex}",
                                "account": account.screen_name.lower(),
                                "items": [asdict(item) for item in items], "error": error, "at": now,
                            }
                        persistence_futures[persister.submit(
                            self.collector_spool.append_reserved_retrying,
                            reservation, record, attempts=8, delay_seconds=0.1,
                        )] = (reservation, record)
                    else:
                        accepted += self._store_poll(account, items, error, now, database=db)
                    errors += int(error is not None)
            if self.postgres_repository is not None and self.collector_spool is not None and spool_failure is None:
                accepted = self._replay_collector_spool()
        if spool_failure:
            self.postgres_repository.log_event(
                "collector_spool_record_too_large", {"error": spool_failure}, iso(utc_now()),
            )
        return {"polled": len(polled_accounts), "accepted": accepted, "errors": errors}

    def _replay_collector_spool(self) -> int:
        if self.postgres_repository is None or self.collector_spool is None:
            return 0
        replayed = 0
        for record in self.collector_spool.pending():
            account_lower = str(record.get("account") or "")
            account = self.postgres_repository.get_account(account_lower)
            if account is None:
                raise RuntimeError(f"collector spool references missing account: {account_lower}")
            now = str(record.get("at") or iso(utc_now()))
            failures = int(account.get("consecutive_failures") or 0) + int(record.get("error") is not None)
            self.postgres_repository.store_poll(
                account_lower=account_lower, posts=list(record.get("items") or []), error=record.get("error"),
                now=now, deployment_started_at=self.deployment_started_at(),
                poll_delay_seconds=self._next_poll_delay_seconds(record.get("error"), failures),
                spool_id=str(record["spool_id"]),
            )
            self.collector_spool.acknowledge(str(record["spool_id"]))
            replayed += 1
        return replayed

    def poll_once(self, *, collect_baseline_accounts: bool = True) -> dict[str, Any]:
        if self.postgres_repository is not None and (
            self.aggregator is None or self.event_tracker is None or self.yesterday_hot_discussion is None
        ):
            raise RuntimeError("PostgreSQL HotTopic worker is unavailable until native aggregation, event tracking, and yesterday discussion are initialized")
        ai_review = self._run_ai_review_if_due()
        # Existing events first: their 1-minute schedule has priority over the
        # broad baseline account pool while sharing the same FXTwitter pacer.
        self.event_tracker.reconcile_topic_merges()
        self.event_tracker.observe_topics()
        self.event_tracker.retry_pending_identity_discoveries()
        self.event_tracker.reverify_pending_account_bindings()
        event_before = self._advance_event_tracking(poll_accounts=True)
        self.scan_visible_topic_merges()
        accounts = self._due_accounts() if collect_baseline_accounts else []
        if not accounts:
            self.aggregate()
            yesterday_hot_discussion = self.yesterday_hot_discussion.run_if_due()
            event_after = self._advance_event_tracking(poll_accounts=True)
            analyzed = self.process_x_agent_jobs()
            self.process_account_cleanup_jobs()
            self.maintain()
            return {
                "polled": 0,
                "accepted": 0,
                "analyzed": analyzed,
                "event_accepted": int(event_before["polling"]["accepted"]) + int(event_after["polling"]["accepted"]),
                "yesterday_hot_discussion": yesterday_hot_discussion.status if yesterday_hot_discussion else "not_due",
                "ai_review": ai_review,
            }
        polling = self.poll_due_accounts_once()
        self.aggregate()
        yesterday_hot_discussion = self.yesterday_hot_discussion.run_if_due()
        event_after = self._advance_event_tracking(poll_accounts=True)
        analyzed = self.process_x_agent_jobs()
        self.process_account_cleanup_jobs()
        self.maintain()
        return {
            "polled": polling["polled"],
            "accepted": polling["accepted"],
            "analyzed": analyzed,
            "event_accepted": int(event_before["polling"]["accepted"]) + int(event_after["polling"]["accepted"]),
            "yesterday_hot_discussion": yesterday_hot_discussion.status if yesterday_hot_discussion else "not_due",
            "ai_review": ai_review,
        }

    def _run_ai_review_if_due(self) -> dict[str, Any]:
        try:
            return self.ai_review.run_if_due()
        except Exception as exc:
            self.event("ai_review_error", {"error": f"{type(exc).__name__}: {exc}"})
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}

    @_serialized
    def _store_poll(
        self,
        account: AccountRow,
        items: list[ContentItem],
        error: dict[str, Any] | None,
        at: str,
        *,
        database: sqlite3.Connection | None = None,
    ) -> int:
        if self.postgres_repository is not None and database is None:
            rows = [asdict(item) for item in items]
            failures = 0 if error is None else (int((self.postgres_repository.get_account(account.screen_name.lower()) or {}).get("consecutive_failures") or 0) + 1)
            accepted = self.postgres_repository.store_poll(
                account_lower=account.screen_name.lower(), posts=rows, error=error, now=at,
                deployment_started_at=self.deployment_started_at(),
                poll_delay_seconds=self._next_poll_delay_seconds(error, failures),
            )
            if error:
                self.event("collect_error", {"account": account.screen_name, "error": error})
                if self._is_rate_limited(error):
                    self.collect_request_pacer.defer(self._retry_after_seconds(error))
            return accepted
        db = database if database is not None else self.db
        lower = account.screen_name.lower()
        deployment_started = datetime.fromisoformat(self.deployment_started_at(db))
        # scan_account applies this bound too.  Keep it at the persistence
        # boundary so a future collector cannot accidentally backfill history.
        with db:
            current = db.execute(
                "SELECT status,consecutive_failures,last_success_at,hot_topic_enabled,market_sentiment_enabled,"
                "project_promotion_enabled,investment_leads_enabled,consecutive_not_found "
                "FROM hottopic_accounts WHERE screen_name_lower=?",
                (lower,),
            ).fetchone()
            if current is None or current["status"] != "followed":
                return 0
            if current["investment_leads_enabled"]:
                for item in items:
                    if item.investment_materials:
                        persist_capture_materials(
                            db,
                            account_lower=lower,
                            materials=item.investment_materials,
                            observed_at=at,
                        )
            items = [item for item in items if datetime.fromisoformat(item.created_at_iso) >= deployment_started]
            accepted = 0
            for item in items:
                if item.investment_only:
                    continue
                processed_at = None if current["hot_topic_enabled"] else at
                cursor = db.execute(
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
                            db.execute(
                                "INSERT OR IGNORE INTO x_agent_analysis_jobs(tweet_id,module,status,created_at,updated_at) VALUES(?,?,?, ?, ?)",
                                (item.tweet_id, module, "pending", at, at),
                            )
            failures = 0 if not error else int(current["consecutive_failures"] or 0) + 1
            not_found_failures = (
                int(current["consecutive_not_found"] or 0) + 1
                if self._is_account_not_found(error)
                else 0
            )
            delay_seconds = self._next_poll_delay_seconds(error, failures)
            db.execute(
                "UPDATE hottopic_accounts SET next_due_at=?,last_polled_at=?,last_success_at=?,last_error=?,"
                "consecutive_failures=?,consecutive_not_found=?,last_item_count=?,"
                "cumulative_content_count=cumulative_content_count+?,updated_at=? "
                "WHERE screen_name_lower=?",
                (iso(utc_now() + timedelta(seconds=delay_seconds)), at, at if not error else current["last_success_at"],
                 json.dumps(error, ensure_ascii=False) if error else None, failures, not_found_failures,
                 len(items), accepted, at, lower),
            )
        if error:
            self.event("collect_error", {"account": account.screen_name, "error": error}, database=db)
            if self._is_rate_limited(error):
                self.collect_request_pacer.defer(self._retry_after_seconds(error))
        return accepted

    @staticmethod
    def _is_account_not_found(error: dict[str, Any] | None) -> bool:
        if not error:
            return False
        if error.get("kind") == "not_found" or error.get("http_status") == 404:
            return True
        return "HTTP Error 404" in str(error.get("error") or "")

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

    def aggregate(self) -> dict[str, Any] | None:
        if self.aggregator is None:
            raise RuntimeError("PostgreSQL HotTopic aggregation repository is not initialized")
        with self.lock:
            if self.postgres_repository is not None:
                rows = self.postgres_repository.pending_inbox(limit=AGGREGATION_BATCH_SIZE)
            else:
                rows = self.db.execute(
                    "SELECT i.tweet_id,i.payload_json FROM hottopic_inbox i JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=i.screen_name_lower WHERE i.processed_at IS NULL AND a.hot_topic_enabled=1 "
                    "ORDER BY i.collected_at LIMIT ?",
                    (AGGREGATION_BATCH_SIZE,),
                ).fetchall()
        now = utc_now()
        if not rows and not self.aggregator.has_due_topic_refresh(now):
            return None
        try:
            payloads = [json.loads(row["payload_json"]) for row in rows]
            result = self.aggregator.process_batch(payloads, now)
            self.scan_visible_topic_merges()
            if self.event_tracker is not None:
                self.event_tracker.reconcile_topic_merges()
            with self.lock:
                if self.postgres_repository is not None:
                    self.postgres_repository.mark_inbox_processed([row["tweet_id"] for row in rows], at=utc_now())
                else:
                    with self.db:
                        self.db.executemany("UPDATE hottopic_inbox SET processed_at=? WHERE tweet_id=?", [(iso(utc_now()), row["tweet_id"]) for row in rows])
                self._refresh_hot_topic_counts()
            self.event("aggregate_ok", result.get("metrics", {}))
            # Task 1 only sees deterministic, successfully aggregated HotTopic
            # state. Its own model calls are deliberately outside the HotTopic
            # aggregation transaction.
            if self.event_tracker is not None:
                self.event_tracker.observe_topics()
            return result
        except Exception as exc:
            self._record_worker_failure("aggregate_error", exc)
            return None

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
        if self.postgres_repository is not None:
            return self._process_x_agent_jobs_postgres()
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
                text = self._x_agent_analysis_text(source)
                if text is None:
                    return job, None, None
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

    def _process_x_agent_jobs_postgres(self) -> int:
        repository = self.postgres_repository
        assert repository is not None
        now = utc_now()
        claimed = repository.claim_x_agent_jobs(
            now=now, batch_size=X_AGENT_ANALYSIS_BATCH_SIZE,
            max_attempts=X_AGENT_ANALYSIS_MAX_ATTEMPTS, processing_lease=X_AGENT_PROCESSING_LEASE,
        )
        if not claimed:
            return 0
        analyzer = self.x_agent_analyzer

        def analyze_job(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
            source = job["payload_json"]
            try:
                if analyzer is None:
                    raise RuntimeError(self.x_agent_analyzer_error or "X Agent 模型路由不可用")
                text = self._x_agent_analysis_text(source)
                if text is None or not is_relevant(str(job["module"]), text):
                    return job, None, None
                result = analyzer.analyze(str(job["module"]), source)
                return job, {
                    "actual_model": result.actual_model, "fallback_reason": result.fallback_reason,
                    "items": result.items,
                }, None
            except Exception as exc:
                return job, None, f"{type(exc).__name__}: {exc}"

        completed = 0
        with ThreadPoolExecutor(max_workers=self.analysis_workers, thread_name_prefix="x-agent") as executor:
            futures = [executor.submit(analyze_job, job) for job in claimed]
            for future in as_completed(futures):
                job, result, error = future.result()
                repository.finish_x_agent_job(
                    job["id"], module=str(job["module"]), source=job["payload_json"], result=result,
                    error=error, attempts=int(job["attempts"]), now=utc_now(),
                    claim_updated_at=job.get("claim_updated_at"),
                    retry_delay=X_AGENT_RETRY_DELAY, max_attempts=X_AGENT_ANALYSIS_MAX_ATTEMPTS,
                )
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
        if self.postgres_repository is not None:
            source = job.get("payload_json") or {}
            result_payload = None if result is None else {
                "actual_model": result.actual_model, "fallback_reason": result.fallback_reason,
                "items": result.items,
            }
            self.postgres_repository.finish_x_agent_job(
                job["id"], module=str(job["module"]), source=source, result=result_payload,
                error=f"{type(error).__name__}: {error}" if error else None,
                attempts=int(job["attempts"]), now=utc_now(), retry_delay=X_AGENT_RETRY_DELAY,
                max_attempts=X_AGENT_ANALYSIS_MAX_ATTEMPTS,
            )
            return
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
            direct_json = json.dumps(item["direct_promotion"], ensure_ascii=False, sort_keys=True) if item.get("direct_promotion") else None
            operation_json = json.dumps(item["operation_advice"], ensure_ascii=False, sort_keys=True) if item.get("operation_advice") else None
            identity_payload = json.dumps(
                {"logic": " ".join(str(item["logic"]).lower().split()), "direct": direct_json, "operation": operation_json},
                ensure_ascii=False,
                sort_keys=True,
            )
            logic_hash = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
            self.db.execute(
                "INSERT INTO x_agent_project_observations("
                "identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,logic_hash,direct_promotion_json,operation_advice_json,account_lower,"
                "source_tweet_id,source_url,source_text,first_mentioned_at,last_mentioned_at,actual_model,fallback_reason,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_key,account_lower,logic_hash) DO UPDATE SET "
                "project_name=excluded.project_name,ticker=excluded.ticker,chain_name=excluded.chain_name,"
                "contract_address=excluded.contract_address,official_url=excluded.official_url,logic=excluded.logic,"
                "direct_promotion_json=excluded.direct_promotion_json,operation_advice_json=excluded.operation_advice_json,"
                "source_tweet_id=excluded.source_tweet_id,source_url=excluded.source_url,source_text=excluded.source_text,"
                "last_mentioned_at=excluded.last_mentioned_at,actual_model=excluded.actual_model,fallback_reason=excluded.fallback_reason,updated_at=excluded.updated_at",
                (
                    identity, item["project_name"], item["ticker"], item["chain"], item["contract_address"], item["official_url"],
                    item["logic"], logic_hash, direct_json, operation_json, account, tweet_id, source_url, source_text, posted_at, posted_at,
                    result.actual_model, result.fallback_reason, at, at,
                ),
            )

    @staticmethod
    def _project_identity(item: dict[str, Any], tweet_id: str) -> str:
        name = str(item.get("project_name") or "").strip().lower()
        contract = str(item.get("contract_address") or "").strip()
        if contract:
            normalized_contract = contract.lower() if contract.lower().startswith("0x") else contract
            return f"ca:{normalized_contract}"
        if name:
            return f"name:{name}"
        return f"tweet:{tweet_id}"

    @_serialized
    def retry_failed_x_agent_jobs(self, *, module: str = "", limit: int = 100) -> int:
        if self.postgres_repository is not None:
            return self.postgres_repository.retry_failed_x_agent_jobs(module=module, limit=limit, now=utc_now())
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
        if self.postgres_repository is not None:
            counts = self.postgres_repository.x_agent_dashboard_counts()
            investment = self.investment_lead_queries.dashboard()
            return {
                "generatedAt": iso(utc_now()),
                "accounts": {
                    "total": counts["total"], "hotTopicEnabled": counts["hot_topic_enabled"],
                    "marketSentimentEnabled": counts["market_sentiment_enabled"],
                    "projectPromotionEnabled": counts["project_promotion_enabled"],
                    "investmentLeadsEnabled": int(investment["accounts"]["investmentLeadsEnabled"] or 0),
                    "errors": counts["errors"],
                },
                "jobs": {"pending": counts["pending"], "failed": counts["failed"]},
                "investmentLeads": investment,
            }
        accounts = self.db.execute(
            "SELECT COUNT(*) total,SUM(hot_topic_enabled=1) hot_topic_enabled,"
            "SUM(market_sentiment_enabled=1) market_sentiment_enabled,"
            "SUM(project_promotion_enabled=1) project_promotion_enabled,"
            "SUM((last_error IS NOT NULL AND last_error NOT LIKE '%HTTP Error 404%' "
            "AND last_error NOT LIKE '%\"kind\": \"not_found\"%') "
            "OR last_x_agent_error IS NOT NULL) errors "
            "FROM hottopic_accounts"
        ).fetchone()
        jobs = self.db.execute(
            "SELECT SUM(status IN ('pending','processing')) pending,SUM(status='failed') failed FROM x_agent_analysis_jobs"
        ).fetchone()
        investment = self.investment_lead_queries.dashboard()
        return {
            "generatedAt": iso(utc_now()),
            "accounts": {
                "total": int(accounts["total"] or 0),
                "hotTopicEnabled": int(accounts["hot_topic_enabled"] or 0),
                "marketSentimentEnabled": int(accounts["market_sentiment_enabled"] or 0),
                "projectPromotionEnabled": int(accounts["project_promotion_enabled"] or 0),
                "investmentLeadsEnabled": int(investment["accounts"]["investmentLeadsEnabled"] or 0),
                "errors": int(accounts["errors"] or 0),
            },
            "jobs": {"pending": int(jobs["pending"] or 0), "failed": int(jobs["failed"] or 0)},
            "investmentLeads": investment,
        }

    @_serialized
    def investment_leads_dashboard(self) -> dict[str, Any]:
        return self.investment_lead_queries.dashboard()

    @_serialized
    def list_investment_leads(self, **kwargs: Any) -> dict[str, Any]:
        return self.investment_lead_queries.list_leads(**kwargs)

    @_serialized
    def investment_lead_detail(self, lead_id: str) -> dict[str, Any] | None:
        return self.investment_lead_queries.lead_detail(lead_id)

    @_serialized
    def retry_investment_lead_jobs(self, *, stage: str = "", limit: int = 100) -> int:
        return self.investment_lead_queries.retry_failed(stage=stage, limit=limit)

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
        if self.postgres_repository is not None:
            return self.postgres_repository.list_market_sentiment(
                cutoff=cutoff, query=query, sentiment=sentiment, offset=safe_offset, limit=safe_limit,
            )
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
                MAX(posted_at) AS latest_at,AVG(score) AS average_score
              FROM filtered GROUP BY instrument_key
            ), snapshots AS (
              SELECT *,CASE
                WHEN average_score>=1.5 THEN '极度狂热'
                WHEN average_score>0.25 THEN '偏多/乐观'
                WHEN average_score<=-1.5 THEN '极度恐慌'
                WHEN average_score<-0.25 THEN '偏空/谨慎'
                ELSE '中性/分歧'
              END AS snapshot,CAST(ROUND(average_score * 50) AS INTEGER) AS score
              FROM grouped
            ), latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY instrument_key ORDER BY posted_at DESC,id DESC) AS row_number
              FROM filtered
            ), result_rows AS (
              SELECT l.instrument_key,l.instrument_name,l.ticker,l.scope,s.snapshot,s.score,s.source_count,s.account_count,s.latest_at,
                CASE WHEN l.sentiment=s.snapshot THEN l.reason ELSE '来源态度综合为' || s.snapshot || '，展开查看各来源。' END AS reason
              FROM snapshots s JOIN latest l ON l.instrument_key=s.instrument_key AND l.row_number=1
            )
        """
        total = int(self.db.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()[0])
        rows = self.db.execute(
            f"{cte} SELECT instrument_key,instrument_name,ticker,scope,snapshot,score,source_count,account_count,latest_at,reason "
            f"FROM result_rows{where} ORDER BY account_count DESC,latest_at DESC LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [{
            "instrumentKey": row["instrument_key"], "instrumentName": row["instrument_name"], "ticker": row["ticker"],
            "scope": row["scope"], "sentiment": row["snapshot"], "score": int(row["score"]),
            "mentionCount": int(row["account_count"]), "latestAt": row["latest_at"], "reason": row["reason"],
        } for row in rows], "total": total}

    @_serialized
    def market_sentiment_history(self, instrument_key: str, *, limit: int = 120) -> dict[str, Any]:
        if self.postgres_repository is not None:
            return self.postgres_repository.market_sentiment_history(instrument_key, limit=limit)
        if not instrument_key.strip():
            raise ValueError("instrument_key 不能为空")
        safe_limit = min(240, max(1, int(limit)))
        rows = self.db.execute(
            "SELECT instrument_key,snapshot_at,instrument_name,ticker,sentiment,score,mention_count,source_count "
            "FROM x_agent_sentiment_snapshots WHERE instrument_key=? ORDER BY snapshot_at DESC LIMIT ?",
            (instrument_key, safe_limit),
        ).fetchall()
        items = list(reversed(rows))
        instrument_name = str(items[-1]["instrument_name"]) if items else ""
        ticker = str(items[-1]["ticker"]) if items else ""
        return {
            "instrumentKey": instrument_key,
            "instrumentName": instrument_name,
            "ticker": ticker,
            "items": [{
                "snapshotAt": row["snapshot_at"], "sentiment": row["sentiment"], "score": int(row["score"]),
                "mentionCount": int(row["mention_count"]), "sourceCount": int(row["source_count"]),
            } for row in items],
        }

    @_serialized
    def market_sentiment_detail(
        self,
        instrument_key: str,
        *,
        window: str = "24h",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self.postgres_repository is not None:
            cutoff = self._window_cutoff(window, {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "7d": timedelta(days=7)})
            safe_offset, safe_limit = self._page(offset, limit)
            return self.postgres_repository.market_sentiment_detail(
                instrument_key, cutoff=cutoff, offset=safe_offset, limit=safe_limit,
            )
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
        if self.postgres_repository is not None:
            result = self.postgres_repository.list_project_promotions(
                cutoff=cutoff, query=query, offset=safe_offset, limit=safe_limit,
            )
            for row in result["items"]:
                row["operationAdvice"] = self._project_operation_advice(row["operationAdvice"])
            return result
        params: list[Any] = [cutoff]
        conditions: list[str] = []
        if query.strip():
            conditions.append(
                "LOWER(project_name || ' ' || ticker || ' ' || COALESCE(legacy_logic,'') || ' ' || "
                "COALESCE(direct_promotion_json,'') || ' ' || COALESCE(operation_advice_json,'')) LIKE ?"
            )
            params.append(f"%{query.strip().lower()}%")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        cte = self._project_identity_cte() + """
            , filtered AS (
              SELECT id,merged_identity_key AS identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,
                direct_promotion_json,operation_advice_json,account_lower,last_mentioned_at
              FROM identified WHERE last_mentioned_at>=?
            ), grouped AS (
              SELECT identity_key,COUNT(*) AS source_count,COUNT(DISTINCT account_lower) AS account_count,
                MAX(last_mentioned_at) AS last_mentioned_at,MAX(NULLIF(contract_address,'')) AS contract_address
              FROM filtered GROUP BY identity_key
            ), latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered
            ), direct_latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered WHERE direct_promotion_json IS NOT NULL
            ), operation_latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered WHERE operation_advice_json IS NOT NULL
            ), legacy_latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered WHERE direct_promotion_json IS NULL AND operation_advice_json IS NULL
            ), result_rows AS (
              SELECT l.identity_key,l.project_name,l.ticker,l.chain_name,COALESCE(NULLIF(l.contract_address,''),g.contract_address) AS contract_address,
                l.official_url,l.logic,d.direct_promotion_json,o.operation_advice_json,h.logic AS legacy_logic,
                g.account_count,g.source_count,g.last_mentioned_at
              FROM grouped g JOIN latest l ON l.identity_key=g.identity_key AND l.row_number=1
              LEFT JOIN direct_latest d ON d.identity_key=g.identity_key AND d.row_number=1
              LEFT JOIN operation_latest o ON o.identity_key=g.identity_key AND o.row_number=1
              LEFT JOIN legacy_latest h ON h.identity_key=g.identity_key AND h.row_number=1
            )
        """
        total = int(self.db.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()[0])
        rows = self.db.execute(
            f"{cte} SELECT identity_key,project_name,ticker,chain_name,contract_address,official_url,direct_promotion_json,operation_advice_json,legacy_logic,last_mentioned_at "
            f"FROM result_rows{where} ORDER BY last_mentioned_at DESC LIMIT ? OFFSET ?",
            [*params, safe_limit, safe_offset],
        ).fetchall()
        return {"items": [{
            "identityKey": row["identity_key"], "projectName": row["project_name"], "ticker": row["ticker"],
            "chainName": row["chain_name"], "contractAddress": row["contract_address"], "officialUrl": row["official_url"],
            "directPromotion": self._project_json_object(row["direct_promotion_json"]),
            "operationAdvice": self._project_operation_advice(row["operation_advice_json"]),
            "legacyLogic": row["legacy_logic"],
            "logic": row["legacy_logic"],
            "lastMentionedAt": row["last_mentioned_at"],
        } for row in rows], "total": total}

    @staticmethod
    def _project_json_object(value: str | None) -> dict[str, Any] | None:
        if not value:
            return None
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _project_operation_advice(value: str | None) -> dict[str, str] | None:
        parsed = HotTopicService._project_json_object(value)
        return HotTopicService._format_project_operation_advice(parsed) if parsed is not None else None

    @staticmethod
    def _format_project_operation_advice(value: dict[str, Any]) -> dict[str, str]:
        return {
            "action": str(value.get("action") or ""),
            "route": str(value.get("route") or ""),
            "profitSource": str(value.get("profit_source") or ""),
            "claimedReturn": str(value.get("claimed_return") or ""),
            "fees": str(value.get("fees") or ""),
            "constraints": str(value.get("constraints") or ""),
            "risks": str(value.get("risks") or ""),
            "validUntil": str(value.get("valid_until") or ""),
        }

    @staticmethod
    def _project_identity_cte() -> str:
        return """
            WITH named AS (
              SELECT p.*,
                CASE WHEN trim(project_name)='' THEN 'tweet:' || source_tweet_id ELSE lower(trim(project_name)) END AS project_name_key,
                CASE
                  WHEN lower(trim(contract_address)) LIKE '0x%' THEN NULLIF(lower(trim(contract_address)),'')
                  ELSE NULLIF(trim(contract_address),'')
                END AS normalized_contract_address
              FROM x_agent_project_observations p
            ), project_ca_counts AS (
              SELECT project_name_key,COUNT(DISTINCT normalized_contract_address) AS ca_count,
                MIN(normalized_contract_address) AS only_contract_address
              FROM named GROUP BY project_name_key
            ), identified AS (
              SELECT n.*,
                CASE
                  WHEN n.normalized_contract_address IS NOT NULL THEN 'ca:' || n.normalized_contract_address
                  WHEN c.ca_count=1 THEN 'ca:' || c.only_contract_address
                  WHEN c.ca_count>1 THEN 'name:' || n.project_name_key || ':ca:unresolved'
                  ELSE 'name:' || n.project_name_key
                END AS merged_identity_key
              FROM named n JOIN project_ca_counts c ON c.project_name_key=n.project_name_key
            )
        """

    @_serialized
    def project_promotion_detail(
        self,
        identity_key: str,
        *,
        window: str = "24h",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if self.postgres_repository is not None:
            return self.postgres_repository.project_promotion_detail(
                identity_key, window=window, offset=offset, limit=limit,
                cutoff=self._window_cutoff(window, {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}),
            )
        if not identity_key.strip():
            raise ValueError("identity_key 不能为空")
        cutoff = self._window_cutoff(window, {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)})
        safe_offset, safe_limit = self._page(offset, limit)
        cte = self._project_identity_cte() + """
            , selected_groups AS (
              SELECT merged_identity_key FROM identified WHERE merged_identity_key=?
              UNION
              SELECT MIN(merged_identity_key) FROM identified WHERE identity_key=?
                GROUP BY identity_key HAVING COUNT(DISTINCT merged_identity_key)=1
              UNION
              SELECT DISTINCT merged_identity_key FROM identified WHERE 'tweet:' || source_tweet_id=?
            ), filtered AS (
              SELECT i.id,i.account_lower,i.source_url,i.source_text,i.source_tweet_id,i.logic,i.direct_promotion_json,
                i.operation_advice_json,i.last_mentioned_at,i.actual_model,i.fallback_reason
              FROM identified i JOIN selected_groups g ON g.merged_identity_key=i.merged_identity_key
              WHERE i.last_mentioned_at>=?
            ), legacy_groups AS (
              SELECT source_tweet_id,group_concat(logic,'；') AS logic
              FROM (SELECT DISTINCT source_tweet_id,logic FROM filtered
                WHERE direct_promotion_json IS NULL AND operation_advice_json IS NULL)
              GROUP BY source_tweet_id
            ), direct_groups AS (
              SELECT source_tweet_id,group_concat(direct_promotion_json) AS direct_promotions
              FROM (SELECT DISTINCT source_tweet_id,direct_promotion_json FROM filtered WHERE direct_promotion_json IS NOT NULL)
              GROUP BY source_tweet_id
            ), operation_groups AS (
              SELECT source_tweet_id,group_concat(operation_advice_json) AS operation_advices
              FROM (SELECT DISTINCT source_tweet_id,operation_advice_json FROM filtered WHERE operation_advice_json IS NOT NULL)
              GROUP BY source_tweet_id
            ), latest AS (
              SELECT *,ROW_NUMBER() OVER (PARTITION BY source_tweet_id ORDER BY last_mentioned_at DESC,id DESC) AS row_number
              FROM filtered
            ), result_rows AS (
              SELECT l.id,l.account_lower,l.source_url,l.source_text,l.source_tweet_id,h.logic,
                d.direct_promotions,o.operation_advices,l.last_mentioned_at,
                l.actual_model,l.fallback_reason
              FROM latest l
              LEFT JOIN legacy_groups h ON h.source_tweet_id=l.source_tweet_id
              LEFT JOIN direct_groups d ON d.source_tweet_id=l.source_tweet_id
              LEFT JOIN operation_groups o ON o.source_tweet_id=l.source_tweet_id
              WHERE l.row_number=1
            )
        """
        total = int(self.db.execute(
            f"{cte} SELECT COUNT(*) FROM result_rows",
            (identity_key, identity_key, identity_key, cutoff),
        ).fetchone()[0])
        rows = self.db.execute(
            f"{cte} SELECT id,account_lower,source_url,source_text,source_tweet_id,logic,direct_promotions,operation_advices,last_mentioned_at,actual_model,fallback_reason "
            "FROM result_rows ORDER BY last_mentioned_at DESC,id DESC LIMIT ? OFFSET ?",
            (identity_key, identity_key, identity_key, cutoff, safe_limit, safe_offset),
        ).fetchall()
        return {"items": [{
            "id": row["id"], "account": row["account_lower"], "sourceUrl": row["source_url"],
            "sourceText": row["source_text"], "sourceTweetId": row["source_tweet_id"], "legacyLogic": row["logic"],
            "logic": row["logic"],
            "directPromotions": self._project_json_array(row["direct_promotions"]),
            "operationAdvices": [
                self._format_project_operation_advice(item)
                for item in self._project_json_array(row["operation_advices"])
            ],
            "lastMentionedAt": row["last_mentioned_at"], "actualModel": row["actual_model"],
            "fallbackReason": row["fallback_reason"],
        } for row in rows], "total": total}

    @staticmethod
    def _project_json_array(value: str | None) -> list[dict[str, Any]]:
        if not value:
            return []
        try:
            parsed = json.loads(f"[{value}]")
        except (TypeError, ValueError):
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    @_serialized
    def _refresh_hot_topic_counts(self) -> None:
        if self.postgres_repository is not None:
            self.postgres_repository.refresh_hot_topic_counts()
            return
        with self.db:
            self.db.execute(
                "WITH participation_counts AS MATERIALIZED ("
                "SELECT lower(p.activity_account) AS account_lower,COUNT(DISTINCT p.topic_id) AS topic_count "
                "FROM topic_participations p JOIN topics t ON t.topic_id=p.topic_id "
                "WHERE t.retention_tier='permanent' GROUP BY lower(p.activity_account)), "
                "desired_counts AS MATERIALIZED (SELECT a.screen_name_lower,"
                "COALESCE(pc.topic_count,0) AS topic_count FROM hottopic_accounts a "
                "LEFT JOIN participation_counts pc ON pc.account_lower=a.screen_name_lower) "
                "UPDATE hottopic_accounts SET cumulative_hot_topic_count=desired_counts.topic_count "
                "FROM desired_counts WHERE desired_counts.screen_name_lower=hottopic_accounts.screen_name_lower "
                "AND cumulative_hot_topic_count!=desired_counts.topic_count"
            )

    def _snapshot_x_agent_sentiment(self, current: datetime) -> int:
        """Persist one rolling 12-hour sentiment point for each active instrument."""
        latest = self.db.execute("SELECT MAX(snapshot_at) FROM x_agent_sentiment_snapshots").fetchone()[0]
        if latest and current - datetime.fromisoformat(str(latest)) < timedelta(hours=12):
            return 0
        snapshot_at = iso(current)
        cutoff = iso(current - timedelta(hours=12))
        rows = self.db.execute(
            """
            WITH filtered AS (
              SELECT instrument_key,instrument_name,ticker,account_lower,
                CASE sentiment
                  WHEN '极度恐慌' THEN -2 WHEN '偏空/谨慎' THEN -1 WHEN '中性/分歧' THEN 0
                  WHEN '偏多/乐观' THEN 1 WHEN '极度狂热' THEN 2
                END AS score
              FROM x_agent_sentiment_results
              WHERE posted_at>=? AND sentiment<>'证据不足'
            ), grouped AS (
              SELECT instrument_key,MAX(instrument_name) AS instrument_name,MAX(ticker) AS ticker,
                COUNT(DISTINCT account_lower) AS mention_count,COUNT(*) AS source_count,AVG(score) AS average_score
              FROM filtered GROUP BY instrument_key
            )
            SELECT instrument_key,instrument_name,ticker,mention_count,source_count,
              CAST(ROUND(average_score * 50) AS INTEGER) AS score,
              CASE
                WHEN average_score>=1.5 THEN '极度狂热'
                WHEN average_score>0.25 THEN '偏多/乐观'
                WHEN average_score<=-1.5 THEN '极度恐慌'
                WHEN average_score<-0.25 THEN '偏空/谨慎'
                ELSE '中性/分歧'
              END AS sentiment
            FROM grouped
            """,
            (cutoff,),
        ).fetchall()
        for row in rows:
            self.db.execute(
                "INSERT OR REPLACE INTO x_agent_sentiment_snapshots("
                "instrument_key,snapshot_at,instrument_name,ticker,sentiment,score,mention_count,source_count"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    row["instrument_key"], snapshot_at, row["instrument_name"], row["ticker"], row["sentiment"],
                    row["score"], row["mention_count"], row["source_count"],
                ),
            )
        return len(rows)

    def maintain(self, *, force: bool = False) -> dict[str, int] | None:
        try:
            return self._maintain(force=force)
        except sqlite3.OperationalError as exc:
            if not self._is_sqlite_lock_error(exc):
                raise
            self._record_worker_failure("maintain_busy", exc)
            return None

    def scan_visible_topic_merges(self, *, force: bool = False) -> dict[str, Any] | None:
        if self.aggregator is None:
            raise RuntimeError("PostgreSQL HotTopic aggregation repository is not initialized")
        result = self.aggregator.scan_visible_topics(utc_now(), force=force)
        if result and result.get("error"):
            self.event("topic_merge_scan_failed", {"scan_id": result["scan_id"], "error": result["error"]})
        if result and result.get("merges"):
            if self.event_tracker is not None:
                self.event_tracker.reconcile_topic_merges()
            self._refresh_hot_topic_counts()
        return result

    def _maintain(self, *, force: bool) -> dict[str, int] | None:
        if self.postgres_repository is not None:
            current = utc_now()
            last_maintenance = self.postgres_repository.last_maintenance_at()
            reconciled = self.aggregator.reconcile_recent_topics(current) if force or not last_maintenance or current - last_maintenance >= MAINTENANCE_INTERVAL else {"topic_merges": []}
            state = self.postgres_repository.prune_transient_state(
                at=current, retention_hours=TRANSIENT_RETENTION_HOURS, batch_size=100,
            )
            sentiment_snapshots = self.postgres_repository.snapshot_market_sentiment(current)
            with connect_postgres(
                schema=self.postgres_repository.schema,
                application_name="odaily-hottopic-maintenance",
                settings=self.postgres_repository.settings,
            ) as connection:
                connection.execute(
                    "UPDATE hottopic_meta SET last_maintenance_at=%s,retention_prune_pending=%s WHERE singleton_key='global'",
                    (current, bool(state.get("has_more"))),
                )
            if self.aggregator:
                self._refresh_hot_topic_counts()
            return {
                "deleted_topics": int(state.get("deleted_topics", 0)),
                "deleted_content_items": int(state.get("deleted_content_items", 0)),
                "deleted_claims": int(state.get("deleted_claims", 0)),
                "retention_prune_pending": int(bool(state.get("has_more"))),
                "merged_topics": len(reconciled.get("topic_merges", [])),
                "semantic_review_pending": self.aggregator._count_pending_claim_reviews() if self.aggregator else 0,
                "sentiment_snapshots": sentiment_snapshots,
            }
        with self.lock:
            row = self.db.execute(
                "SELECT last_maintenance_at,retention_prune_pending FROM hottopic_meta WHERE singleton_key='global'"
            ).fetchone()
        current = utc_now()
        maintenance_due = force or not row["last_maintenance_at"] or (
            current - datetime.fromisoformat(row["last_maintenance_at"]) >= MAINTENANCE_INTERVAL
        )
        prune_pending = bool(row["retention_prune_pending"])
        if not maintenance_due and not prune_pending:
            return None

        reconciled: dict[str, Any] = {"topic_merges": [], "semantic_review": {"pending": 0}}
        merge_scan: dict[str, Any] | None = None
        tracking_reconciled = 0
        inbox = events = sentiment_snapshots = 0
        if maintenance_due:
            reconciled = self.aggregator.reconcile_recent_topics(current)
            merge_scan = self.scan_visible_topic_merges(force=force)
            tracking_reconciled = self.event_tracker.reconcile_topic_merges()
            self.event_tracker.observe_topics()
            self._refresh_hot_topic_counts()

        state = self.aggregator.prune_transient_state(
            current, retention_hours=TRANSIENT_RETENTION_HOURS, batch_size=100,
        )
        prune_pending = bool(state.pop("has_more"))
        inbox_candidates: list[int] = []
        event_candidates: list[int] = []
        if maintenance_due or prune_pending:
            reader = connect_sqlite_readonly(self.path, timeout_seconds=30.0)
            try:
                inbox_cutoff = iso(current - timedelta(hours=TRANSIENT_RETENTION_HOURS))
                event_cutoff = iso(current - timedelta(days=EVENT_RETENTION_DAYS))
                inbox_candidates = [
                    int(row["candidate_id"])
                    for row in reader.execute(
                        "SELECT i.rowid AS candidate_id FROM hottopic_inbox i WHERE i.processed_at IS NOT NULL "
                        "AND i.collected_at<? AND NOT EXISTS (SELECT 1 FROM x_agent_analysis_jobs j "
                        "WHERE j.tweet_id=i.tweet_id AND j.status IN ('pending','processing','failed')) LIMIT 500",
                        (inbox_cutoff,),
                    )
                ]
                event_candidates = [
                    int(row["candidate_id"])
                    for row in reader.execute(
                        "SELECT id AS candidate_id FROM hottopic_events WHERE at<? LIMIT 500",
                        (event_cutoff,),
                    )
                ]
            finally:
                reader.close()
        with self.lock:
            with self.db:
                if maintenance_due or prune_pending:
                    inbox_cutoff = iso(current - timedelta(hours=TRANSIENT_RETENTION_HOURS))
                    event_cutoff = iso(current - timedelta(days=EVENT_RETENTION_DAYS))
                    if inbox_candidates:
                        inbox = self.db.execute(
                            "DELETE FROM hottopic_inbox WHERE rowid IN "
                            f"({','.join('?' for _ in inbox_candidates)}) "
                            "AND processed_at IS NOT NULL AND collected_at<? "
                            "AND NOT EXISTS (SELECT 1 FROM x_agent_analysis_jobs j "
                            "WHERE j.tweet_id=hottopic_inbox.tweet_id "
                            "AND j.status IN ('pending','processing','failed'))",
                            [*inbox_candidates, inbox_cutoff],
                        ).rowcount
                    if event_candidates:
                        events = self.db.execute(
                            "DELETE FROM hottopic_events WHERE rowid IN "
                            f"({','.join('?' for _ in event_candidates)}) AND at<?",
                            [*event_candidates, event_cutoff],
                        ).rowcount
                    prune_pending = (
                        prune_pending
                        or len(inbox_candidates) == 500
                        or len(event_candidates) == 500
                    )
                if maintenance_due:
                    sentiment_snapshots = self._snapshot_x_agent_sentiment(current)
                    self.db.execute(
                        "UPDATE hottopic_meta SET last_maintenance_at=? WHERE singleton_key='global'",
                        (iso(current),),
                    )
                self.db.execute(
                    "UPDATE hottopic_meta SET retention_prune_pending=? WHERE singleton_key='global'",
                    (int(prune_pending),),
                )
        return {
            **state,
            "deleted_inbox": inbox,
            "deleted_events": events,
            "sentiment_snapshots": sentiment_snapshots,
            "retention_prune_pending": int(prune_pending),
            "merged_topics": len(reconciled["topic_merges"]) + len((merge_scan or {}).get("merges", [])),
            "tracking_merges": tracking_reconciled,
            "semantic_review_pending": reconciled["semantic_review"].get("pending", 0),
        }

    @_serialized
    def health(self) -> dict[str, Any]:
        if self.postgres_repository is not None:
            counts = self.postgres_repository.topic_health_counts()
            return {
                "deploymentStartedAt": self.deployment_started_at(),
                "accounts": counts["accounts"], "inboxPending": counts["inboxPending"],
                "topics": counts["topics"], "databasePath": "postgresql",
                "candidateResolverEnabled": bool(self.aggregator and self.aggregator.resolver),
                "candidateResolverError": self.candidate_resolver_error,
                "semanticReviewEnabled": bool(self.aggregator and self.aggregator.semantic_reviewer),
                "semanticReviewError": self.semantic_reviewer_error,
                "semanticReviewPending": self.aggregator._count_pending_claim_reviews() if self.aggregator else None,
                "topicMergeReviewError": self.topic_merge_reviewer_error,
                "topicMergeReviewPending": self.postgres_repository.topic_merge_review_pending_count(),
                "collectorTiming": counts["collectorTiming"],
                "collectorTimingUncertainty": counts["collectorTiming"]["timestampUncertainty"],
                "collectorSpool": {
                    "blocked": bool(self.collector_spool and self.collector_spool.blocked_error),
                    "error": self.collector_spool.blocked_error if self.collector_spool else None,
                    "failedRecordCount": len(self.collector_spool.failed_records()) if self.collector_spool else 0,
                    "failedRecordIds": [row.get("spool_id") for row in self.collector_spool.failed_records()] if self.collector_spool else [],
                },
            }
        counts = self.db.execute(
            "SELECT COUNT(*) total,SUM(status='followed') followed,SUM(status='unfollowed') unfollowed,SUM(status='blacklisted') blacklisted,"
            "SUM(last_error IS NOT NULL AND last_error NOT LIKE '%HTTP Error 404%' "
            "AND last_error NOT LIKE '%\"kind\": \"not_found\"%') errors FROM hottopic_accounts"
        ).fetchone()
        topic_counts = self.db.execute("SELECT matching_status,COUNT(*) FROM topics GROUP BY matching_status").fetchall()
        return {"deploymentStartedAt": self.deployment_started_at(), "accounts": dict(counts),
                "inboxPending": self.db.execute("SELECT COUNT(*) FROM hottopic_inbox WHERE processed_at IS NULL").fetchone()[0],
                "topics": dict(topic_counts), "databasePath": str(self.path),
                "candidateResolverEnabled": bool(self.aggregator.resolver),
                "candidateResolverError": self.candidate_resolver_error,
                "semanticReviewEnabled": bool(self.aggregator.semantic_reviewer),
                "semanticReviewError": self.semantic_reviewer_error,
                "semanticReviewPending": self.aggregator._count_pending_claim_reviews(),
                "topicMergeReviewError": self.topic_merge_reviewer_error,
                "topicMergeReviewPending": int(bool(self.aggregator.visible_merge_scan_due(utc_now()) or self.db.execute(
                    "SELECT pending_new_visible OR next_retry_at IS NOT NULL FROM topic_merge_scan_state WHERE singleton_key='global'"
                ).fetchone()[0]))}

    @_serialized
    def dashboard(self) -> dict[str, Any]:
        if self.postgres_repository is not None:
            rows = self.postgres_repository.topic_dashboard_rows(limit=100)
            topics = [{
                "id": row["topic_id"], "title": row["title"] or row["working_title"],
                "brief": self._display_brief(row["brief"]), "startedAt": row["started_at"].isoformat(),
                "firstSeenAt": row["first_seen_at"].isoformat(), "firstGeneratedAt": row["first_seen_at"].isoformat(),
                "lastEvidenceAt": row["last_evidence_at"].isoformat() if row["last_evidence_at"] else None,
                "lastUpdatedAt": (row["last_evidence_at"] or row["first_seen_at"]).isoformat(),
                "lastParticipationAt": row["last_participation_at"].isoformat() if row["last_participation_at"] else None,
                "hotness": row["hotness_score"], "briefGeneratedAt": row["generated_at"].isoformat() if row["generated_at"] else None,
                "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"],
                                 "twentyFourHours": row["participant_count_24h"], "velocity": row["participant_velocity"]},
            } for row in rows]
            return {"generatedAt": iso(utc_now()), "health": self.health(), "topics": topics,
                    "yesterdayHotDiscussion": self.yesterday_hot_discussion.dashboard_status()}
        rows = self.db.execute(
            "SELECT t.topic_id,t.working_title,t.started_at,t.first_seen_at,t.last_evidence_at,t.last_participation_at,"
            "t.participant_count_1h,t.participant_count_6h,t.participant_count_24h,t.participant_velocity,t.hotness_score,"
            "b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
            "WHERE t.matching_status='active' AND t.visibility='visible' ORDER BY t.hotness_score DESC,t.last_evidence_at DESC LIMIT 100"
        ).fetchall()
        topics = [{"id": row["topic_id"], "title": row["title"] or row["working_title"], "brief": self._display_brief(row["brief"]),
                   "startedAt": row["started_at"], "firstSeenAt": row["first_seen_at"],
                   "firstGeneratedAt": row["first_seen_at"], "lastEvidenceAt": row["last_evidence_at"],
                   "lastUpdatedAt": row["last_evidence_at"] or row["first_seen_at"],
                   "lastParticipationAt": row["last_participation_at"], "hotness": row["hotness_score"],
                   "briefGeneratedAt": row["generated_at"], "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"], "twentyFourHours": row["participant_count_24h"], "velocity": row["participant_velocity"]}}
                  for row in rows]
        return {
            "generatedAt": iso(utc_now()),
            "health": self.health(),
            "topics": topics,
            "yesterdayHotDiscussion": self.yesterday_hot_discussion.dashboard_status(),
        }

    @staticmethod
    def _display_brief(value: str | None) -> str:
        if not value:
            return "正文生成中"
        brief = ModelBriefWriter._clean_generated_brief(value)
        return ModelBriefWriter._split_long_brief(brief) if len(brief) >= 180 and "\n" not in brief else brief

    @_serialized
    def topic_detail(self, topic_id: str) -> dict[str, Any] | None:
        if self.postgres_repository is not None:
            row, speakers = self.postgres_repository.topic_detail_rows(topic_id)
            if row is None:
                return None
            return {
                "id": row["topic_id"], "title": row["title"] or row["working_title"],
                "brief": self._display_brief(row["brief"]),
                "firstGeneratedAt": row["first_seen_at"].isoformat(),
                "lastUpdatedAt": (row["last_evidence_at"] or row["first_seen_at"]).isoformat(),
                "hotness": row["hotness_score"],
                "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"],
                                 "twentyFourHours": row["participant_count_24h"]},
                "autoNewsflashAssessment": self.event_tracker.topic_assessment_status(topic_id, first_seen_at=row["first_seen_at"].isoformat()),
                "speakers": [{"account": item["activity_account"],
                              "lastParticipationAt": item["last_participation_at"].isoformat(),
                              "sourceUrl": item["source_url"]} for item in speakers],
            }
        row = self.db.execute(
            "SELECT t.*,b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
            "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
            "WHERE t.topic_id=? AND t.matching_status='active' AND t.visibility='visible'", (topic_id,)
        ).fetchone()
        if row is None:
            return None
        speakers = self.db.execute(
            "SELECT p.activity_account,p.last_participation_at,ci.source_url FROM topic_participations p "
            "LEFT JOIN content_items ci ON ci.content_item_id=p.last_content_item_id WHERE p.topic_id=? ORDER BY p.last_participation_at DESC", (topic_id,)
        ).fetchall()
        return {"id": row["topic_id"], "title": row["title"] or row["working_title"], "brief": self._display_brief(row["brief"]),
                "firstGeneratedAt": row["first_seen_at"],
                "lastUpdatedAt": row["last_evidence_at"] or row["first_seen_at"],
                "hotness": row["hotness_score"], "participants": {"oneHour": row["participant_count_1h"], "sixHours": row["participant_count_6h"], "twentyFourHours": row["participant_count_24h"]},
                "autoNewsflashAssessment": self.event_tracker.topic_assessment_status(topic_id, first_seen_at=str(row["first_seen_at"])),
                "speakers": [{"account": item["activity_account"], "lastParticipationAt": item["last_participation_at"], "sourceUrl": item["source_url"]} for item in speakers]}

    @_serialized
    def auto_newsflash_dashboard(self) -> dict[str, Any]:
        """Read-only projection for autonomous official-event tracking."""
        return self.event_tracker.dashboard()

    @_serialized
    def auto_newsflash_event_detail(self, event_id: str) -> dict[str, Any] | None:
        return self.event_tracker.event_detail(event_id)

    @_serialized
    def auto_newsflash_prompts(self) -> list[dict[str, Any]]:
        # Prompt versions are immutable and intentionally have no mutation API.
        return self.event_tracker.prompt_versions(include_content=True)

    @_serialized
    def dismiss_auto_newsflash_event(self, event_id: str, *, actor: str = "") -> dict[str, Any]:
        return self.event_tracker.dismiss_event(event_id, actor=actor)


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
        stop_collector = threading.Event()

        def collect_loop() -> None:
            # PG collection uses the native repository for both account
            # claiming and poll persistence; do not pass it a SQLite handle.
            database = None if service.postgres_repository is not None else open_worker_database(service.path)
            try:
                while not stop_collector.is_set():
                    try:
                        result = service.poll_due_accounts_once(database, limit=service.workers * 2)
                    except Exception as exc:
                        try:
                            detail = {"error": f"{type(exc).__name__}: {exc}"}
                            if database is None:
                                service.event("collect_loop_error", detail)
                            else:
                                service.event("collect_loop_error", detail, database=database)
                        except Exception as log_exc:
                            print(f"HotTopic collector error: {exc}; log error: {log_exc}", flush=True)
                        stop_collector.wait(2)
                    else:
                        stop_collector.wait(0.05 if result["polled"] else 2)
            finally:
                if database is not None:
                    database.close()

        collector = threading.Thread(target=collect_loop, name="hottopic-collector", daemon=True)
        collector.start()
        try:
            while True:
                service.poll_once(collect_baseline_accounts=False)
                time.sleep(2)
        finally:
            stop_collector.set()
            collector.join()
    finally:
        service.close()
