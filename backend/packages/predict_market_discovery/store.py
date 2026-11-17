from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import MarketObservation


class PredictMarketStore:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS predict_market_observations (
                    market_id TEXT PRIMARY KEY,
                    category_id TEXT NOT NULL,
                    category_title TEXT NOT NULL,
                    category_slug TEXT NOT NULL,
                    category_published_at TEXT,
                    starts_at TEXT,
                    ends_at TEXT,
                    market_created_at TEXT,
                    question TEXT NOT NULL,
                    market_variant TEXT NOT NULL,
                    market_status TEXT NOT NULL,
                    trading_status TEXT NOT NULL,
                    market_url TEXT NOT NULL,
                    filter_reason TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    from_baseline INTEGER NOT NULL,
                    candidate_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_predict_market_candidate
                    ON predict_market_observations(candidate_at DESC);
                CREATE INDEX IF NOT EXISTS idx_predict_market_start
                    ON predict_market_observations(starts_at);
                CREATE TABLE IF NOT EXISTS predict_market_scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pages INTEGER NOT NULL,
                    categories INTEGER NOT NULL,
                    markets INTEGER NOT NULL,
                    new_markets INTEGER NOT NULL,
                    candidates INTEGER NOT NULL,
                    filtered_markets INTEGER NOT NULL,
                    page_limit_reached INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS predict_market_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                );
                """
            )

    def is_bootstrapped(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT meta_value FROM predict_market_meta WHERE meta_key = 'bootstrapped_at'"
            ).fetchone()
            return row is not None

    def record_scan(
        self,
        *,
        observations: list[MarketObservation],
        pages: int,
        categories: int,
        started_at: datetime,
        page_limit_reached: bool,
    ) -> dict[str, Any]:
        completed_at = datetime.now(UTC)
        now_text = _iso(completed_at)
        is_baseline = not self.is_bootstrapped()
        deduplicated = {observation.market_id: observation for observation in observations}
        new_markets = 0
        filtered_markets = 0
        lower = completed_at - timedelta(hours=72)
        upper = completed_at + timedelta(hours=72)

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for observation in deduplicated.values():
                existing = connection.execute(
                    "SELECT 1 FROM predict_market_observations WHERE market_id = ?",
                    (observation.market_id,),
                ).fetchone()
                is_new = existing is None
                new_markets += is_new and not is_baseline
                filtered_markets += observation.filter_reason is not None
                connection.execute(
                    """INSERT INTO predict_market_observations (
                        market_id, category_id, category_title, category_slug,
                        category_published_at, starts_at, ends_at, market_created_at,
                        question, market_variant, market_status, trading_status,
                        market_url, filter_reason, first_seen_at, last_seen_at,
                        from_baseline, candidate_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(market_id) DO UPDATE SET
                        category_id = excluded.category_id,
                        category_title = excluded.category_title,
                        category_slug = excluded.category_slug,
                        category_published_at = excluded.category_published_at,
                        starts_at = excluded.starts_at,
                        ends_at = excluded.ends_at,
                        market_created_at = excluded.market_created_at,
                        question = excluded.question,
                        market_variant = excluded.market_variant,
                        market_status = excluded.market_status,
                        trading_status = excluded.trading_status,
                        market_url = excluded.market_url,
                        filter_reason = excluded.filter_reason,
                        last_seen_at = excluded.last_seen_at""",
                    (
                        observation.market_id,
                        observation.category_id,
                        observation.category_title,
                        observation.category_slug,
                        observation.category_published_at,
                        observation.starts_at,
                        observation.ends_at,
                        observation.market_created_at,
                        observation.question,
                        observation.market_variant,
                        observation.market_status,
                        observation.trading_status,
                        observation.market_url,
                        observation.filter_reason,
                        now_text,
                        now_text,
                        int(is_baseline),
                    ),
                )

            if is_baseline:
                connection.execute(
                    "INSERT OR REPLACE INTO predict_market_meta (meta_key, meta_value) VALUES ('bootstrapped_at', ?)",
                    (now_text,),
                )
            pending = connection.execute(
                """SELECT market_id, starts_at, market_status, trading_status
                FROM predict_market_observations
                WHERE candidate_at IS NULL AND starts_at IS NOT NULL"""
            ).fetchall()
            for row in pending:
                starts_at = _parse_time(row["starts_at"])
                if (
                    starts_at is None
                    or starts_at < lower
                    or starts_at > upper
                    or row["market_status"].upper() != "OPEN"
                    or row["trading_status"].upper() not in {"", "OPEN"}
                ):
                    continue
                connection.execute(
                    "UPDATE predict_market_observations SET candidate_at = ? WHERE market_id = ?",
                    (now_text, row["market_id"]),
                )

            candidate_items = self._candidate_rows(
                connection,
                now=completed_at,
                limit=100,
                candidate_at=now_text,
            )
            candidate_count = self._candidate_count(connection, now=completed_at)
            connection.execute(
                """INSERT INTO predict_market_scan_runs (
                    started_at, completed_at, status, pages, categories, markets,
                    new_markets, candidates, filtered_markets, page_limit_reached, error
                ) VALUES (?, ?, 'success', ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    _iso(started_at),
                    now_text,
                    pages,
                    categories,
                    len(deduplicated),
                    new_markets,
                    candidate_count,
                    filtered_markets,
                    int(page_limit_reached),
                ),
            )
            connection.commit()

        return {
            "baseline": is_baseline,
            "pages": pages,
            "categories": categories,
            "markets": len(deduplicated),
            "new_markets": new_markets,
            "new_candidates": len(candidate_items),
            "pending_candidates": candidate_count,
            "filtered_markets": filtered_markets,
            "page_limit_reached": page_limit_reached,
            "candidate_items": candidate_items,
        }

    def record_failure(self, *, started_at: datetime, error: str) -> None:
        completed_at = datetime.now(UTC)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO predict_market_scan_runs (
                    started_at, completed_at, status, pages, categories, markets,
                    new_markets, candidates, filtered_markets, page_limit_reached, error
                ) VALUES (?, ?, 'failed', 0, 0, 0, 0, 0, 0, 0, ?)""",
                (_iso(started_at), _iso(completed_at), error[:1000]),
            )

    def dashboard(self, *, candidate_limit: int = 50) -> dict[str, Any]:
        now = datetime.now(UTC)
        with self._connect() as connection:
            total_markets = connection.execute(
                "SELECT COUNT(*) FROM predict_market_observations"
            ).fetchone()[0]
            pending_candidates = self._candidate_rows(connection, now=now, limit=candidate_limit)
            pending_count = self._candidate_count(connection, now=now)
            filtered_count = connection.execute(
                "SELECT COUNT(*) FROM predict_market_observations WHERE filter_reason IS NOT NULL"
            ).fetchone()[0]
            last_scan = connection.execute(
                "SELECT * FROM predict_market_scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            bootstrap = connection.execute(
                "SELECT meta_value FROM predict_market_meta WHERE meta_key = 'bootstrapped_at'"
            ).fetchone()
        return {
            "database_path": str(self.path),
            "bootstrapped_at": bootstrap[0] if bootstrap else None,
            "seen_markets": total_markets,
            "pending_candidates": pending_count,
            "filtered_markets": filtered_count,
            "last_scan": dict(last_scan) if last_scan else None,
            "candidates": pending_candidates,
        }

    @staticmethod
    def _candidate_rows(
        connection: sqlite3.Connection,
        *,
        now: datetime,
        limit: int,
        candidate_at: str | None = None,
    ) -> list[dict[str, Any]]:
        lower = _iso(now - timedelta(hours=72))
        upper = _iso(now + timedelta(hours=72))
        candidate_clause = "AND candidate_at = ?" if candidate_at is not None else ""
        parameters: tuple[Any, ...] = (lower, upper, limit)
        if candidate_at is not None:
            parameters = (lower, upper, candidate_at, limit)
        rows = connection.execute(
            f"""SELECT market_id, question, category_title, category_slug,
                category_published_at, starts_at, market_created_at,
                market_variant, market_status, trading_status, market_url,
                first_seen_at, from_baseline, candidate_at
            FROM predict_market_observations
            WHERE candidate_at IS NOT NULL
                AND filter_reason IS NULL
                AND starts_at >= ? AND starts_at <= ?
                AND market_status = 'OPEN'
                AND trading_status IN ('', 'OPEN')
                {candidate_clause}
            ORDER BY candidate_at DESC, market_id
            LIMIT ?""",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _candidate_count(connection: sqlite3.Connection, *, now: datetime) -> int:
        lower = _iso(now - timedelta(hours=72))
        upper = _iso(now + timedelta(hours=72))
        row = connection.execute(
            """SELECT COUNT(*) FROM predict_market_observations
            WHERE candidate_at IS NOT NULL
                AND filter_reason IS NULL
                AND starts_at >= ? AND starts_at <= ?
                AND market_status = 'OPEN'
                AND trading_status IN ('', 'OPEN', 'open')""",
            (lower, upper),
        ).fetchone()
        return int(row[0])


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")
