from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.external_media_alert.repository import PostgresExternalMediaAlertRepository
from packages.pipeline_supervisor.repository import PostgresPipelineSupervisorRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_pipeline_supervisor_observation_and_alert_dedup(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_supervisor_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        PostgresXProcessingRepository().init_schema()
        PostgresExternalMediaAlertRepository().init_schema()
        repository = PostgresPipelineSupervisorRepository()
        repository.init_schema()
        now = datetime.now(UTC)
        with repository._connect() as connection:
            connection.execute(
                "INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at) "
                "VALUES ('x_capture','worker-1','running',%s,%s)",
                (now - timedelta(hours=1), now - timedelta(hours=2)),
            )
            connection.execute(
                "INSERT INTO tasks(source,source_item_id,content,status,created_at,updated_at) "
                "VALUES ('x','supervisor-task','synthetic','pending',%s,%s)",
                (now - timedelta(hours=1), now - timedelta(hours=1)),
            )
        assert any(row["component"] == "x_capture" for row in repository.list_stale_heartbeats(cutoff=now - timedelta(minutes=5)))
        assert repository.get_latest_heartbeat(component="x_capture")["worker_id"] == "worker-1"
        assert repository.list_old_claimable_tasks(cutoff=now - timedelta(minutes=10))[0]["source"] == "x"

        since = now - timedelta(minutes=1)
        assert repository.claim_alert(alert_key="synthetic-alert", message="first", dedup_cutoff=since)
        assert not repository.claim_alert(alert_key="synthetic-alert", message="duplicate", dedup_cutoff=since)
        with repository._connect(autocommit=True) as connection:
            alert = connection.execute("SELECT send_count,last_message FROM pipeline_alerts WHERE alert_key='synthetic-alert'").fetchone()
        assert alert == {"send_count": 1, "last_message": "first"}
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
