from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


Market = Literal["us", "cn", "hk", "kr", "jp"]
Window = Literal["1h", "24h"]
Tier = Literal["backend", "live"]
Direction = Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class StockInstrument:
    symbol: str
    market: Market
    price_precision: int

    @property
    def asset_name(self) -> str:
        return self.symbol.removesuffix("USDT")


@dataclass(frozen=True, slots=True)
class StockQuote:
    symbol: str
    price: Decimal
    change_24h: Decimal
    observed_at: int


@dataclass(slots=True)
class PriceLock:
    anchor_price: Decimal
    extreme_price: Decimal
    locked_at: int
    tier: Tier | None


@dataclass(slots=True)
class AssetState:
    observed_at: int
    changes: dict[Window, Decimal]
    locks: dict[Direction, PriceLock]


@dataclass(slots=True)
class PendingCandidate:
    first_at: int
    upgrade_floor: Decimal | None = None


@dataclass(slots=True)
class AggregationGroup:
    market: Market
    direction: Direction
    due_at: int | None = None
    next_at: int = 0
    pending: dict[str, PendingCandidate] = field(default_factory=dict)


@dataclass(slots=True)
class BroadcastState:
    assets: dict[str, AssetState] = field(default_factory=dict)
    groups: dict[str, AggregationGroup] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AlertMember:
    instrument: StockInstrument
    changes: dict[Window, Decimal]
    current_price: Decimal
    observed_at: int

    @property
    def primary_window(self) -> Window:
        return max(self.changes, key=lambda window: (abs(self.changes[window]), window))

    @property
    def change_pct(self) -> Decimal:
        return self.changes[self.primary_window]


@dataclass(frozen=True, slots=True)
class StockAlert:
    event_key: str
    market: Market
    tier: Tier
    direction: Direction
    members: tuple[AlertMember, ...]
    title: str
    content: str
