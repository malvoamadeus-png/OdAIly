from __future__ import annotations

import sqlite3
import time
from decimal import Decimal
from pathlib import Path

from packages.publisher import PushResult
from packages.stock_market_broadcast.client import BinanceStockMarketClient, discover_stock_instruments
from packages.stock_market_broadcast.copy import render_alert
from packages.stock_market_broadcast.aggregation import advance_broadcast
from packages.stock_market_broadcast.models import BroadcastState, StockInstrument, StockQuote
from packages.stock_market_broadcast.service import StockMarketBroadcastService
from packages.stock_market_broadcast.settings import StockMarketSettings
from packages.stock_market_broadcast.state_machine import PRICE_LOCK_SECONDS
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


def test_discovery_includes_stock_markets_but_filters_leveraged_etfs() -> None:
    payload = {
        "symbols": [
            _exchange_symbol("AAPLUSDT", "EQUITY"),
            _exchange_symbol("600000USDT", "CN_EQUITY", subtype=["ETF"]),
            _exchange_symbol("CSOPSAMSUNG2LUSDT", "EQUITY"),
            _exchange_symbol("SKDDUSDT", "EQUITY"),
            _exchange_symbol("SOXLUSDT", "EQUITY"),
            _exchange_symbol("SOXSUSDT", "EQUITY"),
            *[
                _exchange_symbol(f"{symbol}USDT", "EQUITY")
                for symbol in (
                    "INTW", "KORU", "MSTU", "MUU", "MVLL", "NVDL", "RAM",
                    "SKDD", "SKUU", "SNXX", "SQQQ", "SOXL", "SOXS", "STXX",
                    "TBT", "TMF", "TQQQ", "TSLL", "TZA", "UVXY",
                )
            ],
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


def test_copy_uses_market_attribution_and_asset_name() -> None:
    instrument = StockInstrument("005930USDT", "kr", 0)
    _title, content = render_alert(
        instrument=instrument,
        window="24h",
        tier="live",
        direction="down",
        change_pct=Decimal("-10.24"),
        current_price=Decimal("67800"),
    )
    assert content == "Odaily星球日报讯 据 Gate 数据，005930 24 小时下跌10.2%，现报67800 USDT。"

    instrument = StockInstrument("600000USDT", "cn", 2)
    _title, content = render_alert(
        instrument=instrument,
        window="1h",
        tier="backend",
        direction="up",
        change_pct=Decimal("5.04"),
        current_price=Decimal("12.34"),
    )
    assert content.startswith("Odaily星球日报讯 据行情数据，600000 1 小时上涨")

    instrument = StockInstrument("SMCIUSDT", "us", 2)
    title, content = render_alert(
        instrument=instrument,
        window="24h",
        tier="live",
        direction="up",
        change_pct=Decimal("14.0"),
        current_price=Decimal("36.85"),
    )
    assert title == "SMCI 24小时上涨14.0%"
    assert content == "Odaily星球日报讯 据 MSX.COM 数据，SMCI 24 小时上涨14.0%，现报36.85 USDT。"


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

    assert result["alert_count"] == 0
    assert result["pending_count"] == 1
    for offset in range(3660, 3811, 30):
        # Maintain a valid 1H reference while waiting for the merge window.
        with sqlite3.connect(database) as conn:
            conn.execute("INSERT OR REPLACE INTO price_samples VALUES(?,?,?)", (instrument.symbol, timestamp + offset - 3600, "100"))
        result = service._process_quotes(
            [instrument],
            {instrument.symbol: StockQuote(instrument.symbol, Decimal("106"), Decimal("11"), timestamp + offset)},
            now=timestamp + offset, started=time.monotonic(),
        )
    assert result["alert_count"] == 1
    assert {call["is_publish"] for call in push.calls} == {False}
    assert all(call["is_push"] is False for call in push.calls)
    assert any("1小时上涨6.0%" in str(call["content"]) for call in push.calls)
    assert any("24小时上涨11.0%" in str(call["content"]) for call in push.calls)
    dashboard = store.dashboard()
    assert Decimal(dashboard["contracts"][0]["change_1h"]) == Decimal("6")
    assert {event["status"] for event in dashboard["recent_events"]} == {"backend_created"}


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


class _Replay:
    def __init__(self, instruments: list[StockInstrument] | None = None) -> None:
        self.instruments = instruments or [StockInstrument("AAAUSDT", "us", 2)]
        self.state = BroadcastState()
        self.now = 1_700_000_000

    def tick(self, values: dict[str, tuple[str, ...]], *, restart: bool = False):
        quotes = {symbol: StockQuote(symbol, Decimal(values[0]), Decimal(values[1]), self.now)
                  for symbol, values in values.items()}
        changes = {symbol: {"24h": quote.change_24h} for symbol, quote in quotes.items()}
        for symbol, item in values.items():
            if len(item) == 3:
                changes[symbol]["1h"] = Decimal(item[2])
        alerts, _ = advance_broadcast(
            self.state, instruments=self.instruments, quotes=quotes, changes=changes,
            now=self.now, restart=restart, max_gap_seconds=90,
        )
        self.now += 30
        return alerts

    def hold(self, seconds: int, values: dict[str, tuple[str, ...]]):
        alerts = []
        for _ in range(seconds // 30):
            alerts.extend(self.tick(values))
        return alerts


def test_many_contracts_merge_by_market_direction_and_tier_with_five_representatives():
    instruments = [StockInstrument(f"A{i}USDT", "us", 2) for i in range(40)]
    replay = _Replay(instruments)
    baseline = {item.symbol: ("100", "0") for item in instruments}
    values = {item.symbol: ("110", "11" if i < 30 else "6") for i, item in enumerate(instruments)}
    assert replay.tick(baseline) == []
    assert replay.tick(values) == []
    alerts = replay.hold(180, values)
    assert len(alerts) == 2
    assert {alert.tier: len(alert.members) for alert in alerts} == {"live": 30, "backend": 10}
    assert all("普涨" in alert.content for alert in alerts)
    assert all(alert.content.count("现报") == 5 for alert in alerts)
    assert "A0USDT" not in alerts[1].content
    assert replay.hold(3600, values) == []


def test_groups_do_not_mix_markets_or_directions_and_cannot_claim_broad_rally():
    instruments = [StockInstrument("AAAUSDT", "us", 2), StockInstrument("BBBUSDT", "us", 2),
                   StockInstrument("CCCUSDT", "hk", 2), StockInstrument("DDDUSDT", "hk", 2)]
    replay = _Replay(instruments)
    replay.tick({item.symbol: ("100", "0") for item in instruments})
    values = {"AAAUSDT": ("111", "11"), "BBBUSDT": ("89", "-11"),
              "CCCUSDT": ("111", "11"), "DDDUSDT": ("112", "12")}
    replay.tick(values)
    alerts = replay.hold(180, values)
    assert len(alerts) == 3
    assert {(alert.market, alert.direction) for alert in alerts} == {("us", "up"), ("us", "down"), ("hk", "up")}
    assert all("据行情数据" in alert.content for alert in alerts if alert.market == "hk")
    assert all("普涨" not in alert.content for alert in alerts if alert.market == "us")


def test_candidate_upgrade_and_retreat_are_rechecked_before_dispatch():
    replay = _Replay([StockInstrument("AAAUSDT", "us", 2), StockInstrument("BBBUSDT", "us", 2)])
    replay.tick({"AAAUSDT": ("100", "0"), "BBBUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("106", "6"), "BBBUSDT": ("106", "6")})
    values = {"AAAUSDT": ("111", "11"), "BBBUSDT": ("104", "4")}
    alerts = replay.hold(180, values)
    assert len(alerts) == 1 and alerts[0].tier == "live"
    assert [member.instrument.asset_name for member in alerts[0].members] == ["AAA"]


def test_repeated_crossings_and_rolling_reference_changes_do_not_repeat_prices():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    assert len(replay.hold(180, {"AAAUSDT": ("110", "11")})) == 1
    for _ in range(50):
        assert replay.tick({"AAAUSDT": ("109", "9.9")}) == []
        assert replay.tick({"AAAUSDT": ("110", "10.1")}) == []
    assert replay.hold(180, {"AAAUSDT": ("110", "20")}) == []
    # Exactly 5% beyond 110 is 115.50; crossing a percentage threshold is not needed.
    replay.tick({"AAAUSDT": ("115.50", "20")})
    assert len(replay.hold(180, {"AAAUSDT": ("115.50", "20")})) == 1


def test_backend_promotion_requires_new_extreme_and_waits_for_followup_slot():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("106", "6")})
    assert replay.hold(180, {"AAAUSDT": ("106", "6")})[0].tier == "backend"
    assert replay.hold(180, {"AAAUSDT": ("106", "11")}) == []
    assert not any(group.pending for group in replay.state.groups.values())
    replay.tick({"AAAUSDT": ("110", "11")})
    assert replay.hold(1500, {"AAAUSDT": ("110", "11")}) == []
    promoted = replay.hold(150, {"AAAUSDT": ("110", "11")})
    assert len(promoted) == 1 and promoted[0].tier == "live"
    assert replay.hold(1800, {"AAAUSDT": ("110", "11")}) == []


def test_falling_prices_repeat_only_at_five_percent_lower_price():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("90", "-11")})
    assert len(replay.hold(180, {"AAAUSDT": ("90", "-11")})) == 1
    assert replay.hold(1800, {"AAAUSDT": ("89", "-15")}) == []
    replay.tick({"AAAUSDT": ("85.5", "-15")})
    alerts = replay.hold(180, {"AAAUSDT": ("85.5", "-15")})
    assert len(alerts) == 1 and "下跌15.0%" in alerts[0].content


def test_expired_lock_rebases_silently_instead_of_resending_current_move():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    replay.hold(180, {"AAAUSDT": ("110", "11")})
    replay.now += PRICE_LOCK_SECONDS
    assert replay.tick({"AAAUSDT": ("120", "20")}) == []
    assert replay.hold(180, {"AAAUSDT": ("120", "20")}) == []
    replay.tick({"AAAUSDT": ("126", "25")})
    assert len(replay.hold(180, {"AAAUSDT": ("126", "25")})) == 1


def test_new_one_hour_window_is_silent_and_both_windows_share_price_lock():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    assert replay.tick({"AAAUSDT": ("100", "0", "11")}) == []
    assert replay.hold(180, {"AAAUSDT": ("100", "0", "11")}) == []
    replay.tick({"AAAUSDT": ("111", "11", "12")})
    alerts = replay.hold(180, {"AAAUSDT": ("111", "11", "12")})
    assert len(alerts) == 1
    assert len(alerts[0].members) == 1
    assert set(alerts[0].members[0].changes) == {"1h", "24h"}
    assert replay.hold(1800, {"AAAUSDT": ("111", "12", "13")}) == []


def test_missing_quote_at_dispatch_drops_candidate():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    assert replay.hold(180, {}) == []
    assert not any(group.pending for group in replay.state.groups.values())
    assert replay.tick({"AAAUSDT": ("110", "11")}) == []


def test_restart_clears_pending_but_preserves_price_lock_and_market_schedule(tmp_path: Path):
    database = tmp_path / "stock-market.sqlite"
    push = _FakePushClient()
    service, store = _service(database, push)
    instrument = StockInstrument("AAAUSDT", "us", 2)
    start = 1_700_000_000
    store.sync_instruments([instrument], now=start)

    def tick(worker, offset, price, change):
        return worker._process_quotes([instrument], {
            instrument.symbol: StockQuote(instrument.symbol, Decimal(price), Decimal(change), start + offset),
        }, now=start + offset, started=time.monotonic())

    tick(service, 0, "100", "0")
    tick(service, 30, "110", "11")
    reloaded, _ = _service(database, push)
    assert tick(reloaded, 60, "110", "11")["alert_count"] == 0
    assert reloaded.store.dashboard()["pending_groups"] == []
    # The restart consumed the old move; a further real 5% creates a new candidate.
    tick(reloaded, 90, "104", "4")
    tick(reloaded, 120, "115.5", "16")
    for offset in range(150, 301, 30):
        tick(reloaded, offset, "115.5", "16")
    assert len(push.calls) == 1
    restarted, _ = _service(database, push)
    tick(restarted, 330, "114", "9.9")
    for offset in range(360, 2401, 30):
        tick(restarted, offset, "115.5", "10.1")
    assert len(push.calls) == 1
    assert restarted.store.dashboard()["contracts"][0]["price_locks"]["up"]["anchor_price"] == "115.5"


def test_failed_push_is_consumed_and_not_retried(tmp_path: Path):
    class _FailingPush(_FakePushClient):
        def push(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("request outcome unknown")

    push = _FailingPush()
    service, store = _service(tmp_path / "stock-market.sqlite", push)
    instrument = StockInstrument("AAAUSDT", "us", 2)
    start = 1_700_000_000
    store.sync_instruments([instrument], now=start)
    for offset in range(0, 601, 30):
        price, change = ("100", "0") if offset == 0 else ("110", "11")
        service._process_quotes([instrument], {
            instrument.symbol: StockQuote(instrument.symbol, Decimal(price), Decimal(change), start + offset),
        }, now=start + offset, started=time.monotonic())
    assert len(push.calls) == 1
    assert store.dashboard()["recent_events"][0]["status"] == "push_failed"


def test_one_large_outlier_or_missing_quotes_cannot_claim_broad_market_move():
    instruments = [StockInstrument(f"A{i}USDT", "us", 2) for i in range(10)]
    replay = _Replay(instruments)
    replay.tick({item.symbol: ("100", "0") for item in instruments})
    # All quotes are fresh, but only two of ten have moved: the median is zero.
    values = {item.symbol: ("200", "100") if i < 2 else ("100", "0") for i, item in enumerate(instruments)}
    replay.tick(values)
    alert = replay.hold(180, values)[0]
    assert "多只" in alert.content and "普涨" not in alert.content

    replay = _Replay(instruments)
    replay.tick({item.symbol: ("100", "0") for item in instruments})
    values = {instruments[i].symbol: ("111", "11") for i in range(2)}
    replay.tick(values)
    alert = replay.hold(180, values)[0]
    assert "普涨" not in alert.content  # 100% up among only 20% coverage.


def test_new_assets_wait_for_existing_market_followup_instead_of_individual_dispatch():
    replay = _Replay([StockInstrument("AAAUSDT", "us", 2), StockInstrument("BBBUSDT", "us", 2)])
    baseline = {"AAAUSDT": ("100", "0"), "BBBUSDT": ("100", "0")}
    first = {"AAAUSDT": ("111", "11"), "BBBUSDT": ("100", "0")}
    replay.tick(baseline)
    replay.tick(first)
    assert len(replay.hold(180, first)) == 1
    both = {"AAAUSDT": ("111", "11"), "BBBUSDT": ("111", "11")}
    replay.tick(both)
    assert replay.hold(1740, both) == []
    alerts = replay.hold(60, both)
    assert len(alerts) == 1
    assert [member.instrument.asset_name for member in alerts[0].members] == ["BBB"]


def test_legacy_events_survive_schema_upgrade_without_becoming_pending(tmp_path: Path):
    database = tmp_path / "legacy.sqlite"
    with sqlite3.connect(database) as conn:
        conn.executescript('''
            CREATE TABLE trigger_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL, market TEXT NOT NULL, window TEXT NOT NULL,
                tier TEXT NOT NULL, direction TEXT NOT NULL, change_percent TEXT NOT NULL,
                current_price TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, error TEXT, observed_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
            );
            INSERT INTO trigger_events VALUES(
                1,'old','AAAUSDT','us','24h','live','up','11','111',
                'legacy title','legacy content','published',NULL,1700000000,1700000000,1700000000
            );
        ''')
    store = StockMarketStore(database)
    store.initialize()
    store.initialize()
    dashboard = store.dashboard()
    assert dashboard["recent_events"][0]["content"] == "legacy content"
    assert dashboard["recent_events"][0]["members"] == []
    assert dashboard["pending_groups"] == []
    assert store.broadcast_state().assets == {}


def test_same_event_key_is_claimed_only_once_before_push(tmp_path: Path):
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    alerts = replay.hold(180, {"AAAUSDT": ("110", "11")})
    store = StockMarketStore(tmp_path / "claim.sqlite")
    store.initialize()
    arguments = dict(quotes={}, changes_1h={}, state=replay.state, alerts=alerts,
                     errors={}, now=replay.now, startup_baseline=False)
    assert len(store.record_cycle(**arguments)) == 1
    assert store.record_cycle(**arguments) == []
    assert len(store.dashboard()["recent_events"]) == 1


def test_restart_does_not_replay_a_price_jump_one_poll_after_silent_baseline():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    replay.hold(180, {"AAAUSDT": ("110", "11")})
    assert replay.tick({"AAAUSDT": ("130", "30")}, restart=True) == []
    assert replay.hold(1800, {"AAAUSDT": ("130", "30")}) == []
    replay.tick({"AAAUSDT": ("136.5", "35")})
    assert len(replay.hold(180, {"AAAUSDT": ("136.5", "35")})) == 1


def test_restart_pullback_keeps_the_prior_price_barrier():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    replay.tick({"AAAUSDT": ("110", "11")})
    replay.hold(180, {"AAAUSDT": ("110", "11")})
    replay.tick({"AAAUSDT": ("100", "5")}, restart=True)
    assert replay.hold(1800, {"AAAUSDT": ("110", "11")}) == []
    assert replay.state.assets["AAAUSDT"].locks["up"].anchor_price == Decimal("110")



def test_resume_gate_blocks_historical_24h_move_until_real_five_then_ten_percent():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "20")}, restart=True)
    assert replay.hold(180, {"AAAUSDT": ("100.1", "25")}) == []
    assert replay.hold(180, {"AAAUSDT": ("104.99", "25")}) == []
    assert replay.tick({"AAAUSDT": ("105", "25")}) == []
    backend = replay.hold(180, {"AAAUSDT": ("105", "25")})
    assert len(backend) == 1 and backend[0].tier == "backend"
    assert replay.hold(1800, {"AAAUSDT": ("109.99", "25")}) == []
    replay.tick({"AAAUSDT": ("110", "25")})
    live = replay.hold(180, {"AAAUSDT": ("110", "25")})
    assert len(live) == 1 and live[0].tier == "live"


def test_resume_gate_is_symmetric_and_still_requires_rolling_window_threshold():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "-20")})
    assert replay.hold(180, {"AAAUSDT": ("95.01", "-30")}) == []
    replay.tick({"AAAUSDT": ("95", "-30")})
    backend = replay.hold(180, {"AAAUSDT": ("95", "-30")})
    assert len(backend) == 1 and backend[0].tier == "backend"
    assert replay.hold(1800, {"AAAUSDT": ("90.26", "-30")}) == []
    replay.tick({"AAAUSDT": ("90", "-30")})
    live = replay.hold(180, {"AAAUSDT": ("90", "-30")})
    assert len(live) == 1 and live[0].tier == "live"

    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "0")})
    assert replay.hold(180, {"AAAUSDT": ("110", "4.9")}) == []
    replay.tick({"AAAUSDT": ("110", "4.9", "11")})
    # First observation of 1H is silent, the next observed move can qualify.
    replay.tick({"AAAUSDT": ("111", "4.9", "12")})
    live = replay.hold(180, {"AAAUSDT": ("111", "4.9", "12")})
    assert len(live) == 1 and live[0].tier == "live"
    assert live[0].members[0].primary_window == "1h"


def test_resume_gate_is_rechecked_before_dispatch_and_survives_six_hour_rebase():
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "20")})
    replay.tick({"AAAUSDT": ("110", "25")})
    alerts = replay.hold(180, {"AAAUSDT": ("107", "25")})
    assert len(alerts) == 1 and alerts[0].tier == "backend"

    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "20")})
    replay.tick({"AAAUSDT": ("110", "25")})
    assert replay.hold(180, {"AAAUSDT": ("104", "25")}) == []
    replay.hold(PRICE_LOCK_SECONDS, {"AAAUSDT": ("100", "20")})
    # Expiring the six-hour repeat lock never expires the recovery-price gate.
    assert replay.hold(180, {"AAAUSDT": ("101", "25")}) == []
    assert replay.state.assets["AAAUSDT"].resume_price == Decimal("100")


def test_restart_replaces_recovery_reference_and_persists_it_in_sqlite(tmp_path: Path):
    replay = _Replay()
    replay.tick({"AAAUSDT": ("100", "20")})
    replay.tick({"AAAUSDT": ("110", "25")}, restart=True)
    assert replay.hold(180, {"AAAUSDT": ("115.49", "30")}) == []
    replay.tick({"AAAUSDT": ("115.5", "30")})
    backend = replay.hold(180, {"AAAUSDT": ("115.5", "30")})
    assert len(backend) == 1 and backend[0].tier == "backend"
    store = StockMarketStore(tmp_path / "resume.sqlite")
    store.initialize()
    store.record_cycle(quotes={}, changes_1h={}, state=replay.state, alerts=backend,
                       errors={}, now=replay.now, startup_baseline=False)
    restored = store.broadcast_state().assets["AAAUSDT"]
    assert restored.resume_price == Decimal("110")
    assert restored.resumed_at == 1_700_000_030
