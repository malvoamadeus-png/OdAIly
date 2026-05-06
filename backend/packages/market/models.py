from __future__ import annotations

from pydantic import BaseModel


class MarketQuote(BaseModel):
    symbol: str
    yahoo_symbol: str
    display_name: str
    quote_type: str | None = None
    market_state: str | None = None
    currency: str | None = None
    regular_market_price: float | None = None
    regular_market_change_percent: float | None = None
    regular_market_time: str | None = None
    pre_market_price: float | None = None
    pre_market_change_percent: float | None = None
    pre_market_time: str | None = None
    post_market_price: float | None = None
    post_market_change_percent: float | None = None
    post_market_time: str | None = None
    source_error: str | None = None


class QuoteBatch(BaseModel):
    quotes: list[MarketQuote]
    missing_symbols: list[str]
    raw_response: dict
