from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .storage import connect_sqlite, load_storage_settings


DEFAULT_LOCAL_OPERATOR_EMAIL = "odaily2026@gmail.com"
DEFAULT_LOCAL_OPERATOR_PASSWORD_HASH = "$2b$12$wWzM3Nnv9u34X9H7n7DBs.piRnn/i4qkJv0VXlhTX6ri8KSCxfoYi"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        import bcrypt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("bcrypt is required for local authentication") from exc
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password is required")
    try:
        import bcrypt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("bcrypt is required for local authentication") from exc
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


@dataclass(frozen=True, slots=True)
class LocalOperator:
    id: int
    email: str
    display_name: str


@dataclass(frozen=True, slots=True)
class LocalAuthSession:
    operator: LocalOperator
    expires_at: datetime


class LocalAuthRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path
        self.init_schema()

    def init_schema(self) -> None:
        email = (os.getenv("ODAILY_LOCAL_AUTH_EMAIL") or DEFAULT_LOCAL_OPERATOR_EMAIL).strip().lower()
        password_hash = os.getenv("ODAILY_LOCAL_AUTH_PASSWORD_HASH") or DEFAULT_LOCAL_OPERATOR_PASSWORD_HASH
        with connect_sqlite(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_auth_users (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    email text NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash text NOT NULL,
                    display_name text NOT NULL,
                    enabled integer NOT NULL DEFAULT 1,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS local_auth_sessions (
                    token_hash text PRIMARY KEY,
                    user_id integer NOT NULL REFERENCES local_auth_users(id) ON DELETE CASCADE,
                    expires_at text NOT NULL,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_local_auth_sessions_expires
                ON local_auth_sessions(expires_at);
                """
            )
            conn.execute(
                "INSERT INTO local_auth_users(email, password_hash, display_name, enabled) VALUES (?, ?, ?, 1) ON CONFLICT(email) DO NOTHING",
                (email, password_hash, email.split("@", 1)[0]),
            )
            conn.commit()

    def login(self, email: str, password: str, *, ttl_hours: float = 720.0) -> tuple[str, LocalAuthSession]:
        normalized = email.strip().lower()
        with connect_sqlite(self.path) as conn:
            row = conn.execute(
                "SELECT id, email, password_hash, display_name FROM local_auth_users WHERE email=? AND enabled=1",
                (normalized,),
            ).fetchone()
            if row is None or not verify_password(password, str(row["password_hash"])):
                raise ValueError("Invalid email or password")
            token = secrets.token_urlsafe(32)
            expires_at = _now() + timedelta(hours=ttl_hours)
            conn.execute("DELETE FROM local_auth_sessions WHERE expires_at<=?", (_now().isoformat(),))
            conn.execute(
                "INSERT INTO local_auth_sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (hash_session_token(token), row["id"], expires_at.isoformat()),
            )
            conn.commit()
        operator = LocalOperator(id=int(row["id"]), email=str(row["email"]), display_name=str(row["display_name"]))
        return token, LocalAuthSession(operator=operator, expires_at=expires_at)

    def authenticate(self, token: str) -> LocalAuthSession | None:
        now = _now().isoformat()
        with connect_sqlite(self.path) as conn:
            row = conn.execute(
                "SELECT u.id, u.email, u.display_name, s.expires_at FROM local_auth_sessions s JOIN local_auth_users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
                (hash_session_token(token), now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE local_auth_sessions SET last_seen_at=? WHERE token_hash=?",
                (now, hash_session_token(token)),
            )
            conn.commit()
        return LocalAuthSession(
            operator=LocalOperator(id=int(row["id"]), email=str(row["email"]), display_name=str(row["display_name"])),
            expires_at=_parse_dt(str(row["expires_at"])),
        )

    def logout(self, token: str) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute("DELETE FROM local_auth_sessions WHERE token_hash=?", (hash_session_token(token),))
            conn.commit()

    def set_password(self, email: str, password: str) -> None:
        with connect_sqlite(self.path) as conn:
            cur = conn.execute(
                "UPDATE local_auth_users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE email=?",
                (hash_password(password), email.strip().lower()),
            )
            if cur.rowcount != 1:
                raise ValueError("local operator not found")
            conn.execute(
                "DELETE FROM local_auth_sessions WHERE user_id=(SELECT id FROM local_auth_users WHERE email=?)",
                (email.strip().lower(),),
            )
            conn.commit()
