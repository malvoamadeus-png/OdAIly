from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.pipeline_timing import (
    PipelineTimingSnapshotService,
    PostgresPipelineTimingLocalStore,
    PostgresPipelineTimingRepository,
)
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_pipeline_timing_range_and_snapshot_round_trip(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_timing_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        processing = PostgresXProcessingRepository()
        processing.init_schema()
        with processing._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS main.pipeline_timing_snapshots "
                "(id integer PRIMARY KEY, generated_at text NOT NULL, payload text NOT NULL)"
            )
            connection.execute(
                "INSERT INTO main.pipeline_timing_snapshots(id,generated_at,payload) "
                "VALUES(1,'legacy','historical-main-snapshot') ON CONFLICT(id) DO NOTHING"
            )
        repository = PostgresPipelineTimingRepository()
        local_store = PostgresPipelineTimingLocalStore()
        now = datetime.now(UTC)
        with processing._connect() as connection:
            row = connection.execute(
                """INSERT INTO tasks(source,source_item_id,title,content,status,created_at,updated_at,metadata)
                   VALUES('x',%s,'timing','sample','published',%s,%s,%s) RETURNING id""",
                (uuid.uuid4().hex, now - timedelta(minutes=5), now, json.dumps({"x_account_is_ai_source": True})),
            ).fetchone()
            task_id = int(row["id"])
            connection.execute(
                """INSERT INTO x_task_pipeline(task_id,judge_completed_at,search_completed_at,write_completed_at,
                   format_completed_at,publisher_decided_at,publish_completed_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                (task_id, now - timedelta(minutes=4), now - timedelta(minutes=3), now - timedelta(minutes=2),
                 now - timedelta(minutes=1), now - timedelta(seconds=30), now),
            )
        rows = repository.list_recent_rows(max_hours=24)
        assert [item.task_id for item in rows] == [task_id]
        assert rows[0].created_at.tzinfo is not None
        payload = {"generated_at": now.isoformat(), "windows": [{"hours": 24}]}
        service = PipelineTimingSnapshotService(local_store=local_store, repository=repository)
        service.refresh_once()
        assert local_store.latest() is not None
        with repository._connect() as connection:
            count = connection.execute("SELECT count(*) AS n FROM pipeline_timing_snapshots").fetchone()["n"]
            assert count == 1
            stored = connection.execute(
                "SELECT payload_json FROM pipeline_timing_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()["payload_json"]
        with processing._connect() as connection:
            legacy = connection.execute(
                "SELECT payload FROM main.pipeline_timing_snapshots WHERE id=1"
            ).fetchone()["payload"]
        assert legacy == "historical-main-snapshot"
        assert stored["generated_at"] == local_store.latest()["generated_at"]
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
