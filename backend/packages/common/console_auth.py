from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packages.common.local_auth import create_local_auth_repository
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
        create_local_auth_repository(self.path).init_schema()
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


class PostgresConsoleAuthRepository:
    def __init__(self) -> None:
        self.local_auth = create_local_auth_repository()
        self.local_auth.init_schema()

    def init_schema(self) -> None:
        self.local_auth.init_schema()

    def upsert_admin(self, email: str) -> ConsoleAdminRecord:
        record = self.get_admin(normalize_console_admin_email(email))
        if record is None:
            raise ValueError("only the configured local operator can be a console admin")
        return record

    def delete_admin(self, email: str) -> bool:
        normalize_console_admin_email(email)
        return False

    def list_admins(self) -> list[ConsoleAdminRecord]:
        with self.local_auth._connect() as conn:
            rows = conn.execute(
                "SELECT email,created_at,updated_at FROM local_auth_users WHERE enabled=true ORDER BY email"
            ).fetchall()
        return [self._record(row) for row in rows]

    def get_admin(self, email: str) -> ConsoleAdminRecord | None:
        with self.local_auth._connect() as conn:
            row = conn.execute(
                "SELECT email,created_at,updated_at FROM local_auth_users WHERE lower(email)=lower(%s) AND enabled=true",
                (normalize_console_admin_email(email),),
            ).fetchone()
        return self._record(row) if row else None

    @staticmethod
    def _record(row) -> ConsoleAdminRecord:
        return ConsoleAdminRecord(
            email=str(row["email"]), created_at=row["created_at"].isoformat(),
            updated_at=row["updated_at"].isoformat(),
        )


def create_console_auth_repository(database_url: str | None = None) -> SQLiteConsoleAuthRepository:
    del database_url
    settings = load_storage_settings()
    if settings.backend == "postgres":
        return PostgresConsoleAuthRepository()
    return SQLiteConsoleAuthRepository(settings.sqlite_path)
