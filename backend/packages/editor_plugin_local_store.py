from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def encode_dt(value: Any = None) -> str:
    if value is None:
        value = utc_now()
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat()
    text = str(value).strip()
    return text or utc_now().isoformat()


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def encode_json(value: Any, fallback: Any) -> str:
    if value is None:
        value = fallback
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def normalize_feed_lane(feed_kind: str, lane: str) -> str:
    if feed_kind in {"whale_onchain", "whale_hyperliquid"}:
        return "high"
    return lane or "high"


@dataclass(frozen=True, slots=True)
class LocalPluginSession:
    token_hash: str
    user_id: str | None
    email: str
    display_name: str | None
    expires_at: datetime
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class PendingPluginFeedback:
    id: int
    feed_item_id: str
    feed_kind: str
    feedback: str
    actor_email: str
    session_id: str | None
    extra_json: dict[str, Any]


class LocalEditorPluginStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _connect(self):
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

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editor_plugin_local_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT,
                    email TEXT NOT NULL,
                    display_name TEXT,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_sessions_expires
                ON editor_plugin_local_sessions(expires_at)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editor_plugin_local_feed_items (
                    feed_item_id TEXT NOT NULL,
                    feed_kind TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 0,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    badges_json TEXT NOT NULL DEFAULT '[]',
                    status_label TEXT,
                    status_tone TEXT,
                    occurred_at TEXT NOT NULL,
                    source_url TEXT,
                    detail_url TEXT,
                    action_schema_json TEXT NOT NULL DEFAULT '{}',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (feed_item_id, feed_kind)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feed_lane_time
                ON editor_plugin_local_feed_items(lane, occurred_at DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feed_time
                ON editor_plugin_local_feed_items(occurred_at DESC)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editor_plugin_local_feedbacks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feed_item_id TEXT NOT NULL,
                    feed_kind TEXT NOT NULL,
                    feedback TEXT NOT NULL CHECK (feedback IN ('accept', 'reject')),
                    actor_user_id TEXT,
                    actor_email TEXT NOT NULL,
                    actor_display_name TEXT,
                    acted_at TEXT NOT NULL,
                    session_id TEXT,
                    extra_json TEXT NOT NULL DEFAULT '{}',
                    sync_status TEXT NOT NULL DEFAULT 'pending',
                    sync_attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feedbacks_actor_feed
                ON editor_plugin_local_feedbacks(actor_email, feed_item_id, feed_kind, acted_at DESC, id DESC)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_editor_plugin_local_feedbacks_sync
                ON editor_plugin_local_feedbacks(sync_status, updated_at)
                """
            )

    def upsert_session(
        self,
        *,
        token_hash: str,
        user_id: str | None,
        email: str,
        display_name: str | None,
        expires_at: datetime,
    ) -> None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute("DELETE FROM editor_plugin_local_sessions WHERE expires_at <= ?", (now,))
            conn.execute(
                """
                INSERT INTO editor_plugin_local_sessions (
                    token_hash, user_id, email, display_name, expires_at, created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(token_hash) DO UPDATE SET
                    user_id = excluded.user_id,
                    email = excluded.email,
                    display_name = excluded.display_name,
                    expires_at = excluded.expires_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (token_hash, user_id, email.strip().lower(), display_name, encode_dt(expires_at), now, now),
            )

    def get_session(self, token_hash: str) -> LocalPluginSession | None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE editor_plugin_local_sessions
                SET last_seen_at = ?
                WHERE token_hash = ?
                  AND expires_at > ?
                """,
                (now, token_hash, now),
            )
            row = conn.execute(
                """
                SELECT token_hash, user_id, email, display_name, expires_at, created_at, last_seen_at
                FROM editor_plugin_local_sessions
                WHERE token_hash = ?
                  AND expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
        if row is None:
            return None
        return LocalPluginSession(
            token_hash=str(row["token_hash"]),
            user_id=str(row["user_id"]) if row["user_id"] is not None else None,
            email=str(row["email"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            expires_at=datetime.fromisoformat(str(row["expires_at"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            last_seen_at=datetime.fromisoformat(str(row["last_seen_at"])),
        )

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM editor_plugin_local_sessions WHERE token_hash = ?", (token_hash,))

    def upsert_feed_items(self, rows: list[dict[str, Any]]) -> int:
        now = encode_dt()
        count = 0
        with self._connect() as conn:
            for row in rows:
                feed_item_id = str(row.get("feed_item_id") or "").strip()
                feed_kind = str(row.get("feed_kind") or "").strip()
                if not feed_item_id or not feed_kind:
                    continue
                conn.execute(
                    """
                    INSERT INTO editor_plugin_local_feed_items (
                        feed_item_id, feed_kind, lane, priority, title, summary, badges_json,
                        status_label, status_tone, occurred_at, source_url, detail_url,
                        action_schema_json, meta_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feed_item_id, feed_kind) DO UPDATE SET
                        lane = excluded.lane,
                        priority = excluded.priority,
                        title = excluded.title,
                        summary = excluded.summary,
                        badges_json = excluded.badges_json,
                        status_label = excluded.status_label,
                        status_tone = excluded.status_tone,
                        occurred_at = excluded.occurred_at,
                        source_url = excluded.source_url,
                        detail_url = excluded.detail_url,
                        action_schema_json = excluded.action_schema_json,
                        meta_json = excluded.meta_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        feed_item_id,
                        feed_kind,
                        normalize_feed_lane(feed_kind, str(row.get("lane") or "high")),
                        int(row.get("priority") or 0),
                        str(row.get("title") or ""),
                        str(row.get("summary") or ""),
                        encode_json(row.get("badges"), []),
                        row.get("status_label"),
                        row.get("status_tone"),
                        encode_dt(row.get("occurred_at")),
                        row.get("source_url"),
                        row.get("detail_url"),
                        encode_json(row.get("action_schema"), {"type": "read"}),
                        encode_json(row.get("meta_json"), {}),
                        now,
                        now,
                    ),
                )
                count += 1
        return count

    def list_feed_items(self, *, limit: int, max_age_hours: int) -> list[dict[str, Any]]:
        cutoff = encode_dt(utc_now() - timedelta(hours=max_age_hours))
        safe_limit = max(1, min(int(limit or 120), 500))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM editor_plugin_local_feed_items
                WHERE occurred_at >= ?
                ORDER BY
                    CASE
                        WHEN feed_kind IN ('whale_onchain', 'whale_hyperliquid') THEN 1
                        WHEN lane = 'high' THEN 1
                        WHEN lane = 'ai' THEN 2
                        WHEN lane = 'low' THEN 3
                        ELSE 4
                    END,
                    occurred_at DESC,
                    updated_at DESC
                LIMIT ?
                """,
                (cutoff, safe_limit),
            ).fetchall()
        return [self._feed_row_to_payload(row) for row in rows]

    def feed_state(self, *, actor_email: str, feed_item_ids: list[str]) -> list[dict[str, Any]]:
        ids = [str(item).strip() for item in feed_item_ids if str(item).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT feed_item_id, feed_kind, feedback, acted_at
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY feed_item_id, feed_kind
                               ORDER BY acted_at DESC, id DESC
                           ) AS rn
                    FROM editor_plugin_local_feedbacks
                    WHERE lower(actor_email) = ?
                      AND feed_item_id IN ({placeholders})
                )
                WHERE rn = 1
                """,
                (actor_email.strip().lower(), *ids),
            ).fetchall()
        return [
            {
                "feed_item_id": str(row["feed_item_id"]),
                "feed_kind": str(row["feed_kind"]),
                "first_seen_at": None,
                "last_seen_at": None,
                "seen_count": None,
                "latest_feedback": str(row["feedback"]),
                "latest_feedback_at": row["acted_at"],
            }
            for row in rows
        ]

    def mark_seen(self, *, feed_item_id: str, feed_kind: str) -> dict[str, Any]:
        if not feed_item_id.strip():
            raise ValueError("editor_plugin_feed_item_id_required")
        if not feed_kind.strip():
            raise ValueError("editor_plugin_feed_kind_required")
        return {"ok": True, "recorded": False, "local": True}

    def record_feedback(
        self,
        *,
        feed_item_id: str,
        feed_kind: str,
        feedback: str,
        actor_user_id: str | None,
        actor_email: str,
        actor_display_name: str | None,
        session_id: str | None,
        extra_json: dict[str, Any],
    ) -> dict[str, Any]:
        clean_feed_item_id = feed_item_id.strip()
        clean_feed_kind = feed_kind.strip()
        clean_feedback = feedback.strip()
        if not clean_feed_item_id:
            raise ValueError("editor_plugin_feed_item_id_required")
        if clean_feedback not in {"accept", "reject"}:
            raise ValueError("editor_plugin_invalid_feedback")
        now = encode_dt()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO editor_plugin_local_feedbacks (
                    feed_item_id, feed_kind, feedback, actor_user_id, actor_email, actor_display_name,
                    acted_at, session_id, extra_json, sync_status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    clean_feed_item_id,
                    clean_feed_kind,
                    clean_feedback,
                    actor_user_id,
                    actor_email.strip().lower(),
                    actor_display_name,
                    now,
                    session_id,
                    encode_json(extra_json, {}),
                    now,
                    now,
                ),
            )
            feedback_id = int(cursor.lastrowid or 0)
        return {"ok": True, "local": True, "queued": True, "feedback_id": feedback_id}

    def list_pending_feedbacks(self, *, limit: int = 50) -> list[PendingPluginFeedback]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, feed_item_id, feed_kind, feedback, actor_email, session_id, extra_json
                FROM editor_plugin_local_feedbacks
                WHERE sync_status IN ('pending', 'failed')
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
        return [
            PendingPluginFeedback(
                id=int(row["id"]),
                feed_item_id=str(row["feed_item_id"]),
                feed_kind=str(row["feed_kind"]),
                feedback=str(row["feedback"]),
                actor_email=str(row["actor_email"]),
                session_id=str(row["session_id"]) if row["session_id"] is not None else None,
                extra_json=decode_json(row["extra_json"], {}),
            )
            for row in rows
        ]

    def mark_feedback_synced(self, feedback_id: int) -> None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE editor_plugin_local_feedbacks
                SET sync_status = 'synced',
                    last_error = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, feedback_id),
            )

    def mark_feedback_failed(self, feedback_id: int, *, error: str) -> None:
        now = encode_dt()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE editor_plugin_local_feedbacks
                SET sync_status = 'failed',
                    sync_attempt_count = sync_attempt_count + 1,
                    last_error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (error[:2000], now, feedback_id),
            )

    def prune_old(self, *, feed_retention_hours: int = 24, synced_feedback_retention_days: int = 90) -> None:
        feed_cutoff = encode_dt(utc_now() - timedelta(hours=feed_retention_hours))
        feedback_cutoff = encode_dt(utc_now() - timedelta(days=synced_feedback_retention_days))
        session_cutoff = encode_dt()
        with self._connect() as conn:
            conn.execute("DELETE FROM editor_plugin_local_feed_items WHERE occurred_at < ?", (feed_cutoff,))
            conn.execute(
                """
                DELETE FROM editor_plugin_local_feedbacks
                WHERE sync_status = 'synced'
                  AND created_at < ?
                """,
                (feedback_cutoff,),
            )
            conn.execute("DELETE FROM editor_plugin_local_sessions WHERE expires_at <= ?", (session_cutoff,))

    def stats(self, *, max_age_hours: int = 2) -> dict[str, Any]:
        cutoff = encode_dt(utc_now() - timedelta(hours=max_age_hours))
        with self._connect() as conn:
            total_feed_items = int(
                conn.execute("SELECT count(*) FROM editor_plugin_local_feed_items").fetchone()[0]
            )
            recent_feed_items = int(
                conn.execute(
                    "SELECT count(*) FROM editor_plugin_local_feed_items WHERE occurred_at >= ?",
                    (cutoff,),
                ).fetchone()[0]
            )
            latest_feed_at = conn.execute(
                "SELECT max(occurred_at) FROM editor_plugin_local_feed_items"
            ).fetchone()[0]
            by_lane = {
                str(row["lane"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT lane, count(*) AS count
                    FROM editor_plugin_local_feed_items
                    WHERE occurred_at >= ?
                    GROUP BY lane
                    ORDER BY lane
                    """,
                    (cutoff,),
                ).fetchall()
            }
            by_kind = {
                str(row["feed_kind"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT feed_kind, count(*) AS count
                    FROM editor_plugin_local_feed_items
                    WHERE occurred_at >= ?
                    GROUP BY feed_kind
                    ORDER BY feed_kind
                    """,
                    (cutoff,),
                ).fetchall()
            }
            feedback_by_status = {
                str(row["sync_status"]): int(row["count"])
                for row in conn.execute(
                    """
                    SELECT sync_status, count(*) AS count
                    FROM editor_plugin_local_feedbacks
                    GROUP BY sync_status
                    ORDER BY sync_status
                    """
                ).fetchall()
            }
            active_sessions = int(
                conn.execute(
                    "SELECT count(*) FROM editor_plugin_local_sessions WHERE expires_at > ?",
                    (encode_dt(),),
                ).fetchone()[0]
            )
        return {
            "path": str(self.path),
            "max_age_hours": max_age_hours,
            "feed_items": {
                "total": total_feed_items,
                "recent": recent_feed_items,
                "latest_occurred_at": latest_feed_at,
                "by_lane": by_lane,
                "by_kind": by_kind,
            },
            "feedbacks": {
                "by_sync_status": feedback_by_status,
                "pending": feedback_by_status.get("pending", 0),
                "failed": feedback_by_status.get("failed", 0),
            },
            "sessions": {
                "active": active_sessions,
            },
        }

    @staticmethod
    def _feed_row_to_payload(row: sqlite3.Row) -> dict[str, Any]:
        feed_kind = str(row["feed_kind"])
        return {
            "feed_item_id": str(row["feed_item_id"]),
            "feed_kind": feed_kind,
            "lane": normalize_feed_lane(feed_kind, str(row["lane"])),
            "priority": int(row["priority"]),
            "title": str(row["title"]),
            "summary": str(row["summary"]),
            "badges": decode_json(row["badges_json"], []),
            "status_label": row["status_label"],
            "status_tone": row["status_tone"],
            "occurred_at": row["occurred_at"],
            "source_url": row["source_url"],
            "detail_url": row["detail_url"],
            "action_schema": decode_json(row["action_schema_json"], {"type": "read"}),
            "meta_json": decode_json(row["meta_json"], {}),
        }
