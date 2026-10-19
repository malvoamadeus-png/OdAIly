from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .models import StockAlert, StockInstrument, StockQuote, TriggerState, Window


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
                CREATE TABLE IF NOT EXISTS trigger_state (
                    symbol TEXT NOT NULL,
                    window TEXT NOT NULL,
                    initialized INTEGER NOT NULL,
                    armed_5_up INTEGER NOT NULL,
                    armed_5_down INTEGER NOT NULL,
                    armed_10_up INTEGER NOT NULL,
                    armed_10_down INTEGER NOT NULL,
                    last_change_pct TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(symbol, window)
                );
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

    def last_changes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        placeholders = ",".join("?" for _ in symbols)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT symbol,observed_at,change_1h,change_24h FROM instruments WHERE symbol IN ({placeholders})",
                symbols,
            ).fetchall()
        return {
            str(row["symbol"]): {
                "observed_at": int(row["observed_at"]) if row["observed_at"] is not None else None,
                "change_1h": Decimal(str(row["change_1h"])) if row["change_1h"] is not None else None,
                "change_24h": Decimal(str(row["change_24h"])) if row["change_24h"] is not None else None,
            }
            for row in rows
        }

    def get_trigger_states(self, symbols: list[str]) -> dict[tuple[str, Window], TriggerState]:
        if not symbols:
            return {}
        placeholders = ",".join("?" for _ in symbols)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM trigger_state WHERE symbol IN ({placeholders})",
                symbols,
            ).fetchall()
        result: dict[tuple[str, Window], TriggerState] = {}
        for row in rows:
            symbol = str(row["symbol"])
            window = str(row["window"])
            if window not in {"1h", "24h"}:
                continue
            result[(symbol, window)] = TriggerState(
                symbol=symbol,
                window=window,  # type: ignore[arg-type]
                initialized=bool(row["initialized"]),
                armed_5_up=bool(row["armed_5_up"]),
                armed_5_down=bool(row["armed_5_down"]),
                armed_10_up=bool(row["armed_10_up"]),
                armed_10_down=bool(row["armed_10_down"]),
                last_change_pct=Decimal(str(row["last_change_pct"])) if row["last_change_pct"] is not None else None,
            )
        return result

    def record_cycle(
        self,
        *,
        quotes: dict[str, StockQuote],
        changes_1h: dict[str, Decimal],
        states: list[TriggerState],
        alerts: list[StockAlert],
        errors: dict[str, str],
        now: int,
        startup_baseline: bool,
    ) -> None:
        with self._connect() as conn:
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
            for state in states:
                conn.execute(
                    """
                    INSERT INTO trigger_state(
                        symbol,window,initialized,armed_5_up,armed_5_down,
                        armed_10_up,armed_10_down,last_change_pct,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(symbol,window) DO UPDATE SET
                        initialized=excluded.initialized,
                        armed_5_up=excluded.armed_5_up,
                        armed_5_down=excluded.armed_5_down,
                        armed_10_up=excluded.armed_10_up,
                        armed_10_down=excluded.armed_10_down,
                        last_change_pct=excluded.last_change_pct,
                        updated_at=excluded.updated_at
                    """,
                    (
                        state.symbol,
                        state.window,
                        int(state.initialized),
                        int(state.armed_5_up),
                        int(state.armed_5_down),
                        int(state.armed_10_up),
                        int(state.armed_10_down),
                        str(state.last_change_pct) if state.last_change_pct is not None else None,
                        now,
                    ),
                )
            for alert in alerts:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO trigger_events(
                        event_key,symbol,market,window,tier,direction,change_percent,current_price,
                        title,content,status,observed_at,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,'publishing',?,?,?)
                    """,
                    (
                        alert.event_key,
                        alert.instrument.symbol,
                        alert.instrument.market,
                        alert.window,
                        alert.tier,
                        alert.direction,
                        str(alert.change_pct),
                        str(alert.current_price),
                        alert.title,
                        alert.content,
                        alert.observed_at,
                        now,
                        now,
                    ),
                )
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('last_cycle_at',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (str(now), now),
            )
            status = "partial_error" if errors else "ok"
            if startup_baseline:
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
            state_rows = conn.execute("SELECT * FROM trigger_state").fetchall()
            events = conn.execute(
                "SELECT * FROM trigger_events ORDER BY created_at DESC,id DESC LIMIT 100"
            ).fetchall()
            settings = {str(row["key"]): str(row["value"]) for row in conn.execute("SELECT key,value FROM settings")}
        states: dict[str, dict[str, dict[str, Any]]] = {}
        for row in state_rows:
            states.setdefault(str(row["symbol"]), {})[str(row["window"])] = {
                "initialized": bool(row["initialized"]),
                "armed_5_up": bool(row["armed_5_up"]),
                "armed_5_down": bool(row["armed_5_down"]),
                "armed_10_up": bool(row["armed_10_up"]),
                "armed_10_down": bool(row["armed_10_down"]),
                "last_change_pct": row["last_change_pct"],
            }
        generated_at = int(time.time())
        contracts = [
            {
                "symbol": str(row["symbol"]),
                "market": str(row["market"]),
                "price_precision": int(row["price_precision"]),
                "last_price": row["last_price"],
                "observed_at": _iso(int(row["observed_at"])) if row["observed_at"] is not None else None,
                "change_1h": row["change_1h"],
                "change_24h": row["change_24h"],
                "trigger_state": states.get(str(row["symbol"]), {}),
                "last_error": row["last_error"],
            }
            for row in instruments
        ]
        recent_events = [
            {
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
