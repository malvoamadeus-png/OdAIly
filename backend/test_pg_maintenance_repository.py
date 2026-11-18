from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.maintenance.repository import PostgresMaintenanceRepository


def test_postgres_maintenance_dry_run_and_cleanup(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_maintenance_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        repository = PostgresMaintenanceRepository()
        repository.init_schema()
        with repository._connect() as connection:
            connection.execute(
                "INSERT INTO pipeline_alerts(alert_key,last_sent_at,last_message,created_at,updated_at) "
                "VALUES ('old-alert',now(),'old',now()-interval '10 days',now()-interval '10 days')"
            )
        dry = repository.cleanup(dry_run=True, retention_days=7)
        assert dry.deleted["pipeline_alerts"] == 1
        with repository._connect(autocommit=True) as connection:
            assert connection.execute("SELECT count(*) AS n FROM pipeline_alerts").fetchone()["n"] == 1
        applied = repository.cleanup(dry_run=False, retention_days=7)
        assert applied.deleted["pipeline_alerts"] == 1
        with repository._connect(autocommit=True) as connection:
            assert connection.execute("SELECT count(*) AS n FROM pipeline_alerts").fetchone()["n"] == 0
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
