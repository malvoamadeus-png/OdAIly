from __future__ import annotations

import statistics
import time
from typing import Any

import requests

from .models import GateAssetQuote, GateQuoteBatch


GATE_API_BASE = "https://api.gateio.ws/api/v4"
BASELINE_FLOOR_PERCENT = 0.2


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _change_percent(open_price: float | None, close_price: float | None) -> float | None:
    if open_price in (None, 0) or close_price is None:
        return None
    return ((close_price - open_price) / open_price) * 100


def _latest_kline(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return sorted(rows, key=lambda row: int(row.get("t") or 0))[-1]


def _daily_abs_changes(rows: list[dict[str, Any]]) -> list[float]:
    values: list[float] = []
    for row in rows:
        value = _change_percent(_to_float(row.get("o")), _to_float(row.get("c")))
        if value is not None:
            values.append(abs(value))
    return values


def baseline_abs_change_percent(rows: list[dict[str, Any]]) -> float:
    values = _daily_abs_changes(rows)
    if not values:
        return BASELINE_FLOOR_PERCENT
    return max(BASELINE_FLOOR_PERCENT, statistics.median(values))


class GateClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
        base_url: str = GATE_API_BASE,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.base_url = base_url.rstrip("/")

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.get(
                    self.base_url + path,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(str(last_error) if last_error else f"GET {path} failed")

    def fetch_tradfi_quote(self, *, symbol: str, display_name: str) -> GateAssetQuote:
        payload = self._get_json(
            f"/tradfi/symbols/{symbol}/klines",
            params={"kline_type": "1d", "limit": 31},
        )
        rows = ((payload or {}).get("data") or {}).get("list") or []
        latest = _latest_kline(rows)
        if latest is None:
            raise RuntimeError(f"Gate TradFi returned no klines for {symbol}")
        open_price = _to_float(latest.get("o"))
        close_price = _to_float(latest.get("c"))
        return GateAssetQuote(
            symbol=symbol,
            display_name=display_name,
            price=close_price,
            change_percent=_change_percent(open_price, close_price),
            source="gate-tradfi",
            source_symbol=symbol,
            raw={
                "latest": latest,
                "baseline_abs_change_percent": baseline_abs_change_percent(rows[-30:]),
            },
        )

    def fetch_futures_tickers(self) -> dict[str, dict[str, Any]]:
        rows = self._get_json("/futures/usdt/tickers")
        return {
            str(row.get("contract")): row
            for row in rows
            if isinstance(row, dict) and row.get("contract")
        }

    def fetch_futures_quote(
        self,
        *,
        symbol: str,
        contract: str,
        display_name: str,
        ticker_rows: dict[str, dict[str, Any]],
    ) -> GateAssetQuote:
        row = ticker_rows.get(contract)
        if row is None:
            raise RuntimeError(f"Gate Futures ticker not found: {contract}")
        return GateAssetQuote(
            symbol=symbol,
            display_name=display_name,
            price=_to_float(row.get("last")),
            change_percent=_to_float(row.get("change_percentage")),
            source="gate-futures",
            source_symbol=contract,
            raw=row,
        )

    def fetch_batch(
        self,
        *,
        tradfi_symbols: dict[str, str],
        futures_symbols: dict[str, dict[str, str]],
    ) -> GateQuoteBatch:
        quotes: dict[str, GateAssetQuote] = {}
        raw_response: dict[str, Any] = {"tradfi": {}, "futures": {}}
        errors: dict[str, str] = {}

        for symbol, display_name in tradfi_symbols.items():
            try:
                quote = self.fetch_tradfi_quote(symbol=symbol, display_name=display_name)
                quotes[symbol] = quote
                raw_response["tradfi"][symbol] = quote.raw
            except Exception as exc:
                errors[symbol] = str(exc)
                quotes[symbol] = GateAssetQuote(
                    symbol=symbol,
                    display_name=display_name,
                    source="gate-tradfi",
                    source_symbol=symbol,
                    error=str(exc),
                )

        ticker_rows: dict[str, dict[str, Any]] = {}
        if futures_symbols:
            try:
                ticker_rows = self.fetch_futures_tickers()
                raw_response["futures"]["tickers_count"] = len(ticker_rows)
            except Exception as exc:
                for symbol in futures_symbols:
                    errors[symbol] = str(exc)

        for symbol, item in futures_symbols.items():
            if symbol in errors:
                quotes[symbol] = GateAssetQuote(
                    symbol=symbol,
                    display_name=item["display_name"],
                    source="gate-futures",
                    source_symbol=item["contract"],
                    error=errors[symbol],
                )
                continue
            try:
                quote = self.fetch_futures_quote(
                    symbol=symbol,
                    contract=item["contract"],
                    display_name=item["display_name"],
                    ticker_rows=ticker_rows,
                )
                quotes[symbol] = quote
                raw_response["futures"][symbol] = quote.raw
            except Exception as exc:
                errors[symbol] = str(exc)
                quotes[symbol] = GateAssetQuote(
                    symbol=symbol,
                    display_name=item["display_name"],
                    source="gate-futures",
                    source_symbol=item["contract"],
                    error=str(exc),
                )

        return GateQuoteBatch(quotes=quotes, raw_response=raw_response, errors=errors)
