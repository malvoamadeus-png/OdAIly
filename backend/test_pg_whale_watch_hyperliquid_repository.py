from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql

from packages.whale_watch.models import HyperliquidActivity, HyperliquidWindowEntry
from packages.whale_watch.postgres_hyperliquid_repository import PostgresWhaleWatchHyperliquidRepository


def test_postgres_hyperliquid_settings_state_and_activity_round_trip(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_hl_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        repository = PostgresWhaleWatchHyperliquidRepository()
        repository.init_schema()
        settings = repository.get_runtime_settings(
            default_single_fill_min_notional_usd=Decimal("1000"),
            default_aggregate_min_notional_usd=Decimal("5000"),
            default_aggregate_window_seconds=600,
        )
        assert settings.single_fill_min_notional_usd == Decimal("1000")
        with repository._connect() as connection:
            address_row = connection.execute(
                "INSERT INTO whale_watch_hyperliquid_addresses(address,address_lower,label) VALUES(%s,%s,%s) RETURNING id",
                ("0x" + "A" * 40, "0x" + "a" * 40, "Synthetic HL"),
            ).fetchone()
        whale = repository.list_addresses()[0]
        assert whale.id == address_row["id"]
        now = datetime.now(UTC)
        entry = HyperliquidWindowEntry(
            fill_key="fill-1", fill_time_ms=1_800_000_000_000, notional_usd=Decimal("1500"),
            summary="Synthetic fill", direction="Open Long", coin="BTC",
        )
        repository.mark_seeded(
            address_id=whale.id,
            last_seen_time=entry.fill_time_ms,
            polled_at=now,
            aggregate_window_entries=[entry],
            aggregate_alert_active=True,
        )
        repository.record_success(address_id=whale.id, last_seen_time=entry.fill_time_ms - 1, polled_at=now)
        state = repository.get_state(address_id=whale.id)
        assert state.last_seen_time == entry.fill_time_ms
        assert state.aggregate_window_entries[0].fill_key == "fill-1"
        assert state.aggregate_alert_active
        activity = HyperliquidActivity(
            alert_kind="aggregate", fill_key="fill-1", coin="BTC", direction="Open Long", side="B",
            price=Decimal("100"), size=Decimal("15"), notional_usd=Decimal("1500"), closed_pnl=Decimal("0"),
            fill_time=now, fill_time_ms=entry.fill_time_ms, telegram_text="Synthetic alert", summary="Synthetic fill",
            aggregate_fill_count=1, raw_payload={"hash": "0xhash", "marker": "hl-test"},
        )
        activity_id = repository.save_activity(whale=whale, activity=activity)
        assert activity_id
        assert repository.save_activity(whale=whale, activity=activity) is None
        repository.update_activity_telegram_result(fill_key="fill-1", telegram_result={"ok": True})
        with repository._connect(autocommit=True) as connection:
            row = connection.execute("SELECT telegram_result,telegram_sent_at,raw_payload FROM whale_watch_hyperliquid_activities WHERE id=%s", (activity_id,)).fetchone()
            events = connection.execute(
                "SELECT count(*) AS n FROM odaily_source_state_outbox "
                "WHERE source_table='whale_watch_hyperliquid_addresses'"
            ).fetchone()["n"]
        assert row["telegram_result"] == {"ok": True}
        assert row["telegram_sent_at"] is not None
        assert row["raw_payload"]["marker"] == "hl-test"
        assert events == 1
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
