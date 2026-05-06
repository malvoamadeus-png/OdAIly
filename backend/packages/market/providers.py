from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests

from packages.common.config import BriefKind
from packages.common.time_utils import utc_timestamp_to_iso

from .models import MarketQuote, QuoteBatch


FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
ALPACA_SNAPSHOTS_URL = "https://data.alpaca.markets/v2/stocks/snapshots"


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _provider_symbol_for(symbol: str, overrides: dict[str, str] | None = None) -> str:
    normalized = symbol.strip().upper()
    return (overrides or {}).get(normalized, normalized)


def _change_percent(price: float | None, previous_close: float | None) -> float | None:
    if price is None or previous_close in (None, 0):
        return None
    return ((price - previous_close) / previous_close) * 100


def _quote_for_kind(
    *,
    kind: BriefKind,
    source_symbol: str,
    provider_symbol: str,
    display_name: str,
    price: float | None,
    change_percent: float | None,
    timestamp: str | None,
    source_error: str | None = None,
) -> MarketQuote:
    payload: dict[str, Any] = {
        "symbol": source_symbol,
        "yahoo_symbol": provider_symbol,
        "display_name": display_name,
        "quote_type": "EQUITY",
        "currency": "USD",
        "source_error": source_error,
    }
    if kind == "premarket":
        payload.update(
            {
                "pre_market_price": price,
                "pre_market_change_percent": change_percent,
                "pre_market_time": timestamp,
            }
        )
    else:
        payload.update(
            {
                "regular_market_price": price,
                "regular_market_change_percent": change_percent,
                "regular_market_time": timestamp,
            }
        )
    return MarketQuote.model_validate(payload)


def _get_with_retries(
    *,
    url: str,
    params: dict[str, Any],
    headers: dict[str, str] | None,
    timeout_seconds: float,
    max_attempts: int,
    backoff_seconds: float,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=headers or {"User-Agent": "Mozilla/5.0"},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < max(1, max_attempts) and backoff_seconds > 0:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(str(last_error) if last_error else f"GET failed: {url}")


def fetch_finnhub_quotes(
    *,
    symbols: list[str],
    kind: BriefKind,
    api_key: str,
    overrides: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> QuoteBatch:
    normalized_symbols = list(dict.fromkeys(item.strip().upper() for item in symbols if item.strip()))
    quotes: list[MarketQuote] = []
    missing: list[str] = []
    errors: dict[str, str] = {}
    summaries: dict[str, dict[str, Any]] = {}

    for source_symbol in normalized_symbols:
        provider_symbol = _provider_symbol_for(source_symbol, overrides)
        try:
            response = _get_with_retries(
                url=FINNHUB_QUOTE_URL,
                params={"symbol": provider_symbol, "token": api_key},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            payload = response.json()
            price = _to_float(payload.get("c"))
            previous_close = _to_float(payload.get("pc"))
            change_pct = _to_float(payload.get("dp")) or _change_percent(price, previous_close)
            timestamp = utc_timestamp_to_iso(payload.get("t"))
            if price is None or change_pct is None:
                missing.append(source_symbol)
                errors[source_symbol] = "Finnhub quote missing price or change percent."
                quotes.append(
                    _quote_for_kind(
                        kind=kind,
                        source_symbol=source_symbol,
                        provider_symbol=provider_symbol,
                        display_name=source_symbol,
                        price=None,
                        change_percent=None,
                        timestamp=None,
                        source_error=errors[source_symbol],
                    )
                )
                continue
            summaries[source_symbol] = {
                "provider_symbol": provider_symbol,
                "price": price,
                "previous_close": previous_close,
                "change_percent": change_pct,
                "timestamp": timestamp,
            }
            quotes.append(
                _quote_for_kind(
                    kind=kind,
                    source_symbol=source_symbol,
                    provider_symbol=provider_symbol,
                    display_name=source_symbol,
                    price=price,
                    change_percent=change_pct,
                    timestamp=timestamp,
                )
            )
        except Exception as exc:
            missing.append(source_symbol)
            errors[source_symbol] = str(exc)
            quotes.append(
                _quote_for_kind(
                    kind=kind,
                    source_symbol=source_symbol,
                    provider_symbol=provider_symbol,
                    display_name=source_symbol,
                    price=None,
                    change_percent=None,
                    timestamp=None,
                    source_error=str(exc),
                )
            )

    return QuoteBatch(
        quotes=quotes,
        missing_symbols=list(dict.fromkeys(missing)),
        raw_response={
            "provider": "finnhub",
            "quote_attempts": max(1, max_attempts),
            "summaries": summaries,
            "errors": errors,
        },
    )


def _alpaca_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")
    return str(value)


def fetch_alpaca_iex_quotes(
    *,
    symbols: list[str],
    kind: BriefKind,
    api_key: str,
    api_secret: str,
    feed: str = "iex",
    overrides: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> QuoteBatch:
    normalized_symbols = list(dict.fromkeys(item.strip().upper() for item in symbols if item.strip()))
    provider_by_source = {
        source_symbol: _provider_symbol_for(source_symbol, overrides)
        for source_symbol in normalized_symbols
    }
    source_by_provider = {provider: source for source, provider in provider_by_source.items()}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    response = _get_with_retries(
        url=ALPACA_SNAPSHOTS_URL,
        params={"symbols": ",".join(provider_by_source.values()), "feed": feed},
        headers=headers,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )
    payload = response.json()
    snapshots = payload.get("snapshots") if isinstance(payload, dict) else None
    if snapshots is None and isinstance(payload, dict):
        snapshots = payload
    snapshots = snapshots or {}

    quotes: list[MarketQuote] = []
    missing: list[str] = []
    errors: dict[str, str] = {}
    summaries: dict[str, dict[str, Any]] = {}

    for provider_symbol, source_symbol in source_by_provider.items():
        snapshot = snapshots.get(provider_symbol) or {}
        latest_trade = snapshot.get("latestTrade") or {}
        minute_bar = snapshot.get("minuteBar") or {}
        daily_bar = snapshot.get("dailyBar") or {}
        prev_daily_bar = snapshot.get("prevDailyBar") or {}
        price = (
            _to_float(latest_trade.get("p"))
            or _to_float(minute_bar.get("c"))
            or _to_float(daily_bar.get("c"))
        )
        previous_close = _to_float(prev_daily_bar.get("c"))
        change_pct = _change_percent(price, previous_close)
        timestamp = _alpaca_time(latest_trade.get("t") or minute_bar.get("t") or daily_bar.get("t"))
        if price is None or change_pct is None:
            missing.append(source_symbol)
            errors[source_symbol] = "Alpaca snapshot missing price or previous close."
            quotes.append(
                _quote_for_kind(
                    kind=kind,
                    source_symbol=source_symbol,
                    provider_symbol=provider_symbol,
                    display_name=source_symbol,
                    price=None,
                    change_percent=None,
                    timestamp=None,
                    source_error=errors[source_symbol],
                )
            )
            continue
        summaries[source_symbol] = {
            "provider_symbol": provider_symbol,
            "price": price,
            "previous_close": previous_close,
            "change_percent": change_pct,
            "timestamp": timestamp,
        }
        quotes.append(
            _quote_for_kind(
                kind=kind,
                source_symbol=source_symbol,
                provider_symbol=provider_symbol,
                display_name=source_symbol,
                price=price,
                change_percent=change_pct,
                timestamp=timestamp,
            )
        )

    return QuoteBatch(
        quotes=quotes,
        missing_symbols=list(dict.fromkeys(missing)),
        raw_response={
            "provider": "alpaca_iex",
            "feed": feed,
            "quote_attempts": max(1, max_attempts),
            "summaries": summaries,
            "errors": errors,
        },
    )
