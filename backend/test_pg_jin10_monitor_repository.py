from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from packages.jin10_monitor.models import Jin10Item, Jin10RunResult
from packages.jin10_monitor.repository import PostgresJin10MonitorRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_jin10_settings_seen_and_task_round_trip(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_jin10_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresJin10MonitorRepository()
        repository.init_schema()
        initial = repository.get_settings()
        assert initial.endpoint_url.endswith("flash_newest.js")
        changed = repository.update_settings(enabled=True, interval_seconds=90, channel="test")
        assert changed.enabled and changed.interval_seconds == 90 and changed.channel == "test"

        assert not repository.has_seen_items()
        repository.mark_seeded(["seeded-1"])
        assert repository.has_seen_items()
        assert repository.unseen_source_item_ids(["seeded-1", "fresh-1"]) == {"fresh-1"}
        assert not repository.mark_seen("seeded-1", seeded=False)
        assert repository.mark_seen("fresh-1", seeded=False)

        item = Jin10Item(
            source_item_id="flash-1",
            title="Synthetic Jin10 flash",
            content="Synthetic content",
            source_url="https://example.test/flash-1",
            published_at=datetime.now(UTC),
            raw_payload={"marker": "jin10-test"},
            metadata={"fixture": True},
        )
        task_id = repository.save_task(item)
        assert task_id
        assert repository.save_task(item) == task_id
        repository.record_run(Jin10RunResult(status="success", fetched=1, saved=1), finished_at=datetime.now(UTC))
        with repository._connect(autocommit=True) as connection:
            task = connection.execute("SELECT source,source_item_id,metadata FROM tasks WHERE id=%s", (task_id,)).fetchone()
            source_state_events = connection.execute(
                "SELECT count(*) AS n FROM odaily_source_state_outbox WHERE source_table='jin10_settings'"
            ).fetchone()["n"]
        assert task["source"] == "jin10"
        assert task["source_item_id"] == "flash-1"
        assert task["metadata"]["fixture"] is True
        assert source_state_events >= 1
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
