from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from packages.gate_market_broadcast.copy import render_brief
from packages.gate_market_broadcast.defaults import DEFAULT_TEMPLATES
from packages.gate_market_broadcast.models import (
    ReferenceMetrics,
    SymbolConfig,
    TickerQuote,
)
from packages.gate_market_broadcast.service import GateMarketBroadcastService
from packages.gate_market_broadcast.settings import GateMarketSettings
from packages.gate_market_broadcast.state_machine import advance_state
from packages.gate_market_broadcast.store import GateMarketStore
from packages.publisher import PushResult


def test_default_store_uses_confirmed_thresholds(tmp_path: Path) -> None:
    store = GateMarketStore(tmp_path / "gate-market.sqlite")
    store.initialize()

    assert store.get_mode() == "backend"
    assert {
        item.symbol: item.threshold_text for item in store.list_symbol_configs()
    } == {
        "EUSTX50": "100",
        "UK100": "150",
        "GER40": "400",
        "XBRUSD": "2",
        "USDJPY": "0.5",
        "USDCNH": "0.010",
        "XAUUSD": "50",
        "XAGUSD": "3",
    }


def test_boundary_requires_adjacent_level_before_return_trigger() -> None:
    step = Decimal("1")
    trigger, disarmed = advance_state(
        previous_price=Decimal("80"),
        current_price=Decimal("81.2"),
        step=step,
        disarmed_levels=set(),
    )
    assert trigger is not None and trigger.level == Decimal("81")

    trigger, disarmed = advance_state(
        previous_price=Decimal("81.2"),
        current_price=Decimal("80.9"),
        step=step,
        disarmed_levels=disarmed,
    )
    assert trigger is None

    trigger, disarmed = advance_state(
        previous_price=Decimal("80.9"),
        current_price=Decimal("82"),
        step=step,
        disarmed_levels=disarmed,
    )
    assert trigger is not None and trigger.level == Decimal("82")

    trigger, disarmed = advance_state(
        previous_price=Decimal("82"),
        current_price=Decimal("80.9"),
        step=step,
        disarmed_levels=disarmed,
    )
    assert trigger is not None and trigger.level == Decimal("81")


def test_multiple_crossed_levels_emit_only_furthest_level() -> None:
    trigger, disarmed = advance_state(
        previous_price=Decimal("80"),
        current_price=Decimal("83.2"),
        step=Decimal("1"),
        disarmed_levels=set(),
    )
    assert trigger is not None
    assert trigger.level == Decimal("83")
    assert 83 in disarmed


def test_copy_uses_full_unit_and_one_step_high_tolerance() -> None:
    config = SymbolConfig(
        symbol="XBRUSD",
        display_name="布伦特原油",
        threshold_text="2",
        price_precision=2,
        unit="美元/桶",
    )
    templates = {key: (title, body) for key, _label, title, body in DEFAULT_TEMPLATES}
    brief = render_brief(
        config=config,
        current_price=Decimal("85.1234"),
        trigger_price=Decimal("84"),
        metrics=ReferenceMetrics(
            reference_kind="rolling_24h",
            reference_price=Decimal("80"),
            high=Decimal("85.9"),
            low=Decimal("79"),
        ),
        templates=templates,
    )

    assert brief is not None
    assert brief.template_key == "breakout"
    assert brief.title == "布伦特原油上涨突破84美元/桶，24小时上涨6.4%"
    assert "现报 85.12 美元/桶" in brief.content
    assert "24 小时涨幅 6.4%" in brief.content


def test_copy_suppresses_change_that_rounds_to_zero() -> None:
    config = SymbolConfig("USDCNH", "美元兑人民币", "0.010", 5, "")
    templates = {key: (title, body) for key, _label, title, body in DEFAULT_TEMPLATES}
    assert (
        render_brief(
            config=config,
            current_price=Decimal("7.002"),
            trigger_price=Decimal("7.000"),
            metrics=ReferenceMetrics(
                reference_kind="rolling_24h",
                reference_price=Decimal("7.000"),
                high=Decimal("7.002"),
                low=Decimal("6.99"),
            ),
            templates=templates,
        )
        is None
    )


class _FakeClient:
    def __init__(self, quotes: list[TickerQuote]) -> None:
        self.quotes = quotes

    def fetch_ticker(self, symbol: str) -> TickerQuote:
        quote = self.quotes.pop(0)
        assert quote.symbol == symbol
        return quote

    def fetch_history(self, symbol: str, *, start_time: int, end_time: int):
        del symbol, start_time, end_time
        return []


class _FakePushClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def push(self, **kwargs) -> PushResult:
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text="ok")


class _FakeTelegram:
    def send_message(self, text: str):
        del text
        return type("Result", (), {"ok": True, "error": None})()


def _quote(symbol: str, price: str, timestamp: int) -> TickerQuote:
    value = Decimal(price)
    return TickerQuote(
        symbol=symbol,
        price=value,
        observed_at=timestamp,
        status="open",
        high=value,
        low=Decimal("79"),
        today_open=Decimal("80"),
        previous_close=Decimal("80"),
    )


def test_backend_and_live_modes_share_trigger_state_but_keep_publish_flags(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "gate-market.sqlite"
    store = GateMarketStore(database_path)
    store.initialize()
    # Isolate this service test to one symbol.
    with store._connect() as conn:
        conn.execute("UPDATE symbol_config SET enabled=(symbol='XBRUSD')")
    base = 1_800_000_000
    store.add_samples(
        "XBRUSD",
        [(base - 86400 - index * 60, Decimal("80")) for index in range(61)],
    )
    fake_push = _FakePushClient()
    service = GateMarketBroadcastService(
        settings=GateMarketSettings(
            database_path=database_path,
            gate_api_base="https://example.invalid",
            push_endpoint="https://example.invalid/push",
            request_timeout_seconds=1,
            gate_max_attempts=1,
            telegram_bot_token=None,
            telegram_chat_id=None,
            telegram_message_thread_id=None,
            telegram_timeout_seconds=1,
            alert_dedup_minutes=30,
            disk_free_alert_mb=1,
        ),
        store=store,
        client=_FakeClient(
            [
                _quote("XBRUSD", "80", base),
                _quote("XBRUSD", "82.1", base + 60),
                _quote("XBRUSD", "81.9", base + 120),
                _quote("XBRUSD", "84.1", base + 180),
            ]
        ),
        push_client=fake_push,
        telegram_client=_FakeTelegram(),
    )
    config = store.get_symbol_config("XBRUSD")

    assert service.process_symbol(config)["status"] == "initialized"
    assert service.process_symbol(config)["status"] == "backend_created"
    store.set_mode("live")
    # Crossing the same 82 line in the opposite direction after a mode switch
    # must not create a second brief. Publish mode is not market state.
    assert service.process_symbol(config)["status"] == "no_trigger"
    assert service.process_symbol(config)["status"] == "published"

    assert [call["is_publish"] for call in fake_push.calls] == [False, True]
    assert all(call["is_push"] is False and call["dry_run"] is False for call in fake_push.calls)


def test_initialize_migrates_legacy_mode_states_and_unions_disarmed_levels(
    tmp_path: Path,
) -> None:
    store = GateMarketStore(tmp_path / "gate-market.sqlite")
    store.initialize()
    with store._connect() as conn:
        conn.execute("DELETE FROM symbol_state")
        conn.execute(
            """
            INSERT INTO symbol_state(
                symbol,mode,initialized,last_price,last_quote_at,disarmed_levels,
                market_status,last_success_at,updated_at
            ) VALUES
                ('XAGUSD','live',1,'57.021',100,'[19]','open',100,100),
                ('XAGUSD','backend',1,'57.010',200,'[20]','open',200,200)
            """
        )

    store.initialize()

    state = store.get_state("XAGUSD", "backend")
    assert state.last_price == Decimal("57.010")
    assert state.disarmed_levels == {19, 20}
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT mode FROM symbol_state WHERE symbol='XAGUSD'"
        ).fetchall()
    assert [row["mode"] for row in rows] == ["shared"]
