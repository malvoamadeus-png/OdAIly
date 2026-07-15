from __future__ import annotations

from dataclasses import dataclass

from packages.common.postgres import build_psycopg_connect_kwargs, load_database_url
from .pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL


def get_database_url(database_url: str | None = None) -> str:
    return load_database_url(database_url)


def _import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - import error is environment-specific
        raise RuntimeError("psycopg is required for Supabase/Postgres access") from exc
    return psycopg, dict_row


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


class PostgresConsoleAuthRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = get_database_url(database_url)
        self._psycopg, self._dict_row = _import_psycopg()
        self.application_name = "odaily-console-auth"

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=self._dict_row,
                autocommit=autocommit,
                application_name=self.application_name,
            ),
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(CONSOLE_AUTH_SCHEMA_SQL)

    def upsert_admin(self, email: str) -> ConsoleAdminRecord:
        normalized = normalize_console_admin_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO console_admins (email, updated_at)
                    VALUES (%s, now())
                    ON CONFLICT (email) DO UPDATE
                    SET updated_at = EXCLUDED.updated_at
                    RETURNING email, created_at, updated_at
                    """,
                    (normalized,),
                )
                row = cur.fetchone()
        if not row:
            raise RuntimeError("Failed to upsert console admin")
        return ConsoleAdminRecord(
            email=str(row["email"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def delete_admin(self, email: str) -> bool:
        normalized = normalize_console_admin_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM console_admins WHERE email = %s", (normalized,))
                return cur.rowcount > 0

    def list_admins(self) -> list[ConsoleAdminRecord]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT email, created_at, updated_at
                    FROM console_admins
                    ORDER BY email ASC
                    """
                )
                rows = cur.fetchall()
        return [
            ConsoleAdminRecord(
                email=str(row["email"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
            for row in rows
        ]

    def get_admin(self, email: str) -> ConsoleAdminRecord | None:
        normalized = normalize_console_admin_email(email)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT email, created_at, updated_at
                    FROM console_admins
                    WHERE email = %s
                    """,
                    (normalized,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return ConsoleAdminRecord(
            email=str(row["email"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
