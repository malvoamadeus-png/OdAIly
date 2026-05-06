from __future__ import annotations

import time
from typing import Any

import requests

from packages.common.time_utils import utc_timestamp_to_iso

from .models import MarketQuote, QuoteBatch


INDEX_OVERRIDES = {
    "SPX": "^GSPC",
    "VIX": "^VIX",
    "IXIC": "^IXIC",
    "DJI": "^DJI",
}

YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"


def yahoo_symbol_for(symbol: str, overrides: dict[str, str] | None = None) -> str:
    normalized = symbol.strip().upper()
    merged = {**INDEX_OVERRIDES, **(overrides or {})}
    return merged.get(normalized, normalized.replace(".", "-"))


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _quote_from_row(symbol_by_yahoo: dict[str, str], row: dict[str, Any]) -> MarketQuote:
    yahoo_symbol = str(row.get("symbol") or "").strip()
    source_symbol = symbol_by_yahoo.get(yahoo_symbol, yahoo_symbol).upper()
    return MarketQuote(
        symbol=source_symbol,
        yahoo_symbol=yahoo_symbol,
        display_name=str(row.get("shortName") or row.get("longName") or source_symbol),
        quote_type=row.get("quoteType"),
        market_state=row.get("marketState"),
        currency=row.get("currency"),
        regular_market_price=_to_float(row.get("regularMarketPrice")),
        regular_market_change_percent=_to_float(row.get("regularMarketChangePercent")),
        regular_market_time=utc_timestamp_to_iso(row.get("regularMarketTime")),
        pre_market_price=_to_float(row.get("preMarketPrice")),
        pre_market_change_percent=_to_float(row.get("preMarketChangePercent")),
        pre_market_time=utc_timestamp_to_iso(row.get("preMarketTime")),
        post_market_price=_to_float(row.get("postMarketPrice")),
        post_market_change_percent=_to_float(row.get("postMarketChangePercent")),
        post_market_time=utc_timestamp_to_iso(row.get("postMarketTime")),
    )


def _chart_quote(
    *,
    source_symbol: str,
    yahoo_symbol: str,
    timeout_seconds: float,
) -> MarketQuote:
    response = requests.get(
        f"{YAHOO_CHART_URL}/{yahoo_symbol}",
        params={"range": "5d", "interval": "1d", "includePrePost": "false"},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    chart = payload.get("chart") or {}
    result = ((chart.get("result") or [None])[0]) or {}
    meta = result.get("meta") or {}
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    closes = [_to_float(item) for item in (quote.get("close") or [])]
    valid_closes = [item for item in closes if item is not None]
    latest_price = _to_float(meta.get("regularMarketPrice")) or (valid_closes[-1] if valid_closes else None)
    previous_close = _to_float(meta.get("chartPreviousClose"))
    if previous_close is None and len(valid_closes) >= 2:
        previous_close = valid_closes[-2]
    change_percent = None
    if latest_price is not None and previous_close not in (None, 0):
        change_percent = ((latest_price - previous_close) / previous_close) * 100

    return MarketQuote(
        symbol=source_symbol,
        yahoo_symbol=yahoo_symbol,
        display_name=str(meta.get("shortName") or meta.get("longName") or source_symbol),
        quote_type=str(meta.get("instrumentType") or "") or None,
        market_state=meta.get("marketState"),
        currency=meta.get("currency"),
        regular_market_price=latest_price,
        regular_market_change_percent=change_percent,
        regular_market_time=utc_timestamp_to_iso(meta.get("regularMarketTime")),
    )


def _fetch_chart_fallback(
    *,
    yahoo_by_symbol: dict[str, str],
    timeout_seconds: float,
    raw_error: str,
) -> QuoteBatch:
    quotes: list[MarketQuote] = []
    missing: list[str] = []
    errors: dict[str, str] = {}
    for source_symbol, yahoo_symbol in yahoo_by_symbol.items():
        try:
            quotes.append(
                _chart_quote(
                    source_symbol=source_symbol,
                    yahoo_symbol=yahoo_symbol,
                    timeout_seconds=timeout_seconds,
                )
            )
        except Exception as exc:
            errors[source_symbol] = str(exc)
            quotes.append(
                MarketQuote(
                    symbol=source_symbol,
                    yahoo_symbol=yahoo_symbol,
                    display_name=source_symbol,
                    source_error=str(exc),
                )
            )
    return QuoteBatch(
        quotes=quotes,
        missing_symbols=missing,
        raw_response={"quote_error": raw_error, "chart_errors": errors},
    )


def fetch_quotes(
    *,
    symbols: list[str],
    overrides: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 2,
    backoff_seconds: float = 1.0,
) -> QuoteBatch:
    normalized_symbols = list(dict.fromkeys(item.strip().upper() for item in symbols if item.strip()))
    yahoo_by_symbol = {symbol: yahoo_symbol_for(symbol, overrides) for symbol in normalized_symbols}
    symbol_by_yahoo = {value: key for key, value in yahoo_by_symbol.items()}

    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(
                YAHOO_QUOTE_URL,
                params={"symbols": ",".join(yahoo_by_symbol.values())},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            rows = ((payload.get("quoteResponse") or {}).get("result") or [])
            quotes = [_quote_from_row(symbol_by_yahoo, row) for row in rows if isinstance(row, dict)]
            found = {quote.symbol for quote in quotes}
            missing = [symbol for symbol in normalized_symbols if symbol not in found]
            return QuoteBatch(quotes=quotes, missing_symbols=missing, raw_response=payload)
        except Exception as exc:
            last_error = exc
            if attempt < max(1, max_attempts) and backoff_seconds > 0:
                time.sleep(backoff_seconds * attempt)

    return _fetch_chart_fallback(
        yahoo_by_symbol=yahoo_by_symbol,
        timeout_seconds=timeout_seconds,
        raw_error=str(last_error) if last_error else "Yahoo quote request failed.",
    )
