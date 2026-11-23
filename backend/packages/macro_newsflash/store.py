from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from packages.common.time_utils import SHANGHAI_TZ

from .models import MacroUpdate


class MacroNewsflashStore:
    def __new__(cls, database_path: Path):
        from packages.common.storage import load_storage_settings

        if cls is MacroNewsflashStore and load_storage_settings().backend == "postgres":
            from .postgres_store import PostgresMacroNewsflashStore

            return PostgresMacroNewsflashStore(database_path)
        return super().__new__(cls)

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
                CREATE TABLE IF NOT EXISTS macro_metric_state (
                    metric_id TEXT PRIMARY KEY,
                    last_reference_period TEXT NOT NULL,
                    last_event_key TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    last_source_name TEXT NOT NULL,
                    last_source_url TEXT NOT NULL,
                    last_raw_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS macro_newsflash_events (
                    event_key TEXT PRIMARY KEY,
                    metric_id TEXT NOT NULL,
                    reference_period TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    actual TEXT NOT NULL,
                    previous TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    delivery_started_at TEXT,
                    published_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_macro_newsflash_events_created
                    ON macro_newsflash_events(created_at DESC);
                CREATE TABLE IF NOT EXISTS macro_release_schedule (
                    release_id TEXT PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    title TEXT NOT NULL,
                    metric_ids_json TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    is_estimate INTEGER NOT NULL,
                    refreshed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_macro_release_schedule_time
                    ON macro_release_schedule(scheduled_at);
                CREATE TABLE IF NOT EXISTS macro_newsflash_meta (
                    meta_key TEXT PRIMARY KEY,
                    meta_value TEXT NOT NULL
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(macro_newsflash_events)").fetchall()
            }
            if "delivery_started_at" not in columns:
                connection.execute(
                    "ALTER TABLE macro_newsflash_events ADD COLUMN delivery_started_at TEXT"
                )

    def schedule_refresh_due(self, *, max_age: timedelta = timedelta(hours=6)) -> bool:
        with self._connect() as connection:
            metadata = {
                row["meta_key"]: row["meta_value"]
                for row in connection.execute(
                    "SELECT meta_key, meta_value FROM macro_newsflash_meta"
                ).fetchall()
            }
        refreshed_at_raw = metadata.get("schedule_refreshed_at")
        if refreshed_at_raw is None:
            return True
        try:
            refreshed_at = datetime.fromisoformat(refreshed_at_raw)
        except ValueError:
            return True
        if refreshed_at.tzinfo is None:
            refreshed_at = refreshed_at.replace(tzinfo=UTC)
        refresh_after = timedelta(minutes=15) if json.loads(metadata.get("schedule_errors") or "{}") else max_age
        return datetime.now(UTC) - refreshed_at.astimezone(UTC) >= refresh_after

    def replace_release_schedule(self, releases: list[Any], *, refreshed_sources: set[str]) -> None:
        refreshed_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for source_name in refreshed_sources:
                connection.execute("DELETE FROM macro_release_schedule WHERE source_name = ?", (source_name,))
            for release in releases:
                existing = connection.execute(
                    "SELECT source_name, is_estimate FROM macro_release_schedule WHERE release_id = ?",
                    (release.release_id,),
                ).fetchone()
                if existing and not existing["is_estimate"] and release.is_estimate:
                    continue
                if existing and existing["source_name"] == "Census" and release.source_name.startswith("TradingView"):
                    continue
                connection.execute(
                    """INSERT OR REPLACE INTO macro_release_schedule (
                        release_id, source_name, title, metric_ids_json, scheduled_at,
                        source_url, is_estimate, refreshed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        release.release_id,
                        release.source_name,
                        release.title,
                        json.dumps(release.metric_ids, ensure_ascii=False),
                        release.scheduled_at,
                        release.source_url,
                        int(release.is_estimate),
                        refreshed_at,
                    ),
                )

    def record_schedule_refresh(self, *, errors: dict[str, str]) -> None:
        refreshed_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO macro_newsflash_meta (meta_key, meta_value) VALUES (?, ?)",
                (
                    ("schedule_refreshed_at", refreshed_at),
                    ("schedule_errors", json.dumps(errors, ensure_ascii=False)),
                ),
            )

    def record_source_refresh(self, *, checked_sources: set[str], errors: dict[str, str]) -> None:
        if not checked_sources:
            return
        checked_at = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT meta_value FROM macro_newsflash_meta WHERE meta_key = 'source_errors'"
            ).fetchone()
            source_errors = json.loads(row["meta_value"]) if row else {}
            for source in checked_sources:
                source_errors.pop(source, None)
            source_errors.update(errors)
            connection.executemany(
                "INSERT OR REPLACE INTO macro_newsflash_meta (meta_key, meta_value) VALUES (?, ?)",
                (
                    ("source_checked_at", checked_at),
                    ("source_errors", json.dumps(source_errors, ensure_ascii=False)),
                ),
            )

    def upcoming_releases(self, *, limit: int = 30) -> list[dict[str, Any]]:
        cutoff = (datetime.now(UTC) - timedelta(hours=4)).isoformat(timespec="seconds")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM macro_release_schedule WHERE scheduled_at >= ? ORDER BY scheduled_at LIMIT ?",
                (cutoff, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metric_ids"] = json.loads(item.pop("metric_ids_json"))
            item["is_estimate"] = bool(item["is_estimate"])
            scheduled_at = datetime.fromisoformat(item["scheduled_at"])
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            item["scheduled_at_eastern"] = scheduled_at.astimezone(ZoneInfo("America/New_York")).isoformat(timespec="minutes")
            item["scheduled_at_beijing"] = scheduled_at.astimezone(SHANGHAI_TZ).isoformat(timespec="minutes")
            result.append(item)
        return result
    def observe_update(
        self,
        update: MacroUpdate,
        *,
        title: str,
        content: str,
        publish_latest: bool,
    ) -> str:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        raw_snapshot = {
            **update.raw_payload,
            "reported_actual": update.actual,
            "reported_previous": update.previous,
            "reported_expected": update.expected,
            "rate_action": update.rate_action,
        }
        raw_json = json.dumps(raw_snapshot, ensure_ascii=False, separators=(",", ":"))
        is_scheduled_jin10 = bool(update.raw_payload.get("jin10_release_id"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT last_reference_period FROM macro_metric_state WHERE metric_id = ?",
                (update.metric_id,),
            ).fetchone()
            if state is None:
                connection.execute(
                    """INSERT INTO macro_metric_state (
                        metric_id, last_reference_period, last_event_key, last_observed_at,
                        last_source_name, last_source_url, last_raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        update.metric_id,
                        update.reference_period,
                        update.event_key,
                        update.observed_at,
                        update.source_name,
                        update.source_url,
                        raw_json,
                    ),
                )
                if not publish_latest and not is_scheduled_jin10:
                    return "baselined"
            elif update.reference_period <= state["last_reference_period"]:
                if not is_scheduled_jin10:
                    return "unchanged"
            else:
                connection.execute(
                    """UPDATE macro_metric_state SET last_reference_period = ?, last_event_key = ?,
                        last_observed_at = ?, last_source_name = ?, last_source_url = ?, last_raw_json = ?
                        WHERE metric_id = ?""",
                    (
                        update.reference_period,
                        update.event_key,
                        update.observed_at,
                        update.source_name,
                        update.source_url,
                        raw_json,
                        update.metric_id,
                    ),
                )
            cursor = connection.execute(
                """INSERT OR IGNORE INTO macro_newsflash_events (
                    event_key, metric_id, reference_period, source_name, source_url, actual,
                    previous, title, content, status, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    update.event_key,
                    update.metric_id,
                    update.reference_period,
                    update.source_name,
                    update.source_url,
                    update.actual,
                    update.previous,
                    title,
                    content,
                    raw_json,
                    now,
                ),
            )
        return "queued" if cursor.rowcount else "unchanged"

    def event_exists(self, event_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM macro_newsflash_events WHERE event_key = ? LIMIT 1",
                (event_key,),
            ).fetchone()
        return row is not None

    def claim_pending(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM macro_newsflash_events WHERE status = 'pending' ORDER BY created_at, event_key LIMIT ?",
                (limit,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE macro_newsflash_events SET status = 'publishing', delivery_started_at = ? "
                    "WHERE event_key = ? AND status = 'pending'",
                    (datetime.now(UTC).isoformat(timespec="seconds"), row["event_key"]),
                )
            return [dict(row) for row in rows]

    def finish_delivery(self, *, event_key: str, ok: bool, error: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE macro_newsflash_events SET status = ?, error = ?, published_at = ?
                    WHERE event_key = ?""",
                (
                    "published" if ok else "uncertain",
                    None if ok else (error or "delivery result is unknown"),
                    datetime.now(UTC).isoformat(timespec="seconds") if ok else None,
                    event_key,
                ),
            )

    def recover_inflight(self) -> int:
        stale_before = datetime.now(UTC).timestamp() - 120
        stale_before_iso = datetime.fromtimestamp(stale_before, UTC).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE macro_newsflash_events SET status = 'uncertain',
                    error = 'worker stopped during delivery; check the feed before manual recovery'
                    WHERE status = 'publishing' AND delivery_started_at < ?""",
                (stale_before_iso,),
            )
            return cursor.rowcount

    def dashboard(self, *, limit: int = 50) -> dict[str, Any]:
        empty: dict[str, Any] = {
            "available": False,
            "database_path": str(self.path),
            "metrics": [],
            "events": [],
            "scheduled_releases": [],
            "schedule_refreshed_at": None,
            "schedule_errors": {},
            "source_checked_at": None,
            "source_errors": {},
        }
        if not self.path.is_file():
            return empty
        uri = f"{self.path.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5)
        except sqlite3.Error:
            return empty
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                row["name"]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            required = {"macro_metric_state", "macro_newsflash_events", "macro_release_schedule", "macro_newsflash_meta"}
            if not required.issubset(tables):
                return empty
            states = connection.execute(
                "SELECT * FROM macro_metric_state ORDER BY metric_id"
            ).fetchall()
            events = connection.execute(
                "SELECT event_key, metric_id, reference_period, actual, previous, title, status, error, created_at, published_at, raw_json "
                "FROM macro_newsflash_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            metadata = {
                row["meta_key"]: row["meta_value"]
                for row in connection.execute("SELECT meta_key, meta_value FROM macro_newsflash_meta").fetchall()
            }
            cutoff = (datetime.now(UTC) - timedelta(hours=4)).isoformat(timespec="seconds")
            releases = connection.execute(
                "SELECT * FROM macro_release_schedule WHERE scheduled_at >= ? ORDER BY scheduled_at LIMIT 30",
                (cutoff,),
            ).fetchall()
        finally:
            connection.close()
        result = {
            "available": True,
            "database_path": str(self.path),
            "metrics": [
                {
                    "metric_id": row["metric_id"],
                    "last_reference_period": row["last_reference_period"],
                    "last_observed_at": row["last_observed_at"],
                    "source_name": row["last_source_name"],
                    "source_url": row["last_source_url"],
                }
                for row in states
            ],
            "events": [],
            "scheduled_releases": [],
            "schedule_refreshed_at": metadata.get("schedule_refreshed_at"),
            "schedule_errors": json.loads(metadata.get("schedule_errors") or "{}"),
            "source_checked_at": metadata.get("source_checked_at"),
            "source_errors": json.loads(metadata.get("source_errors") or "{}"),
        }
        for row in events:
            event = dict(row)
            try:
                snapshot = json.loads(event.pop("raw_json"))
            except (TypeError, ValueError):
                snapshot = {}
            event["expected"] = snapshot.get("reported_expected") or ""
            event["release_id"] = snapshot.get("jin10_release_id")
            result["events"].append(event)
        for metric, row in zip(result["metrics"], states):
            try:
                snapshot = json.loads(row["last_raw_json"])
            except (TypeError, ValueError):
                snapshot = {}
            metric["actual"] = snapshot.get("reported_actual")
            metric["previous"] = snapshot.get("reported_previous")
            metric["expected"] = snapshot.get("reported_expected")
            metric["rate_action"] = snapshot.get("rate_action")
        for row in releases:
            item = dict(row)
            item["metric_ids"] = json.loads(item.pop("metric_ids_json"))
            item["is_estimate"] = bool(item["is_estimate"])
            scheduled_at = datetime.fromisoformat(item["scheduled_at"])
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            item["scheduled_at_eastern"] = scheduled_at.astimezone(ZoneInfo("America/New_York")).isoformat(timespec="minutes")
            item["scheduled_at_beijing"] = scheduled_at.astimezone(SHANGHAI_TZ).isoformat(timespec="minutes")
            result["scheduled_releases"].append(item)
        return result
