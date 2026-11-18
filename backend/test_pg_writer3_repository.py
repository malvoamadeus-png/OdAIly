from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.writer3.models import OdailyReference
from packages.writer3.repository import PostgresWriter3Repository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_writer3_reference_claim_and_completion(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_writer3_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresWriter3Repository()
        repository.init_schema()
        published = datetime.now(UTC) - timedelta(minutes=1)
        item = OdailyReference(
            source_item_id="writer3-synthetic",
            source_url="https://example.test/news/writer3",
            title="Synthetic title",
            content="Synthetic body",
            published_at=published,
            metadata={"fixture": "writer3"},
            raw_payload={"raw": "body"},
        )
        assert repository.upsert_odaily_references([item]) == 1
        assert repository.upsert_odaily_references([item]) == 1
        assert repository.list_odaily_references(since=published - timedelta(minutes=5))[0].source_item_id == item.source_item_id
        task = repository.claim_task(
            worker_id="writer3-test",
            start_after=published - timedelta(minutes=5),
            freshness_window_seconds=3600,
        )
        assert task is not None
        assert task.source_item_id == item.source_item_id
        repository.complete_failed(task, error="synthetic failure")
        assert repository.reset_task(task.context_id)
        with repository._connect(autocommit=True) as connection:
            row = connection.execute("SELECT status,last_error,metadata FROM writer3_contexts WHERE id=%s", (task.context_id,)).fetchone()
        assert row["status"] == "pending"
        assert row["last_error"] is None
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
