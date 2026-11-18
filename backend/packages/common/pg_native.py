from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from psycopg import Connection

from packages.common.pg_runtime import (
    PostgresMigration,
    apply_schema_migrations,
    connect_postgres as _connect_postgres,
    postgres_transaction,
    postgres_pool_stats,
)
from packages.common.storage import PostgresSettings


@contextmanager
def connect_postgres(
    *,
    schema: str,
    application_name: str | None = None,
    settings: PostgresSettings | None = None,
) -> Iterator[Connection]:
    """Open a short lived native connection scoped to one business schema."""
    with _connect_postgres(
        application_name=application_name or f"odaily-{schema}",
        search_path=(schema, "main"),
        settings=settings,
    ) as connection:
        yield connection


__all__ = [
    "PostgresMigration",
    "PostgresSettings",
    "apply_schema_migrations",
    "connect_postgres",
    "postgres_pool_stats",
    "postgres_transaction",
]
