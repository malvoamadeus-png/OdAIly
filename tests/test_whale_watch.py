from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from packages.common.config import WhaleWatchHyperliquidSettings, WhaleWatchSettings
from packages.whale_watch.chains import CHAIN_REGISTRY
from packages.whale_watch.detector import detect_activity, normalize_evm_address
from packages.whale_watch.hyperliquid_detector import detect_hyperliquid_activity
from packages.whale_watch.models import ChainState, HyperliquidAddress, HyperliquidRuntimeSettings, HyperliquidState, WhaleAddress
from packages.whale_watch.hyperliquid_worker import WhaleWatchHyperliquidWorker
from packages.whale_watch.worker import WhaleWatchWorker
from packages.x_processing.telegram import TelegramResult


WATCHED = "0xB5ebEd2830f0fd89Af32327b4cb573FaD4362b5a"
WATCHED_LOWER = WATCHED.lower()
TX_HASH = "0x02a8e8ebae7c89afe6711be1b12f19d5e63107039409f8ff0cd5d4f80859bb87"
HYPER_WATCHED = "0x2fc3195efbf91ad90854bc3c02fe739895c23460"
HYPER_WATCHED_LOWER = HYPER_WATCHED.lower()


def test_normalize_evm_address_accepts_valid_and_rejects_invalid() -> None:
    assert normalize_evm_address(WATCHED) == WATCHED

    with pytest.raises(ValueError):
        normalize_evm_address("b5eb")

    with pytest.raises(ValueError):
        normalize_evm_address("0xnot-an-address")


def test_detects_base_uniswap_swap_from_native_out_and_token_in() -> None:
    whale = WhaleAddress(id=1, address=WATCHED, address_lower=WATCHED_LOWER, label="测试巨鲸")

    activity = detect_activity(
        chain=CHAIN_REGISTRY["base"],
        whale=whale,
        tx=base_swap_tx(),
        token_transfers=[base_pitch_transfer()],
    )

    assert activity is not None
    assert activity.kind == "swap"
    assert activity.asset_out is not None
    assert activity.asset_out.symbol == "ETH"
    assert activity.asset_out.amount == Decimal("0.1")
    assert activity.asset_in is not None
    assert activity.asset_in.symbol == "PITCH"
    assert "0.1 ETH" in activity.telegram_text
    assert "43.85423291942851882 PITCH" in activity.telegram_text


def test_worker_first_run_seeds_without_telegram() -> None:
    repo = FakeWhaleRepository()
    client = FakeBlockscoutClient()
    telegram = FakeTelegramClient()
    worker = WhaleWatchWorker(
        repository=repo,
        settings=WhaleWatchSettings(chain_keys=("base",)),
        client=client,
        telegram_client=telegram,
        worker_id="test",
    )

    result = worker.run_once()

    assert result.seeded_pairs == 1
    assert result.inserted == 0
    assert telegram.messages == []
    assert repo.states[(1, "base")].seeded_at is not None
    assert repo.states[(1, "base")].last_seen_block == 46452941


def test_worker_sends_new_activity_once_after_seed() -> None:
    repo = FakeWhaleRepository()
    repo.states[(1, "base")] = ChainState(address_id=1, chain_key="base", seeded_at=datetime.now(UTC), last_seen_block=46452940)
    client = FakeBlockscoutClient()
    telegram = FakeTelegramClient()
    worker = WhaleWatchWorker(
        repository=repo,
        settings=WhaleWatchSettings(chain_keys=("base",)),
        client=client,
        telegram_client=telegram,
        worker_id="test",
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.detected == 1
    assert first.inserted == 1
    assert first.sent == 1
    assert second.detected == 0
    assert second.inserted == 0
    assert len(telegram.messages) == 1
    assert TX_HASH in repo.telegram_results


def test_detect_hyperliquid_open_long_activity() -> None:
    whale = HyperliquidAddress(id=1, address=HYPER_WATCHED, address_lower=HYPER_WATCHED_LOWER, label="Hyper test")

    activity = detect_hyperliquid_activity(whale=whale, fill=hyper_fill())

    assert activity is not None
    assert activity.direction == "Open Long"
    assert activity.coin == "BTC"
    assert activity.notional_usd == Decimal("791412.51922999987952")
    assert "Hyperliquid" in activity.telegram_text
    assert "开多" in activity.telegram_text


def test_hyperliquid_worker_first_run_seeds_without_telegram() -> None:
    repo = FakeHyperliquidRepository()
    client = FakeHyperliquidClient([hyper_fill()])
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(),
        client=client,
        telegram_client=telegram,
        worker_id="test-hl",
    )

    result = worker.run_once()

    assert result.seeded == 1
    assert result.inserted == 0
    assert telegram.messages == []
    assert repo.states[1].seeded_at is not None
    assert repo.states[1].last_seen_time == 1779704646349


def test_hyperliquid_worker_does_not_replay_latest_historical_fill_after_seed() -> None:
    repo = FakeHyperliquidRepository()
    client = FakeHyperliquidClient([hyper_fill()])
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(),
        client=client,
        telegram_client=telegram,
        worker_id="test-hl-seed-boundary",
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.seeded == 1
    assert second.detected == 0
    assert second.inserted == 0
    assert second.sent == 0
    assert telegram.messages == []


def test_hyperliquid_worker_sends_large_activity_once_and_skips_small() -> None:
    repo = FakeHyperliquidRepository()
    repo.states[1] = HyperliquidState(address_id=1, seeded_at=datetime.now(UTC), last_seen_time=1779701000000)
    client = FakeHyperliquidClient([small_hyper_fill(), hyper_fill()])
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(
            single_fill_min_notional_usd=50000,
            aggregate_min_notional_usd=1000000,
            aggregate_window_seconds=600,
        ),
        client=client,
        telegram_client=telegram,
        worker_id="test-hl",
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.detected == 2
    assert first.inserted == 1
    assert first.sent == 1
    assert first.suppressed == 1
    assert second.inserted == 0
    assert len(telegram.messages) == 1
    assert "fill:" in next(iter(repo.telegram_results))


def test_hyperliquid_worker_sends_aggregate_alert_when_small_fills_cross_window_threshold() -> None:
    repo = FakeHyperliquidRepository()
    repo.runtime_settings = HyperliquidRuntimeSettings(
        single_fill_min_notional_usd=Decimal("500000"),
        aggregate_min_notional_usd=Decimal("1000000"),
        aggregate_window_seconds=600,
    )
    repo.states[1] = HyperliquidState(address_id=1, seeded_at=datetime.now(UTC), last_seen_time=1779701000000)
    client = FakeHyperliquidClient([aggregate_small_fill_one(), aggregate_small_fill_two(), aggregate_small_fill_three()])
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(),
        client=client,
        telegram_client=telegram,
        worker_id="test-hl-aggregate",
    )

    result = worker.run_once()

    assert result.detected == 3
    assert result.inserted == 1
    assert result.sent == 1
    assert "累计开平仓 3 笔" in telegram.messages[0]
    assert "已超过聚合门槛" in telegram.messages[0]


def test_hyperliquid_worker_does_not_repeat_aggregate_alert_while_window_stays_above_threshold() -> None:
    repo = FakeHyperliquidRepository()
    repo.runtime_settings = HyperliquidRuntimeSettings(
        single_fill_min_notional_usd=Decimal("500000"),
        aggregate_min_notional_usd=Decimal("1000000"),
        aggregate_window_seconds=600,
    )
    repo.states[1] = HyperliquidState(address_id=1, seeded_at=datetime.now(UTC), last_seen_time=1779701000000)
    client = FakeHyperliquidClient([aggregate_small_fill_one(), aggregate_small_fill_two(), aggregate_small_fill_three()])
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(),
        client=client,
        telegram_client=telegram,
        worker_id="test-hl-aggregate-repeat",
    )

    first = worker.run_once()
    second = worker.run_once()

    assert first.inserted == 1
    assert second.inserted == 0
    assert len(telegram.messages) == 1


def test_hyperliquid_worker_can_rearm_aggregate_alert_after_window_drops_below_threshold() -> None:
    repo = FakeHyperliquidRepository()
    repo.runtime_settings = HyperliquidRuntimeSettings(
        single_fill_min_notional_usd=Decimal("500000"),
        aggregate_min_notional_usd=Decimal("1000000"),
        aggregate_window_seconds=600,
    )
    repo.states[1] = HyperliquidState(address_id=1, seeded_at=datetime.now(UTC), last_seen_time=1779701000000)
    telegram = FakeTelegramClient()
    worker = WhaleWatchHyperliquidWorker(
        repository=repo,
        settings=WhaleWatchHyperliquidSettings(),
        client=FakeHyperliquidClient([aggregate_small_fill_one(), aggregate_small_fill_two(), aggregate_small_fill_three()]),
        telegram_client=telegram,
        worker_id="test-hl-aggregate-rearm",
    )

    first = worker.run_once()
    worker.client = FakeHyperliquidClient(
        [aggregate_small_fill_one(), aggregate_small_fill_two(), aggregate_small_fill_three(), aggregate_far_late_fill()]
    )
    second = worker.run_once()
    worker.client = FakeHyperliquidClient(
        [
            aggregate_small_fill_one(),
            aggregate_small_fill_two(),
            aggregate_small_fill_three(),
            aggregate_far_late_fill(),
            aggregate_far_late_fill_two(),
        ]
    )
    third = worker.run_once()

    assert first.inserted == 1
    assert second.inserted == 0
    assert third.inserted == 1
    assert len(telegram.messages) == 2


def base_swap_tx() -> dict[str, Any]:
    return {
        "hash": TX_HASH,
        "block_number": 46452941,
        "timestamp": "2026-05-25T07:47:09.000000Z",
        "value": "100000000000000000",
        "method": "execute",
        "from": {"hash": WATCHED},
        "to": {"hash": "0xFdf682F51FE81Aa4898F0AE2163d8A55c127fbC7", "name": "UniversalRouter"},
    }


def base_pitch_transfer() -> dict[str, Any]:
    return {
        "transaction_hash": TX_HASH,
        "block_number": 46452941,
        "from": {
            "hash": "0x498581fF718922c3f8e6A244956aF099B2652b2b",
            "name": "PoolManager",
            "metadata": {"tags": [{"name": "Uniswap V4: Pool Manager"}]},
        },
        "to": {"hash": WATCHED},
        "token": {
            "address_hash": "0xeaE13ea73BEc936664A51734c8c01ec7c3B0699C",
            "symbol": "PITCH",
            "decimals": "18",
        },
        "total": {"decimals": "18", "value": "43854232919428518820"},
    }


def hyper_fill() -> dict[str, Any]:
    return {
        "coin": "BTC",
        "px": "77525.314272308",
        "sz": "10.20844",
        "side": "B",
        "time": 1779704646349,
        "startPosition": "120.46291",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0x2aafc315d7e552302c29043c321a73018700dafb72e87102ce786e6896e92c1a",
        "oid": 441444013037,
        "tid": 761521047376038,
    }


def small_hyper_fill() -> dict[str, Any]:
    return {
        "coin": "ETH",
        "px": "2500",
        "sz": "10",
        "side": "B",
        "time": 1779704700000,
        "startPosition": "0.0",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xsmall",
        "oid": 111,
        "tid": 222,
    }


def aggregate_small_fill_one() -> dict[str, Any]:
    return {
        "coin": "ETH",
        "px": "2500",
        "sz": "180",
        "side": "B",
        "time": 1779704600000,
        "startPosition": "0.0",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xaggregate1",
        "oid": 211,
        "tid": 311,
    }


def aggregate_small_fill_two() -> dict[str, Any]:
    return {
        "coin": "ETH",
        "px": "2600",
        "sz": "170",
        "side": "S",
        "time": 1779704660000,
        "startPosition": "200",
        "dir": "Close Long",
        "closedPnl": "1200.0",
        "hash": "0xaggregate2",
        "oid": 212,
        "tid": 312,
    }


def aggregate_small_fill_three() -> dict[str, Any]:
    return {
        "coin": "SOL",
        "px": "180",
        "sz": "1500",
        "side": "B",
        "time": 1779704720000,
        "startPosition": "0.0",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xaggregate3",
        "oid": 213,
        "tid": 313,
    }


def aggregate_far_late_fill() -> dict[str, Any]:
    return {
        "coin": "ARB",
        "px": "1.2",
        "sz": "300000",
        "side": "B",
        "time": 1779705600000,
        "startPosition": "0.0",
        "dir": "Open Long",
        "closedPnl": "0.0",
        "hash": "0xaggregate4",
        "oid": 214,
        "tid": 314,
    }


def aggregate_far_late_fill_two() -> dict[str, Any]:
    return {
        "coin": "ARB",
        "px": "1.3",
        "sz": "500000",
        "side": "S",
        "time": 1779705660000,
        "startPosition": "300000",
        "dir": "Close Long",
        "closedPnl": "23000.0",
        "hash": "0xaggregate5",
        "oid": 215,
        "tid": 315,
    }


class FakeBlockscoutClient:
    def list_address_transactions(self, chain, address: str) -> list[dict[str, Any]]:
        return [base_swap_tx()]

    def list_address_token_transfers(self, chain, address: str) -> list[dict[str, Any]]:
        return [base_pitch_transfer()]

    def get_transaction(self, chain, tx_hash: str) -> dict[str, Any]:
        return base_swap_tx()


class FakeTelegramClient:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, text: str, **_kwargs) -> TelegramResult:
        self.messages.append(text)
        return TelegramResult(ok=True, status_code=200, response_json={"ok": True, "result": {"message_id": 1}})


class FakeWhaleRepository:
    def __init__(self) -> None:
        self.addresses = [WhaleAddress(id=1, address=WATCHED, address_lower=WATCHED_LOWER, label="测试巨鲸")]
        self.states: dict[tuple[int, str], ChainState] = {}
        self.activities: set[tuple[int, str, str, str]] = set()
        self.telegram_results: dict[str, dict[str, Any]] = {}

    def list_addresses(self, *, include_disabled: bool = False) -> list[WhaleAddress]:
        return self.addresses

    def get_chain_state(self, *, address_id: int, chain_key: str) -> ChainState | None:
        return self.states.get((address_id, chain_key))

    def mark_chain_seeded(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        self.states[(address_id, chain_key)] = ChainState(
            address_id=address_id,
            chain_key=chain_key,
            seeded_at=polled_at,
            last_polled_at=polled_at,
            last_success_at=polled_at,
            last_seen_block=block_number,
        )

    def record_chain_success(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        self.states[(address_id, chain_key)] = ChainState(
            address_id=address_id,
            chain_key=chain_key,
            seeded_at=(self.states.get((address_id, chain_key)) or ChainState(address_id, chain_key)).seeded_at,
            last_polled_at=polled_at,
            last_success_at=polled_at,
            last_seen_block=block_number,
        )

    def record_chain_error(self, *, address_id: int, chain_key: str, error: str, polled_at: datetime) -> None:
        raise AssertionError(error)

    def save_activity(self, *, whale: WhaleAddress, chain_key: str, activity) -> bool:
        key = (whale.id, chain_key, activity.tx_hash, activity.fingerprint)
        if key in self.activities:
            return False
        self.activities.add(key)
        return True

    def update_activity_telegram_result(self, *, tx_hash: str, fingerprint: str, telegram_result: dict[str, Any]) -> None:
        self.telegram_results[tx_hash] = telegram_result

    def record_worker_heartbeat(self, **_kwargs) -> None:
        return None


class FakeHyperliquidClient:
    def __init__(self, fills: list[dict[str, Any]]) -> None:
        self.fills = fills

    def user_fills(self, address: str) -> list[dict[str, Any]]:
        return list(self.fills)


class FakeHyperliquidRepository:
    def __init__(self) -> None:
        self.addresses = [HyperliquidAddress(id=1, address=HYPER_WATCHED, address_lower=HYPER_WATCHED_LOWER, label="Hyper test")]
        self.states: dict[int, HyperliquidState] = {}
        self.activities: set[tuple[int, str]] = set()
        self.telegram_results: dict[str, dict[str, Any]] = {}
        self.runtime_settings = HyperliquidRuntimeSettings(
            single_fill_min_notional_usd=Decimal("500000"),
            aggregate_min_notional_usd=Decimal("1000000"),
            aggregate_window_seconds=600,
        )

    def get_runtime_settings(
        self,
        *,
        default_single_fill_min_notional_usd: Decimal,
        default_aggregate_min_notional_usd: Decimal,
        default_aggregate_window_seconds: int,
    ) -> HyperliquidRuntimeSettings:
        return self.runtime_settings

    def list_addresses(self, *, include_disabled: bool = False) -> list[HyperliquidAddress]:
        return self.addresses

    def get_state(self, *, address_id: int) -> HyperliquidState | None:
        return self.states.get(address_id)

    def mark_seeded(
        self,
        *,
        address_id: int,
        last_seen_time: int | None,
        polled_at: datetime,
        aggregate_window_entries=None,
        aggregate_alert_active: bool = False,
    ) -> None:
        self.states[address_id] = HyperliquidState(
            address_id=address_id,
            seeded_at=polled_at,
            last_polled_at=polled_at,
            last_success_at=polled_at,
            last_seen_time=last_seen_time,
            aggregate_window_entries=tuple(aggregate_window_entries or []),
            aggregate_alert_active=aggregate_alert_active,
        )

    def record_success(
        self,
        *,
        address_id: int,
        last_seen_time: int | None,
        polled_at: datetime,
        aggregate_window_entries=None,
        aggregate_alert_active: bool | None = None,
    ) -> None:
        current = self.states.get(address_id) or HyperliquidState(address_id=address_id)
        self.states[address_id] = HyperliquidState(
            address_id=address_id,
            seeded_at=current.seeded_at,
            last_polled_at=polled_at,
            last_success_at=polled_at,
            last_seen_time=last_seen_time,
            aggregate_window_entries=tuple(aggregate_window_entries if aggregate_window_entries is not None else current.aggregate_window_entries),
            aggregate_alert_active=current.aggregate_alert_active if aggregate_alert_active is None else aggregate_alert_active,
        )

    def record_error(self, *, address_id: int, error: str, polled_at: datetime) -> None:
        raise AssertionError(error)

    def save_activity(self, *, whale: HyperliquidAddress, activity) -> bool:
        key = (whale.id, activity.fill_key)
        if key in self.activities:
            return False
        self.activities.add(key)
        return True

    def update_activity_telegram_result(self, *, fill_key: str, telegram_result: dict[str, Any]) -> None:
        self.telegram_results[f"fill:{fill_key}"] = telegram_result

    def record_worker_heartbeat(self, **_kwargs) -> None:
        return None
