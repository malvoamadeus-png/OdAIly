from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from packages.publisher import PushResult
from packages.stock_market_broadcast.client import BinanceStockMarketClient, discover_stock_instruments
from packages.stock_market_broadcast.copy import render_alert
from packages.stock_market_broadcast.models import StockInstrument, StockQuote
from packages.stock_market_broadcast.service import StockMarketBroadcastService
from packages.stock_market_broadcast.settings import StockMarketSettings
from packages.stock_market_broadcast.state_machine import advance_trigger_state
from packages.stock_market_broadcast.store import StockMarketStore


def _exchange_symbol(
    symbol: str,
    underlying_type: str,
    *,
    status: str = "TRADING",
    contract_type: str = "TRADIFI_PERPETUAL",
    subtype: list[str] | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "status": status,
        "contractType": contract_type,
        "underlyingType": underlying_type,
        "underlyingSubType": subtype or ["STOCK"],
        "pricePrecision": 4,
    }


def test_discovery_includes_stock_markets_and_etf_but_filters_other_symbols() -> None:
    payload = {
        "symbols": [
            _exchange_symbol("AAPLUSDT", "EQUITY"),
            _exchange_symbol("600000USDT", "CN_EQUITY", subtype=["ETF"]),
            _exchange_symbol("0700USDT", "HK_EQUITY"),
            _exchange_symbol("005930USDT", "KR_EQUITY"),
            _exchange_symbol("6758USDT", "JP_EQUITY"),
            _exchange_symbol("FUTUREUSDT", "EQUITY", subtype=["PRE-IPO"]),
            _exchange_symbol("INDEXUSDT", "EQUITY", subtype=["INDEX"]),
            _exchange_symbol("GOLDUSDT", "COMMODITY"),
            _exchange_symbol("EURUSDT", "CURRENCY"),
            _exchange_symbol("OLDUSDT", "EQUITY", status="SETTLING"),
            _exchange_symbol("SPOTUSDT", "EQUITY", contract_type="PERPETUAL"),
        ]
    }

    instruments = discover_stock_instruments(payload)

    assert {item.symbol: item.market for item in instruments} == {
        "005930USDT": "kr",
        "0700USDT": "hk",
        "600000USDT": "cn",
        "6758USDT": "jp",
        "AAPLUSDT": "us",
    }
    assert next(item for item in instruments if item.symbol == "600000USDT").price_precision == 4


def test_binance_24_hour_tickers_are_read_in_one_batch(monkeypatch) -> None:
    now = 1_700_000_000

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            return [
                {
                    "symbol": "AAPLUSDT",
                    "closeTime": (now - 10) * 1000,
                    "lastPrice": "123.45",
                    "priceChangePercent": "4.25",
                },
                {
                    "symbol": "OLDUSDT",
                    "closeTime": (now - 100) * 1000,
                    "lastPrice": "5",
                    "priceChangePercent": "1",
                },
            ]

    class _Session:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def get(self, url: str, **kwargs: object) -> _Response:
            self.calls.append((url, kwargs))
            return _Response()

    monkeypatch.setattr("packages.stock_market_broadcast.client.time.time", lambda: now)
    client = BinanceStockMarketClient(base_url="https://fapi.binance.com", timeout_seconds=5)
    session = _Session()
    client.session = session  # type: ignore[assignment]

    quotes = client.fetch_quotes([StockInstrument("AAPLUSDT", "us", 2)])

    assert len(session.calls) == 1
    assert session.calls[0][0] == "https://fapi.binance.com/fapi/v1/ticker/24hr"
    assert quotes["AAPLUSDT"].price == Decimal("123.45")
    assert quotes["AAPLUSDT"].change_24h == Decimal("4.25")
    assert quotes["AAPLUSDT"].observed_at == now - 10
    assert "OLDUSDT" not in quotes


def test_trigger_state_handles_threshold_jump_direction_and_rearming() -> None:
    baseline = advance_trigger_state(None, symbol="AAPLUSDT", window="24h", change_pct=Decimal("0"))
    jump = advance_trigger_state(baseline.state, symbol="AAPLUSDT", window="24h", change_pct=Decimal("11"))
    assert jump.tier == "live"
    assert jump.direction == "up"
    assert not jump.state.armed_5_up and not jump.state.armed_10_up

    still_above_five = advance_trigger_state(
        jump.state, symbol="AAPLUSDT", window="24h", change_pct=Decimal("8")
    )
    assert still_above_five.tier is None
    assert still_above_five.state.armed_10_up
    recross_ten = advance_trigger_state(
        still_above_five.state, symbol="AAPLUSDT", window="24h", change_pct=Decimal("10.1")
    )
    assert recross_ten.tier == "live"

    below_five = advance_trigger_state(
        recross_ten.state, symbol="AAPLUSDT", window="24h", change_pct=Decimal("4.9")
    )
    recross_five = advance_trigger_state(
        below_five.state, symbol="AAPLUSDT", window="24h", change_pct=Decimal("5.2")
    )
    assert recross_five.tier == "backend"
    assert recross_five.direction == "up"

    downside_baseline = advance_trigger_state(None, symbol="AAPLUSDT", window="1h", change_pct=Decimal("0"))
    downside = advance_trigger_state(
        downside_baseline.state,
        symbol="AAPLUSDT",
        window="1h",
        change_pct=Decimal("-5.01"),
    )
    assert downside.tier == "backend"
    assert downside.direction == "down"


def test_copy_uses_market_attribution_and_contract_code() -> None:
    instrument = StockInstrument("005930USDT", "kr", 0)
    _title, content = render_alert(
        instrument=instrument,
        window="24h",
        tier="live",
        direction="down",
        change_pct=Decimal("-10.24"),
        current_price=Decimal("67800"),
    )
    assert content == "据Gate数据，005930USDT 24小时下跌10.2%，现报67800 USDT。"

    instrument = StockInstrument("600000USDT", "cn", 2)
    _title, content = render_alert(
        instrument=instrument,
        window="1h",
        tier="backend",
        direction="up",
        change_pct=Decimal("5.04"),
        current_price=Decimal("12.34"),
    )
    assert content.startswith("据行情数据，600000USDT")


class _FakePushClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def push(self, **kwargs: object) -> PushResult:
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text="ok")


def _service(path: Path, push_client: _FakePushClient) -> tuple[StockMarketBroadcastService, StockMarketStore]:
    settings = StockMarketSettings(
        database_path=path,
        binance_api_base="https://example.invalid",
        push_endpoint="https://example.invalid/push",
        request_timeout_seconds=1,
    )
    store = StockMarketStore(path)
    service = StockMarketBroadcastService(settings=settings, store=store, client=object(), push_client=push_client)  # type: ignore[arg-type]
    return service, store


def test_service_calculates_one_hour_change_and_dispatches_tiers(tmp_path: Path) -> None:
    database = tmp_path / "stock-market.sqlite"
    instrument = StockInstrument("AAPLUSDT", "us", 2)
    push = _FakePushClient()
    service, store = _service(database, push)
    store.sync_instruments([instrument], now=1_700_000_000)
    timestamp = 1_700_000_000
    with sqlite3.connect(database) as conn:
        conn.executemany(
            "INSERT INTO price_samples(symbol,observed_at,price) VALUES(?,?,?)",
            [(instrument.symbol, timestamp, "100"), (instrument.symbol, timestamp + 30, "100")],
        )

    service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("100"), Decimal("0"), timestamp + 3570)},
        now=timestamp + 3570,
        started=time.monotonic(),
    )
    service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("100"), Decimal("0"), timestamp + 3600)},
        now=timestamp + 3600,
        started=time.monotonic(),
    )
    result = service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("106"), Decimal("11"), timestamp + 3630)},
        now=timestamp + 3630,
        started=time.monotonic(),
    )

    assert result["alert_count"] == 2
    assert {call["is_publish"] for call in push.calls} == {False, True}
    assert all(call["is_push"] is False for call in push.calls)
    assert any("1小时上涨6.0%" in str(call["content"]) for call in push.calls)
    assert any("24小时上涨11.0%" in str(call["content"]) for call in push.calls)
    dashboard = store.dashboard()
    assert Decimal(dashboard["contracts"][0]["change_1h"]) == Decimal("6")
    assert {event["status"] for event in dashboard["recent_events"]} == {"backend_created", "published"}


def test_stale_quote_is_skipped_and_recorded(tmp_path: Path) -> None:
    instrument = StockInstrument("AAPLUSDT", "us", 2)
    service, store = _service(tmp_path / "stock-market.sqlite", _FakePushClient())
    store.sync_instruments([instrument], now=1_700_000_000)

    result = service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("100"), Decimal("10"), 1_700_000_000)},
        now=1_700_000_091,
        started=time.monotonic(),
    )

    assert result["quoted_count"] == 0
    assert result["alert_count"] == 0
    assert store.dashboard()["contracts"][0]["last_error"] == "Binance ticker is stale (91s)"


def test_initial_and_restart_baselines_do_not_publish_existing_change(tmp_path: Path) -> None:
    instrument = StockInstrument("AAPLUSDT", "us", 2)
    push = _FakePushClient()
    database = tmp_path / "stock-market.sqlite"
    service, store = _service(database, push)
    store.sync_instruments([instrument], now=1_700_000_000)

    for service_instance, timestamp, change in (
        (service, 1_700_000_000, "15"),
        (_service(database, push)[0], 1_700_000_030, "15.5"),
    ):
        result = service_instance._process_quotes(
            [instrument],
            {instrument.symbol: StockQuote(instrument.symbol, Decimal("100"), Decimal(change), timestamp)},
            now=timestamp,
            started=time.monotonic(),
        )
        assert result["alert_count"] == 0

    assert push.calls == []
    assert store.dashboard()["recent_events"] == []


def test_restart_baseline_waits_for_each_contract_first_valid_quote(tmp_path: Path) -> None:
    instrument = StockInstrument("AAPLUSDT", "us", 2)
    push = _FakePushClient()
    database = tmp_path / "stock-market.sqlite"
    first_service, store = _service(database, push)
    store.sync_instruments([instrument], now=1_700_000_000)
    first_service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("100"), Decimal("4"), 1_700_000_000)},
        now=1_700_000_000,
        started=time.monotonic(),
    )

    restarted_service, _ = _service(database, push)
    restarted_service._process_quotes(
        [instrument],
        {},
        now=1_700_000_030,
        started=time.monotonic(),
    )
    result = restarted_service._process_quotes(
        [instrument],
        {instrument.symbol: StockQuote(instrument.symbol, Decimal("106"), Decimal("6"), 1_700_000_060)},
        now=1_700_000_060,
        started=time.monotonic(),
    )

    assert result["alert_count"] == 0
    assert push.calls == []
    assert store.dashboard()["recent_events"] == []
    assert Decimal(store.dashboard()["contracts"][0]["change_24h"]) == Decimal("6")


def test_binance_failure_is_saved_in_worker_status(tmp_path: Path) -> None:
    class _FailingQuoteClient:
        def fetch_quotes(self, _instruments: list[StockInstrument]) -> dict[str, StockQuote]:
            raise RuntimeError("Binance unavailable")

    instrument = StockInstrument("AAPLUSDT", "us", 2)
    service, store = _service(tmp_path / "stock-market.sqlite", _FakePushClient())
    store.sync_instruments([instrument], now=int(time.time()))
    service.client = _FailingQuoteClient()  # type: ignore[assignment]

    try:
        service.run_once()
    except RuntimeError as exc:
        assert str(exc) == "Binance unavailable"
    else:
        raise AssertionError("failed quote requests must propagate to the worker boundary")

    dashboard = store.dashboard()
    assert dashboard["last_cycle_status"] == "error"
    assert dashboard["last_cycle_error"] == "Binance unavailable"
