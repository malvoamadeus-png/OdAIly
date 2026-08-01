from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from packages.common.local_auth import DEFAULT_LOCAL_OPERATOR_EMAIL, LocalAuthRepository
from packages.common.storage import connect_sqlite, load_storage_settings


def normalize_editor_plugin_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("Editor plugin user email must be a valid email address")
    return normalized


def normalize_editor_plugin_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    normalized = display_name.strip()
    if not normalized:
        return None
    return normalized[:80]


@dataclass(frozen=True)
class EditorPluginUserRecord:
    email: str
    display_name: str | None
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EditorPluginSessionRecord:
    token_hash: str
    user_id: str | None
    email: str
    display_name: str | None
    expires_at: datetime
    created_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True)
class EditorPluginGenerationLogInput:
    action: str
    actor_user_id: str | None
    actor_email: str
    actor_display_name: str | None
    source_type: str
    platform: str
    post_id: str | None
    post_url: str | None
    author_display_name: str | None
    author_handle: str | None
    posted_at: str | None
    request_text: str
    route: str | None
    result_json: dict
    status: str
    error_message: str | None = None


EDITOR_PLUGIN_FUNCTIONS = {
    "editor_plugin_profile",
    "editor_plugin_feed",
    "editor_plugin_state",
    "editor_plugin_mark_seen",
    "editor_plugin_submit_feedback",
}


class SQLiteEditorPluginAuthRepository:
    """Local replacement for plugin users, sessions and generation audit logs."""

    PLUGIN_FUNCTIONS = EDITOR_PLUGIN_FUNCTIONS

    def __init__(self, path=None) -> None:
        self.path = path or load_storage_settings().sqlite_path
        self.local_auth = LocalAuthRepository(self.path)
        self.init_schema()

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS editor_plugin_users (
                    email text PRIMARY KEY COLLATE NOCASE,
                    display_name text,
                    enabled integer NOT NULL DEFAULT 1,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS editor_plugin_generation_logs (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    action text NOT NULL,
                    actor_user_id text,
                    actor_email text NOT NULL,
                    actor_display_name text,
                    source_type text NOT NULL,
                    platform text NOT NULL,
                    post_id text,
                    post_url text,
                    author_display_name text,
                    author_handle text,
                    posted_at text,
                    request_text text NOT NULL,
                    route text,
                    result_json text NOT NULL DEFAULT '{}',
                    status text NOT NULL,
                    error_message text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS editor_plugin_feedbacks (
                    id integer PRIMARY KEY AUTOINCREMENT, feed_item_id text NOT NULL, feed_kind text NOT NULL,
                    feedback text NOT NULL, actor_user_id text, actor_email text NOT NULL, actor_display_name text,
                    acted_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, session_id text, extra_json text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS editor_plugin_receipts (
                    id integer PRIMARY KEY AUTOINCREMENT, feed_item_id text NOT NULL, feed_kind text NOT NULL,
                    viewer_user_id text, viewer_email text NOT NULL, viewer_display_name text,
                    first_seen_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    seen_count integer NOT NULL DEFAULT 1, session_id text, extra_json text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(feed_item_id, feed_kind, viewer_email)
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO editor_plugin_users(email, display_name, enabled) VALUES (?, ?, 1)",
                (DEFAULT_LOCAL_OPERATOR_EMAIL, DEFAULT_LOCAL_OPERATOR_EMAIL.split("@", 1)[0]),
            )
            conn.commit()

    @staticmethod
    def _user(row) -> EditorPluginUserRecord:
        return EditorPluginUserRecord(
            email=str(row["email"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def upsert_user(self, email: str, display_name: str | None = None, *, enabled: bool = True) -> EditorPluginUserRecord:
        email = normalize_editor_plugin_email(email)
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "INSERT INTO editor_plugin_users(email, display_name, enabled) VALUES (?, ?, ?) ON CONFLICT(email) DO UPDATE SET display_name=COALESCE(excluded.display_name, editor_plugin_users.display_name), enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP",
                (email, normalize_editor_plugin_display_name(display_name), int(enabled)),
            )
            row = conn.execute("SELECT * FROM editor_plugin_users WHERE email=?", (email,)).fetchone()
            conn.commit()
        return self._user(row)

    def delete_user(self, email: str) -> bool:
        with connect_sqlite(self.path) as conn:
            cur = conn.execute("DELETE FROM editor_plugin_users WHERE email=?", (normalize_editor_plugin_email(email),))
            conn.commit()
            return cur.rowcount > 0

    def list_users(self) -> list[EditorPluginUserRecord]:
        with connect_sqlite(self.path) as conn:
            rows = conn.execute("SELECT * FROM editor_plugin_users ORDER BY enabled DESC, email").fetchall()
        return [self._user(row) for row in rows]

    def get_enabled_user(self, email: str) -> EditorPluginUserRecord | None:
        with connect_sqlite(self.path) as conn:
            row = conn.execute("SELECT * FROM editor_plugin_users WHERE email=? AND enabled=1", (normalize_editor_plugin_email(email),)).fetchone()
        return self._user(row) if row else None

    def verify_local_password(self, email: str, password: str) -> tuple[str | None, str]:
        token, session = self.local_auth.login(email, password, ttl_hours=1 / 60)
        self.local_auth.logout(token)
        return str(session.operator.id), session.operator.email

    def create_session(self, *, token_hash: str, user_id: str | None, email: str, display_name: str | None, expires_at: datetime) -> None:
        # The authoritative session was created by LocalAuthRepository during login.
        return None

    def get_session(self, token_hash: str) -> EditorPluginSessionRecord | None:
        return None

    def delete_session(self, token_hash: str) -> None:
        return None

    def call_plugin_function(self, *, email: str, function_name: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return []

    def call_plugin_json_function(self, *, email: str, function_name: str, args: tuple[Any, ...]) -> Any:
        return None

    def insert_generation_log(self, payload: EditorPluginGenerationLogInput) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "INSERT INTO editor_plugin_generation_logs(action, actor_user_id, actor_email, actor_display_name, source_type, platform, post_id, post_url, author_display_name, author_handle, posted_at, request_text, route, result_json, status, error_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (payload.action, payload.actor_user_id, normalize_editor_plugin_email(payload.actor_email), normalize_editor_plugin_display_name(payload.actor_display_name), payload.source_type, payload.platform, payload.post_id, payload.post_url, payload.author_display_name, payload.author_handle, payload.posted_at, payload.request_text, payload.route, json.dumps(payload.result_json, ensure_ascii=False), payload.status, payload.error_message),
            )
            conn.commit()


def create_editor_plugin_auth_repository(database_url: str | None = None):
    del database_url
    return SQLiteEditorPluginAuthRepository(load_storage_settings().sqlite_path)
