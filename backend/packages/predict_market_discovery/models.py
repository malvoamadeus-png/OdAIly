from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class MarketObservation:
    market_id: str
    category_id: str
    category_title: str
    category_slug: str
    category_published_at: str | None
    starts_at: str | None
    ends_at: str | None
    market_created_at: str | None
    question: str
    market_variant: str
    market_status: str
    trading_status: str
    market_url: str
    filter_reason: str | None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)
