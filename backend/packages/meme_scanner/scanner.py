from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from packages.common.paths import get_paths

from .gmgn import GMGN, ensure_cli_ready, gmgn_subprocess_env
from .narrative import generate_reader_text


CHAIN = "bsc"
PLATFORMS = ("fourmeme", "flap")
MARKET_CAP_GATE = 500_000.0
MARKET_CAP_LEVELS = (500_000.0, 1_000_000.0, 3_000_000.0)
TG_MARKET_CAP_GATE = 300_000.0
VOLUME_RATIO_GATE = 0.5
QUEUE_EXPIRY_SECONDS = 3600
MAX_JOB_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (60, 300, 900)
PATHS = get_paths()
PROJECT_ROOT = PATHS.root_dir
PROCESSED_DATA_DIR = PATHS.processed_dir
DEFAULT_DB = PATHS.processed_dir / "meme_scanner.sqlite3"
DEFAULT_AUDIT_DIR = PATHS.exports_dir / "meme_scanner"
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

    @property
    def volume_ratio(self) -> float:
        return self.volume_24h / self.market_cap if self.market_cap > 0 else 0.0


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def token_from_row(row: dict[str, Any]) -> Token | None:
    address = str(row.get("address") or "").strip().lower()
    platform = str(row.get("launchpad_platform") or row.get("launchpad") or "").strip().lower()
    if not address or (platform and platform not in (*PLATFORMS, "telegram")):
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
    )


def fetch_completed_tokens(limit: int) -> list[Token]:
    if not ensure_cli_ready():
        raise RuntimeError("GMGN CLI is not ready")
    command = [GMGN, "market", "trenches", "--chain", CHAIN, "--type", "completed", "--limit", str(limit), "--sort-by", "created_timestamp", "--direction", "desc", "--raw"]
    for platform in PLATFORMS:
        command.extend(("--launchpad-platform", platform))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=gmgn_subprocess_env(), check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "GMGN query failed")
    payload = json.loads(result.stdout)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    rows = data.get("completed", []) if isinstance(data, dict) else []
    tokens = [token for row in rows if isinstance(row, dict) if (token := token_from_row(row))]
    return sorted(tokens, key=lambda item: item.created_timestamp or 0, reverse=True)


def fetch_market_cap_band(minimum: float, maximum: float | None, limit: int) -> list[Token]:
    if not ensure_cli_ready():
        raise RuntimeError("GMGN CLI is not ready")
    command = [
        GMGN,
        "market",
        "trenches",
        "--chain",
        CHAIN,
        "--type",
        "completed",
        "--limit",
        str(limit),
        "--sort-by",
        "usd_market_cap",
        "--direction",
        "asc",
        "--min-marketcap",
        str(minimum),
        "--raw",
    ]
    if maximum is not None:
        command.extend(("--max-marketcap", str(maximum)))
    for platform in PLATFORMS:
        command.extend(("--launchpad-platform", platform))
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=gmgn_subprocess_env(), check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "GMGN market-cap query failed")
    payload = json.loads(result.stdout)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    rows = data.get("completed", []) if isinstance(data, dict) else []
    return [token for row in rows if isinstance(row, dict) if (token := token_from_row(row))]


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


def fetch_token_info(address: str) -> Token | None:
    if not ensure_cli_ready():
        raise RuntimeError("GMGN CLI is not ready")
    command = [GMGN, "token", "info", "--chain", CHAIN, "--address", address, "--raw"]
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
    return token_from_row(normalized)


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
              address TEXT PRIMARY KEY, platform TEXT NOT NULL, symbol TEXT NOT NULL,
              last_market_cap REAL NOT NULL, highest_market_cap REAL NOT NULL DEFAULT 0,
              last_seen_at TEXT NOT NULL,
              triggered_at TEXT, published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tg_candidates (
              id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL,
              detected_at TEXT NOT NULL, window_start TEXT NOT NULL,
              mention_count INTEGER NOT NULL, chat_count INTEGER NOT NULL,
              sender_count INTEGER NOT NULL, evidence_json TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending', reason TEXT,
              market_cap REAL, updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tg_candidates_status ON tg_candidates(status, id);
            """
        )
        self._ensure_jobs_v2()
        observation_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(observations)")}
        if "highest_market_cap" not in observation_columns:
            self.conn.execute("ALTER TABLE observations ADD COLUMN highest_market_cap REAL NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE observations SET highest_market_cap=last_market_cap")
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        if "attempts" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "next_attempt_at" not in columns:
            self.conn.execute("ALTER TABLE jobs ADD COLUMN next_attempt_at TEXT")
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

    def upsert_observation(self, token: Token, *, triggered_at: str | None = None, published_at: str | None = None) -> None:
        old = self.observation(token.address)
        self.conn.execute(
            """INSERT INTO observations(address, platform, symbol, last_market_cap, highest_market_cap, last_seen_at, triggered_at, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET platform=excluded.platform, symbol=excluded.symbol,
              last_market_cap=excluded.last_market_cap, last_seen_at=excluded.last_seen_at,
              highest_market_cap=MAX(observations.highest_market_cap, excluded.highest_market_cap),
              triggered_at=COALESCE(excluded.triggered_at, observations.triggered_at),
              published_at=COALESCE(excluded.published_at, observations.published_at)""",
            (token.address, token.platform, token.symbol, token.market_cap, token.market_cap, now_iso(), triggered_at or (old["triggered_at"] if old else None), published_at or (old["published_at"] if old else None)),
        )
        self.conn.commit()

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
            self.conn.execute(
                "UPDATE jobs SET status='processing', attempts=attempts+1, next_attempt_at=NULL, updated_at=? WHERE id=?",
                (now_iso(), job["id"]),
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
        self.conn.execute(
            """UPDATE jobs SET status=?, reason=COALESCE(?, reason), narrative_json=COALESCE(?, narrative_json),
            title=COALESCE(?, title), content=COALESCE(?, content), publish_json=COALESCE(?, publish_json), updated_at=? WHERE id=?""",
            (status, reason, json.dumps(narrative, ensure_ascii=False) if narrative else None, title, content, json.dumps(publish, ensure_ascii=False) if publish else None, now_iso(), job_id),
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
    if trigger_kind == "tg_burst":
        title = f"Meme速递：BSC上{token.symbol}社群热议中，市值{cap}万美元"
        content = f"BSC上{token.symbol}社群热议中，当前市值{cap}万美元。\n\n{narrative.strip()}"
    else:
        title = f"Meme速递：BSC上{token.symbol}市值突破{cap}万美元"
        content = f"BSC上{token.symbol}市值突破{cap}万美元。\n\n{narrative.strip()}"
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
        CHAIN,
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


def push_pending(
    title: str,
    content: str,
    *,
    endpoint: str,
    timeout: int,
    send: bool,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    payload = {"title": title, "content": content, "isPublish": False, "isPush": False}
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
        idempotency_key=f"odaily:meme:bsc:{token.address}:{job['trigger_key']}",
    )
    if pushed["ok"]:
        store.update_job(job["id"], "publisher_pending", publish=pushed)
        store.upsert_observation(token, published_at=now_iso())
        return "publisher_pending"
    else:
        store.update_job(job["id"], "publish_failed", publish=pushed)
        return "publish_failed"


def milestone_level(previous_high: float, current: float) -> float | None:
    crossed = [level for level in MARKET_CAP_LEVELS if previous_high < level <= current]
    return max(crossed) if crossed else None


def milestone_scan_due(store: Store, interval_seconds: int) -> bool:
    value = store.meta("milestone_scan_at")
    if not value:
        return True
    try:
        last = datetime.fromisoformat(value)
    except ValueError:
        return True
    return (datetime.now(UTC) - last).total_seconds() >= interval_seconds


def fetch_milestone_tokens(limit: int) -> tuple[list[Token], list[str]]:
    bounds = list(zip(MARKET_CAP_LEVELS, (*MARKET_CAP_LEVELS[1:], None)))
    by_address: dict[str, Token] = {}
    saturated: list[str] = []
    for minimum, maximum in bounds:
        rows = fetch_market_cap_band(minimum, maximum, limit)
        if len(rows) >= limit:
            saturated.append(f"{int(minimum)}-{int(maximum) if maximum else 'up'}")
        for token in rows:
            by_address[token.address] = token
    return list(by_address.values()), saturated


def evaluate_market_token(store: Store, token: Token, *, bootstrap: bool) -> tuple[int, int]:
    observed = store.observation(token.address)
    previous_high = float(observed["highest_market_cap"] or observed["last_market_cap"] or 0) if observed else 0.0
    if bootstrap:
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
    store.upsert_observation(token, triggered_at=now_iso() if level else None)
    if level is None:
        return (0, 0)
    trigger_key = f"market_cap:{int(level)}"
    if token.volume_ratio < VOLUME_RATIO_GATE:
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


def process_tg_candidate(store: Store) -> tuple[int, int]:
    candidate = store.next_tg_candidate()
    if candidate is None:
        return (0, 0)
    try:
        token = fetch_token_info(str(candidate["address"]))
    except Exception as exc:
        store.update_tg_candidate(candidate["id"], "pending", reason=f"market_lookup_failed:{exc}")
        return (0, 0)
    if token is None:
        store.update_tg_candidate(candidate["id"], "discarded", reason="bsc_token_not_found")
        return (0, 1)
    if token.market_cap < TG_MARKET_CAP_GATE:
        store.update_tg_candidate(candidate["id"], "discarded", reason="tg_market_cap_gate_failed", market_cap=token.market_cap)
        return (0, 1)
    evidence = json.loads(candidate["evidence_json"])
    trigger_key = f"tg_burst:{candidate['id']}"
    if token.volume_ratio < VOLUME_RATIO_GATE:
        inserted = store.add_job(
            token,
            "tg_burst",
            "discarded",
            "volume_gate_failed",
            trigger_key=trigger_key,
            trigger_level=TG_MARKET_CAP_GATE,
            evidence=evidence,
        )
        store.update_tg_candidate(candidate["id"], "discarded", reason="volume_gate_failed", market_cap=token.market_cap)
        return (0, int(inserted))
    inserted = store.add_job(
        token,
        "tg_burst",
        "queued",
        trigger_key=trigger_key,
        trigger_level=TG_MARKET_CAP_GATE,
        evidence=evidence,
    )
    store.upsert_observation(token, triggered_at=now_iso())
    store.update_tg_candidate(candidate["id"], "queued", market_cap=token.market_cap)
    return (int(inserted), 0)


def discover_once(store: Store, args: argparse.Namespace) -> dict[str, Any]:
    recent_tokens = fetch_completed_tokens(args.limit)
    forced_address = str(getattr(args, "force_contract", "") or "").strip().lower()
    forced_token = next((token for token in recent_tokens if token.address == forced_address), None) if forced_address else None
    if forced_address and forced_token is None:
        forced_token = fetch_token_info(forced_address)
    if forced_address and forced_token is None:
        raise RuntimeError(f"forced contract was not found on BSC: {forced_address}")
    if forced_token and (forced_token.market_cap < MARKET_CAP_GATE or forced_token.volume_ratio < VOLUME_RATIO_GATE):
        raise RuntimeError(f"forced contract does not meet gates: {forced_address}")
    first_run = not store.initialized()
    milestone_bootstrap = store.meta("milestone_initialized") is None
    queued = 0
    discarded = 0
    saturated: list[str] = []
    market_tokens: dict[str, Token] = {token.address: token for token in recent_tokens}
    if milestone_scan_due(store, int(getattr(args, "milestone_interval", 300))):
        band_tokens, saturated = fetch_milestone_tokens(args.limit)
        market_tokens.update({token.address: token for token in band_tokens})
        store.set_meta("milestone_scan_at")
    bootstrap = first_run or milestone_bootstrap
    for token in market_tokens.values():
        if forced_token and token.address == forced_token.address:
            continue
        added_queued, added_discarded = evaluate_market_token(store, token, bootstrap=bootstrap)
        queued += added_queued
        discarded += added_discarded
    if first_run:
        store.mark_initialized()
    if milestone_bootstrap:
        store.set_meta("milestone_initialized")
    tg_queued, tg_discarded = process_tg_candidate(store)
    queued += tg_queued
    discarded += tg_discarded
    if forced_token:
        store.force_requeue(forced_token)
    return {
        "completed": len(recent_tokens),
        "market_observed": len(market_tokens),
        "saturated_bands": saturated,
        "startup": first_run,
        "queued": queued,
        "discarded": discarded,
        "forced_address": forced_address or None,
    }


def scan_once(store: Store, args: argparse.Namespace) -> None:
    summary = discover_once(store, args)
    result = process_one(store, args, address=summary["forced_address"])
    print(
        f"[meme-scan] completed={summary['completed']} startup={summary['startup']} "
        f"market_observed={summary['market_observed']} queued={summary['queued']} "
        f"discarded={summary['discarded']} saturated={summary['saturated_bands'] or 'none'} "
        f"processed={result or 'none'}"
    )


def process_from_db(db_path: str, args: argparse.Namespace) -> str | None:
    worker_store = Store(Path(db_path))
    try:
        return process_one(worker_store, args)
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
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="narrative-worker") as executor:
            worker: Future[str | None] | None = None
            while True:
                started = time.monotonic()
                processed: str | None = None
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
                        f"[meme-scan] completed={summary['completed']} startup={summary['startup']} "
                        f"market_observed={summary['market_observed']} queued={summary['queued']} "
                        f"discarded={summary['discarded']} saturated={summary['saturated_bands'] or 'none'} "
                        f"processed={processed or 'none'} worker={'busy' if worker else 'idle'}"
                    )
                except Exception as exc:
                    print(f"[meme-scan] poll failed: {exc}", file=sys.stderr)
                sleep_for = max(0.0, args.interval - (time.monotonic() - started))
                time.sleep(sleep_for)
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan BSC Four.meme and Flap completed tokens for Meme narrative drafts.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--audit-dir", default=str(DEFAULT_AUDIT_DIR))
    parser.add_argument("--limit", type=int, default=80, help="GMGN completed rows per poll.")
    parser.add_argument("--interval", type=int, default=60, help="Polling interval in seconds.")
    parser.add_argument("--milestone-interval", type=int, default=300, help="Seconds between full market-cap band scans.")
    parser.add_argument("--once", action="store_true", help="Run one poll and process at most one queued job.")
    parser.add_argument("--send", action="store_true", help="Create an OdAIly publisher_pending draft. Default is dry-run.")
    parser.add_argument("--push-timeout", type=int, default=20)
    parser.add_argument("--narrative-timeout", type=int, default=180)
    parser.add_argument("--narrative-command", help="Optional command template; supports {contract}, {symbol}, {name}, {output}.")
    parser.add_argument("--force-contract", help="Operator-only replay for a qualifying CA currently present in the completed list.")
    return parser


def main() -> int:
    return run(build_parser().parse_args())
