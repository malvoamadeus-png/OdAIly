from __future__ import annotations

from pydantic import BaseModel


class GateAssetQuote(BaseModel):
    symbol: str
    display_name: str
    price: float | None = None
    change_percent: float | None = None
    source: str
    source_symbol: str
    raw: dict | None = None
    error: str | None = None


class GateQuoteBatch(BaseModel):
    quotes: dict[str, GateAssetQuote]
    raw_response: dict
    errors: dict[str, str]
