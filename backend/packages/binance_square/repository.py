from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings

from .client import normalize_profile_url
from .models import (
    BINANCE_SQUARE_SOURCE,
    POLL_INTERVAL_SECONDS,
    BinanceSquareAccount,
    BinanceSquarePost,
    BinanceSquareRunStats,
    BinanceSquareSettings,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _account(row: Any) -> BinanceSquareAccount:
    return BinanceSquareAccount(
        id=int(row["id"]), slug=str(row["slug"]), slug_lower=str(row["slug_lower"]),
        profile_url=str(row["profile_url"]), square_uid=row["square_uid"], display_name=row["display_name"],
        write_name=row["write_name"], enabled=bool(row["enabled"]), seeded_at=_dt(row["seeded_at"]),
        last_polled_at=_dt(row["last_polled_at"]), last_success_at=_dt(row["last_success_at"]),
        last_error=row["last_error"],
    )


class BinanceSquareRepository:
    def __init__(self, path: Path) -> None:
        self.path = path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS binance_square_settings (
                    singleton_key text PRIMARY KEY CHECK(singleton_key='global'),
                    enabled integer NOT NULL DEFAULT 0,
                    interval_seconds integer NOT NULL DEFAULT {POLL_INTERVAL_SECONDS},
                    worker_status text NOT NULL DEFAULT 'stopped',
                    worker_last_seen_at text,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO binance_square_settings(singleton_key) VALUES('global');
                CREATE TABLE IF NOT EXISTS binance_square_accounts (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    slug text NOT NULL,
                    slug_lower text NOT NULL UNIQUE,
                    profile_url text NOT NULL,
                    square_uid text,
                    display_name text,
                    write_name text,
                    enabled integer NOT NULL DEFAULT 1,
                    seeded_at text,
                    last_polled_at text,
                    last_success_at text,
                    last_error text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS binance_square_seen_posts (
                    post_id text PRIMARY KEY,
                    account_id integer REFERENCES binance_square_accounts(id) ON DELETE SET NULL,
                    seeded integer NOT NULL DEFAULT 0,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS binance_square_attempts (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    account_id integer REFERENCES binance_square_accounts(id) ON DELETE SET NULL,
                    slug_lower text NOT NULL,
                    status text NOT NULL,
                    candidate_count integer NOT NULL DEFAULT 0,
                    seeded_count integer NOT NULL DEFAULT 0,
                    new_count integer NOT NULL DEFAULT 0,
                    saved_count integer NOT NULL DEFAULT 0,
                    error text,
                    metadata text NOT NULL DEFAULT '{{}}',
                    started_at text NOT NULL,
                    finished_at text NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_binance_square_accounts_enabled ON binance_square_accounts(enabled);
                CREATE INDEX IF NOT EXISTS idx_binance_square_attempts_started ON binance_square_attempts(started_at DESC);
                """
            )
            conn.commit()

    def get_settings(self) -> BinanceSquareSettings:
        with connect_sqlite(self.path) as conn:
            row = conn.execute("SELECT * FROM binance_square_settings WHERE singleton_key='global'").fetchone()
        if row is None:
            raise RuntimeError("币安广场数据表尚未初始化")
        return BinanceSquareSettings(
            enabled=bool(row["enabled"]), interval_seconds=int(row["interval_seconds"]),
            worker_status=str(row["worker_status"]), worker_last_seen_at=_dt(row["worker_last_seen_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def list_accounts(self, *, include_disabled: bool = False) -> list[BinanceSquareAccount]:
        where = "" if include_disabled else "WHERE enabled=1"
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f"SELECT * FROM binance_square_accounts {where} ORDER BY enabled DESC,slug_lower").fetchall()
        return [_account(row) for row in rows]

    def set_worker_status(self, status: str) -> None:
        now = _now().isoformat()
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "UPDATE binance_square_settings SET worker_status=?,worker_last_seen_at=? WHERE singleton_key='global'",
                (status, now),
            )

    def update_account_identity(self, account_id: int, post: BinanceSquarePost) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "UPDATE binance_square_accounts SET square_uid=COALESCE(?,square_uid),display_name=COALESCE(NULLIF(?,''),display_name),updated_at=? WHERE id=?",
                (post.square_uid, post.display_name, _now().isoformat(), account_id),
            )

    def mark_seeded(self, account: BinanceSquareAccount, post_ids: list[str]) -> None:
        now = _now().isoformat()
        with connect_sqlite(self.path) as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO binance_square_seen_posts(post_id,account_id,seeded) VALUES(?,?,1)",
                [(post_id, account.id) for post_id in post_ids],
            )
            conn.execute("UPDATE binance_square_accounts SET seeded_at=?,updated_at=? WHERE id=?", (now, now, account.id))

    def unseen_post_ids(self, post_ids: list[str]) -> set[str]:
        if not post_ids:
            return set()
        marks = ",".join("?" for _ in post_ids)
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f"SELECT post_id FROM binance_square_seen_posts WHERE post_id IN ({marks})", post_ids).fetchall()
        return set(post_ids) - {str(row["post_id"]) for row in rows}

    def mark_seen(self, account_id: int, post_id: str) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO binance_square_seen_posts(post_id,account_id,seeded) VALUES(?,?,0)",
                (post_id, account_id),
            )

    def save_task(self, account: BinanceSquareAccount, post: BinanceSquarePost) -> int:
        effective_name = (account.write_name or post.display_name or post.username or account.slug).strip()
        metadata = {
            "platform": BINANCE_SQUARE_SOURCE, "account_id": account.id, "account_username": account.slug,
            "author_username": post.username or account.slug, "author_display_name": post.display_name,
            "effective_author_name": effective_name, "media_urls": post.media_urls,
        }
        raw_payload = {"platform": BINANCE_SQUARE_SOURCE, "post": post.raw_payload}
        now = _now().isoformat()
        with connect_sqlite(self.path) as conn:
            conn.execute(
                """INSERT OR IGNORE INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,'pending',?,?)""",
                (BINANCE_SQUARE_SOURCE, post.post_id, post.url, f"{effective_name}：{post.text[:80]}", post.text,
                 post.published_at, json.dumps(raw_payload, ensure_ascii=False), json.dumps(metadata, ensure_ascii=False), now, now),
            )
            row = conn.execute(
                "SELECT id FROM tasks WHERE source=? AND source_item_id=?", (BINANCE_SQUARE_SOURCE, post.post_id)
            ).fetchone()
        if row is None:
            raise RuntimeError(f"无法保存币安广场任务: {post.post_id}")
        return int(row["id"])

    def record_attempt(self, stats: BinanceSquareRunStats, *, started_at: datetime, finished_at: datetime) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                """INSERT INTO binance_square_attempts(account_id,slug_lower,status,candidate_count,seeded_count,new_count,saved_count,error,metadata,started_at,finished_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (stats.account.id, stats.account.slug_lower, stats.status, stats.candidate_count, stats.seeded_count,
                 stats.new_count, stats.saved_count, stats.error, json.dumps(stats.metadata, ensure_ascii=False),
                 started_at.isoformat(), finished_at.isoformat()),
            )
            conn.execute(
                """UPDATE binance_square_accounts SET last_polled_at=?,last_success_at=CASE WHEN ?='success' THEN ? ELSE last_success_at END,
                   last_error=CASE WHEN ?='success' THEN NULL ELSE ? END,updated_at=? WHERE id=?""",
                (finished_at.isoformat(), stats.status, finished_at.isoformat(), stats.status, stats.error, finished_at.isoformat(), stats.account.id),
            )


def create_binance_square_repository(path: Path | None = None) -> BinanceSquareRepository:
    return BinanceSquareRepository(path or load_storage_settings().sqlite_path)
