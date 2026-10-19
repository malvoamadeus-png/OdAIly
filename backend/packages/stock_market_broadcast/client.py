from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from .models import Market, StockInstrument, StockQuote


EQUITY_TYPES: dict[str, Market] = {
    "EQUITY": "us",
    "CN_EQUITY": "cn",
    "HK_EQUITY": "hk",
    "KR_EQUITY": "kr",
    "JP_EQUITY": "jp",
}


def _decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid Binance numeric value: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid Binance numeric value: {value!r}")
    return parsed


def discover_stock_instruments(payload: dict[str, Any]) -> list[StockInstrument]:
    instruments: list[StockInstrument] = []
    for item in payload.get("symbols", []):
        if not isinstance(item, dict) or item.get("status") != "TRADING":
            continue
        if item.get("contractType") != "TRADIFI_PERPETUAL":
            continue
        market = EQUITY_TYPES.get(str(item.get("underlyingType") or ""))
        if market is None:
            continue
        subtype = item.get("underlyingSubType") or []
        if isinstance(subtype, str):
            subtype = [subtype]
        excluded_subtypes = (
            "pre-ipo",
            "index",
            "commodity",
            "forex",
            "foreign-exchange",
            "foreign_exchange",
            "currency",
            "fx",
        )
        if any(
            marker in str(value).lower()
            for value in subtype
            for marker in excluded_subtypes
        ):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or not symbol.endswith("USDT"):
            continue
        try:
            precision = max(0, min(12, int(item.get("pricePrecision", 8))))
        except (TypeError, ValueError):
            precision = 8
        instruments.append(StockInstrument(symbol, market, precision))
    return sorted(instruments, key=lambda item: item.symbol)


class BinanceStockMarketClient:
    def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def _get(self, path: str) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}",
            headers={"Accept": "application/json", "User-Agent": "OdAIly-StockMarketBroadcast/1.0"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def fetch_instruments(self) -> list[StockInstrument]:
        payload = self._get("/fapi/v1/exchangeInfo")
        if not isinstance(payload, dict):
            raise RuntimeError("Binance exchangeInfo returned an invalid response")
        instruments = discover_stock_instruments(payload)
        if not instruments:
            raise RuntimeError("Binance exchangeInfo contained no eligible stock contracts")
        return instruments

    def fetch_quotes(self, instruments: list[StockInstrument]) -> dict[str, StockQuote]:
        payload = self._get("/fapi/v1/ticker/24hr")
        if not isinstance(payload, list):
            raise RuntimeError("Binance 24hr ticker returned an invalid response")
        wanted = {item.symbol for item in instruments}
        now_ms = int(time.time() * 1000)
        quotes: dict[str, StockQuote] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in wanted:
                continue
            try:
                close_time = int(item.get("closeTime") or 0)
                price = _decimal(item.get("lastPrice"))
                change = _decimal(item.get("priceChangePercent"))
            except (TypeError, ValueError):
                continue
            age_ms = now_ms - close_time
            if close_time <= 0 or age_ms > 90_000 or age_ms < -15_000:
                continue
            if price <= 0:
                continue
            quotes[symbol] = StockQuote(
                symbol=symbol,
                price=price,
                change_24h=change,
                observed_at=close_time // 1000,
            )
        return quotes
