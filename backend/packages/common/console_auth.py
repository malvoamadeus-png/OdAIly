from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packages.common.local_auth import LocalAuthRepository
from packages.common.storage import connect_sqlite, load_storage_settings


def normalize_console_admin_email(email: str) -> str:
    normalized = email.strip().lower()
    if not normalized or "@" not in normalized:
        raise ValueError("Console admin email must be a valid email address")
    return normalized


@dataclass(frozen=True)
class ConsoleAdminRecord:
    email: str
    created_at: str
    updated_at: str


class SQLiteConsoleAuthRepository:
    """Console authorization view over the single local operator account."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path
        self.init_schema()

    def init_schema(self) -> None:
        LocalAuthRepository(self.path)
        with connect_sqlite(self.path) as conn:
            conn.execute(
                """
                CREATE VIEW IF NOT EXISTS console_admins AS
                SELECT email, created_at, updated_at
                FROM local_auth_users
                WHERE enabled = 1
                """
            )
            conn.commit()

    def upsert_admin(self, email: str) -> ConsoleAdminRecord:
        normalized = normalize_console_admin_email(email)
        record = self.get_admin(normalized)
        if record is None:
            raise ValueError("only the configured local operator can be a console admin")
        return record

    def delete_admin(self, email: str) -> bool:
        # The only operator is configured by deployment, not mutable through the UI.
        normalize_console_admin_email(email)
        return False

    def list_admins(self) -> list[ConsoleAdminRecord]:
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(
                "SELECT email, created_at, updated_at FROM local_auth_users WHERE enabled = 1 ORDER BY email"
            ).fetchall()
        return [
            ConsoleAdminRecord(email=str(row["email"]), created_at=str(row["created_at"]), updated_at=str(row["updated_at"]))
            for row in rows
        ]

    def get_admin(self, email: str) -> ConsoleAdminRecord | None:
        normalized = normalize_console_admin_email(email)
        with connect_sqlite(self.path) as conn:
            row = conn.execute(
                "SELECT email, created_at, updated_at FROM local_auth_users WHERE email = ? AND enabled = 1",
                (normalized,),
            ).fetchone()
        if row is None:
            return None
        return ConsoleAdminRecord(
            email=str(row["email"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )


def create_console_auth_repository(database_url: str | None = None) -> SQLiteConsoleAuthRepository:
    del database_url
    return SQLiteConsoleAuthRepository(load_storage_settings().sqlite_path)
