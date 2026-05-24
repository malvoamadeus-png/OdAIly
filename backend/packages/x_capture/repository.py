from __future__ import annotations

import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, Protocol

from dotenv import load_dotenv

from packages.common.attempt_sampling import (
    NOOP_SUCCESS_WINDOW,
    should_sample_x_capture_attempt,
    x_capture_attempt_fingerprint,
)
from packages.common.pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL

from .client import normalize_username
from .models import CaptureRecord, CaptureRunStats, XCaptureAccount, XCaptureSettings


CONFIG_NOTIFY_CHANNEL = "x_capture_config_changed"


class XCaptureRepository(Protocol):
    def get_settings(self) -> XCaptureSettings: ...
    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        max_concurrency: int | None = None,
        jitter_seconds: int | None = None,
    ) -> XCaptureSettings: ...
    def list_accounts(self, *, include_disabled: bool = False) -> list[XCaptureAccount]: ...
    def create_account(
        self,
        *,
        username_or_url: str,
        display_name: str | None = None,
        interval_seconds: int | None = None,
        enabled: bool = True,
    ) -> XCaptureAccount: ...
    def update_account(
        self,
        account_id: int,
        *,
        display_name: str | None | _Unset = None,
        interval_seconds: int | None | _Unset = None,
        enabled: bool | None = None,
    ) -> XCaptureAccount: ...
    def delete_account(self, account_id: int) -> None: ...
    def mark_account_seeded(self, account: XCaptureAccount, tweet_ids: list[str]) -> None: ...
    def mark_seen(self, account: XCaptureAccount, tweet_id: str, *, seeded: bool) -> bool: ...
    def unseen_tweet_ids(self, tweet_ids: list[str]) -> set[str]: ...
    def save_task(self, account: XCaptureAccount, record: CaptureRecord) -> bool: ...
    def record_attempt(self, stats: CaptureRunStats, *, started_at: datetime, finished_at: datetime) -> None: ...
    def prune_attempts_before(self, cutoff: datetime) -> int: ...
    def list_recent_attempts(self, *, limit: int = 25) -> list[dict[str, Any]]: ...
    def list_recent_tasks(self, *, limit: int = 25) -> list[dict[str, Any]]: ...
    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class _Unset:
    pass


UNSET = _Unset()


def get_database_url() -> str:
    load_dotenv()
    value = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Missing SUPABASE_DB_URL or DATABASE_URL")
    return value


def utc_now() -> datetime:
    return datetime.now(UTC)


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except Exception as exc:  # pragma: no cover - exercised only when dependency is absent.
        raise RuntimeError("psycopg is required for Supabase/Postgres access") from exc
    return psycopg, dict_row, Jsonb


def _row_to_settings(row: dict[str, Any]) -> XCaptureSettings:
    return XCaptureSettings(
        global_interval_seconds=int(row["global_interval_seconds"]),
        max_concurrency=int(row["max_concurrency"]),
        jitter_seconds=int(row["jitter_seconds"]),
        updated_at=row.get("updated_at"),
    )


def _row_to_account(row: dict[str, Any]) -> XCaptureAccount:
    return XCaptureAccount(
        id=int(row["id"]),
        username=str(row["username"]),
        username_lower=str(row["username_lower"]),
        display_name=row.get("display_name"),
        profile_url=row.get("profile_url"),
        enabled=bool(row["enabled"]),
        interval_seconds=row.get("interval_seconds"),
        seeded_at=row.get("seeded_at"),
        last_polled_at=row.get("last_polled_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


class PostgresXCaptureRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()

    def notify_config_changed(self) -> None:
        with self._connect() as conn:
            conn.execute("SELECT pg_notify(%s, %s)", (CONFIG_NOTIFY_CHANNEL, "manual"))
            conn.commit()

    def get_settings(self) -> XCaptureSettings:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO x_capture_settings (singleton_key)
                VALUES ('global')
                ON CONFLICT (singleton_key) DO NOTHING
                RETURNING *
                """
            ).fetchone()
            if row is None:
                row = conn.execute("SELECT * FROM x_capture_settings WHERE singleton_key = 'global'").fetchone()
            conn.commit()
            return _row_to_settings(row)

    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        max_concurrency: int | None = None,
        jitter_seconds: int | None = None,
    ) -> XCaptureSettings:
        current = self.get_settings()
        values = {
            "global_interval_seconds": global_interval_seconds or current.global_interval_seconds,
            "max_concurrency": max_concurrency or current.max_concurrency,
            "jitter_seconds": jitter_seconds if jitter_seconds is not None else current.jitter_seconds,
        }
        with self._connect() as conn:
            row = conn.execute(
                """
                UPDATE x_capture_settings
                SET global_interval_seconds = %(global_interval_seconds)s,
                    max_concurrency = %(max_concurrency)s,
                    jitter_seconds = %(jitter_seconds)s,
                    config_version = config_version + 1,
                    updated_at = now()
                WHERE singleton_key = 'global'
                RETURNING *
                """,
                values,
            ).fetchone()
            conn.commit()
            return _row_to_settings(row)

    def list_accounts(self, *, include_disabled: bool = False) -> list[XCaptureAccount]:
        where = "" if include_disabled else "WHERE enabled = true"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM x_capture_accounts
                {where}
                ORDER BY enabled DESC, username_lower ASC
                """
            ).fetchall()
        return [_row_to_account(row) for row in rows]

    def create_account(
        self,
        *,
        username_or_url: str,
        display_name: str | None = None,
        interval_seconds: int | None = None,
        enabled: bool = True,
    ) -> XCaptureAccount:
        username = normalize_username(username_or_url)
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO x_capture_accounts (
                    username, username_lower, display_name, profile_url, enabled, interval_seconds
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (username_lower) DO UPDATE
                SET username = EXCLUDED.username,
                    display_name = COALESCE(EXCLUDED.display_name, x_capture_accounts.display_name),
                    profile_url = EXCLUDED.profile_url,
                    enabled = EXCLUDED.enabled,
                    interval_seconds = EXCLUDED.interval_seconds,
                    updated_at = now()
                RETURNING *
                """,
                (
                    username,
                    username.lower(),
                    display_name.strip() if display_name else None,
                    f"https://x.com/{username}",
                    enabled,
                    interval_seconds,
                ),
            ).fetchone()
            conn.commit()
            return _row_to_account(row)

    def update_account(
        self,
        account_id: int,
        *,
        display_name: str | None | _Unset = UNSET,
        interval_seconds: int | None | _Unset = UNSET,
        enabled: bool | None = None,
    ) -> XCaptureAccount:
        fields: list[str] = ["updated_at = now()"]
        params: dict[str, Any] = {"id": account_id}
        if not isinstance(display_name, _Unset):
            fields.append("display_name = %(display_name)s")
            params["display_name"] = display_name.strip() if display_name else None
        if not isinstance(interval_seconds, _Unset):
            fields.append("interval_seconds = %(interval_seconds)s")
            params["interval_seconds"] = interval_seconds
        if enabled is not None:
            fields.append("enabled = %(enabled)s")
            params["enabled"] = enabled
        with self._connect() as conn:
            row = conn.execute(
                f"""
                UPDATE x_capture_accounts
                SET {", ".join(fields)}
                WHERE id = %(id)s
                RETURNING *
                """,
                params,
            ).fetchone()
            if row is None:
                raise ValueError(f"X capture account not found: {account_id}")
            conn.commit()
            return _row_to_account(row)

    def delete_account(self, account_id: int) -> None:
        with self._connect() as conn:
            row = conn.execute("DELETE FROM x_capture_accounts WHERE id = %s RETURNING id", (account_id,)).fetchone()
            if row is None:
                raise ValueError(f"X capture account not found: {account_id}")
            conn.commit()

    def mark_account_seeded(self, account: XCaptureAccount, tweet_ids: list[str]) -> None:
        with self._connect() as conn:
            for tweet_id in tweet_ids:
                conn.execute(
                    """
                    INSERT INTO x_seen_tweets (tweet_id, account_id, username_lower, seeded)
                    VALUES (%s, %s, %s, true)
                    ON CONFLICT (tweet_id) DO NOTHING
                    """,
                    (tweet_id, account.id, account.username_lower),
                )
            conn.execute("UPDATE x_capture_accounts SET seeded_at = now(), updated_at = now() WHERE id = %s", (account.id,))
            conn.commit()

    def mark_seen(self, account: XCaptureAccount, tweet_id: str, *, seeded: bool) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO x_seen_tweets (tweet_id, account_id, username_lower, seeded)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (tweet_id) DO NOTHING
                RETURNING tweet_id
                """,
                (tweet_id, account.id, account.username_lower, seeded),
            ).fetchone()
            conn.commit()
            return row is not None

    def unseen_tweet_ids(self, tweet_ids: list[str]) -> set[str]:
        if not tweet_ids:
            return set()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tweet_id FROM x_seen_tweets WHERE tweet_id = ANY(%s)",
                (tweet_ids,),
            ).fetchall()
        seen = {str(row["tweet_id"]) for row in rows}
        return set(tweet_ids) - seen

    def save_task(self, account: XCaptureAccount, record: CaptureRecord) -> bool:
        metadata = {
            **record.metadata,
            "platform": record.platform,
            "account_id": account.id,
            "account_username": account.username,
            "author_username": record.author_username,
            "author_display_name": record.author_display_name,
            "created_at": record.created_at,
            "reply_count": record.reply_count,
            "retweet_count": record.retweet_count,
            "like_count": record.like_count,
            "bookmark_count": record.bookmark_count,
            "view_count": record.view_count,
            "media_urls": record.media_urls,
        }
        payload = {
            "platform": record.platform,
            "tweet_id": record.tweet_id,
            "url": record.url,
            "text": record.text,
            "metadata": metadata,
            "raw_payload": record.raw_payload,
        }
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO tasks (
                    source, source_item_id, source_url, title, content, published_at, raw_payload, metadata, status
                )
                VALUES ('x', %(tweet_id)s, %(url)s, %(title)s, %(content)s, %(published_at)s, %(raw_payload)s, %(metadata)s, 'pending')
                ON CONFLICT (source, source_item_id) DO NOTHING
                RETURNING id
                """,
                {
                    "tweet_id": record.tweet_id,
                    "url": record.url,
                    "title": f"@{record.author_username}: {record.text[:80]}",
                    "content": record.text,
                    "published_at": record.created_at,
                    "raw_payload": self._Jsonb(payload["raw_payload"]),
                    "metadata": self._Jsonb(payload["metadata"]),
                },
            ).fetchone()
            conn.commit()
            return row is not None

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
        with self._connect() as conn:
            previous = conn.execute(
                """
                SELECT finished_at, metadata
                FROM x_capture_attempts
                WHERE account_id = %s
                  AND status = 'success'
                  AND new_count = 0
                  AND saved_count = 0
                  AND finished_at >= %s
                ORDER BY finished_at DESC, id DESC
                LIMIT 1
                """,
                (stats.account.id, finished_at - NOOP_SUCCESS_WINDOW),
            ).fetchone()
            previous_finished_at = previous.get("finished_at") if previous else None
            previous_metadata = previous.get("metadata") if previous else {}
            previous_fingerprint = None
            if previous is not None:
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
                previous_finished_at=previous_finished_at,
                previous_fingerprint=previous_fingerprint,
            ):
                persisted_metadata = {
                    **metadata,
                    "candidate_count": stats.candidate_count,
                    "seeded_count": stats.seeded_count,
                }
                conn.execute(
                    """
                    INSERT INTO x_capture_attempts (
                        account_id, username_lower, status, source, candidate_count, seeded_count,
                        new_count, saved_count, error, started_at, finished_at, metadata
                    )
                    VALUES (%s, %s, %s, 'fxtwitter', %s, %s, %s, %s, %s, %s, %s, %s)
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
                        started_at,
                        finished_at,
                        self._Jsonb(persisted_metadata),
                    ),
                )
            conn.execute(
                """
                UPDATE x_capture_accounts
                SET last_polled_at = %(finished_at)s,
                    last_success_at = CASE WHEN %(status)s = 'success' THEN %(finished_at)s ELSE last_success_at END,
                    last_error = CASE WHEN %(status)s = 'success' THEN NULL ELSE %(error)s END,
                    updated_at = now()
                WHERE id = %(account_id)s
                """,
                {
                    "finished_at": finished_at,
                    "status": stats.status,
                    "error": stats.error,
                    "account_id": stats.account.id,
                },
            )
            conn.commit()

    def prune_attempts_before(self, cutoff: datetime) -> int:
        with self._connect() as conn:
            rows = conn.execute(
                """
                DELETE FROM x_capture_attempts
                WHERE started_at < %s
                RETURNING id
                """,
                (cutoff,),
            ).fetchall()
            conn.commit()
            return len(rows)

    def list_recent_attempts(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT a.*, c.username, c.display_name
                FROM x_capture_attempts a
                LEFT JOIN x_capture_accounts c ON c.id = a.account_id
                ORDER BY a.started_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

    def list_recent_tasks(self, *, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT id, source, source_item_id, source_url, title, content, metadata, status, created_at, updated_at
                FROM tasks
                WHERE source = 'x'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

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
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_worker_heartbeats (
                    component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                )
                VALUES (%s, %s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END, %s, %s)
                ON CONFLICT (component, worker_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_success_at = CASE
                        WHEN %s THEN EXCLUDED.last_success_at
                        ELSE pipeline_worker_heartbeats.last_success_at
                    END,
                    last_error = EXCLUDED.last_error,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    component,
                    worker_id,
                    status,
                    success,
                    (error or "")[:2000] if error else None,
                    self._Jsonb(metadata or {}),
                    success,
                ),
            )
            conn.commit()


class InMemoryXCaptureRepository:
    def __init__(self) -> None:
        self.settings = XCaptureSettings()
        self.accounts: dict[int, XCaptureAccount] = {}
        self._account_id = 1
        self.seen: dict[str, dict[str, Any]] = {}
        self.tasks: list[dict[str, Any]] = []
        self.attempts: list[dict[str, Any]] = []

    def get_settings(self) -> XCaptureSettings:
        return self.settings

    def update_settings(
        self,
        *,
        global_interval_seconds: int | None = None,
        max_concurrency: int | None = None,
        jitter_seconds: int | None = None,
    ) -> XCaptureSettings:
        self.settings = XCaptureSettings(
            global_interval_seconds=global_interval_seconds or self.settings.global_interval_seconds,
            max_concurrency=max_concurrency or self.settings.max_concurrency,
            jitter_seconds=jitter_seconds if jitter_seconds is not None else self.settings.jitter_seconds,
            updated_at=utc_now(),
        )
        return self.settings

    def list_accounts(self, *, include_disabled: bool = False) -> list[XCaptureAccount]:
        accounts = list(self.accounts.values())
        if not include_disabled:
            accounts = [account for account in accounts if account.enabled]
        return sorted(accounts, key=lambda item: item.username_lower)

    def create_account(
        self,
        *,
        username_or_url: str,
        display_name: str | None = None,
        interval_seconds: int | None = None,
        enabled: bool = True,
    ) -> XCaptureAccount:
        username = normalize_username(username_or_url)
        for account in self.accounts.values():
            if account.username_lower == username.lower():
                updated = XCaptureAccount(
                    **{
                        **asdict(account),
                        "display_name": display_name or account.display_name,
                        "interval_seconds": interval_seconds,
                        "enabled": enabled,
                        "updated_at": utc_now(),
                    }
                )
                self.accounts[account.id] = updated
                return updated
        account = XCaptureAccount(
            id=self._account_id,
            username=username,
            username_lower=username.lower(),
            display_name=display_name,
            profile_url=f"https://x.com/{username}",
            enabled=enabled,
            interval_seconds=interval_seconds,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self._account_id += 1
        self.accounts[account.id] = account
        return account

    def update_account(
        self,
        account_id: int,
        *,
        display_name: str | None | _Unset = UNSET,
        interval_seconds: int | None | _Unset = UNSET,
        enabled: bool | None = None,
    ) -> XCaptureAccount:
        account = self.accounts.get(account_id)
        if not account:
            raise ValueError(f"X capture account not found: {account_id}")
        payload = asdict(account)
        if not isinstance(display_name, _Unset):
            payload["display_name"] = display_name
        if not isinstance(interval_seconds, _Unset):
            payload["interval_seconds"] = interval_seconds
        if enabled is not None:
            payload["enabled"] = enabled
        payload["updated_at"] = utc_now()
        updated = XCaptureAccount(**payload)
        self.accounts[account_id] = updated
        return updated

    def delete_account(self, account_id: int) -> None:
        if account_id not in self.accounts:
            raise ValueError(f"X capture account not found: {account_id}")
        del self.accounts[account_id]

    def mark_account_seeded(self, account: XCaptureAccount, tweet_ids: list[str]) -> None:
        for tweet_id in tweet_ids:
            self.seen.setdefault(tweet_id, {"account_id": account.id, "seeded": True})
        payload = asdict(account)
        payload["seeded_at"] = utc_now()
        payload["updated_at"] = utc_now()
        self.accounts[account.id] = XCaptureAccount(**payload)

    def mark_seen(self, account: XCaptureAccount, tweet_id: str, *, seeded: bool) -> bool:
        if tweet_id in self.seen:
            return False
        self.seen[tweet_id] = {"account_id": account.id, "seeded": seeded}
        return True

    def unseen_tweet_ids(self, tweet_ids: list[str]) -> set[str]:
        return {tweet_id for tweet_id in tweet_ids if tweet_id not in self.seen}

    def save_task(self, account: XCaptureAccount, record: CaptureRecord) -> bool:
        if any(item["source_item_id"] == record.tweet_id for item in self.tasks):
            return False
        self.tasks.append(
            {
                "id": len(self.tasks) + 1,
                "source": "x",
                "source_item_id": record.tweet_id,
                "source_url": record.url,
                "title": f"@{record.author_username}: {record.text[:80]}",
                "content": record.text,
                "published_at": record.created_at,
                "metadata": {**record.metadata, "account_id": account.id},
                "status": "pending",
                "created_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        return True

    def record_attempt(self, stats: CaptureRunStats, *, started_at: datetime, finished_at: datetime) -> None:
        metadata = {
            **stats.metadata,
            "candidate_count": stats.candidate_count,
            "seeded_count": stats.seeded_count,
        }
        fingerprint = x_capture_attempt_fingerprint(
            status=stats.status,
            candidate_count=stats.candidate_count,
            seeded_count=stats.seeded_count,
            new_count=stats.new_count,
            saved_count=stats.saved_count,
            error=stats.error,
            metadata=metadata,
        )
        previous = next(
            (
                attempt
                for attempt in self.attempts
                if attempt["account_id"] == stats.account.id
                and attempt["status"] == "success"
                and attempt["new_count"] == 0
                and attempt["saved_count"] == 0
                and attempt["finished_at"] >= finished_at - NOOP_SUCCESS_WINDOW
            ),
            None,
        )
        previous_fingerprint = None
        if previous is not None:
            previous_metadata = dict(previous["metadata"])
            previous_fingerprint = x_capture_attempt_fingerprint(
                status="success",
                candidate_count=int(previous["candidate_count"]),
                seeded_count=int(previous["seeded_count"]),
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
            previous_finished_at=previous["finished_at"] if previous else None,
            previous_fingerprint=previous_fingerprint,
        ):
            self.attempts.insert(
                0,
                {
                    "id": len(self.attempts) + 1,
                    "account_id": stats.account.id,
                    "username_lower": stats.account.username_lower,
                    "status": stats.status,
                    "source": "fxtwitter",
                    "candidate_count": stats.candidate_count,
                    "seeded_count": stats.seeded_count,
                    "new_count": stats.new_count,
                    "saved_count": stats.saved_count,
                    "error": stats.error,
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "metadata": metadata,
                },
            )
        account = self.accounts.get(stats.account.id)
        if account:
            payload = asdict(account)
            payload["last_polled_at"] = finished_at
            payload["last_success_at"] = finished_at if stats.status == "success" else account.last_success_at
            payload["last_error"] = None if stats.status == "success" else stats.error
            payload["updated_at"] = utc_now()
            self.accounts[account.id] = XCaptureAccount(**payload)

    def prune_attempts_before(self, cutoff: datetime) -> int:
        before = len(self.attempts)
        self.attempts = [attempt for attempt in self.attempts if attempt["started_at"] >= cutoff]
        return before - len(self.attempts)

    def list_recent_attempts(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return self.attempts[:limit]

    def list_recent_tasks(self, *, limit: int = 25) -> list[dict[str, Any]]:
        return self.tasks[-limit:][::-1]

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
        return None


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + CONSOLE_AUTH_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS x_capture_settings (
    singleton_key text PRIMARY KEY DEFAULT 'global',
    global_interval_seconds integer NOT NULL DEFAULT 30 CHECK (global_interval_seconds BETWEEN 5 AND 3600),
    max_concurrency integer NOT NULL DEFAULT 2 CHECK (max_concurrency BETWEEN 1 AND 20),
    jitter_seconds integer NOT NULL DEFAULT 5 CHECK (jitter_seconds BETWEEN 0 AND 300),
    config_version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO x_capture_settings (singleton_key)
VALUES ('global')
ON CONFLICT (singleton_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS x_capture_accounts (
    id bigserial PRIMARY KEY,
    username text NOT NULL,
    username_lower text NOT NULL UNIQUE,
    display_name text,
    profile_url text,
    enabled boolean NOT NULL DEFAULT true,
    interval_seconds integer CHECK (interval_seconds IS NULL OR interval_seconds BETWEEN 5 AND 3600),
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS x_seen_tweets (
    tweet_id text PRIMARY KEY,
    account_id bigint REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
    username_lower text NOT NULL,
    seeded boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tasks (
    id bigserial PRIMARY KEY,
    source text NOT NULL,
    source_item_id text NOT NULL,
    source_url text,
    title text,
    content text NOT NULL,
    published_at timestamptz,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_item_id)
);

CREATE TABLE IF NOT EXISTS x_capture_attempts (
    id bigserial PRIMARY KEY,
    account_id bigint REFERENCES x_capture_accounts(id) ON DELETE SET NULL,
    username_lower text NOT NULL,
    status text NOT NULL,
    source text NOT NULL DEFAULT 'fxtwitter',
    candidate_count integer NOT NULL DEFAULT 0,
    seeded_count integer NOT NULL DEFAULT 0,
    new_count integer NOT NULL DEFAULT 0,
    saved_count integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz NOT NULL,
    finished_at timestamptz NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS published_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_x_capture_accounts_enabled ON x_capture_accounts(enabled);
CREATE INDEX IF NOT EXISTS idx_x_seen_tweets_username ON x_seen_tweets(username_lower);
CREATE INDEX IF NOT EXISTS idx_tasks_source_status_created ON tasks(source, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_x_capture_attempts_started ON x_capture_attempts(started_at DESC);

CREATE OR REPLACE FUNCTION notify_x_capture_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'x_capture_config_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP)::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_x_capture_settings_notify ON x_capture_settings;
CREATE TRIGGER trg_x_capture_settings_notify
AFTER INSERT OR UPDATE OR DELETE ON x_capture_settings
FOR EACH ROW EXECUTE FUNCTION notify_x_capture_config_changed();

DROP TRIGGER IF EXISTS trg_x_capture_accounts_notify ON x_capture_accounts;
CREATE TRIGGER trg_x_capture_accounts_notify
AFTER INSERT OR UPDATE OR DELETE ON x_capture_accounts
FOR EACH ROW EXECUTE FUNCTION notify_x_capture_config_changed();

ALTER TABLE x_capture_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_capture_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE x_capture_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS x_capture_settings_anon_all ON x_capture_settings;
DROP POLICY IF EXISTS x_capture_accounts_anon_all ON x_capture_accounts;
DROP POLICY IF EXISTS x_capture_attempts_anon_select ON x_capture_attempts;
DROP POLICY IF EXISTS tasks_x_anon_select ON tasks;
DROP POLICY IF EXISTS x_capture_settings_console_admin_all ON x_capture_settings;
DROP POLICY IF EXISTS x_capture_accounts_console_admin_all ON x_capture_accounts;
DROP POLICY IF EXISTS x_capture_attempts_console_admin_select ON x_capture_attempts;
DROP POLICY IF EXISTS tasks_x_console_admin_select ON tasks;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON x_capture_settings, x_capture_accounts, x_capture_attempts, tasks FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE x_capture_accounts_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON x_capture_settings, x_capture_accounts TO authenticated;
        GRANT SELECT ON x_capture_attempts, tasks TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE x_capture_accounts_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY x_capture_settings_console_admin_all ON x_capture_settings
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY x_capture_accounts_console_admin_all ON x_capture_accounts
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY x_capture_attempts_console_admin_select ON x_capture_attempts
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY tasks_x_console_admin_select ON tasks
            FOR SELECT TO authenticated USING (is_console_admin() AND source = ''x'')';
    END IF;
END
$$;
"""
