from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from .defaults import DEFAULT_SYMBOLS, DEFAULT_TEMPLATES
from .models import PublishMode, ReferenceMetrics, SymbolConfig, SymbolState, TickerQuote


_SHARED_STATE_MODE = "shared"


def _iso(timestamp: int | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


class GateMarketStore:
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

                CREATE TABLE IF NOT EXISTS symbol_config (
                    symbol TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    threshold TEXT NOT NULL,
                    price_precision INTEGER NOT NULL,
                    unit TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS templates (
                    template_key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    title_template TEXT NOT NULL,
                    body_template TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS symbol_state (
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    initialized INTEGER NOT NULL DEFAULT 0,
                    last_price TEXT,
                    last_quote_at INTEGER,
                    disarmed_levels TEXT NOT NULL DEFAULT '[]',
                    market_status TEXT,
                    next_open_at INTEGER,
                    last_success_at INTEGER,
                    last_error TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (symbol, mode)
                );

                CREATE TABLE IF NOT EXISTS price_samples (
                    symbol TEXT NOT NULL,
                    observed_at INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    PRIMARY KEY (symbol, observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_gate_market_samples_time
                    ON price_samples(symbol, observed_at);

                CREATE TABLE IF NOT EXISTS trigger_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL UNIQUE,
                    mode TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    trigger_level TEXT NOT NULL,
                    current_price TEXT NOT NULL,
                    reference_kind TEXT,
                    reference_price TEXT,
                    window_high TEXT,
                    window_low TEXT,
                    change_percent TEXT,
                    template_key TEXT,
                    status TEXT NOT NULL,
                    title TEXT,
                    content TEXT,
                    push_status_code INTEGER,
                    push_response TEXT,
                    error TEXT,
                    observed_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_gate_market_events_recent
                    ON trigger_events(created_at DESC);

                CREATE TABLE IF NOT EXISTS alert_state (
                    alert_key TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    last_sent_at INTEGER,
                    last_message TEXT,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('mode','backend',?)",
                (now,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('poll_interval_seconds','60',?)",
                (now,),
            )
            for item in DEFAULT_SYMBOLS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO symbol_config(
                        symbol,display_name,threshold,price_precision,unit,enabled,updated_at
                    ) VALUES(?,?,?,?,?,1,?)
                    """,
                    (
                        item.symbol,
                        item.display_name,
                        item.threshold,
                        item.price_precision,
                        item.unit,
                        now,
                    ),
                )
            for key, label, title, body in DEFAULT_TEMPLATES:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO templates(
                        template_key,label,title_template,body_template,updated_at
                    ) VALUES(?,?,?,?,?)
                    """,
                    (key, label, title, body, now),
                )
            self._migrate_shared_symbol_states(conn, now)

    @staticmethod
    def _migrate_shared_symbol_states(conn: sqlite3.Connection, now: int) -> None:
        """Collapse legacy per-publish-mode states into one market state per symbol."""
        symbols = conn.execute(
            "SELECT DISTINCT symbol FROM symbol_state WHERE mode<>?",
            (_SHARED_STATE_MODE,),
        ).fetchall()
        for symbol_row in symbols:
            symbol = str(symbol_row["symbol"])
            shared = conn.execute(
                "SELECT 1 FROM symbol_state WHERE symbol=? AND mode=?",
                (symbol, _SHARED_STATE_MODE),
            ).fetchone()
            if shared is None:
                rows = conn.execute(
                    """
                    SELECT * FROM symbol_state
                    WHERE symbol=? AND mode<>?
                    ORDER BY updated_at DESC
                    """,
                    (symbol, _SHARED_STATE_MODE),
                ).fetchall()
                source = next(
                    (
                        row
                        for row in rows
                        if bool(row["initialized"]) and row["last_price"] is not None
                    ),
                    rows[0],
                )
                disarmed_levels: set[int] = set()
                for row in rows:
                    try:
                        disarmed_levels.update(
                            int(value)
                            for value in json.loads(str(row["disarmed_levels"] or "[]"))
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                conn.execute(
                    """
                    INSERT INTO symbol_state(
                        symbol,mode,initialized,last_price,last_quote_at,disarmed_levels,
                        market_status,next_open_at,last_success_at,last_error,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        symbol,
                        _SHARED_STATE_MODE,
                        source["initialized"],
                        source["last_price"],
                        source["last_quote_at"],
                        json.dumps(sorted(disarmed_levels)),
                        source["market_status"],
                        source["next_open_at"],
                        source["last_success_at"],
                        source["last_error"],
                        now,
                    ),
                )
            conn.execute(
                "DELETE FROM symbol_state WHERE symbol=? AND mode<>?",
                (symbol, _SHARED_STATE_MODE),
            )

    def get_mode(self) -> PublishMode:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key='mode'").fetchone()
        value = str(row["value"] if row else "backend")
        if value not in {"backend", "live"}:
            raise ValueError(f"Invalid Gate market mode in SQLite: {value}")
        return value  # type: ignore[return-value]

    def set_mode(self, mode: str) -> PublishMode:
        if mode not in {"backend", "live"}:
            raise ValueError("mode must be backend or live")
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings(key,value,updated_at) VALUES('mode',?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
                """,
                (mode, now),
            )
        return mode  # type: ignore[return-value]

    def poll_interval_seconds(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='poll_interval_seconds'"
            ).fetchone()
        return max(10, int(row["value"] if row else 60))

    def list_symbol_configs(self) -> list[SymbolConfig]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT symbol,display_name,threshold,price_precision,unit,enabled
                FROM symbol_config ORDER BY rowid
                """
            ).fetchall()
        return [
            SymbolConfig(
                symbol=str(row["symbol"]),
                display_name=str(row["display_name"]),
                threshold_text=str(row["threshold"]),
                price_precision=int(row["price_precision"]),
                unit=str(row["unit"]),
                enabled=bool(row["enabled"]),
            )
            for row in rows
        ]

    def get_symbol_config(self, symbol: str) -> SymbolConfig:
        normalized = symbol.strip().upper()
        configs = {item.symbol: item for item in self.list_symbol_configs()}
        if normalized not in configs:
            raise ValueError(f"Unknown Gate market symbol: {normalized}")
        return configs[normalized]

    def set_threshold(self, symbol: str, threshold_text: str) -> SymbolConfig:
        normalized = symbol.strip().upper()
        threshold = Decimal(threshold_text)
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        # Preserve the operator's chosen decimal grid, including 0.010.
        stored = threshold_text.strip()
        now = int(time.time())
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE symbol_config SET threshold=?,updated_at=? WHERE symbol=?",
                (stored, now, normalized),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown Gate market symbol: {normalized}")
            conn.execute("DELETE FROM symbol_state WHERE symbol=?", (normalized,))
        return self.get_symbol_config(normalized)

    def templates(self) -> dict[str, tuple[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT template_key,title_template,body_template FROM templates"
            ).fetchall()
        return {
            str(row["template_key"]): (
                str(row["title_template"]),
                str(row["body_template"]),
            )
            for row in rows
        }

    def get_state(self, symbol: str, mode: PublishMode) -> SymbolState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM symbol_state WHERE symbol=? AND mode=?",
                (symbol, _SHARED_STATE_MODE),
            ).fetchone()
        if row is None:
            return SymbolState(symbol=symbol, mode=mode)
        try:
            raw_levels = json.loads(str(row["disarmed_levels"] or "[]"))
            levels = {int(value) for value in raw_levels}
        except (TypeError, ValueError, json.JSONDecodeError):
            levels = set()
        return SymbolState(
            symbol=symbol,
            mode=mode,
            initialized=bool(row["initialized"]),
            last_price=Decimal(str(row["last_price"])) if row["last_price"] is not None else None,
            last_quote_at=int(row["last_quote_at"]) if row["last_quote_at"] is not None else None,
            disarmed_levels=levels,
            market_status=str(row["market_status"]) if row["market_status"] else None,
            next_open_at=int(row["next_open_at"]) if row["next_open_at"] else None,
            last_success_at=int(row["last_success_at"]) if row["last_success_at"] else None,
            last_error=str(row["last_error"]) if row["last_error"] else None,
        )

    def add_samples(self, symbol: str, points: list[tuple[int, Decimal]]) -> None:
        if not points:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO price_samples(symbol,observed_at,price) VALUES(?,?,?)",
                [(symbol, timestamp, str(price)) for timestamp, price in points],
            )

    def recent_sample_count(self, symbol: str, since: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT count(*) AS count FROM price_samples WHERE symbol=? AND observed_at>=?",
                (symbol, since),
            ).fetchone()
        return int(row["count"] if row else 0)

    def reference_metrics(
        self,
        *,
        symbol: str,
        quote: TickerQuote,
    ) -> ReferenceMetrics | None:
        target = quote.observed_at - 86400
        with self._connect() as conn:
            reference = conn.execute(
                """
                SELECT observed_at,price FROM price_samples
                WHERE symbol=? AND observed_at<=?
                ORDER BY observed_at DESC LIMIT 1
                """,
                (symbol, target),
            ).fetchone()
            if reference is not None and target - int(reference["observed_at"]) <= 3600:
                extrema = conn.execute(
                    """
                    SELECT min(CAST(price AS REAL)) AS low,max(CAST(price AS REAL)) AS high
                    FROM price_samples WHERE symbol=? AND observed_at>=?
                    """,
                    (symbol, target),
                ).fetchone()
                if extrema and extrema["low"] is not None and extrema["high"] is not None:
                    return ReferenceMetrics(
                        reference_kind="rolling_24h",
                        reference_price=Decimal(str(reference["price"])),
                        high=Decimal(str(extrema["high"])),
                        low=Decimal(str(extrema["low"])),
                    )
        if quote.previous_close is not None and quote.previous_close != 0:
            return ReferenceMetrics(
                reference_kind="previous_session",
                reference_price=quote.previous_close,
                high=quote.high or quote.price,
                low=quote.low or quote.price,
            )
        if quote.today_open is not None and quote.today_open != 0:
            return ReferenceMetrics(
                reference_kind="since_open",
                reference_price=quote.today_open,
                high=quote.high or quote.price,
                low=quote.low or quote.price,
            )
        return None

    def save_state_and_event(
        self,
        state: SymbolState,
        *,
        event: dict[str, Any] | None = None,
    ) -> int | None:
        now = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO symbol_state(
                    symbol,mode,initialized,last_price,last_quote_at,disarmed_levels,
                    market_status,next_open_at,last_success_at,last_error,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,mode) DO UPDATE SET
                    initialized=excluded.initialized,
                    last_price=excluded.last_price,
                    last_quote_at=excluded.last_quote_at,
                    disarmed_levels=excluded.disarmed_levels,
                    market_status=excluded.market_status,
                    next_open_at=excluded.next_open_at,
                    last_success_at=excluded.last_success_at,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    state.symbol,
                    _SHARED_STATE_MODE,
                    int(state.initialized),
                    str(state.last_price) if state.last_price is not None else None,
                    state.last_quote_at,
                    json.dumps(sorted(state.disarmed_levels)),
                    state.market_status,
                    state.next_open_at,
                    state.last_success_at,
                    state.last_error,
                    now,
                ),
            )
            if event is None:
                return None
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO trigger_events(
                    event_key,mode,symbol,direction,trigger_level,current_price,
                    reference_kind,reference_price,window_high,window_low,
                    status,observed_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    event["event_key"],
                    state.mode,
                    state.symbol,
                    event["direction"],
                    event["trigger_level"],
                    event["current_price"],
                    event.get("reference_kind"),
                    event.get("reference_price"),
                    event.get("window_high"),
                    event.get("window_low"),
                    event.get("status", "pending"),
                    event["observed_at"],
                    now,
                    now,
                ),
            )
            return int(cursor.lastrowid) if cursor.rowcount else None

    def mark_event(
        self,
        event_id: int,
        *,
        status: str,
        template_key: str | None = None,
        change_percent: Decimal | None = None,
        title: str | None = None,
        content: str | None = None,
        push_status_code: int | None = None,
        push_response: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trigger_events SET
                    status=?,template_key=?,change_percent=?,title=?,content=?,
                    push_status_code=?,push_response=?,error=?,updated_at=?
                WHERE id=?
                """,
                (
                    status,
                    template_key,
                    str(change_percent) if change_percent is not None else None,
                    title,
                    content,
                    push_status_code,
                    push_response,
                    error,
                    int(time.time()),
                    event_id,
                ),
            )

    def record_fetch_error(self, symbol: str, mode: PublishMode, message: str) -> SymbolState:
        state = self.get_state(symbol, mode)
        state.last_error = message[:1000]
        self.save_state_and_event(state)
        return state

    def prune(self, *, sample_retention_hours: int = 48, event_limit: int = 100) -> None:
        cutoff = int(time.time()) - sample_retention_hours * 3600
        with self._connect() as conn:
            conn.execute("DELETE FROM price_samples WHERE observed_at<?", (cutoff,))
            conn.execute(
                """
                DELETE FROM trigger_events
                WHERE id NOT IN (
                    SELECT id FROM trigger_events ORDER BY created_at DESC,id DESC LIMIT ?
                )
                """,
                (event_limit,),
            )

    def should_send_alert(self, alert_key: str, message: str, dedup_seconds: int) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT active,last_sent_at FROM alert_state WHERE alert_key=?",
                (alert_key,),
            ).fetchone()
            allowed = (
                row is None
                or not bool(row["active"])
                or row["last_sent_at"] is None
                or now - int(row["last_sent_at"]) >= dedup_seconds
            )
            if allowed:
                conn.execute(
                    """
                    INSERT INTO alert_state(alert_key,active,last_sent_at,last_message,updated_at)
                    VALUES(?,1,?,?,?)
                    ON CONFLICT(alert_key) DO UPDATE SET
                        active=1,last_sent_at=excluded.last_sent_at,
                        last_message=excluded.last_message,updated_at=excluded.updated_at
                    """,
                    (alert_key, now, message[:1000], now),
                )
            return allowed

    def recover_alert(self, alert_key: str) -> bool:
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT active FROM alert_state WHERE alert_key=?",
                (alert_key,),
            ).fetchone()
            if row is None or not bool(row["active"]):
                return False
            conn.execute(
                "UPDATE alert_state SET active=0,updated_at=? WHERE alert_key=?",
                (now, alert_key),
            )
            return True

    def dashboard(self) -> dict[str, Any]:
        mode = self.get_mode()
        with self._connect() as conn:
            settings = {
                str(row["key"]): str(row["value"])
                for row in conn.execute("SELECT key,value FROM settings").fetchall()
            }
            templates = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT template_key,label,title_template,body_template,updated_at
                    FROM templates ORDER BY rowid
                    """
                ).fetchall()
            ]
            rows = conn.execute(
                """
                SELECT c.*,s.initialized,s.last_price,s.last_quote_at,s.disarmed_levels,
                       s.market_status,s.next_open_at,s.last_success_at,s.last_error
                FROM symbol_config c
                LEFT JOIN symbol_state s ON s.symbol=c.symbol AND s.mode=?
                ORDER BY c.rowid
                """,
                (_SHARED_STATE_MODE,),
            ).fetchall()
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM trigger_events ORDER BY created_at DESC,id DESC LIMIT 100"
                ).fetchall()
            ]
        symbols: list[dict[str, Any]] = []
        for row in rows:
            payload = dict(row)
            payload["enabled"] = bool(payload["enabled"])
            payload["initialized"] = bool(payload["initialized"])
            payload["last_quote_at"] = _iso(payload["last_quote_at"])
            payload["next_open_at"] = _iso(payload["next_open_at"])
            payload["last_success_at"] = _iso(payload["last_success_at"])
            symbols.append(payload)
        for template in templates:
            template["updated_at"] = _iso(template["updated_at"])
        for event in events:
            event["observed_at"] = _iso(event["observed_at"])
            event["created_at"] = _iso(event["created_at"])
            event["updated_at"] = _iso(event["updated_at"])
        return {
            "mode": mode,
            "poll_interval_seconds": int(settings.get("poll_interval_seconds", "60")),
            "database_path": str(self.path),
            "symbols": symbols,
            "templates": templates,
            "recent_events": events,
            "generated_at": datetime.now(UTC).isoformat(),
        }
