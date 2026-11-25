from __future__ import annotations

import atexit
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Iterator, Sequence

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from psycopg.sql import Identifier

from .storage import PostgresSettings, load_postgres_settings


SCHEMA_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MIGRATION_TABLE = "_odaily_schema_migrations"
_POOL_LOCK = threading.Lock()
_POOLS: dict[tuple[object, ...], ConnectionPool] = {}
_MIGRATION_CACHE_LOCK = threading.RLock()
_APPLIED_MIGRATION_CACHE: set[tuple[str, str, str, tuple[tuple[int, str, str], ...]]] = set()


def _pool_key(settings: PostgresSettings) -> tuple[object, ...]:
    return (
        settings.dsn,
        settings.connect_timeout_seconds,
        settings.statement_timeout_ms,
        settings.lock_timeout_ms,
        settings.idle_transaction_timeout_ms,
        settings.pool_max_size,
        settings.pool_checkout_timeout_seconds,
        settings.pool_max_waiting,
    )


def _pool_for(settings: PostgresSettings) -> ConnectionPool:
    key = _pool_key(settings)
    with _POOL_LOCK:
        pool = _POOLS.get(key)
        if pool is None or pool.closed:
            options = (
                f"-c statement_timeout={settings.statement_timeout_ms} "
                f"-c lock_timeout={settings.lock_timeout_ms} "
                f"-c idle_in_transaction_session_timeout={settings.idle_transaction_timeout_ms}"
            )
            pool = ConnectionPool(
                conninfo=settings.dsn,
                kwargs={
                    "autocommit": True,
                    "connect_timeout": settings.connect_timeout_seconds,
                    "options": options,
                    "row_factory": dict_row,
                },
                min_size=0,
                max_size=settings.pool_max_size,
                max_waiting=settings.pool_max_waiting,
                timeout=settings.pool_checkout_timeout_seconds,
                name="odaily",
                open=False,
            )
            pool.open(wait=False)
            _POOLS[key] = pool
        return pool


def postgres_pool_stats() -> dict[str, int]:
    """Return process-local pool counts without exposing DSNs or credentials."""
    with _POOL_LOCK:
        pools = tuple(_POOLS.values())
    totals = {"pools": len(pools), "capacity": 0, "size": 0, "available": 0, "borrowed": 0, "waiting": 0}
    for pool in pools:
        stats = pool.get_stats()
        totals["capacity"] += int(pool.max_size)
        totals["size"] += int(stats.get("pool_size", 0))
        available = int(stats.get("pool_available", 0))
        totals["available"] += available
        totals["borrowed"] += int(stats.get("pool_size", 0)) - available
        totals["waiting"] += int(stats.get("requests_waiting", 0))
    return totals


def close_postgres_pools() -> None:
    with _POOL_LOCK:
        pools = tuple(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()
    with _MIGRATION_CACHE_LOCK:
        _APPLIED_MIGRATION_CACHE.clear()


atexit.register(close_postgres_pools)


@contextmanager
def connect_postgres(
    *,
    application_name: str,
    search_path: Sequence[str] = ("main",),
    settings: PostgresSettings | None = None,
) -> Iterator[Connection]:
    """Check out a bounded process-local connection for one short operation."""
    if not application_name or not application_name.strip():
        raise ValueError("application_name is required to identify the connection role")
    if not search_path:
        raise ValueError("search_path must include at least one schema")
    schemas = list(dict.fromkeys(search_path))
    if "main" not in schemas:
        schemas.append("main")
    search_path_sql = ", ".join(_schema_identifier(schema).as_string() for schema in schemas)
    current = settings or load_postgres_settings()
    pool = _pool_for(current)
    with pool.connection(timeout=current.pool_checkout_timeout_seconds) as connection:
        connection.execute(
            "SELECT pg_catalog.set_config('search_path', %s, false), "
            "pg_catalog.set_config('TimeZone', 'UTC', false), "
            "pg_catalog.set_config('statement_timeout', %s, false), "
            "pg_catalog.set_config('lock_timeout', %s, false), "
            "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, false), "
            "pg_catalog.set_config('application_name', %s, false)",
            (
                search_path_sql,
                f"{current.statement_timeout_ms}ms",
                f"{current.lock_timeout_ms}ms",
                f"{current.idle_transaction_timeout_ms}ms",
                application_name.strip(),
            ),
        )
        yield connection


@contextmanager
def postgres_transaction(connection: Connection) -> Iterator[Connection]:
    """Run a visible atomic unit; nested use follows psycopg savepoint semantics."""
    with connection.transaction():
        yield connection


@dataclass(frozen=True, slots=True)
class PostgresMigration:
    version: int
    name: str
    checksum: str
    apply: Callable[[Connection], None]


def _schema_identifier(schema: str) -> Identifier:
    if not SCHEMA_NAME_PATTERN.fullmatch(schema):
        raise ValueError(f"Unsupported PostgreSQL schema name: {schema!r}")
    return Identifier(schema)


def apply_schema_migrations(
    connection: Connection,
    *,
    schema: str,
    component: str | None = None,
    migrations: Sequence[PostgresMigration],
    settings: PostgresSettings | None = None,
) -> tuple[int, ...]:
    """Apply explicit, ordered migrations under a schema scoped transaction lock."""
    schema_id = _schema_identifier(schema)
    migration_component = component or schema
    if not SCHEMA_NAME_PATTERN.fullmatch(migration_component):
        raise ValueError(f"Unsupported PostgreSQL migration component: {migration_component!r}")
    ordered = tuple(sorted(migrations, key=lambda migration: migration.version))
    versions = [migration.version for migration in ordered]
    if any(version < 1 for version in versions) or len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique positive integers")
    if any(not migration.name.strip() or not migration.checksum.strip() for migration in ordered):
        raise ValueError("Every migration requires a name and checksum")

    current = settings or load_postgres_settings()
    fingerprint = tuple((migration.version, migration.name, migration.checksum) for migration in ordered)
    cache_key = (connection.info.dsn, schema, migration_component, fingerprint)
    nested_transaction = connection.info.transaction_status.name != "IDLE"
    with _MIGRATION_CACHE_LOCK:
        if not nested_transaction and cache_key in _APPLIED_MIGRATION_CACHE:
            return ()

    schema_sql = schema_id.as_string(connection)
    table_sql = Identifier("main", MIGRATION_TABLE).as_string(connection)
    lock_key = f"odaily:schema-migrations:{schema}"
    bootstrap_key = "odaily:schema-migrations:bootstrap"
    with postgres_transaction(connection):
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (bootstrap_key,))
            connection.execute("CREATE SCHEMA IF NOT EXISTS " + Identifier("main").as_string(connection))
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS {table_sql} ("
                "schema_name text NOT NULL, component text NOT NULL, version integer NOT NULL, "
                "name text NOT NULL, checksum text NOT NULL, "
                "PRIMARY KEY (schema_name, component, version), "
                "applied_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
    with postgres_transaction(connection):
            connection.execute(
                "SELECT pg_catalog.set_config('statement_timeout', %s, true), "
                "pg_catalog.set_config('lock_timeout', %s, true), "
                "pg_catalog.set_config('idle_in_transaction_session_timeout', %s, true)",
                (
                    f"{current.migration_statement_timeout_ms}ms",
                    f"{current.migration_lock_timeout_ms}ms",
                    f"{current.migration_idle_transaction_timeout_ms}ms",
                ),
            )
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
            connection.execute("CREATE SCHEMA IF NOT EXISTS " + schema_sql)
            connection.execute(
                "SELECT pg_catalog.set_config('search_path', %s, true)",
                ((f"{schema},main" if schema != "main" else "main"),),
            )
            applied_rows = connection.execute(
                f"SELECT version, name, checksum FROM {table_sql} "
                "WHERE schema_name = %s AND component = %s ORDER BY version",
                (schema, migration_component),
            ).fetchall()
            applied = {int(row["version"]): (row["name"], row["checksum"]) for row in applied_rows}
            for migration in ordered:
                existing = applied.get(migration.version)
                if existing is not None:
                    if existing != (migration.name, migration.checksum):
                        raise RuntimeError(
                            f"Applied PostgreSQL migration {schema}/{migration.version} changed"
                        )
                    continue
                migration.apply(connection)
                connection.execute(
                    f"INSERT INTO {table_sql} (schema_name, component, version, name, checksum) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (schema, migration_component, migration.version, migration.name, migration.checksum),
                )
    if not nested_transaction:
        with _MIGRATION_CACHE_LOCK:
            _APPLIED_MIGRATION_CACHE.add(cache_key)
    return tuple(version for version in versions if version not in applied)
