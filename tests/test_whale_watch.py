from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from packages.common.config import WhaleWatchSettings
from packages.whale_watch.chains import CHAIN_REGISTRY
from packages.whale_watch.detector import detect_activity, normalize_evm_address
from packages.whale_watch.models import ChainState, WhaleAddress
from packages.whale_watch.worker import WhaleWatchWorker
from packages.x_processing.telegram import TelegramResult


WATCHED = "0xB5ebEd2830f0fd89Af32327b4cb573FaD4362b5a"
WATCHED_LOWER = WATCHED.lower()
TX_HASH = "0x02a8e8ebae7c89afe6711be1b12f19d5e63107039409f8ff0cd5d4f80859bb87"


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
