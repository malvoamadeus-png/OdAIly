from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class StockQuote:
    symbol: str
    price: Decimal
    change_24h: Decimal
    observed_at: int


@dataclass(frozen=True, slots=True)
class TriggerState:
    symbol: str
    window: Window
    initialized: bool
    armed_5_up: bool
    armed_5_down: bool
    armed_10_up: bool
    armed_10_down: bool
    last_change_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    state: TriggerState
    tier: Tier | None
    direction: Direction | None


@dataclass(frozen=True, slots=True)
class StockAlert:
    event_key: str
    instrument: StockInstrument
    window: Window
    tier: Tier
    direction: Direction
    change_pct: Decimal
    current_price: Decimal
    observed_at: int
    title: str
    content: str
