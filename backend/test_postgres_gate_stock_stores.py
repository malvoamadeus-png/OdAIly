from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def isolated_pg(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run Gate/Stock PostgreSQL checks")
    database_name = f"odaily_gate_stock_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    yield
    from packages.common.pg_runtime import close_postgres_pools

    close_postgres_pools()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_gate_factory_initializes_native_schema_and_persists_state(isolated_pg, tmp_path):
    from packages.gate_market_broadcast.models import SymbolState, TickerQuote
    from packages.gate_market_broadcast.store import GateMarketStore

    store = GateMarketStore(tmp_path / "unused.sqlite")
    assert store.get_mode() == "backend"
    config = store.get_symbol_config("USDCNH")
    assert config.threshold_text == "0.010"
    assert store.set_threshold("USDCNH", "0.020").threshold_text == "0.020"
    state = SymbolState("USDCNH", "backend", initialized=True, last_price=Decimal("7.23"), last_quote_at=10, disarmed_levels={1, 2})
    event_id = store.save_state_and_event(state, event={
        "event_key": "gate-test:usdcnh", "direction": "up", "trigger_level": "7.2", "current_price": "7.23",
        "observed_at": 10, "status": "pending",
    })
    assert event_id is not None
    loaded = store.get_state("USDCNH", "live")
    assert loaded.last_price == Decimal("7.23")
    assert loaded.disarmed_levels == {1, 2}
    assert store.dashboard()["recent_events"][0]["event_key"] == "gate-test:usdcnh"
    sample_points=[(100,Decimal('7.0')),(200,Decimal('7.1'))]
    store.add_samples('USDCNH',sample_points)
    assert store.recent_sample_count('USDCNH',100)==2
    quote=TickerQuote('USDCNH',Decimal('7.2'),200+86400,'open',previous_close=Decimal('7.0'))
    metrics=store.reference_metrics(symbol='USDCNH',quote=quote)
    assert metrics is not None and metrics.reference_kind=='rolling_24h'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:store.should_send_alert('test-alert','message',60),range(2)))
    assert sorted(outcomes)==[False,True]


def test_stock_factory_commits_cycle_state_samples_and_unique_alert(isolated_pg, tmp_path):
    from packages.stock_market_broadcast.models import AlertMember, BroadcastState, StockAlert, StockInstrument, StockQuote
    from packages.stock_market_broadcast.store import StockMarketStore

    store = StockMarketStore(tmp_path / "unused-stock.sqlite")
    instrument = StockInstrument("AAPLUSDT", "us", 2)
    store.sync_instruments([instrument], now=100)
    quote = StockQuote("AAPLUSDT", Decimal("106"), Decimal("6"), 200)
    member = AlertMember(instrument, {"1h": Decimal("6")}, Decimal("106"), 200)
    alert = StockAlert("stock-test:aapl", "us", "backend", "up", (member,), "AAPL 上涨", "正文")
    claimed = store.record_cycle(
        quotes={instrument.symbol: quote}, changes_1h={instrument.symbol: Decimal("6")},
        state=BroadcastState(), alerts=[alert], errors={}, now=200, startup_baseline=False,
    )
    assert len(claimed) == 1
    assert store.record_cycle(
        quotes={instrument.symbol: quote}, changes_1h={instrument.symbol: Decimal("6")},
        state=BroadcastState(), alerts=[alert], errors={}, now=201, startup_baseline=False,
    ) == []
    dashboard = store.dashboard()
    assert dashboard["contract_count"] == 1
    assert dashboard["contracts"][0]["last_price"] == "106"
    assert dashboard["recent_events"][0]["title"] == "AAPL 上涨"


def test_macro_factory_baselines_then_claims_delivery_once(isolated_pg, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from packages.macro_newsflash.models import MacroUpdate
    from packages.macro_newsflash.store import MacroNewsflashStore

    store = MacroNewsflashStore(tmp_path / "unused-macro.sqlite")
    first = MacroUpdate("cpi", "2026-10", "10月", "CPI", "3.0%", "3.1%", "BLS", "https://example.invalid/cpi", "2026-11-18T00:00:00+00:00", {"value": 3})
    assert store.observe_update(first, title="CPI", content="正文", publish_latest=False) == "baselined"
    second = MacroUpdate("cpi", "2026-11", "11月", "CPI", "2.9%", "3.0%", "BLS", "https://example.invalid/cpi", "2026-12-18T00:00:00+00:00", {"value": 2.9})
    assert store.observe_update(second, title="新CPI", content="新正文", publish_latest=True) == "queued"
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: store.claim_pending(limit=10), range(2)))
    flattened = [row for batch in claims for row in batch]
    assert len(flattened) == 1
    assert flattened[0]["event_key"] == second.event_key
    store.finish_delivery(event_key=second.event_key, ok=True)
    dashboard = store.dashboard()
    assert dashboard["metrics"][0]["actual"] == "2.9%"
    assert dashboard["events"][0]["status"] == "published"


def test_predict_market_factory_persists_baseline_and_candidate(isolated_pg, tmp_path):
    from datetime import timedelta
    from packages.predict_market_discovery.models import MarketObservation
    from packages.predict_market_discovery.store import PredictMarketStore

    store = PredictMarketStore(tmp_path / "unused-predict.sqlite")
    now = datetime.now(UTC)
    def observation(market_id):
        return MarketObservation(
            market_id=market_id, category_id="cat", category_title="Category", category_slug="category",
            category_published_at=now.isoformat(), starts_at=(now + timedelta(hours=1)).isoformat(),
            ends_at=(now + timedelta(hours=2)).isoformat(), market_created_at=(now - timedelta(hours=1)).isoformat(),
            question=f"Will event {market_id} happen?", market_variant="binary", market_status="active",
            trading_status="OPEN", market_url=f"https://example.invalid/{market_id}", filter_reason=None,
        )
    baseline = store.record_scan(observations=[observation("baseline")], pages=1, categories=1, started_at=now, page_limit_reached=False)
    assert baseline["baseline"] is True
    later = store.record_scan(observations=[observation("candidate")], pages=2, categories=1, started_at=now, page_limit_reached=True)
    assert later["baseline"] is False
    assert later["new_markets"] == 1
    assert later["new_candidates"] == 1
    dashboard = store.dashboard()
    assert dashboard["seen_markets"] == 2
    assert dashboard["last_scan"]["page_limit_reached"] is True
    assert {item["market_id"] for item in dashboard["candidates"]} == {"baseline", "candidate"}
