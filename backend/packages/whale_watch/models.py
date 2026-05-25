from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal


ActivityKind = Literal["transfer", "swap"]
HyperliquidDirection = Literal["Open Long", "Open Short", "Close Long", "Close Short"]
Direction = Literal["in", "out"]


@dataclass(frozen=True, slots=True)
class WhaleAddress:
    id: int
    address: str
    address_lower: str
    label: str
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HyperliquidAddress:
    id: int
    address: str
    address_lower: str
    label: str
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ChainState:
    address_id: int
    chain_key: str
    seeded_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_seen_block: int | None = None


@dataclass(frozen=True, slots=True)
class HyperliquidState:
    address_id: int
    seeded_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    last_seen_time: int | None = None


@dataclass(frozen=True, slots=True)
class AssetAmount:
    symbol: str
    amount: Decimal
    token_address: str | None = None
    decimals: int = 18
    is_native: bool = False


@dataclass(frozen=True, slots=True)
class Activity:
    kind: ActivityKind
    tx_hash: str
    block_number: int
    timestamp: datetime | None
    fingerprint: str
    summary: str
    telegram_text: str
    tx_url: str
    direction: Direction | None = None
    counterparty: str | None = None
    asset_in: AssetAmount | None = None
    asset_out: AssetAmount | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HyperliquidActivity:
    fill_key: str
    coin: str
    direction: HyperliquidDirection
    side: str
    price: Decimal
    size: Decimal
    notional_usd: Decimal
    closed_pnl: Decimal
    fill_time: datetime
    fill_time_ms: int
    telegram_text: str
    summary: str
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WhaleRunResult:
    addresses: int
    chains: int
    processed_pairs: int
    seeded_pairs: int
    detected: int
    inserted: int
    sent: int
    failed: dict[str, str]


@dataclass(frozen=True, slots=True)
class HyperliquidRunResult:
    addresses: int
    processed: int
    seeded: int
    detected: int
    inserted: int
    sent: int
    skipped_small: int
    failed: dict[str, str]
