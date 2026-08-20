from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from packages.common.paths import get_paths
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.publisher import content_to_paragraph_html

from .gmgn import GMGN, ensure_cli_ready, gmgn_subprocess_env
from .narrative import generate_reader_text


CHAIN = "bsc"
SUPPORTED_CHAINS = {"bsc", "robinhood", "solana"}
MARKET_CAP_GATE = 500_000.0
MARKET_CAP_LEVELS = (500_000.0, 1_000_000.0, 3_000_000.0)
TG_MARKET_CAP_GATE = 300_000.0
TG_SOLANA_MARKET_CAP_GATE = 500_000.0
TG_ROBINHOOD_MARKET_CAP_GATE = 1_000_000.0
VOLUME_RATIO_GATE_MIN_CAP = 300_000.0
VOLUME_RATIO_GATE_MAX_CAP = 3_000_000.0
VOLUME_RATIO_GATE_AT_MIN_CAP = 0.5
VOLUME_RATIO_GATE_AT_MAX_CAP = 0.2
TRACKING_WINDOW_SECONDS = 7 * 24 * 60 * 60
COMPLETED_SCAN_INTERVAL_SECONDS = 300
TOKEN_INFO_HIGH_INTERVAL_SECONDS = 300
TOKEN_INFO_LOW_INTERVAL_SECONDS = 3600
TOKEN_INFO_MIN_GAP_SECONDS = 3
TRACKING_STATUS_ACTIVE = "active"
TRACKING_STATUS_EXPIRED = "expired"
TRACKING_STATUS_LEGACY = "legacy_untracked"
QUEUE_EXPIRY_SECONDS = 3600
MAX_JOB_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (60, 300, 900)
PATHS = get_paths()
PROJECT_ROOT = PATHS.root_dir
PROCESSED_DATA_DIR = PATHS.processed_dir
DEFAULT_DB = PATHS.processed_dir / "meme_scanner.sqlite3"
DEFAULT_AUDIT_DIR = PATHS.exports_dir / "meme_scanner"
READER_OPENING = "据Odaily Meme速递监测，"
# Kept in sync with OdAIly's production PushClient default.  A deployment may
# override it with MEME_ODAILY_PUSH_ENDPOINT (or the shared ODAILY_PUSH_ENDPOINT).
DEFAULT_ODAILY_PUSH_ENDPOINT = "http://47.113.217.70:8501/push/data"
READER_TEXT_FORBIDDEN = re.compile(
    r"这里不能写成|不应延伸为|不应写成|只能写为|只能视作|"
    r"项目页面(?:将|称)|项目页(?:将|称)|材料(?:里|中)(?:显示|表明)|"
    r"(?:Grok|X Search)(?:找到|显示)|没有证据(?:证明|表明)|"
    r"非官方(?:币|发行|背书|关联)|不构成(?:官方|投资)|"
    r"风险提示|谨慎参与|DYOR",
    re.IGNORECASE,
)
READER_TEXT_NOT_AN_ANGLE = re.compile(
    r"(?:今日|近日|当前).*Telegram.*(?:集中提及|出现.*提及)|"
    r"Telegram.{0,10}(?:多条|多份).*消息.*重复提及|"
    r"(?:多条|多份).*消息.*重复提及|"
    r"(?:来自|涉及).{0,30}(?:两个|多个|若干)群组.{0,20}(?:多名|多位|用户)|"
    r"(?:有人|还有人|用户).{0,20}(?:感叹|惊呼).{0,30}(?:卧槽|真能飞|起飞)|"
    r"(?:税务|官网).{0,20}(?:链接|页面)",
    re.IGNORECASE,
)
RATE_LIMIT_REMAINING_SECONDS = re.compile(r"~(\d+)s remaining")


@dataclass(frozen=True)
class Token:
    address: str
    platform: str
    name: str
    symbol: str
    market_cap: float
    volume_24h: float
    created_timestamp: int | None
    raw: dict[str, Any]
    chain: str = CHAIN

    @property
    def volume_ratio(self) -> float:
        return self.volume_24h / self.market_cap if self.market_cap > 0 else 0.0


def volume_ratio_gate(market_cap: float) -> float:
    """Return the minimum 24h-volume/market-cap ratio for a token."""
    cap = max(float(market_cap), 0.0)
    if cap <= VOLUME_RATIO_GATE_MIN_CAP:
        return VOLUME_RATIO_GATE_AT_MIN_CAP
    if cap >= VOLUME_RATIO_GATE_MAX_CAP:
        return VOLUME_RATIO_GATE_AT_MAX_CAP
    progress = (cap - VOLUME_RATIO_GATE_MIN_CAP) / (VOLUME_RATIO_GATE_MAX_CAP - VOLUME_RATIO_GATE_MIN_CAP)
    return VOLUME_RATIO_GATE_AT_MIN_CAP + progress * (
        VOLUME_RATIO_GATE_AT_MAX_CAP - VOLUME_RATIO_GATE_AT_MIN_CAP
    )


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def tracking_interval(market_cap: float, args: argparse.Namespace) -> int:
    return (
        int(getattr(args, "token_info_high_interval", TOKEN_INFO_HIGH_INTERVAL_SECONDS))
        if market_cap >= MARKET_CAP_GATE
        else int(getattr(args, "token_info_low_interval", TOKEN_INFO_LOW_INTERVAL_SECONDS))
    )


def tracking_phase_seconds(address: str, interval_seconds: int) -> int:
    digest = hashlib.sha256(f"meme-token-info:{address}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % max(interval_seconds, 1)


def next_tracking_time(address: str, current: datetime, interval_seconds: int) -> str:
    interval = max(int(interval_seconds), 1)
    phase = tracking_phase_seconds(address, interval)
    anchor = int(current.timestamp()) // interval * interval + interval
    candidate = datetime.fromtimestamp(anchor + phase, tz=UTC)
    if candidate <= current:
        candidate += timedelta(seconds=interval)
    return candidate.isoformat()


def number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def timestamp(value: Any) -> int | None:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def normalize_chain(value: Any, *, default: str = CHAIN) -> str:
    normalized = str(value or "").strip().lower().replace("_", "-")
    if normalized in {"sol", "solana"}:
        return "solana"
    if normalized in {"robinhood", "robinhood-chain", "robinhoodchain"}:
        return "robinhood"
    if normalized in {"bsc", "bnb", "bnb-chain", "binance-smart-chain"}:
        return "bsc"
    return default if default in SUPPORTED_CHAINS else CHAIN


def token_from_row(row: dict[str, Any], *, allow_unknown_platform: bool = False) -> Token | None:
    # Launchpad/platform is metadata only. Keep the keyword for compatibility
    # with older callers, but never reject a token based on its platform value.
    address = str(row.get("address") or "").strip().lower()
    platform = str(row.get("launchpad_platform") or row.get("launchpad") or "").strip().lower()
    chain = normalize_chain(row.get("chain"), default="")
    if not address:
        return None
    return Token(
        address=address,
        platform=platform or "telegram",
        name=str(row.get("name") or "").strip(),
        symbol=str(row.get("symbol") or row.get("name") or "?").strip(),
        market_cap=number(row.get("usd_market_cap") or row.get("market_cap")),
        volume_24h=number(row.get("volume_24h")),
        created_timestamp=timestamp(row.get("created_timestamp")),
        raw=row,
        chain="solana" if chain == "solana" or platform == "solana" else chain or CHAIN,
    )


def fetch_completed_tokens(limit: int) -> list[Token]:
    if not ensure_cli_ready():
        raise RuntimeError("GMGN CLI is not ready")
    # Keep the first discovery request broad. GMGN returns a capped candidate
    # set, so platform filters and client-side timestamp sorting can hide tokens.
    command = [GMGN, "market", "trenches", "--chain", CHAIN, "--type", "completed", "--limit", str(limit), "--raw"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=gmgn_subprocess_env(), check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "GMGN query failed")
    payload = json.loads(result.stdout)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    rows = data.get("completed", []) if isinstance(data, dict) else []
    tokens = [token for row in rows if isinstance(row, dict) if (token := token_from_row(row, allow_unknown_platform=True))]
    return tokens


def _find_token_info_row(value: Any, address: str) -> dict[str, Any] | None:
    if isinstance(value, dict):
        candidate_address = str(value.get("address") or value.get("token_address") or "").lower()
        if candidate_address == address and any(key in value for key in ("market_cap", "usd_market_cap")):
            return value
        for child in value.values():
            found = _find_token_info_row(child, address)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_token_info_row(child, address)
            if found:
                return found
    return None


def _recursive_pick(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if key in value and value[key] not in (None, ""):
                candidate = value[key]
                if isinstance(candidate, (dict, list)):
                    nested = _recursive_pick(candidate, keys)
                    if nested not in (None, ""):
                        return nested
                else:
                    return candidate
        for child in value.values():
            found = _recursive_pick(child, keys)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _recursive_pick(child, keys)
            if found not in (None, ""):
                return found
    return None


def fetch_token_info(
    address: str,
    chain: str = CHAIN,
    *,
    allow_unknown_platform: bool = False,
) -> Token | None:
    if not ensure_cli_ready():
        raise RuntimeError("GMGN CLI is not ready")
    requested_chain = normalize_chain(chain)
    command = [GMGN, "token", "info", "--chain", "sol" if requested_chain == "solana" else requested_chain, "--address", address, "--raw"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=gmgn_subprocess_env(), check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "GMGN token-info query failed")
    payload = json.loads(result.stdout)
    row = _find_token_info_row(payload, address.lower())
    top_level = payload if isinstance(payload, dict) and str(payload.get("address") or "").lower() == address.lower() else {}
    normalized = {**top_level, **(row or {})}
    market_cap = number(normalized.get("usd_market_cap") or normalized.get("market_cap"))
    if market_cap <= 0:
        market_cap = number(_recursive_pick(payload, ("usd_market_cap", "market_cap")))
    if market_cap <= 0:
        price = number(_recursive_pick(payload, ("price",)))
        circulating_supply = number(_recursive_pick(payload, ("circulating_supply",)))
        market_cap = price * circulating_supply
    if market_cap <= 0:
        return None
    normalized["usd_market_cap"] = market_cap
    normalized.setdefault("address", address.lower())
    normalized["chain"] = requested_chain
    normalized.setdefault(
        "launchpad_platform",
        _recursive_pick(payload, ("launchpad_platform", "launchpad")) or "telegram",
    )
    for field, aliases in (
        ("volume_24h", ("volume_24h",)),
        ("name", ("name", "token_name")),
        ("symbol", ("symbol", "token_symbol")),
        ("created_timestamp", ("created_timestamp", "creation_timestamp", "open_timestamp")),
    ):
        if normalized.get(field) in (None, ""):
            normalized[field] = _recursive_pick(payload, aliases)
    token = token_from_row(normalized, allow_unknown_platform=allow_unknown_platform)
    return token


def fetch_tg_token_info(address: str, address_chain: str) -> Token | None:
    """Resolve a Telegram CA by chain; launchpad platform is not a gate."""
    if address_chain == "solana":
        return fetch_token_info(address, "solana", allow_unknown_platform=True)
    if address_chain != "evm":
        return None
    # EVM addresses do not encode their network. Probe Robinhood first so a
    # usable Robinhood response cannot be shadowed by a BSC fallback result.
    for chain in ("robinhood", "bsc"):
        token = fetch_token_info(address, chain, allow_unknown_platform=True)
        if token is not None:
            return token
    return None


def tg_market_cap_gate(chain: str) -> float | None:
    normalized = normalize_chain(chain, default="")
    if normalized == "bsc":
        return TG_MARKET_CAP_GATE
    if normalized == "robinhood":
        return TG_ROBINHOOD_MARKET_CAP_GATE
    if normalized == "solana":
        return TG_SOLANA_MARKET_CAP_GATE
    return None


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS scanner_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
              address TEXT PRIMARY KEY, chain TEXT NOT NULL DEFAULT 'bsc',
              platform TEXT NOT NULL, symbol TEXT NOT NULL,
              last_market_cap REAL NOT NULL, highest_market_cap REAL NOT NULL DEFAULT 0,
              last_seen_at TEXT NOT NULL, triggered_at TEXT, published_at TEXT,
              tracking_status TEXT NOT NULL DEFAULT 'legacy_untracked',
              tracking_started_at TEXT, tracking_expires_at TEXT,
              last_completed_seen_at TEXT, last_token_info_at TEXT,
              next_token_info_at TEXT, tracking_interval_seconds INTEGER,
              tracking_source TEXT, last_volume_24h REAL NOT NULL DEFAULT 0,
              token_info_failures INTEGER NOT NULL DEFAULT 0,
              last_token_info_error TEXT
            );
            CREATE TABLE IF NOT EXISTS tg_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
              chain TEXT NOT NULL DEFAULT 'evm', source_chat TEXT,
              detected_at TEXT NOT NULL, window_start TEXT NOT NULL,
              mention_count INTEGER NOT NULL, chat_count INTEGER NOT NULL,
              sender_count INTEGER NOT NULL, evidence_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', reason TEXT,
              market_cap REAL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS token_snapshots (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              address TEXT NOT NULL, chain TEXT NOT NULL, platform TEXT NOT NULL,
              symbol TEXT NOT NULL, name TEXT NOT NULL, market_cap REAL NOT NULL,
              volume_24h REAL NOT NULL, observed_at TEXT NOT NULL,
              source TEXT NOT NULL, scan_id TEXT, payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_token_snapshots_address_time
              ON token_snapshots(address, observed_at, id);
            CREATE INDEX IF NOT EXISTS idx_token_snapshots_scan
              ON token_snapshots(source, scan_id);
            CREATE TABLE IF NOT EXISTS market_cap_milestones (
              address TEXT NOT NULL, chain TEXT NOT NULL, platform TEXT NOT NULL,
              symbol TEXT NOT NULL, level REAL NOT NULL, observed_at TEXT NOT NULL,
              snapshot_id INTEGER NOT NULL, trigger_key TEXT NOT NULL UNIQUE,
              job_id INTEGER, status TEXT NOT NULL,
              PRIMARY KEY(address, level)
            );
            CREATE INDEX IF NOT EXISTS idx_market_cap_milestones_address
              ON market_cap_milestones(address, level);
            CREATE INDEX IF NOT EXISTS idx_tg_candidates_status ON tg_candidates(status, id);
            """
        )
        self._ensure_jobs_v2()
        observation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(observations)")}
        candidate_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tg_candidates)")}
        if "chain" not in observation_columns:
            self.conn.execute("ALTER TABLE observations ADD COLUMN chain TEXT NOT NULL DEFAULT 'bsc'")
            self.conn.execute(
                """UPDATE observations
                   SET chain=COALESCE(
                     (SELECT ts.chain FROM token_snapshots ts
                      WHERE ts.address=observations.address AND ts.chain IS NOT NULL AND ts.chain<>''
                      ORDER BY ts.observed_at DESC, ts.id DESC LIMIT 1),
                     'bsc'
                   )"""
            )
        if "chain" not in candidate_columns:
            self.conn.execute("ALTER TABLE tg_candidates ADD COLUMN chain TEXT NOT NULL DEFAULT 'evm'")
        if "source_chat" not in candidate_columns:
            self.conn.execute("ALTER TABLE tg_candidates ADD COLUMN source_chat TEXT")
        if "highest_market_cap" not in observation_columns:
            self.conn.execute("ALTER TABLE observations ADD COLUMN highest_market_cap REAL NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE observations SET highest_market_cap=last_market_cap")
        observation_migrations = {
            "tracking_status": "TEXT NOT NULL DEFAULT 'legacy_untracked'",
            "tracking_started_at": "TEXT",
            "tracking_expires_at": "TEXT",
            "last_completed_seen_at": "TEXT",
            "last_token_info_at": "TEXT",
            "next_token_info_at": "TEXT",
            "tracking_interval_seconds": "INTEGER",
            "tracking_source": "TEXT",
            "last_volume_24h": "REAL NOT NULL DEFAULT 0",
            "token_info_failures": "INTEGER NOT NULL DEFAULT 0",
            "last_token_info_error": "TEXT",
        }
        for column, definition in observation_migrations.items():
            if column not in observation_columns:
                self.conn.execute(f"ALTER TABLE observations ADD COLUMN {column} {definition}")
        self.conn.execute(
            "UPDATE observations SET tracking_status='legacy_untracked', tracking_source=COALESCE(tracking_source, 'legacy') "
            "WHERE tracking_status IS NULL OR tracking_status=''"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_observations_tracking_due "
            "ON observations(tracking_status, next_token_info_at, tracking_expires_at)"
        )
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        if "attempts" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
        self.conn.commit()
        candidate_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(tg_candidates)")}
        if "chain" not in candidate_columns:
            self.conn.execute("ALTER TABLE tg_candidates ADD COLUMN chain TEXT NOT NULL DEFAULT 'evm'")
        if "source_chat" not in candidate_columns:
            self.conn.execute("ALTER TABLE tg_candidates ADD COLUMN source_chat TEXT")
        self.conn.commit()

    def _ensure_jobs_v2(self) -> None:
        table_sql_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
        if table_sql and "trigger_key" not in table_sql:
            legacy_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
            if "attempts" not in legacy_columns:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            if "next_attempt_at" not in legacy_columns:
                self.conn.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
            self.conn.execute("ALTER TABLE jobs RENAME TO jobs_v1")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
              trigger_key TEXT NOT NULL UNIQUE, trigger_level REAL,
              payload_json TEXT NOT NULL, trigger_kind TEXT NOT NULL, queued_at TEXT NOT NULL,
              status TEXT NOT NULL, reason TEXT, evidence_json TEXT,
              narrative_json TEXT, title TEXT, content TEXT,
              publish_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
              next_attempt_at TEXT, updated_at TEXT NOT NULL
            )"""
        )
        job_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for column in ("processing_started_at", "publishing_started_at", "completed_at"):
            if column not in job_columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_ready ON jobs(status, next_attempt_at, id)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_address ON jobs(address, id)")
        legacy_exists = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs_v1'"
        ).fetchone()
        if legacy_exists:
            self.conn.execute(
                """INSERT OR IGNORE INTO jobs(
                  id, address, trigger_key, payload_json, trigger_kind, queued_at,
                  status, reason, narrative_json, title, content, publish_json,
                  attempts, next_attempt_at, updated_at
                )
                SELECT id, address, 'legacy:' || address, payload_json, trigger_kind, queued_at,
                  status, reason, narrative_json, title, content, publish_json,
                  attempts, next_attempt_at, updated_at
                FROM jobs_v1"""
            )
            self.conn.execute("DROP TABLE jobs_v1")

    def close(self) -> None:
        self.conn.close()

    def initialized(self) -> bool:
        return self.conn.execute("SELECT 1 FROM scanner_meta WHERE key='initialized'").fetchone() is not None

    def mark_initialized(self) -> None:
        self.conn.execute("INSERT OR REPLACE INTO scanner_meta(key, value) VALUES('initialized', ?)", (now_iso(),))
        self.conn.commit()

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM scanner_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO scanner_meta(key, value) VALUES(?, ?)",
            (key, value or now_iso()),
        )
        self.conn.commit()

    def observation(self, address: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM observations WHERE address=?", (address,)).fetchone()

    def record_snapshot(self, token: Token, *, observed_at: str, source: str, scan_id: str | None = None) -> int:
        cursor = self.conn.execute(
            """INSERT INTO token_snapshots(
              address, chain, platform, symbol, name, market_cap, volume_24h,
              observed_at, source, scan_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (token.address, token.chain, token.platform, token.symbol, token.name,
             token.market_cap, token.volume_24h, observed_at, source, scan_id,
             json.dumps(token.raw, ensure_ascii=False)),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def record_milestone(self, token: Token, *, level: float, observed_at: str, snapshot_id: int) -> bool:
        trigger_key = f"market_cap:{token.address}:{int(level)}"
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO market_cap_milestones(
              address, chain, platform, symbol, level, observed_at, snapshot_id,
              trigger_key, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'detected')""",
            (token.address, token.chain, token.platform, token.symbol, level, observed_at,
             snapshot_id, trigger_key),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def upsert_observation(
        self,
        token: Token,
        *,
        triggered_at: str | None = None,
        published_at: str | None = None,
        advance_market_cap_high_watermark: bool = True,
        tracking_source: str = "legacy",
    ) -> None:
        old = self.observation(token.address)
        if advance_market_cap_high_watermark:
            observed_high = token.market_cap
        elif old:
            observed_high = float(old["highest_market_cap"] or 0)
        else:
            observed_high = 0.0
        self.conn.execute(
            """INSERT INTO observations(
              address, chain, platform, symbol, last_market_cap, highest_market_cap, last_seen_at,
              triggered_at, published_at, tracking_status, tracking_source, last_volume_24h
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET chain=excluded.chain, platform=excluded.platform, symbol=excluded.symbol,
              last_market_cap=excluded.last_market_cap, last_seen_at=excluded.last_seen_at,
              last_volume_24h=excluded.last_volume_24h,
              highest_market_cap=MAX(observations.highest_market_cap, excluded.highest_market_cap),
              triggered_at=COALESCE(excluded.triggered_at, observations.triggered_at),
              published_at=COALESCE(excluded.published_at, observations.published_at)""",
            (
                token.address,
                token.chain,
                token.platform,
                token.symbol,
                token.market_cap,
                observed_high,
                now_iso(),
                triggered_at or (old["triggered_at"] if old else None),
                published_at or (old["published_at"] if old else None),
                TRACKING_STATUS_LEGACY,
                tracking_source,
                token.volume_24h,
            ),
        )
        self.conn.commit()

    def start_or_observe_completed(
        self,
        token: Token,
        *,
        observed_at: str,
        tracking_window_seconds: int,
        args: argparse.Namespace,
    ) -> tuple[float, bool, str]:
        """Register a completed discovery or refresh an already active window."""
        old = self.observation(token.address)
        previous_high = float(old["highest_market_cap"] or old["last_market_cap"] or 0) if old else 0.0
        if old and str(old["tracking_source"] or "") == "tg":
            previous_high = 0.0
        expires_at = parse_iso(old["tracking_expires_at"]) if old else None
        observed_time = parse_iso(observed_at) or datetime.now(UTC)
        if old and old["tracking_status"] == TRACKING_STATUS_ACTIVE and expires_at and expires_at <= observed_time:
            self.expire_tracking(observed_at)
            old = self.observation(token.address)

        status = str(old["tracking_status"]) if old else None
        if old and status in (TRACKING_STATUS_LEGACY, TRACKING_STATUS_EXPIRED) and str(old["tracking_source"] or "") == "tg":
            started_at = str(old["tracking_started_at"] or observed_at)
            expires = str(old["tracking_expires_at"] or (observed_time + timedelta(seconds=max(int(tracking_window_seconds), 1))).isoformat())
            interval = tracking_interval(token.market_cap, args)
            next_at = next_tracking_time(token.address, observed_time, interval)
            self.conn.execute(
                """UPDATE observations SET platform=?, symbol=?, last_market_cap=?,
                  chain=?,
                  highest_market_cap=MAX(highest_market_cap, ?), last_seen_at=?,
                  tracking_status='active', tracking_started_at=?, tracking_expires_at=?,
                  last_completed_seen_at=?, next_token_info_at=?, tracking_interval_seconds=?,
                  tracking_source='completed', last_volume_24h=? WHERE address=?""",
                (token.platform, token.symbol, token.market_cap, token.chain, token.market_cap, observed_at,
                 started_at, expires, observed_at, next_at, interval, token.volume_24h, token.address),
            )
            self.conn.commit()
            return previous_high, False, TRACKING_STATUS_ACTIVE
        if old and status in (TRACKING_STATUS_LEGACY, TRACKING_STATUS_EXPIRED):
            self.conn.execute(
                """UPDATE observations SET platform=?, symbol=?, last_market_cap=?,
                  chain=?,
                  highest_market_cap=MAX(highest_market_cap, ?), last_seen_at=?,
                  last_completed_seen_at=?, last_volume_24h=? WHERE address=?""",
                (token.platform, token.symbol, token.market_cap, token.chain, token.market_cap, observed_at, observed_at, token.volume_24h, token.address),
            )
            self.conn.commit()
            return previous_high, False, status

        interval = tracking_interval(token.market_cap, args)
        next_at = next_tracking_time(token.address, observed_time, interval)
        if old:
            started_at = str(old["tracking_started_at"])
            expires = str(old["tracking_expires_at"])
            self.conn.execute(
                """UPDATE observations SET platform=?, symbol=?, last_market_cap=?,
                  chain=?,
                  highest_market_cap=MAX(highest_market_cap, ?), last_seen_at=?,
                  last_completed_seen_at=?, next_token_info_at=?, tracking_interval_seconds=?,
                  tracking_source='completed', last_volume_24h=?, token_info_failures=0,
                  last_token_info_error=NULL WHERE address=? AND tracking_status='active'""",
                (token.platform, token.symbol, token.market_cap, token.chain, token.market_cap, observed_at, observed_at, next_at, interval, token.volume_24h, token.address),
            )
        else:
            started_at = observed_at
            expires = (observed_time + timedelta(seconds=max(int(tracking_window_seconds), 1))).isoformat()
            self.conn.execute(
                """INSERT INTO observations(
                  address, chain, platform, symbol, last_market_cap, highest_market_cap, last_seen_at,
                  tracking_status, tracking_started_at, tracking_expires_at, last_completed_seen_at,
                  next_token_info_at, tracking_interval_seconds, tracking_source, last_volume_24h
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, 'completed', ?)""",
                (token.address, token.chain, token.platform, token.symbol, token.market_cap, token.market_cap, observed_at, started_at, expires, observed_at, next_at, interval, token.volume_24h),
            )
        self.conn.commit()
        return previous_high, old is None, TRACKING_STATUS_ACTIVE

    def observe_token_info(
        self,
        token: Token,
        *,
        observed_at: str,
        args: argparse.Namespace,
    ) -> float | None:
        old = self.observation(token.address)
        if old is None or old["tracking_status"] != TRACKING_STATUS_ACTIVE:
            return None
        previous_high = float(old["highest_market_cap"] or old["last_market_cap"] or 0)
        expires_at = parse_iso(old["tracking_expires_at"])
        observed_time = parse_iso(observed_at) or datetime.now(UTC)
        if expires_at and expires_at <= observed_time:
            self.expire_tracking(observed_at)
            return None
        interval = tracking_interval(token.market_cap, args)
        self.conn.execute(
            """UPDATE observations SET platform=?, symbol=?, last_market_cap=?,
              chain=?,
              highest_market_cap=MAX(highest_market_cap, ?), last_seen_at=?,
              last_token_info_at=?, next_token_info_at=?, tracking_interval_seconds=?,
              tracking_source='token_info', last_volume_24h=?, token_info_failures=0,
              last_token_info_error=NULL WHERE address=? AND tracking_status='active'""",
            (token.platform, token.symbol, token.market_cap, token.chain, token.market_cap, observed_at, observed_at, next_tracking_time(token.address, observed_time, interval), interval, token.volume_24h, token.address),
        )
        self.conn.commit()
        return previous_high

    def mark_tracking_triggered(self, address: str) -> None:
        self.conn.execute("UPDATE observations SET triggered_at=COALESCE(triggered_at, ?) WHERE address=?", (now_iso(), address))
        self.conn.commit()

    def expire_tracking(self, now: str | None = None) -> int:
        stamp = now or now_iso()
        cursor = self.conn.execute(
            """UPDATE observations SET tracking_status='expired', next_token_info_at=NULL,
              tracking_interval_seconds=NULL WHERE tracking_status='active'
              AND tracking_expires_at IS NOT NULL AND tracking_expires_at<=?""",
            (stamp,),
        )
        self.conn.commit()
        return cursor.rowcount

    def due_token_info(self, *, now: str, latest_completed_seen_at: str | None) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM observations WHERE tracking_status='active'
              AND tracking_expires_at>? AND (next_token_info_at IS NULL OR next_token_info_at<=?)
              AND (last_completed_seen_at IS NULL OR last_completed_seen_at<>?)
              ORDER BY COALESCE(next_token_info_at, '') ASC, address ASC LIMIT 1""",
            (now, now, latest_completed_seen_at or ""),
        ).fetchone()

    def token_info_backlog(self, *, now: str, latest_completed_seen_at: str | None) -> int:
        row = self.conn.execute(
            """SELECT COUNT(*) AS count FROM observations WHERE tracking_status='active'
              AND tracking_expires_at>? AND (next_token_info_at IS NULL OR next_token_info_at<=?)
              AND (last_completed_seen_at IS NULL OR last_completed_seen_at<>?)""",
            (now, now, latest_completed_seen_at or ""),
        ).fetchone()
        return int(row["count"] or 0)

    def record_token_info_failure(self, address: str, *, error: str, next_at: str, observed_at: str) -> None:
        self.conn.execute(
            """UPDATE observations SET token_info_failures=token_info_failures+1,
              last_token_info_error=?, next_token_info_at=?, last_token_info_at=?, last_seen_at=?
              WHERE address=? AND tracking_status='active'""",
            (error[:1000], next_at, observed_at, observed_at, address),
        )
        self.conn.commit()

    def token_info_request_allowed(self, now: datetime, min_gap_seconds: int) -> tuple[bool, float]:
        value = self.meta("token_info_last_request_at")
        last = parse_iso(value)
        remaining = max(0.0, float(min_gap_seconds) - ((now - last).total_seconds() if last else float("inf")))
        return remaining <= 0, remaining

    def token_info_backoff_remaining(self, now: datetime) -> float:
        until = parse_iso(self.meta("token_info_backoff_until"))
        return max(0.0, (until - now).total_seconds()) if until else 0.0

    def set_token_info_backoff(self, until: datetime) -> None:
        self.set_meta("token_info_backoff_until", until.isoformat())

    def mark_token_info_request(self, now: datetime) -> None:
        self.set_meta("token_info_last_request_at", now.isoformat())

    def add_job(
        self,
        token: Token,
        trigger_kind: str,
        status: str,
        reason: str | None = None,
        *,
        trigger_key: str | None = None,
        trigger_level: float | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        payload = json.dumps(token.raw, ensure_ascii=False)
        key = trigger_key or f"{trigger_kind}:{token.address}"
        cursor = self.conn.execute(
            """INSERT OR IGNORE INTO jobs(
              address, trigger_key, trigger_level, payload_json, trigger_kind, queued_at,
              status, reason, evidence_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                token.address,
                key,
                trigger_level,
                payload,
                trigger_kind,
                now_iso(),
                status,
                reason,
                json.dumps(evidence, ensure_ascii=False) if evidence else None,
                now_iso(),
            ),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def next_job(self, address: str | None = None) -> sqlite3.Row | None:
        now = now_iso()
        if address:
            return self.conn.execute(
                """SELECT * FROM jobs
                WHERE address=? AND (status='queued' OR (status='retry_wait' AND COALESCE(next_attempt_at, '')<=?))""",
                (address, now),
            ).fetchone()
        return self.conn.execute(
            """SELECT * FROM jobs
            WHERE status='queued' OR (status='retry_wait' AND COALESCE(next_attempt_at, '')<=?)
            ORDER BY id LIMIT 1""",
            (now,),
        ).fetchone()

    def claim_job(self, address: str | None = None) -> sqlite3.Row | None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            job = self.next_job(address)
            if job is None:
                self.conn.commit()
                return None
            stamp = now_iso()
            self.conn.execute(
                "UPDATE jobs SET status='processing', attempts=attempts+1, next_attempt_at=NULL, "
                "processing_started_at=COALESCE(processing_started_at, ?), updated_at=? WHERE id=?",
                (stamp, stamp, job["id"]),
            )
            claimed = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
            self.conn.commit()
            return claimed
        except Exception:
            self.conn.rollback()
            raise

    def recover_inflight(self) -> int:
        cursor = self.conn.execute(
            """UPDATE jobs SET status='retry_wait', reason='service_restarted',
              next_attempt_at=?, updated_at=?
            WHERE status IN ('processing', 'publishing')""",
            (now_iso(), now_iso()),
        )
        self.conn.commit()
        return cursor.rowcount

    def retry_job(self, job: sqlite3.Row, reason: str, *, narrative: dict[str, Any] | None = None) -> str:
        attempts = int(job["attempts"] or 0)
        if attempts >= MAX_JOB_ATTEMPTS:
            self.update_job(job["id"], "discarded", reason=f"transient_exhausted:{reason}", narrative=narrative)
            return "transient_exhausted"
        delay = RETRY_DELAYS_SECONDS[min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        next_attempt_at = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        self.conn.execute(
            """UPDATE jobs SET status='retry_wait', reason=?, narrative_json=COALESCE(?, narrative_json),
              next_attempt_at=?, updated_at=? WHERE id=?""",
            (
                reason,
                json.dumps(narrative, ensure_ascii=False) if narrative else None,
                next_attempt_at,
                now_iso(),
                job["id"],
            ),
        )
        self.conn.commit()
        return "retry_wait"

    def force_requeue(self, token: Token) -> None:
        """Explicit operator replay, including jobs previously archived at startup."""
        stamp = now_iso()
        self.conn.execute(
            """INSERT INTO jobs(address, trigger_key, payload_json, trigger_kind, queued_at, status, updated_at)
            VALUES (?, ?, ?, 'manual_replay', ?, 'queued', ?)""",
            (token.address, f"manual_replay:{token.address}:{stamp}", json.dumps(token.raw, ensure_ascii=False), stamp, stamp),
        )
        self.conn.commit()

    def update_job(self, job_id: int, status: str, *, reason: str | None = None, narrative: dict[str, Any] | None = None, title: str | None = None, content: str | None = None, publish: dict[str, Any] | None = None) -> None:
        stamp = now_iso()
        publishing_started_at = stamp if status == "publishing" else None
        completed_at = stamp if status in {"publisher_pending", "discarded", "publish_failed"} else None
        self.conn.execute(
            """UPDATE jobs SET status=?, reason=COALESCE(?, reason), narrative_json=COALESCE(?, narrative_json),
            title=COALESCE(?, title), content=COALESCE(?, content), publish_json=COALESCE(?, publish_json),
            publishing_started_at=COALESCE(publishing_started_at, ?), completed_at=COALESCE(completed_at, ?), updated_at=? WHERE id=?""",
            (status, reason, json.dumps(narrative, ensure_ascii=False) if narrative else None, title, content,
             json.dumps(publish, ensure_ascii=False) if publish else None,
             publishing_started_at, completed_at, stamp, job_id),
        )
        self.conn.commit()

    def next_tg_candidate(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tg_candidates WHERE status='pending' ORDER BY updated_at, id LIMIT 1"
        ).fetchone()

    def update_tg_candidate(
        self,
        candidate_id: int,
        status: str,
        *,
        reason: str | None = None,
        market_cap: float | None = None,
    ) -> None:
        self.conn.execute(
            """UPDATE tg_candidates SET status=?, reason=?, market_cap=COALESCE(?, market_cap), updated_at=?
            WHERE id=?""",
            (status, reason, market_cap, now_iso(), candidate_id),
        )
        self.conn.commit()


def display_market_cap(value: float) -> str:
    return f"{value / 10_000:.0f}"


def format_text(token: Token, narrative: str, sampled_at: datetime, trigger_kind: str = "market_cap_milestone") -> tuple[str, str]:
    del sampled_at
    cap = display_market_cap(token.market_cap)
    chain_label = {"solana": "Solana", "robinhood": "Robinhood"}.get(token.chain, "BSC")
    if trigger_kind == "tg_burst":
        title = f"Meme速递：{chain_label}上{token.symbol}社群热议中，市值{cap}万美元"
        summary = f"{chain_label}上{token.symbol}社群热议中，GMGN显示当前市值为{cap}万美元。"
    else:
        title = f"Meme速递：{chain_label}上{token.symbol}市值突破{cap}万美元"
        summary = f"{chain_label}上{token.symbol}市值突破{cap}万美元。"
    reader_text = narrative.strip()
    if reader_text.startswith(READER_OPENING):
        content = f"{READER_OPENING}{summary}\n\n{reader_text[len(READER_OPENING):].lstrip()}"
    else:
        content = f"{READER_OPENING}{summary}\n\n{reader_text}"
    return normalize_writer2(title), normalize_writer2(content)


def normalize_writer2(value: str) -> str:
    """Copied narrowly from Writer 2: whitespace, punctuation, and paragraph cleanup only."""
    lines = [" ".join(line.strip().split()) for line in value.replace("\r\n", "\n").split("\n")]
    paragraphs = [line for line in lines if line]
    return "\n\n".join(paragraphs).replace("。。", "。").replace("，，", "，").strip()


def default_narrative_command(token: Token, output: Path) -> list[str]:
    telegram_session = os.environ.get("MEME_TELEGRAM_SESSION") or str(PROCESSED_DATA_DIR / "meme_telegram")
    return [
        sys.executable,
        "-m",
        "backend.src.main",
        "narrative",
        "generate",
        "--chain",
        token.chain,
        "--contract",
        token.address,
        "--telegram-session",
        telegram_session,
        "--output",
        str(output),
    ]


def render_command(template: str, token: Token, output: Path) -> list[str]:
    values = {"contract": token.address, "symbol": token.symbol, "name": token.name, "output": str(output)}
    return [part.format(**values) for part in template.split()]


def collect_narrative(
    token: Token,
    *,
    command_template: str | None,
    audit_dir: Path,
    timeout: int,
    audit_suffix: str | None = None,
    database_path: Path | None = None,
    evidence: dict[str, Any] | None = None,
    trigger_kind: str = "market_cap_milestone",
) -> dict[str, Any]:
    audit_dir.mkdir(parents=True, exist_ok=True)
    safe_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "-", audit_suffix or "").strip("-")
    output = audit_dir / f"{token.address}{'-' + safe_suffix if safe_suffix else ''}.narrative.json"
    if not command_template:
        narrative = generate_reader_text(
            address=token.address,
            symbol=token.symbol,
            chain=token.chain,
            trigger_kind=trigger_kind,
            database_path=database_path or DEFAULT_DB,
            evidence=evidence,
            timeout=timeout,
            audit_output=output,
        )
        output.write_text(json.dumps(narrative, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            **narrative,
            "command": None,
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "output_path": str(output),
        }
    command = render_command(command_template, token, output) if command_template else default_narrative_command(token, output)
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": str(exc.stdout or "")[-4000:],
            "stderr": str(exc.stderr or "")[-4000:],
            "output_path": str(output),
            "reader_text": "",
            "transient_error": "narrative_timeout",
        }
    except OSError as exc:
        return {
            "command": command,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "output_path": str(output),
            "reader_text": "",
            "transient_error": "narrative_process_error",
        }

    text = ""
    payload: dict[str, Any] = {}
    if output.exists():
        try:
            loaded = json.loads(output.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                payload = loaded
                text = str(loaded.get("reader_text") or loaded.get("output_text") or "").strip()
        except json.JSONDecodeError:
            pass
    diagnostics = payload.get("grok_diagnostics") if isinstance(payload.get("grok_diagnostics"), list) else []
    transient_status = next(
        (
            int(item["http_status"])
            for item in diagnostics
            if isinstance(item, dict)
            and isinstance(item.get("http_status"), int)
            and int(item["http_status"]) >= 400
        ),
        None,
    )
    narrative = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
        "output_path": str(output),
        "reader_text": text,
        "performance": payload.get("performance"),
        "grok_diagnostics": diagnostics,
    }
    if result.returncode != 0:
        narrative["transient_error"] = "narrative_command_failed"
    elif transient_status is not None and not text:
        narrative["transient_error"] = f"grok_http_{transient_status}"
    return narrative


def validate_reader_text(value: str) -> str | None:
    """Return a policy error when reader text leaks internal-review language."""
    match = READER_TEXT_FORBIDDEN.search(value)
    if match:
        return f"forbidden_reader_text_phrase:{match.group(0)}"
    angle_match = READER_TEXT_NOT_AN_ANGLE.search(value)
    if angle_match:
        return f"not_final_angle_phrase:{angle_match.group(0)}"
    return None


def log_narrative_result(job: sqlite3.Row, token: Token, narrative: dict[str, Any]) -> None:
    counts = narrative.get("material_counts") if isinstance(narrative.get("material_counts"), dict) else {}
    print(
        "[meme-scan] narrative "
        f"job_id={job['id']} address={token.address} "
        f"status={narrative.get('status') or 'unknown'} "
        f"failure_stage={narrative.get('failure_stage') or '-'} "
        f"failure_code={narrative.get('failure_code') or narrative.get('decision_code') or '-'} "
        f"telegram={counts.get('telegram_messages', 0)} "
        f"x_posts={counts.get('x_posts', 0)} "
        f"grok_materials={counts.get('grok_narrative_materials', 0)} "
        f"reader_text={'yes' if str(narrative.get('reader_text') or '').strip() else 'no'}"
    )


def push_pending(
    title: str,
    content: str,
    *,
    endpoint: str,
    timeout: int,
    send: bool,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "title": title,
        "content": content_to_paragraph_html(content),
        "isPublish": False,
        "isPush": False,
    }
    if not send:
        return {"ok": True, "dry_run": True, "payload": payload}
    headers = {"Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            response = requests.post(endpoint, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            return {"ok": True, "status_code": response.status_code, "response_text": response.text[:1000], "payload": payload}
        except requests.RequestException as exc:
            last_error = exc
            if attempt == 1:
                time.sleep(60)
    return {"ok": False, "error": str(last_error) if last_error else "push failed", "payload": payload}


def process_one(store: Store, args: argparse.Namespace, *, address: str | None = None) -> str | None:
    job = store.claim_job(address)
    if job is None:
        return None
    queued_at = datetime.fromisoformat(job["queued_at"])
    if (datetime.now(UTC) - queued_at).total_seconds() > QUEUE_EXPIRY_SECONDS:
        store.update_job(job["id"], "discarded", reason="queue_expired")
        return "queue_expired"
    raw = json.loads(job["payload_json"])
    token = token_from_row(raw)
    if token is None:
        store.update_job(job["id"], "discarded", reason="invalid_token_payload")
        return "invalid_token_payload"
    narrative = collect_narrative(
        token,
        command_template=args.narrative_command,
        audit_dir=Path(args.audit_dir),
        timeout=args.narrative_timeout,
        audit_suffix=str(job["trigger_key"]),
        database_path=(
            Path(args.db)
            if getattr(args, "db", None)
            else Path(store.conn.execute("PRAGMA database_list").fetchone()[2])
        ),
        evidence=json.loads(job["evidence_json"]) if job["evidence_json"] else None,
        trigger_kind=str(job["trigger_kind"]),
    )
    log_narrative_result(job, token, narrative)
    reader_text = str(narrative.get("reader_text") or "").strip()
    if narrative.get("transient_error"):
        return store.retry_job(job, str(narrative["transient_error"]), narrative=narrative)
    if not reader_text:
        store.update_job(job["id"], "discarded", reason="no_usable_narrative", narrative=narrative)
        return "no_usable_narrative"
    policy_error = validate_reader_text(reader_text)
    if policy_error:
        store.update_job(job["id"], "discarded", reason=policy_error, narrative=narrative)
        return policy_error
    title, content = format_text(token, reader_text, queued_at, str(job["trigger_kind"]))
    store.update_job(job["id"], "publishing", narrative=narrative, title=title, content=content)
    endpoint = os.environ.get("MEME_ODAILY_PUSH_ENDPOINT") or os.environ.get("ODAILY_PUSH_ENDPOINT") or DEFAULT_ODAILY_PUSH_ENDPOINT
    pushed = push_pending(
        title,
        content,
        endpoint=endpoint,
        timeout=args.push_timeout,
        send=args.send,
        idempotency_key=f"odaily:meme:{token.chain}:{token.address}:{job['trigger_key']}",
    )
    if pushed["ok"]:
        store.update_job(job["id"], "publisher_pending", publish=pushed)
        store.upsert_observation(
            token,
            published_at=now_iso(),
            advance_market_cap_high_watermark=str(job["trigger_kind"]) != "tg_burst",
        )
        if args.send:
            try:
                LocalEditorPluginFeedWriter().upsert_meme_digest(
                    job_id=int(job["id"]),
                    address=token.address,
                    platform=token.platform,
                    symbol=token.symbol,
                    title=title,
                    content=content,
                    trigger_kind=str(job["trigger_kind"]),
                    market_cap=token.market_cap,
                    occurred_at=datetime.now(UTC),
                )
            except Exception as exc:
                print(f"[meme-scan] feed write failed job_id={job['id']} error={exc}")
        return "publisher_pending"
    else:
        store.update_job(job["id"], "publish_failed", publish=pushed)
        return "publish_failed"


def milestone_level(previous_high: float, current: float) -> float | None:
    crossed = [level for level in MARKET_CAP_LEVELS if previous_high < level <= current]
    return max(crossed) if crossed else None


def completed_scan_due(store: Store, interval_seconds: int) -> bool:
    last = parse_iso(store.meta("completed_scan_at"))
    return last is None or (datetime.now(UTC) - last).total_seconds() >= max(int(interval_seconds), 1)


def evaluate_market_token(
    store: Store,
    token: Token,
    *,
    bootstrap: bool,
    previous_high: float | None = None,
    persist_observation: bool = True,
    snapshot_id: int | None = None,
    snapshot_source: str = "completed",
) -> tuple[int, int]:
    observed = store.observation(token.address)
    if previous_high is None:
        previous_high = float(observed["highest_market_cap"] or observed["last_market_cap"] or 0) if observed else 0.0
    snapshot_id = snapshot_id or store.record_snapshot(
        token,
        observed_at=now_iso(),
        source=snapshot_source,
        scan_id=store.meta("completed_scan_at"),
    )
    if bootstrap:
        if persist_observation:
            store.upsert_observation(token)
        if token.market_cap >= MARKET_CAP_GATE:
            inserted = store.add_job(
                token,
                "startup_seen",
                "discarded",
                "startup_seen",
                trigger_key=f"startup:{token.address}",
            )
            return (0, int(inserted))
        return (0, 0)
    level = milestone_level(previous_high, token.market_cap)
    if persist_observation:
        store.upsert_observation(token, triggered_at=now_iso() if level else None)
    elif level:
        store.mark_tracking_triggered(token.address)
    if level is None:
        return (0, 0)
    if not store.record_milestone(token, level=level, observed_at=now_iso(), snapshot_id=snapshot_id):
        return (0, 0)
    trigger_key = f"market_cap:{token.address}:{int(level)}"
    if token.volume_ratio < volume_ratio_gate(token.market_cap):
        inserted = store.add_job(
            token,
            "market_cap_milestone",
            "discarded",
            "volume_gate_failed",
            trigger_key=trigger_key,
            trigger_level=level,
        )
        return (0, int(inserted))
    inserted = store.add_job(
        token,
        "market_cap_milestone",
        "queued",
        trigger_key=trigger_key,
        trigger_level=level,
    )
    return (int(inserted), 0)


def rate_limit_delay_seconds(error: str) -> int:
    match = RATE_LIMIT_REMAINING_SECONDS.search(error)
    if match:
        return int(match.group(1))
    retry_after = re.search(r"retry[- ]after\D+(\d+)", error, re.IGNORECASE)
    return int(retry_after.group(1)) if retry_after else 0


def process_due_token_info(store: Store, args: argparse.Namespace) -> dict[str, Any]:
    """Run at most one persisted token_info task; the caller provides serialization."""
    now = datetime.now(UTC)
    stamp = now.isoformat()
    expired = store.expire_tracking(stamp)
    backoff_remaining = store.token_info_backoff_remaining(now)
    if backoff_remaining > 0:
        return {"status": "rate_limited_server", "remaining": backoff_remaining, "expired": expired}
    latest_completed = store.meta("completed_scan_at")
    row = store.due_token_info(now=stamp, latest_completed_seen_at=latest_completed)
    if row is None:
        return {"status": "idle", "expired": expired}
    allowed, remaining = store.token_info_request_allowed(
        now,
        int(getattr(args, "token_info_min_gap", TOKEN_INFO_MIN_GAP_SECONDS)),
    )
    if not allowed:
        return {"status": "rate_limited_local", "remaining": remaining, "expired": expired}

    address = str(row["address"])
    backlog = store.token_info_backlog(now=stamp, latest_completed_seen_at=latest_completed)
    if backlog > 1:
        print(f"[meme-scan] token_info backlog={backlog}", file=sys.stderr)
    store.mark_token_info_request(now)
    try:
        chain = str(row["chain"] or CHAIN)
        token = fetch_token_info(address, chain) if chain != CHAIN else fetch_token_info(address)
        if token is None:
            raise RuntimeError("token_info returned no usable market cap")
    except Exception as exc:
        error = str(exc)
        interval = int(row["tracking_interval_seconds"] or TOKEN_INFO_LOW_INTERVAL_SECONDS)
        next_at = (now + timedelta(seconds=interval)).isoformat()
        server_delay = rate_limit_delay_seconds(error)
        if server_delay:
            backoff_until = now + timedelta(seconds=server_delay)
            store.set_token_info_backoff(backoff_until)
            next_at = max(next_at, backoff_until.isoformat())
            print(f"[meme-scan] token_info rate limited address={address} retry={server_delay}s", file=sys.stderr)
        failures = int(row["token_info_failures"] or 0) + 1
        store.record_token_info_failure(address, error=error, next_at=next_at, observed_at=stamp)
        print(f"[meme-scan] token_info failed address={address} failures={failures}: {error}", file=sys.stderr)
        return {"status": "failed", "address": address, "failures": failures, "error": error, "backlog": backlog, "expired": expired}

    snapshot_id = store.record_snapshot(token, observed_at=stamp, source="token_info")
    previous_high = store.observe_token_info(token, observed_at=stamp, args=args)
    if previous_high is None:
        return {"status": "expired", "address": address, "expired": expired + 1}
    queued, discarded = evaluate_market_token(
        store,
        token,
        bootstrap=False,
        previous_high=previous_high,
        persist_observation=False,
        snapshot_id=snapshot_id,
        snapshot_source="token_info",
    )
    return {
        "status": "observed",
        "address": address,
        "market_cap": token.market_cap,
        "queued": queued,
        "discarded": discarded,
        "backlog": backlog,
        "expired": expired,
    }


def process_tg_candidate(store: Store) -> tuple[int, int]:
    candidate = store.next_tg_candidate()
    if candidate is None:
        return (0, 0)
    try:
        chain = str(candidate["chain"] or "evm").lower()
        token = fetch_tg_token_info(str(candidate["address"]), chain)
    except Exception as exc:
        store.update_tg_candidate(candidate["id"], "pending", reason=f"market_lookup_failed:{exc}")
        return (0, 0)
    if token is None:
        store.update_tg_candidate(candidate["id"], "discarded", reason="token_not_found")
        return (0, 1)
    market_cap_gate = tg_market_cap_gate(token.chain)
    if market_cap_gate is None:
        store.update_tg_candidate(candidate["id"], "discarded", reason="unsupported_chain", market_cap=token.market_cap)
        return (0, 1)
    if token.market_cap < market_cap_gate:
        store.update_tg_candidate(candidate["id"], "discarded", reason="tg_market_cap_gate_failed", market_cap=token.market_cap)
        return (0, 1)
    evidence = json.loads(candidate["evidence_json"])
    trigger_key = f"tg_burst:{candidate['id']}"
    if token.volume_ratio < volume_ratio_gate(token.market_cap):
        inserted = store.add_job(
            token,
            "tg_burst",
            "discarded",
            "volume_gate_failed",
            trigger_key=trigger_key,
            trigger_level=market_cap_gate,
            evidence=evidence,
        )
        store.update_tg_candidate(candidate["id"], "discarded", reason="volume_gate_failed", market_cap=token.market_cap)
        return (0, int(inserted))
    store.record_snapshot(token, observed_at=now_iso(), source="tg", scan_id=f"tg_candidate:{candidate['id']}")
    inserted = store.add_job(
        token,
        "tg_burst",
        "queued",
        trigger_key=trigger_key,
        trigger_level=market_cap_gate,
        evidence=evidence,
    )
    store.upsert_observation(
        token,
        triggered_at=now_iso(),
        advance_market_cap_high_watermark=False,
        tracking_source="tg",
    )
    store.update_tg_candidate(candidate["id"], "queued", market_cap=token.market_cap)
    return (int(inserted), 0)


def discover_once(store: Store, args: argparse.Namespace) -> dict[str, Any]:
    completed_interval = int(
        getattr(args, "completed_interval", getattr(args, "interval", COMPLETED_SCAN_INTERVAL_SECONDS))
    )
    forced_address = str(getattr(args, "force_contract", "") or "").strip().lower()
    should_scan_completed = bool(getattr(args, "once", False) or forced_address or completed_scan_due(store, completed_interval))
    recent_tokens: list[Token] = []
    scan_stamp: str | None = None
    queued = 0
    discarded = 0
    first_run = not store.initialized()
    if should_scan_completed:
        recent_tokens = fetch_completed_tokens(args.limit)
        scan_stamp = now_iso()
        expired = store.expire_tracking(scan_stamp)
        for token in {item.address: item for item in recent_tokens}.values():
            previous_high, _, status = store.start_or_observe_completed(
                token,
                observed_at=scan_stamp,
                tracking_window_seconds=int(getattr(args, "tracking_window", TRACKING_WINDOW_SECONDS)),
                args=args,
            )
            if status == TRACKING_STATUS_ACTIVE:
                added_queued, added_discarded = evaluate_market_token(
                    store,
                    token,
                    bootstrap=False,
                    previous_high=previous_high,
                    persist_observation=False,
                )
                queued += added_queued
                discarded += added_discarded
        store.set_meta("completed_scan_at", scan_stamp)
    else:
        expired = store.expire_tracking()

    forced_token = next((token for token in recent_tokens if token.address == forced_address), None) if forced_address else None
    if forced_address and forced_token is None:
        forced_token = fetch_token_info(forced_address)
    if forced_address and forced_token is None:
        raise RuntimeError(f"forced contract was not found on BSC: {forced_address}")
    if forced_token and (
        forced_token.market_cap < MARKET_CAP_GATE
        or forced_token.volume_ratio < volume_ratio_gate(forced_token.market_cap)
    ):
        raise RuntimeError(f"forced contract does not meet gates: {forced_address}")
    if first_run:
        store.mark_initialized()
    tg_queued, tg_discarded = process_tg_candidate(store)
    queued += tg_queued
    discarded += tg_discarded
    if forced_token:
        store.force_requeue(forced_token)
    return {
        "completed": len(recent_tokens),
        "market_observed": len(recent_tokens),
        "completed_scanned": bool(scan_stamp),
        "expired": expired,
        "startup": first_run,
        "queued": queued,
        "discarded": discarded,
        "forced_address": forced_address or None,
    }


def scan_once(store: Store, args: argparse.Namespace) -> None:
    summary = discover_once(store, args)
    token_info_result = process_due_token_info(store, args)
    result = process_one(store, args, address=summary["forced_address"])
    print(
        f"[meme-scan] completed={summary['completed']} startup={summary['startup']} "
        f"market_observed={summary['market_observed']} queued={summary['queued']} "
        f"discarded={summary['discarded']} token_info={token_info_result['status']} "
        f"processed={result or 'none'}"
    )


def process_from_db(db_path: str, args: argparse.Namespace) -> str | None:
    worker_store = Store(Path(db_path))
    try:
        return process_one(worker_store, args)
    finally:
        worker_store.close()


def token_info_worker(db_path: str, args: argparse.Namespace, stop_event: Any) -> None:
    worker_store = Store(Path(db_path))
    try:
        while not stop_event.is_set():
            result = process_due_token_info(worker_store, args)
            if result["status"] in ("rate_limited_local", "rate_limited_server"):
                stop_event.wait(min(float(result["remaining"]), 1.0))
            else:
                stop_event.wait(1.0)
    finally:
        worker_store.close()


def run(args: argparse.Namespace) -> int:
    load_dotenv()
    store = Store(Path(args.db))
    recovered = store.recover_inflight()
    if recovered:
        print(f"[meme-scan] recovered={recovered}")
    try:
        if args.once:
            scan_once(store, args)
            return 0
        stop_event = threading.Event()
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrative-worker") as executor, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="token-info-worker"
        ) as tracking_executor:
            tracking_future = tracking_executor.submit(token_info_worker, args.db, args, stop_event)
            worker: Future[str | None] | None = None
            try:
                while True:
                    started = time.monotonic()
                    processed: str | None = None
                    if tracking_future.done():
                        try:
                            tracking_future.result()
                        except Exception as exc:
                            print(f"[meme-scan] token_info worker failed: {exc}", file=sys.stderr)
                        tracking_future = tracking_executor.submit(token_info_worker, args.db, args, stop_event)
                    if worker is not None and worker.done():
                        try:
                            processed = worker.result()
                        except Exception as exc:
                            print(f"[meme-scan] worker failed: {exc}", file=sys.stderr)
                        worker = None
                    try:
                        summary = discover_once(store, args)
                        if worker is None:
                            worker = executor.submit(process_from_db, args.db, args)
                        print(
                            f"[meme-scan] completed={summary['completed']} scanned={summary['completed_scanned']} "
                            f"startup={summary['startup']} market_observed={summary['market_observed']} "
                            f"queued={summary['queued']} discarded={summary['discarded']} "
                            f"expired={summary['expired']} processed={processed or 'none'} "
                            f"worker={'busy' if worker else 'idle'}"
                        )
                    except Exception as exc:
                        print(f"[meme-scan] poll failed: {exc}", file=sys.stderr)
                    completed_interval = int(
                        getattr(args, "completed_interval", getattr(args, "interval", COMPLETED_SCAN_INTERVAL_SECONDS))
                    )
                    time.sleep(max(0.0, completed_interval - (time.monotonic() - started)))
            finally:
                stop_event.set()
                tracking_future.result(timeout=10)
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan BSC Four.meme and Flap completed tokens for Meme narrative drafts.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--limit", type=int, default=80, help="GMGN completed rows per poll.")
    parser.add_argument("--completed-interval", type=int, default=int(os.getenv("MEME_COMPLETED_SCAN_INTERVAL") or COMPLETED_SCAN_INTERVAL_SECONDS), help="Seconds between completed discovery scans.")
    parser.add_argument("--token-info-high-interval", type=int, default=int(os.getenv("MEME_TOKEN_INFO_HIGH_INTERVAL") or TOKEN_INFO_HIGH_INTERVAL_SECONDS))
    parser.add_argument("--token-info-low-interval", type=int, default=int(os.getenv("MEME_TOKEN_INFO_LOW_INTERVAL") or TOKEN_INFO_LOW_INTERVAL_SECONDS))
    parser.add_argument("--tracking-window", type=int, default=int(os.getenv("MEME_TRACKING_WINDOW_SECONDS") or TRACKING_WINDOW_SECONDS))
    parser.add_argument("--token-info-min-gap", type=int, default=int(os.getenv("MEME_TOKEN_INFO_MIN_GAP_SECONDS") or TOKEN_INFO_MIN_GAP_SECONDS))
    parser.add_argument("--once", action="store_true", help="Run one poll and process at most one queued job.")
    parser.add_argument("--send", action="store_true", help="Create an OdAIly publisher_pending draft. Default is dry-run.")
    parser.add_argument("--push-timeout", type=int, default=20)
    parser.add_argument("--narrative-timeout", type=int, default=180)
    parser.add_argument("--narrative-command", help="Optional command template; supports {contract}, {symbol}, {name}, {output}.")
    parser.add_argument("--force-contract", help="Operator-only replay for a qualifying CA currently present in the completed list.")
    return parser


def main() -> int:
    return run(build_parser().parse_args())
