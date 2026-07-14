from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

from .pipeline_schema import EDITOR_PLUGIN_SCHEMA_SQL


def get_database_url(database_url: str | None = None) -> str:
    if database_url:
        return database_url
    load_dotenv()
    value = os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("Missing SUPABASE_DB_URL or DATABASE_URL")
    return value


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - import error is environment-specific
        raise RuntimeError("psycopg is required for Supabase/Postgres access") from exc
    return psycopg, dict_row


def get_postgres_connect_timeout_seconds() -> int:
    value = str(os.getenv("POSTGRES_CONNECT_TIMEOUT_SECONDS") or "10").strip()
    try:
        return max(1, int(value))
    except ValueError:
        return 10


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


def verify_supabase_bcrypt_password(password: str, encrypted_password: str) -> bool:
    try:
        import bcrypt
    except ImportError as exc:  # pragma: no cover - import error is environment-specific
        raise RuntimeError("bcrypt is required for editor plugin password login") from exc
    try:
        return bcrypt.checkpw(password.encode("utf-8"), encrypted_password.encode("utf-8"))
    except ValueError:
        return False


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


class PostgresEditorPluginAuthRepository:
    PLUGIN_FUNCTIONS = {
        "editor_plugin_profile",
        "editor_plugin_feed",
        "editor_plugin_state",
        "editor_plugin_mark_seen",
        "editor_plugin_submit_feedback",
    }

    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = get_database_url(database_url)
        self._psycopg, self._dict_row = _import_psycopg()
        self.connect_timeout_seconds = get_postgres_connect_timeout_seconds()

    def _connect(self):
        return self._psycopg.connect(
            self.database_url,
            row_factory=self._dict_row,
            connect_timeout=self.connect_timeout_seconds,
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(EDITOR_PLUGIN_SCHEMA_SQL)

    def upsert_user(self, email: str, display_name: str | None = None, *, enabled: bool = True) -> EditorPluginUserRecord:
        normalized_email = normalize_editor_plugin_email(email)
        normalized_display_name = normalize_editor_plugin_display_name(display_name)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO editor_plugin_users (email, display_name, enabled, updated_at)
                    VALUES (%s, %s, %s, now())
                    ON CONFLICT (email) DO UPDATE
                    SET display_name = COALESCE(EXCLUDED.display_name, editor_plugin_users.display_name),
                        enabled = EXCLUDED.enabled,
                        updated_at = EXCLUDED.updated_at
                    RETURNING email, display_name, enabled, created_at, updated_at
                    """,
                    (normalized_email, normalized_display_name, enabled),
                )
                row = cur.fetchone()
        if not row:
            raise RuntimeError("Failed to upsert editor plugin user")
        return EditorPluginUserRecord(
            email=str(row["email"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def delete_user(self, email: str) -> bool:
        normalized_email = normalize_editor_plugin_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM editor_plugin_users WHERE email = %s", (normalized_email,))
                return cur.rowcount > 0

    def list_users(self) -> list[EditorPluginUserRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT email, display_name, enabled, created_at, updated_at
                    FROM editor_plugin_users
                    ORDER BY enabled DESC, email ASC
                    """
                )
                rows = cur.fetchall()
        return [
            EditorPluginUserRecord(
                email=str(row["email"]),
                display_name=str(row["display_name"]) if row["display_name"] is not None else None,
                enabled=bool(row["enabled"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def get_enabled_user(self, email: str) -> EditorPluginUserRecord | None:
        normalized_email = normalize_editor_plugin_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT email, display_name, enabled, created_at, updated_at
                    FROM editor_plugin_users
                    WHERE lower(email) = %s
                      AND enabled = true
                    LIMIT 1
                    """,
                    (normalized_email,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return EditorPluginUserRecord(
            email=str(row["email"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            enabled=bool(row["enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def verify_supabase_password(self, email: str, password: str) -> tuple[str | None, str]:
        normalized_email = normalize_editor_plugin_email(email)
        if not password:
            raise ValueError("Password is required")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id::text AS id, email, encrypted_password
                    FROM auth.users
                    WHERE lower(email) = %s
                    LIMIT 1
                    """,
                    (normalized_email,),
                )
                row = cur.fetchone()
        if not row or not row["encrypted_password"]:
            raise ValueError("Invalid email or password")
        if not verify_supabase_bcrypt_password(password, str(row["encrypted_password"])):
            raise ValueError("Invalid email or password")
        return str(row["id"]) if row["id"] is not None else None, normalize_editor_plugin_email(str(row["email"]))

    def create_session(
        self,
        *,
        token_hash: str,
        user_id: str | None,
        email: str,
        display_name: str | None,
        expires_at: datetime,
    ) -> None:
        normalized_email = normalize_editor_plugin_email(email)
        normalized_display_name = normalize_editor_plugin_display_name(display_name)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM editor_plugin_sessions WHERE expires_at <= now()")
                cur.execute(
                    """
                    INSERT INTO editor_plugin_sessions (
                        token_hash,
                        user_id,
                        email,
                        display_name,
                        expires_at,
                        created_at,
                        last_seen_at
                    )
                    VALUES (%s, %s, %s, %s, %s, now(), now())
                    """,
                    (token_hash, user_id, normalized_email, normalized_display_name, expires_at),
                )

    def get_session(self, token_hash: str) -> EditorPluginSessionRecord | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE editor_plugin_sessions
                    SET last_seen_at = now()
                    WHERE token_hash = %s
                      AND expires_at > now()
                    RETURNING token_hash, user_id::text AS user_id, email, display_name, expires_at, created_at, last_seen_at
                    """,
                    (token_hash,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return EditorPluginSessionRecord(
            token_hash=str(row["token_hash"]),
            user_id=str(row["user_id"]) if row["user_id"] is not None else None,
            email=str(row["email"]),
            display_name=str(row["display_name"]) if row["display_name"] is not None else None,
            expires_at=row["expires_at"],
            created_at=row["created_at"],
            last_seen_at=row["last_seen_at"],
        )

    def delete_session(self, token_hash: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM editor_plugin_sessions WHERE token_hash = %s", (token_hash,))

    def call_plugin_function(self, *, email: str, function_name: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if function_name not in self.PLUGIN_FUNCTIONS:
            raise ValueError("Unsupported editor plugin function")
        normalized_email = normalize_editor_plugin_email(email)
        placeholders = ", ".join(["%s"] * len(args))
        sql = f"SELECT * FROM {function_name}({placeholders})" if placeholders else f"SELECT * FROM {function_name}()"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"email": normalized_email}),),
                )
                cur.execute(sql, args)
                rows = cur.fetchall()
                conn.commit()
        return [dict(row) for row in rows]

    def call_plugin_json_function(self, *, email: str, function_name: str, args: tuple[Any, ...]) -> Any:
        if function_name not in {"editor_plugin_mark_seen", "editor_plugin_submit_feedback"}:
            raise ValueError("Unsupported editor plugin json function")
        normalized_email = normalize_editor_plugin_email(email)
        expected_args_by_function = {
            "editor_plugin_mark_seen": 4,
            "editor_plugin_submit_feedback": 5,
        }
        expected_args = expected_args_by_function[function_name]
        if len(args) != expected_args:
            raise ValueError(f"Editor plugin json function requires {expected_args} arguments")
        placeholders = ", ".join(["%s"] * (expected_args - 1))
        sql = f"SELECT * FROM {function_name}({placeholders}, %s::jsonb)"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('request.jwt.claims', %s, true)",
                    (json.dumps({"email": normalized_email}),),
                )
                cur.execute(sql, args)
                rows = cur.fetchall()
                conn.commit()
        if not rows:
            return None
        return next(iter(rows[0].values()))

    def insert_generation_log(self, payload: EditorPluginGenerationLogInput) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO editor_plugin_generation_logs (
                        action,
                        actor_user_id,
                        actor_email,
                        actor_display_name,
                        source_type,
                        platform,
                        post_id,
                        post_url,
                        author_display_name,
                        author_handle,
                        posted_at,
                        request_text,
                        route,
                        result_json,
                        status,
                        error_message
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        payload.action,
                        payload.actor_user_id,
                        normalize_editor_plugin_email(payload.actor_email),
                        normalize_editor_plugin_display_name(payload.actor_display_name),
                        payload.source_type.strip(),
                        payload.platform.strip(),
                        payload.post_id.strip() if payload.post_id else None,
                        payload.post_url.strip() if payload.post_url else None,
                        normalize_editor_plugin_display_name(payload.author_display_name),
                        payload.author_handle.strip() if payload.author_handle else None,
                        payload.posted_at,
                        payload.request_text.strip(),
                        payload.route.strip() if payload.route else None,
                        json.dumps(payload.result_json, ensure_ascii=False),
                        payload.status.strip(),
                        payload.error_message.strip() if payload.error_message else None,
                    ),
                )
