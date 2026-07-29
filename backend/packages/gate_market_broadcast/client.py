from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import requests

from .models import TickerQuote


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _positive_timestamp(value: Any) -> int | None:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return None
    return timestamp if timestamp > 0 else None


class GateMarketClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        max_attempts: int,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.session = requests.Session()

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(
                    self.base_url + path,
                    params=params,
                    headers={"Accept": "application/json"},
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Gate returned a non-object response")
                return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts:
                    time.sleep(float(attempt))
        raise RuntimeError(str(last_error) if last_error else f"GET {path} failed")

    def fetch_ticker(self, symbol: str) -> TickerQuote:
        payload = self._get(f"/tradfi/symbols/{symbol}/tickers")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"Gate ticker missing data for {symbol}")
        last_price = _decimal(data.get("last_price"))
        used_mid_price = False
        if last_price is None:
            bid = _decimal(data.get("bid_price"))
            ask = _decimal(data.get("ask_price"))
            if bid is None or ask is None:
                raise RuntimeError(f"Gate ticker missing last/bid/ask price for {symbol}")
            last_price = (bid + ask) / Decimal(2)
            used_mid_price = True
        raw_timestamp = int(payload.get("timestamp") or 0)
        observed_at = raw_timestamp // 1000 if raw_timestamp > 10_000_000_000 else raw_timestamp
        if observed_at <= 0:
            observed_at = int(time.time())
        return TickerQuote(
            symbol=symbol,
            price=last_price,
            observed_at=observed_at,
            status=str(data.get("status") or "unknown").strip().lower(),
            high=_decimal(data.get("highest_price")),
            low=_decimal(data.get("lowest_price")),
            today_open=_decimal(data.get("today_open_price")),
            previous_close=_decimal(data.get("last_today_close_price")),
            open_time=_positive_timestamp(data.get("open_time")),
            close_time=_positive_timestamp(data.get("close_time")),
            next_open_time=_positive_timestamp(data.get("next_open_time")),
            used_mid_price=used_mid_price,
        )

    def fetch_klines_page(
        self,
        symbol: str,
        *,
        end_time: int,
        limit: int = 500,
    ) -> list[tuple[int, Decimal]]:
        payload = self._get(
            f"/tradfi/symbols/{symbol}/klines",
            params={"kline_type": "1m", "limit": min(500, max(1, limit)), "end_time": end_time},
        )
        data = payload.get("data")
        rows = data.get("list") if isinstance(data, dict) else None
        result: list[tuple[int, Decimal]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            timestamp = _positive_timestamp(row.get("t"))
            close = _decimal(row.get("c"))
            if timestamp is not None and close is not None:
                result.append((timestamp, close))
        return sorted(set(result))

    def fetch_history(
        self,
        symbol: str,
        *,
        start_time: int,
        end_time: int,
    ) -> list[tuple[int, Decimal]]:
        cursor = end_time
        points: dict[int, Decimal] = {}
        while cursor >= start_time:
            page = self.fetch_klines_page(symbol, end_time=cursor)
            if not page:
                break
            for timestamp, price in page:
                if timestamp >= start_time:
                    points[timestamp] = price
            oldest = page[0][0]
            if oldest <= start_time:
                break
            next_cursor = oldest - 1
            if next_cursor >= cursor:
                raise RuntimeError(f"Gate kline pagination stalled for {symbol}")
            cursor = next_cursor
            time.sleep(0.22)
        return sorted(points.items())
