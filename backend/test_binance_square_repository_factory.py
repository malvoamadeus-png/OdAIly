from __future__ import annotations

from pathlib import Path
import os
import uuid

import psycopg
import pytest
from psycopg import sql

from packages.binance_square.repository import BinanceSquareRepository, create_binance_square_repository


def test_binance_square_factory_keeps_explicit_sqlite_path(monkeypatch, tmp_path):
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "sqlite")
    path = tmp_path / "square.sqlite"
    repository = create_binance_square_repository(path)
    assert isinstance(repository, BinanceSquareRepository)
    assert repository.path == path


def test_binance_square_factory_selects_postgres(monkeypatch):
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", "postgresql://malvo@127.0.0.1:55432/postgres")
    monkeypatch.setattr("packages.binance_square.postgres_repository.PostgresBinanceSquareRepository.__init__", lambda self: None)
    from packages.binance_square.postgres_repository import PostgresBinanceSquareRepository

    assert isinstance(create_binance_square_repository(), PostgresBinanceSquareRepository)


def test_binance_square_factory_keeps_explicit_sqlite_path_under_postgres(monkeypatch, tmp_path):
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    path = tmp_path / "square.sqlite"
    repository = create_binance_square_repository(path)
    assert isinstance(repository, BinanceSquareRepository)
    assert repository.path == path


def test_postgres_binance_square_mark_seeded_inserts_posts(monkeypatch):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_square_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(dsn, dbname=database_name))
    try:
        from packages.binance_square.postgres_repository import PostgresBinanceSquareRepository

        repository = PostgresBinanceSquareRepository()
        with repository._connect() as connection:
            account_id = connection.execute(
                "INSERT INTO binance_square_accounts(slug,slug_lower,profile_url) "
                "VALUES('fixture_account','fixture_account','https://example.invalid/fixture_account') RETURNING id"
            ).fetchone()["id"]
        from packages.binance_square.models import BinanceSquareAccount

        account = BinanceSquareAccount(
            id=int(account_id), slug="fixture_account", slug_lower="fixture_account",
            profile_url="https://example.invalid/fixture_account",
        )
        repository.mark_seeded(account, ["seed-post-1", "seed-post-2"])
        with repository._connect() as connection:
            rows = connection.execute(
                "SELECT post_id,seeded FROM binance_square_seen_posts ORDER BY post_id"
            ).fetchall()
        assert rows == [
            {"post_id": "seed-post-1", "seeded": True},
            {"post_id": "seed-post-2", "seeded": True},
        ]
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
