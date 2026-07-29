from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal


PublishMode = Literal["backend", "live"]
ReferenceKind = Literal["rolling_24h", "previous_session", "since_open"]


@dataclass(frozen=True, slots=True)
class SymbolConfig:
    symbol: str
    display_name: str
    threshold_text: str
    price_precision: int
    unit: str
    enabled: bool = True

    @property
    def threshold(self) -> Decimal:
        return Decimal(self.threshold_text)


@dataclass(frozen=True, slots=True)
class TickerQuote:
    symbol: str
    price: Decimal
    observed_at: int
    status: str
    high: Decimal | None = None
    low: Decimal | None = None
    today_open: Decimal | None = None
    previous_close: Decimal | None = None
    open_time: int | None = None
    close_time: int | None = None
    next_open_time: int | None = None
    used_mid_price: bool = False


@dataclass(slots=True)
class SymbolState:
    symbol: str
    mode: PublishMode
    initialized: bool = False
    last_price: Decimal | None = None
    last_quote_at: int | None = None
    disarmed_levels: set[int] = field(default_factory=set)
    market_status: str | None = None
    next_open_at: int | None = None
    last_success_at: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class Trigger:
    level_index: int
    level: Decimal
    direction: Literal["up", "down"]


@dataclass(frozen=True, slots=True)
class ReferenceMetrics:
    reference_kind: ReferenceKind
    reference_price: Decimal
    high: Decimal
    low: Decimal


@dataclass(frozen=True, slots=True)
class RenderedBrief:
    template_key: str
    title: str
    content: str
    change_percent: Decimal
