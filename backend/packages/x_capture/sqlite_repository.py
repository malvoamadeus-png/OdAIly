from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.attempt_sampling import (
    NOOP_SUCCESS_WINDOW,
    should_sample_x_capture_attempt,
    x_capture_attempt_fingerprint,
)
from packages.common.storage import connect_sqlite

from .client import normalize_username
from .models import CaptureRecord, CaptureRunStats, XCaptureAccount, XCaptureSettings
from .naming import choose_effective_author_name, normalize_lookup_username, normalize_write_name
from .repository import UNSET, _Unset


def _now() -> datetime:
    return datetime.now(UTC)


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else {}


def _account(row: Any) -> XCaptureAccount:
    data = dict(row)
    return XCaptureAccount(
        id=int(data["id"]),
        username=str(data["username"]),
        username_lower=str(data["username_lower"]),
        display_name=data.get("display_name"),
        write_name=data.get("write_name"),
        profile_url=data.get("profile_url"),
        enabled=bool(data["enabled"]),
        is_ai_source=bool(data.get("is_ai_source", 0)),
        interval_seconds=data.get("interval_seconds"),
        seeded_at=_dt(data.get("seeded_at")),
        last_polled_at=_dt(data.get("last_polled_at")),
        last_success_at=_dt(data.get("last_success_at")),
        last_error=data.get("last_error"),
        created_at=_dt(data.get("created_at")),
        updated_at=_dt(data.get("updated_at")),
    )


class SQLiteXCaptureRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.init_schema()

    def _connect(self):
        return connect_sqlite(self.path)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS x_capture_settings (
                    singleton_key text PRIMARY KEY DEFAULT 'global',
                    global_interval_seconds integer NOT NULL DEFAULT 30,
                    max_concurrency integer NOT NULL DEFAULT 2,
                    jitter_seconds integer NOT NULL DEFAULT 5,
                    config_version integer NOT NULL DEFAULT 1,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT OR IGNORE INTO x_capture_settings(singleton_key) VALUES ('global');

                CREATE TABLE IF NOT EXISTS x_capture_accounts (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    username text NOT NULL,
                    username_lower text NOT NULL UNIQUE,
                    display_name text,
                    write_name text,
                    profile_url text,
                    enabled integer NOT NULL DEFAULT 1,
                    is_ai_source integer NOT NULL DEFAULT 0,
                    interval_seconds integer,
                    seeded_at text,
                    last_polled_at text,
                    last_success_at text,
                    last_error text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS x_seen_tweets (
                    tweet_id text PRIMARY KEY,
                    account_id integer REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
                    username_lower text NOT NULL,
                    seeded integer NOT NULL DEFAULT 0,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    source text NOT NULL,
                    source_item_id text NOT NULL,
                    source_url text,
                    title text,
                    content text NOT NULL,
                    published_at text,
                    raw_payload text NOT NULL DEFAULT '{}',
                    metadata text NOT NULL DEFAULT '{}',
                    status text NOT NULL DEFAULT 'pending',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    locked_by text,
                    locked_until text,
                    attempt_count integer NOT NULL DEFAULT 0,
                    UNIQUE(source, source_item_id)
                );

                CREATE TABLE IF NOT EXISTS x_capture_attempts (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    account_id integer REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
                    username_lower text NOT NULL,
                    status text NOT NULL,
                    source text NOT NULL DEFAULT 'fxtwitter',
                    candidate_count integer NOT NULL DEFAULT 0,
                    seeded_count integer NOT NULL DEFAULT 0,
                    new_count integer NOT NULL DEFAULT 0,
                    saved_count integer NOT NULL DEFAULT 0,
                    error text,
                    started_at text NOT NULL,
                    finished_at text NOT NULL,
                    metadata text NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL,
                    worker_id text NOT NULL,
                    status text NOT NULL,
                    last_seen_at text NOT NULL,
                    last_success_at text,
                    last_error text,
                    metadata text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component, worker_id)
                );

                CREATE INDEX IF NOT EXISTS idx_x_capture_accounts_enabled ON x_capture_accounts(enabled);
                CREATE INDEX IF NOT EXISTS idx_x_seen_tweets_username ON x_seen_tweets(username_lower);
                CREATE INDEX IF NOT EXISTS idx_tasks_source_status_created ON tasks(source, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_x_capture_attempts_started ON x_capture_attempts(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_x_capture_attempts_noop_recent
                ON x_capture_attempts(account_id, finished_at DESC, id DESC)
                WHERE status = 'success' AND new_count = 0 AND saved_count = 0;
                """
            )
            conn.commit()

    def notify_config_changed(self) -> None:
        return None

    def get_settings(self) -> XCaptureSettings:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM x_capture_settings WHERE singleton_key='global'").fetchone()
        return XCaptureSettings(
            global_interval_seconds=int(row["global_interval_seconds"]),
            max_concurrency=int(row["max_concurrency"]),
            jitter_seconds=int(row["jitter_seconds"]),
            updated_at=_dt(row["updated_at"]),
        )

    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        max_concurrency: int | None = None,
        jitter_seconds: int | None = None,
    ) -> XCaptureSettings:
        current = self.get_settings()
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE x_capture_settings
                SET global_interval_seconds=?, max_concurrency=?, jitter_seconds=?,
                    config_version=config_version+1, updated_at=?
                WHERE singleton_key='global'
                """,
                (
                    global_interval_seconds or current.global_interval_seconds,
                    max_concurrency or current.max_concurrency,
                    jitter_seconds if jitter_seconds is not None else current.jitter_seconds,
                    now,
                ),
            )
        return self.get_settings()

    def list_accounts(self, *, include_disabled: bool = False) -> list[XCaptureAccount]:
        where = "" if include_disabled else "WHERE enabled=1"
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM x_capture_accounts {where} ORDER BY enabled DESC, username_lower").fetchall()
        return [_account(row) for row in rows]

    def create_account(
        self,
        *,
        username_or_url: str,
        display_name: str | None = None,
        write_name: str | None = None,
        interval_seconds: int | None = None,
        enabled: bool = True,
        is_ai_source: bool = False,
    ) -> XCaptureAccount:
        username = normalize_username(username_or_url)
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO x_capture_accounts(
                    username,username_lower,display_name,write_name,profile_url,enabled,is_ai_source,interval_seconds,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(username_lower) DO UPDATE SET
                    username=excluded.username,
                    display_name=COALESCE(excluded.display_name,x_capture_accounts.display_name),
                    write_name=COALESCE(excluded.write_name,x_capture_accounts.write_name),
                    profile_url=excluded.profile_url,
                    enabled=excluded.enabled,
                    is_ai_source=excluded.is_ai_source,
                    interval_seconds=excluded.interval_seconds,
                    updated_at=excluded.updated_at
                """,
                (
                    username,
                    username.lower(),
                    display_name.strip() if display_name else None,
                    normalize_write_name(write_name),
                    f"https://x.com/{username}",
                    int(enabled),
                    int(is_ai_source),
                    interval_seconds,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM x_capture_accounts WHERE username_lower=?", (username.lower(),)).fetchone()
        return _account(row)

    def update_account(
        self,
        account_id: int,
        *,
        display_name: str | None | _Unset = UNSET,
        write_name: str | None | _Unset = UNSET,
        interval_seconds: int | None | _Unset = UNSET,
        enabled: bool | None = None,
        is_ai_source: bool | None = None,
    ) -> XCaptureAccount:
        fields = ["updated_at=?"]
        params: list[Any] = [_now().isoformat()]
        if not isinstance(display_name, _Unset):
            fields.append("display_name=?")
            params.append(display_name.strip() if display_name else None)
        if not isinstance(write_name, _Unset):
            fields.append("write_name=?")
            params.append(normalize_write_name(write_name))
        if not isinstance(interval_seconds, _Unset):
            fields.append("interval_seconds=?")
            params.append(interval_seconds)
        if enabled is not None:
            fields.append("enabled=?")
            params.append(int(enabled))
        if is_ai_source is not None:
            fields.append("is_ai_source=?")
            params.append(int(is_ai_source))
        params.append(account_id)
        with self._connect() as conn:
            cursor = conn.execute(f"UPDATE x_capture_accounts SET {', '.join(fields)} WHERE id=?", params)
            if cursor.rowcount == 0:
                raise ValueError(f"X capture account not found: {account_id}")
            row = conn.execute("SELECT * FROM x_capture_accounts WHERE id=?", (account_id,)).fetchone()
        return _account(row)

    def get_account_by_username(self, username_or_url: str) -> XCaptureAccount | None:
        username = normalize_lookup_username(username_or_url)
        if not username:
            return None
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM x_capture_accounts WHERE username_lower=?", (username.lower(),)).fetchone()
        return _account(row) if row else None

    def resolve_effective_author_name(self, *, author_username: str | None, author_display_name: str | None) -> str | None:
        account = self.get_account_by_username(author_username or "")
        return choose_effective_author_name(
            write_name=account.write_name if account else None,
            author_display_name=author_display_name,
            author_username=author_username,
        )

    def delete_account(self, account_id: int) -> None:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM x_capture_accounts WHERE id=?", (account_id,))
            if cursor.rowcount == 0:
                raise ValueError(f"X capture account not found: {account_id}")

    def mark_account_seeded(self, account: XCaptureAccount, tweet_ids: list[str]) -> None:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO x_seen_tweets(tweet_id,account_id,username_lower,seeded) VALUES(?,?,?,1)",
                [(tweet_id, account.id, account.username_lower) for tweet_id in tweet_ids],
            )
            conn.execute("UPDATE x_capture_accounts SET seeded_at=?,updated_at=? WHERE id=?", (now, now, account.id))

    def mark_seen(self, account: XCaptureAccount, tweet_id: str, *, seeded: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO x_seen_tweets(tweet_id,account_id,username_lower,seeded) VALUES(?,?,?,?)",
                (tweet_id, account.id, account.username_lower, int(seeded)),
            )
            return cursor.rowcount > 0

    def unseen_tweet_ids(self, tweet_ids: list[str]) -> set[str]:
        if not tweet_ids:
            return set()
        placeholders = ",".join("?" for _ in tweet_ids)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT tweet_id FROM x_seen_tweets WHERE tweet_id IN ({placeholders})", tweet_ids).fetchall()
        return set(tweet_ids) - {str(row["tweet_id"]) for row in rows}

    def save_task(self, account: XCaptureAccount, record: CaptureRecord) -> int | None:
        effective_author_name = choose_effective_author_name(
            write_name=account.write_name,
            author_display_name=record.author_display_name,
            author_username=record.author_username,
        )
        metadata = {
            **record.metadata,
            "platform": record.platform,
            "account_id": account.id,
            "account_username": account.username,
            "author_username": record.author_username,
            "author_display_name": record.author_display_name,
            "effective_author_name": effective_author_name,
            "x_account_is_ai_source": account.is_ai_source,
            "created_at": record.created_at,
            "reply_count": record.reply_count,
            "retweet_count": record.retweet_count,
            "like_count": record.like_count,
            "bookmark_count": record.bookmark_count,
            "view_count": record.view_count,
            "media_urls": record.media_urls,
        }
        raw_payload = {
            "platform": record.platform,
            "tweet_id": record.tweet_id,
            "url": record.url,
            "text": record.text,
            "metadata": metadata,
            "raw_payload": record.raw_payload,
        }
        now = _now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO tasks(
                    source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status,created_at,updated_at
                ) VALUES('x',?,?,?,?,?,?,?,'pending',?,?)
                """,
                (
                    record.tweet_id,
                    record.url,
                    f"@{record.author_username}: {record.text[:80]}",
                    record.text,
                    record.created_at,
                    json.dumps(raw_payload, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT id FROM tasks WHERE source='x' AND source_item_id=?",
                    (record.tweet_id,),
                ).fetchone()
                return int(row["id"]) if row else None
            return int(cursor.lastrowid)

    def record_attempt(self, stats: CaptureRunStats, *, started_at: datetime, finished_at: datetime) -> None:
        metadata = dict(stats.metadata)
        fingerprint = x_capture_attempt_fingerprint(
            status=stats.status,
            candidate_count=stats.candidate_count,
            seeded_count=stats.seeded_count,
            new_count=stats.new_count,
            saved_count=stats.saved_count,
            error=stats.error,
            metadata=metadata,
        )
        cutoff = (finished_at - NOOP_SUCCESS_WINDOW).isoformat()
        with self._connect() as conn:
            previous_row = conn.execute(
                """
                SELECT finished_at,metadata FROM x_capture_attempts
                WHERE account_id=? AND status='success' AND new_count=0 AND saved_count=0 AND finished_at>=?
                ORDER BY finished_at DESC,id DESC LIMIT 1
                """,
                (stats.account.id, cutoff),
            ).fetchone()
            previous = dict(previous_row) if previous_row else None
            previous_metadata = _json(previous["metadata"]) if previous else {}
            previous_fingerprint = None
            if previous:
                previous_fingerprint = x_capture_attempt_fingerprint(
                    status="success",
                    candidate_count=int(previous_metadata.get("candidate_count") or 0),
                    seeded_count=int(previous_metadata.get("seeded_count") or 0),
                    new_count=0,
                    saved_count=0,
                    error=None,
                    metadata=previous_metadata,
                )
            if should_sample_x_capture_attempt(
                status=stats.status,
                new_count=stats.new_count,
                saved_count=stats.saved_count,
                fingerprint=fingerprint,
                finished_at=finished_at,
                previous_finished_at=_dt(previous["finished_at"]) if previous else None,
                previous_fingerprint=previous_fingerprint,
            ):
                persisted_metadata = {
                    **metadata,
                    "candidate_count": stats.candidate_count,
                    "seeded_count": stats.seeded_count,
                }
                conn.execute(
                    """
                    INSERT INTO x_capture_attempts(
                        account_id,username_lower,status,source,candidate_count,seeded_count,new_count,saved_count,
                        error,started_at,finished_at,metadata
                    ) VALUES(?,?,?,'fxtwitter',?,?,?,?,?,?,?,?)
                    """,
                    (
                        stats.account.id,
                        stats.account.username_lower,
                        stats.status,
                        stats.candidate_count,
                        stats.seeded_count,
                        stats.new_count,
                        stats.saved_count,
                        stats.error,
                        started_at.isoformat(),
                        finished_at.isoformat(),
                        json.dumps(persisted_metadata, ensure_ascii=False),
                    ),
                )
            conn.execute(
                """
                UPDATE x_capture_accounts
                SET last_polled_at=?,
                    last_success_at=CASE WHEN ?='success' THEN ? ELSE last_success_at END,
                    last_error=CASE WHEN ?='success' THEN NULL ELSE ? END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    finished_at.isoformat(),
                    stats.status,
                    finished_at.isoformat(),
                    stats.status,
                    stats.error,
                    _now().isoformat(),
                    stats.account.id,
                ),
            )

    def prune_attempts_before(self, cutoff: datetime) -> int:
        with self._connect() as conn:
            cursor = conn.execute("DELETE FROM x_capture_attempts WHERE started_at<?", (cutoff.isoformat(),))
            return int(cursor.rowcount or 0)

    def list_recent_attempts(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*,c.username,c.display_name
                FROM x_capture_attempts a LEFT JOIN x_capture_accounts c ON c.id=a.account_id
                ORDER BY a.started_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "metadata": _json(row["metadata"])} for row in rows]

    def list_recent_tasks(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id,source,source_item_id,source_url,title,content,metadata,status,created_at,updated_at
                FROM tasks WHERE source='x' ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "metadata": _json(row["metadata"])} for row in rows]

    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_worker_heartbeats(
                    component,worker_id,status,last_seen_at,last_success_at,last_error,metadata,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(component,worker_id) DO UPDATE SET
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END,
                    last_error=excluded.last_error,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
                """,
                (
                    component,
                    worker_id,
                    status,
                    now,
                    now if success else None,
                    (error or "")[:2000] if error else None,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                    int(success),
                ),
            )
