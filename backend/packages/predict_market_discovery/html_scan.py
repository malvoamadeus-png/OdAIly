"""Persistent discovery ledger for the Predict.fun HTML feed.

This module records discovery only. It does not write or deliver newsflashes.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .html_feed import FEEDS, fetch_feed


def daily_filter_reason(node: dict[str, Any]) -> str | None:
    """Identify explicit recurring daily feeds; uncertain topics need semantic review."""
    variant = str(node.get("marketVariant") or "").upper()
    if variant in {"CRYPTO_UP_DOWN", "UP_DOWN"}:
        return "日度或短周期涨跌模板"
    title = str(node.get("title") or "").casefold()
    slug = str(node.get("slug") or "").casefold()
    date_hint = bool(re.search(r"\d{1,2}月\d{1,2}日|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[- ]\d{1,2}\b", title + " " + slug))
    if date_hint and ("up or down" in title or "up-or-down" in slug or "涨跌" in title or "涨还是跌" in title or "涨或跌" in title):
        return "日度涨跌模板"
    if date_hint and any(word in title + " " + slug for word in ("temperature", "rainfall", "snowfall", "气温", "温度", "降雨", "降雪")):
        return "日度天气模板"
    return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except ValueError:
        return None


class HtmlDiscoveryStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize(self) -> None:
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS feed_baselines (
                    feed TEXT PRIMARY KEY, initialized_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS html_topics (
                    category_id TEXT PRIMARY KEY, slug TEXT NOT NULL, title TEXT NOT NULL,
                    created_at TEXT, starts_at TEXT, first_seen_at TEXT NOT NULL,
                    first_feed TEXT NOT NULL, status TEXT NOT NULL, reason TEXT,
                    raw_json TEXT NOT NULL, detail_json TEXT, writer_json TEXT,
                    final_title TEXT, final_content TEXT, push_result TEXT,
                    last_error TEXT, updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS html_scan_runs (
                    id INTEGER PRIMARY KEY, feed TEXT NOT NULL, scanned_at TEXT NOT NULL,
                    status TEXT NOT NULL, baseline INTEGER NOT NULL DEFAULT 0,
                    total_count INTEGER, returned_count INTEGER, has_next_page INTEGER,
                    snapshot_ms INTEGER, new_count INTEGER NOT NULL DEFAULT 0, error TEXT
                );
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(html_topics)")}
            for name in ("detail_json", "writer_json", "final_title", "final_content", "push_result", "last_error", "updated_at"):
                if name not in columns:
                    con.execute(f"ALTER TABLE html_topics ADD COLUMN {name} TEXT")

    def record_feed(self, feed: str, result: dict[str, Any], now: datetime) -> dict[str, Any]:
        now = now.astimezone(UTC)
        now_text = now.isoformat()
        new_count = 0
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            baseline = con.execute("SELECT 1 FROM feed_baselines WHERE feed=?", (feed,)).fetchone() is None
            for node in result["nodes"]:
                created = _time(node.get("createdAt"))
                if baseline:
                    status, reason = "baseline", "首次成功扫描"
                elif created is None or created > now:
                    status, reason = "invalid_time", "创建时间缺失或晚于扫描时间"
                elif created < now - timedelta(hours=48):
                    status, reason = "too_old", "类目创建超过48小时"
                elif daily_filter_reason(node):
                    status, reason = "filtered", daily_filter_reason(node)
                else:
                    status, reason = "pending", None
                cur = con.execute("""INSERT OR IGNORE INTO html_topics
                    (category_id,slug,title,created_at,starts_at,first_seen_at,first_feed,status,reason,raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                    str(node["id"]), str(node["slug"]), str(node.get("title") or ""),
                    node.get("createdAt"), node.get("startsAt"), now_text, feed, status, reason,
                    json.dumps(node, ensure_ascii=False),
                ))
                new_count += int(cur.rowcount == 1 and status == "pending")
            con.execute("INSERT OR IGNORE INTO feed_baselines VALUES (?,?)", (feed, now_text))
            con.execute("""INSERT INTO html_scan_runs
                (feed,scanned_at,status,baseline,total_count,returned_count,has_next_page,snapshot_ms,new_count)
                VALUES (?,?,'success',?,?,?,?,?,?)""", (
                feed, now_text, int(baseline), result.get("total_count"), len(result["nodes"]),
                int(bool(result.get("has_next_page"))), result.get("snapshot_ms"), new_count,
            ))
        return {"feed": feed, "baseline": baseline, "returned": len(result["nodes"]),
                "total": result.get("total_count"), "has_next_page": result.get("has_next_page"),
                "new_topics": new_count}

    def record_error(self, feed: str, now: datetime, error: str) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO html_scan_runs(feed,scanned_at,status,error) VALUES (?,?,'failed',?)",
                        (feed, now.astimezone(UTC).isoformat(), error[:1000]))


def scan_once(store: HtmlDiscoveryStore, *, fetch: Callable[[str], dict[str, Any]] = fetch_feed,
              now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    store.initialize()
    output = []
    for feed in FEEDS:
        try:
            output.append(store.record_feed(feed, fetch(feed), now))
        except Exception as exc:
            store.record_error(feed, now, f"{type(exc).__name__}: {exc}")
            output.append({"feed": feed, "error": f"{type(exc).__name__}: {exc}"})
    return output
