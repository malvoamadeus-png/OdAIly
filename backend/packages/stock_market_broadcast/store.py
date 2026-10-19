from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .models import BroadcastState, StockAlert, StockInstrument, StockQuote
from .state_machine import (
    AGGREGATION_SECONDS, FOLLOWUP_SECONDS, PRICE_LOCK_SECONDS, PRICE_STEP_PERCENT,
    state_from_dict, state_to_dict,
)


def _iso(timestamp: int | None) -> str | None:
    return datetime.fromtimestamp(timestamp, UTC).isoformat() if timestamp is not None else None


class StockMarketStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        now = int(time.time())
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS instruments (
                    symbol TEXT PRIMARY KEY,
                    market TEXT NOT NULL,
                    price_precision INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    last_price TEXT,
                    observed_at INTEGER,
                    change_1h TEXT,
                    change_24h TEXT,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS price_samples (
                    symbol TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    PRIMARY KEY(symbol, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_market_samples_time
                    ON price_samples(symbol, observed_at);
                CREATE TABLE IF NOT EXISTS trigger_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    window TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    change_percent TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    observed_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stock_market_events_recent
                    ON trigger_events(created_at DESC);
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(trigger_events)")}
            if "details_json" not in columns:
                conn.execute("ALTER TABLE trigger_events ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}'")
            conn.execute("CREATE TABLE IF NOT EXISTS broadcast_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)")
            defaults = {
                "poll_interval_seconds": "30",
                "instrument_refresh_seconds": "3600",
                "last_discovery_at": "0",
                "last_discovery_error": "",
                "last_cycle_at": "0",
                "last_cycle_status": "not_started",
                "last_cycle_error": "",
            }
            conn.executemany(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                [(key, value, now) for key, value in defaults.items()],
            )

    def _setting(self, conn: sqlite3.Connection, key: str, default: str = "") -> str:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            return self._setting(conn, key, default)

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (key, value, int(time.time())),
            )

    def last_discovery_at(self) -> int:
        try:
            return int(self.get_setting("last_discovery_at", "0"))
        except ValueError:
            return 0

    def list_instruments(self) -> list[StockInstrument]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol,market,price_precision FROM instruments WHERE active=1 ORDER BY symbol"
            ).fetchall()
        return [
            StockInstrument(
                symbol=str(row["symbol"]),
                market=str(row["market"]),  # type: ignore[arg-type]
                price_precision=int(row["price_precision"]),
            )
            for row in rows
        ]

    def sync_instruments(self, instruments: list[StockInstrument], *, now: int) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE instruments SET active=0,updated_at=?", (now,))
            conn.executemany(
                """
                INSERT INTO instruments(symbol,market,price_precision,active,updated_at)
                VALUES(?,?,?,1,?)
                ON CONFLICT(symbol) DO UPDATE SET
                    market=excluded.market,
                    price_precision=excluded.price_precision,
                    active=1,
                    last_error=NULL,
                    updated_at=excluded.updated_at
                """,
                [(item.symbol, item.market, item.price_precision, now) for item in instruments],
            )
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_discovery_at',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (str(now), now),
            )
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_discovery_error','',?)
                ON CONFLICT(key) DO UPDATE SET value='',updated_at=excluded.updated_at
                """,
                (now,),
            )

    def record_discovery_error(self, error: str, *, now: int) -> None:
        self.set_setting("last_discovery_error", error[:1000])

    def record_worker_failure(self, error: str, *, now: int) -> None:
        with self._connect() as conn:
            for key, value in (
                ("last_cycle_at", str(now)),
                ("last_cycle_status", "error"),
                ("last_cycle_error", error[:1000]),
            ):
                conn.execute(
                    """
                    INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                    """,
                    (key, value, now),
                )

    def one_hour_references(self, quotes: dict[str, StockQuote]) -> dict[str, Decimal]:
        references: dict[str, Decimal] = {}
        with self._connect() as conn:
            for symbol, quote in quotes.items():
                target = quote.observed_at - 3600
                row = conn.execute(
                    """
                    SELECT observed_at,price FROM price_samples
                    WHERE symbol=? AND observed_at<=?
                    ORDER BY observed_at DESC LIMIT 1
                    """,
                    (symbol, target),
                ).fetchone()
                if row is None:
                    continue
                age = target - int(row["observed_at"])
                if age < 0 or age > 90:
                    continue
                price = Decimal(str(row["price"]))
                if price > 0:
                    references[symbol] = price
        return references

    def broadcast_state(self) -> BroadcastState:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM broadcast_state WHERE id=1").fetchone()
        return state_from_dict(json.loads(row["payload"])) if row else BroadcastState()

    def record_cycle(
        self,
        *,
        quotes: dict[str, StockQuote],
        changes_1h: dict[str, Decimal],
        state: BroadcastState,
        alerts: list[StockAlert],
        errors: dict[str, str],
        now: int,
        startup_baseline: bool,
    ) -> list[StockAlert]:
        claimed: list[StockAlert] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for symbol, quote in quotes.items():
                conn.execute(
                    "INSERT OR REPLACE INTO price_samples(symbol,observed_at,price) VALUES(?,?,?)",
                    (symbol, quote.observed_at, str(quote.price)),
                )
                instrument = conn.execute(
                    "SELECT 1 FROM instruments WHERE symbol=? AND active=1", (symbol,)
                ).fetchone()
                if instrument is None:
                    continue
                conn.execute(
                    """
                    UPDATE instruments SET last_price=?,observed_at=?,change_1h=?,change_24h=?,
                        last_error=NULL,updated_at=? WHERE symbol=?
                    """,
                    (
                        str(quote.price),
                        quote.observed_at,
                        str(changes_1h[symbol]) if symbol in changes_1h else None,
                        str(quote.change_24h),
                        now,
                        symbol,
                    ),
                )
            for symbol, error in errors.items():
                conn.execute(
                    "UPDATE instruments SET last_error=?,updated_at=? WHERE symbol=? AND active=1",
                    (error[:1000], now, symbol),
                )
            conn.execute(
                "INSERT INTO broadcast_state(id,payload) VALUES(1,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (json.dumps(state_to_dict(state), default=str),),
            )
            for alert in alerts:
                primary = alert.members[0]
                details = {"members": [{
                    "symbol": member.instrument.symbol, "asset_name": member.instrument.asset_name,
                    "changes": {window: str(change) for window, change in member.changes.items()},
                    "current_price": str(member.current_price), "observed_at": _iso(member.observed_at),
                } for member in alert.members]}
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO trigger_events(
                        event_key,symbol,market,window,tier,direction,change_percent,current_price,
                        title,content,status,observed_at,created_at,updated_at,details_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'publishing',?,?,?,?)
                    """,
                    (
                        alert.event_key, primary.instrument.symbol, alert.market,
                        primary.primary_window, alert.tier, alert.direction,
                        str(primary.change_pct), str(primary.current_price),
                        alert.title, alert.content, max(member.observed_at for member in alert.members),
                        now, now, json.dumps(details, ensure_ascii=False),
                    ),
                )
                if cursor.rowcount:
                    claimed.append(alert)
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_cycle_at',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (str(now), now),
            )
            status = "partial_error" if errors else "ok"
            if startup_baseline and not errors:
                status = "baseline"
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_cycle_status',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (status, now),
            )
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_cycle_error',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                ("; ".join(errors.values())[:1000], now),
            )
            conn.execute("DELETE FROM price_samples WHERE observed_at<?", (now - 3 * 3600,))
            conn.execute(
                "DELETE FROM trigger_events WHERE id NOT IN "
                "(SELECT id FROM trigger_events ORDER BY created_at DESC,id DESC LIMIT 500)"
            )

        return claimed

    def mark_event(self, event_key: str, *, status: str, error: str | None, now: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE trigger_events SET status=?,error=?,updated_at=? WHERE event_key=?",
                (status, error[:1000] if error else None, now, event_key),
            )

    def dashboard(self) -> dict[str, Any]:
        with self._connect() as conn:
            instruments = conn.execute(
                "SELECT * FROM instruments WHERE active=1 ORDER BY symbol"
            ).fetchall()
            snapshot = conn.execute("SELECT payload FROM broadcast_state WHERE id=1").fetchone()
            events = conn.execute(
                "SELECT * FROM trigger_events ORDER BY created_at DESC,id DESC LIMIT 100"
            ).fetchall()
            settings = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key,value FROM settings")}
        state = state_from_dict(json.loads(snapshot["payload"])) if snapshot else BroadcastState()
        generated_at = int(time.time())
        contracts = [
            {
                "symbol": str(row["symbol"]),
                "asset_name": str(row["symbol"]).removesuffix("USDT"),
                "market": str(row["market"]),
                "price_precision": int(row["price_precision"]),
                "last_price": row["last_price"],
                "observed_at": _iso(int(row["observed_at"])) if row["observed_at"] is not None else None,
                "change_1h": row["change_1h"],
                "change_24h": row["change_24h"],
                "trigger_state": {},  # Older console tabs can safely render during rollout.
                "price_locks": {
                    direction: {
                        "anchor_price": str(lock.anchor_price), "extreme_price": str(lock.extreme_price),
                        "tier": lock.tier, "locked_until": _iso(lock.locked_at + PRICE_LOCK_SECONDS),
                    } for direction, lock in state.assets[str(row["symbol"])].locks.items()
                } if str(row["symbol"]) in state.assets else {},
                "last_error": row["last_error"],
            }
            for row in instruments
        ]
        recent_events = [
            {
                "members": json.loads(row["details_json"]).get("members", []),
                "id": int(row["id"]),
                "symbol": str(row["symbol"]),
                "market": str(row["market"]),
                "window": str(row["window"]),
                "tier": str(row["tier"]),
                "direction": str(row["direction"]),
                "change_percent": str(row["change_percent"]),
                "current_price": str(row["current_price"]),
                "title": str(row["title"]),
                "content": str(row["content"]),
                "status": str(row["status"]),
                "error": row["error"],
                "observed_at": _iso(int(row["observed_at"])),
                "created_at": _iso(int(row["created_at"])),
            }
            for row in events
        ]
        last_cycle_at = int(settings.get("last_cycle_at", "0") or 0)
        last_discovery_at = int(settings.get("last_discovery_at", "0") or 0)
        return {
            "poll_interval_seconds": int(settings.get("poll_interval_seconds", "30")),
            "instrument_refresh_seconds": int(settings.get("instrument_refresh_seconds", "3600")),
            "aggregation_seconds": AGGREGATION_SECONDS,
            "followup_seconds": FOLLOWUP_SECONDS,
            "price_lock_seconds": PRICE_LOCK_SECONDS,
            "price_step_percent": str(PRICE_STEP_PERCENT),
            "pending_groups": [{
                "market": group.market, "direction": group.direction,
                "due_at": _iso(group.due_at), "next_at": _iso(group.next_at),
                "assets": [symbol.removesuffix("USDT") for symbol in sorted(group.pending)],
            } for group in state.groups.values() if group.pending],
            "contract_count": len(contracts),
            "contracts": contracts,
            "recent_events": recent_events,
            "last_cycle_at": _iso(last_cycle_at),
            "last_cycle_status": settings.get("last_cycle_status", "not_started"),
            "last_cycle_error": settings.get("last_cycle_error") or None,
            "last_discovery_at": _iso(last_discovery_at),
            "last_discovery_error": settings.get("last_discovery_error") or None,
            "generated_at": _iso(generated_at),
        }
