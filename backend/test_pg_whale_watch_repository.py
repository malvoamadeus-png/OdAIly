from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from packages.whale_watch.models import Activity
from packages.whale_watch.repository import PostgresWhaleWatchRepository


def test_postgres_whale_watch_activity_state_and_source_outbox(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_whale_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        repository = PostgresWhaleWatchRepository()
        repository.init_schema()
        with repository._connect() as connection:
            address = connection.execute(
                "INSERT INTO whale_watch_addresses(address,address_lower,label,enabled) "
                "VALUES (%s,%s,%s,true) RETURNING id",
                ("0x" + "A" * 40, "0x" + "a" * 40, "Synthetic whale"),
            ).fetchone()
        whale = repository.list_addresses()[0]
        assert whale.id == address["id"]
        now = datetime.now(UTC)
        repository.mark_chain_seeded(address_id=whale.id, chain_key="ethereum", block_number=100, polled_at=now)
        repository.record_chain_success(address_id=whale.id, chain_key="ethereum", block_number=101, polled_at=now)
        state = repository.get_chain_state(address_id=whale.id, chain_key="ethereum")
        assert state.last_seen_block == 101
        activity = Activity(
            kind="transfer",
            tx_hash="0xsynthetic-tx",
            block_number=101,
            timestamp=now,
            fingerprint="synthetic-fingerprint",
            summary="Synthetic transfer",
            telegram_text="Synthetic alert",
            tx_url="https://example.test/tx/1",
            direction="in",
            raw_payload={"marker": "whale-test"},
        )
        activity_id = repository.save_activity(whale=whale, chain_key="ethereum", activity=activity)
        assert activity_id
        assert repository.save_activity(whale=whale, chain_key="ethereum", activity=activity) is None
        repository.update_activity_telegram_result(tx_hash=activity.tx_hash, fingerprint=activity.fingerprint, telegram_result={"ok": True})
        with repository._connect(autocommit=True) as connection:
            row = connection.execute("SELECT telegram_result,telegram_sent_at FROM whale_watch_activities WHERE id=%s", (activity_id,)).fetchone()
            events = connection.execute(
                "SELECT count(*) AS n FROM odaily_source_state_outbox WHERE source_table='whale_watch_addresses'"
            ).fetchone()["n"]
        assert row["telegram_result"] == {"ok": True}
        assert row["telegram_sent_at"] is not None
        assert events == 1
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
