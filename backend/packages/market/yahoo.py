from __future__ import annotations

import time
from datetime import datetime, time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

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
EASTERN_TZ = ZoneInfo("America/New_York")


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


def _get_json_with_retries(
    *,
    url: str,
    params: dict[str, Any],
    timeout_seconds: float,
    max_attempts: int,
    backoff_seconds: float,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json,text/plain,*/*",
                },
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < max(1, max_attempts) and backoff_seconds > 0:
                time.sleep(backoff_seconds * attempt)
    raise RuntimeError(str(last_error) if last_error else f"GET failed: {url}")


def _latest_valid_point(
    *,
    timestamps: list[Any],
    closes: list[Any],
    premarket_only: bool = False,
) -> tuple[int | float | None, float | None]:
    points: list[tuple[int | float | None, float]] = []
    for index, close in enumerate(closes):
        price = _to_float(close)
        if price is None:
            continue
        timestamp = timestamps[index] if index < len(timestamps) else None
        if premarket_only:
            if timestamp is None or not _is_premarket_timestamp(timestamp):
                continue
        points.append((timestamp, price))
    if not points:
        return None, None
    return points[-1]


def _change_percent(price: float | None, previous_close: float | None) -> float | None:
    if price is None or previous_close in (None, 0):
        return None
    return ((price - previous_close) / previous_close) * 100


def _quote_from_chart_result(
    *,
    source_symbol: str,
    yahoo_symbol: str,
    result: dict[str, Any],
    kind: str,
) -> MarketQuote:
    meta = result.get("meta") or {}
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    timestamps = result.get("timestamp") or []
    closes = [_to_float(item) for item in (quote.get("close") or [])]
    valid_closes = [item for item in closes if item is not None]

    source_error: str | None = None
    latest_timestamp: int | float | None = None
    latest_price: float | None = None
    previous_close: float | None = None

    if kind == "close":
        valid_points = [
            (timestamps[index] if index < len(timestamps) else None, price)
            for index, price in enumerate(closes)
            if price is not None
        ]
        if len(valid_points) >= 2:
            latest_timestamp, latest_price = valid_points[-1]
            previous_close = valid_points[-2][1]
        else:
            source_error = "Yahoo chart daily data missing at least two valid closes."
    elif kind == "premarket":
        latest_timestamp, latest_price = _latest_valid_point(
            timestamps=timestamps,
            closes=quote.get("close") or [],
            premarket_only=True,
        )
        previous_close = _to_float(meta.get("previousClose")) or _to_float(meta.get("chartPreviousClose"))
        if latest_price is None:
            source_error = "Yahoo chart premarket data missing valid 1m close."
    elif kind == "open":
        latest_timestamp, latest_price = _latest_valid_point(
            timestamps=timestamps,
            closes=quote.get("close") or [],
            premarket_only=False,
        )
        previous_close = _to_float(meta.get("previousClose")) or _to_float(meta.get("chartPreviousClose"))
        if latest_price is None:
            source_error = "Yahoo chart intraday data missing valid 1m close."
    else:
        source_error = f"Unsupported Yahoo chart brief kind: {kind}"

    change_percent = _change_percent(latest_price, previous_close)
    if source_error is None and change_percent is None:
        source_error = "Yahoo chart data missing previous close."

    payload: dict[str, Any] = {
        "symbol": source_symbol,
        "yahoo_symbol": yahoo_symbol,
        "display_name": str(meta.get("shortName") or meta.get("longName") or source_symbol),
        "quote_type": str(meta.get("instrumentType") or "") or None,
        "market_state": meta.get("marketState"),
        "currency": meta.get("currency"),
        "source_error": source_error,
    }
    if kind == "premarket":
        payload.update(
            {
                "pre_market_price": latest_price,
                "pre_market_change_percent": change_percent,
                "pre_market_time": utc_timestamp_to_iso(latest_timestamp),
                "regular_market_price": _to_float(meta.get("regularMarketPrice")),
                "regular_market_time": utc_timestamp_to_iso(meta.get("regularMarketTime")),
            }
        )
    else:
        payload.update(
            {
                "regular_market_price": latest_price,
                "regular_market_change_percent": change_percent,
                "regular_market_time": utc_timestamp_to_iso(latest_timestamp or meta.get("regularMarketTime")),
            }
        )
    return MarketQuote.model_validate(payload)


def _chart_summary(quote: MarketQuote, previous_close: float | None = None) -> dict[str, Any]:
    return {
        "symbol": quote.symbol,
        "provider_symbol": quote.yahoo_symbol,
        "regular_market_price": quote.regular_market_price,
        "regular_market_change_percent": quote.regular_market_change_percent,
        "regular_market_time": quote.regular_market_time,
        "pre_market_price": quote.pre_market_price,
        "pre_market_change_percent": quote.pre_market_change_percent,
        "pre_market_time": quote.pre_market_time,
        "previous_close": previous_close,
        "source_error": quote.source_error,
    }


def _chart_request_params(kind: str) -> dict[str, str]:
    if kind == "close":
        return {"range": "5d", "interval": "1d", "includePrePost": "false"}
    return {"range": "1d", "interval": "1m", "includePrePost": "true"}


def _chart_quote(
    *,
    source_symbol: str,
    yahoo_symbol: str,
    kind: str,
    timeout_seconds: float,
    max_attempts: int,
    backoff_seconds: float,
) -> tuple[MarketQuote, dict[str, Any]]:
    payload = _get_json_with_retries(
        url=f"{YAHOO_CHART_URL}/{yahoo_symbol}",
        params=_chart_request_params(kind),
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )
    chart = payload.get("chart") or {}
    result = ((chart.get("result") or [None])[0]) or {}
    quote = _quote_from_chart_result(
        source_symbol=source_symbol,
        yahoo_symbol=yahoo_symbol,
        result=result,
        kind=kind,
    )
    meta = result.get("meta") or {}
    summary = _chart_summary(
        quote,
        previous_close=_to_float(meta.get("previousClose")) or _to_float(meta.get("chartPreviousClose")),
    )
    return quote, summary


def _is_premarket_timestamp(timestamp: int | float) -> bool:
    eastern_time = datetime.fromtimestamp(float(timestamp), tz=EASTERN_TZ).time()
    return dt_time(4, 0) <= eastern_time < dt_time(9, 30)


def fetch_chart_quotes(
    *,
    symbols: list[str],
    kind: str,
    overrides: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 2,
    backoff_seconds: float = 1.0,
) -> QuoteBatch:
    normalized_symbols = list(dict.fromkeys(item.strip().upper() for item in symbols if item.strip()))
    yahoo_by_symbol = {symbol: yahoo_symbol_for(symbol, overrides) for symbol in normalized_symbols}
    quotes: list[MarketQuote] = []
    missing: list[str] = []
    errors: dict[str, str] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for source_symbol, yahoo_symbol in yahoo_by_symbol.items():
        try:
            quote, summary = _chart_quote(
                source_symbol=source_symbol,
                yahoo_symbol=yahoo_symbol,
                kind=kind,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            quotes.append(quote)
            summaries[source_symbol] = summary
            if quote.source_error:
                missing.append(source_symbol)
                errors[source_symbol] = quote.source_error
        except Exception as exc:
            missing.append(source_symbol)
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
        missing_symbols=list(dict.fromkeys(missing)),
        raw_response={
            "provider": "yahoo_chart",
            "kind": kind,
            "request_params": _chart_request_params(kind),
            "quote_attempts": max(1, max_attempts),
            "summaries": summaries,
            "errors": errors,
        },
    )


def fetch_quotes(
    *,
    symbols: list[str],
    overrides: dict[str, str] | None = None,
    timeout_seconds: float = 10.0,
    max_attempts: int = 2,
    backoff_seconds: float = 1.0,
    include_premarket: bool = False,
) -> QuoteBatch:
    normalized_symbols = list(dict.fromkeys(item.strip().upper() for item in symbols if item.strip()))
    yahoo_by_symbol = {symbol: yahoo_symbol_for(symbol, overrides) for symbol in normalized_symbols}
    symbol_by_yahoo = {value: key for key, value in yahoo_by_symbol.items()}

    try:
        payload = _get_json_with_retries(
            url=YAHOO_QUOTE_URL,
            params={"symbols": ",".join(yahoo_by_symbol.values())},
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    except Exception as exc:
        return QuoteBatch(
            quotes=[],
            missing_symbols=normalized_symbols,
            raw_response={
                "provider": "yahoo_quote",
                "quote_error": str(exc),
                "quote_attempts": max(1, max_attempts),
            },
        )

    rows = ((payload.get("quoteResponse") or {}).get("result") or [])
    quotes = [_quote_from_row(symbol_by_yahoo, row) for row in rows if isinstance(row, dict)]
    found = {quote.symbol for quote in quotes}
    missing = [symbol for symbol in normalized_symbols if symbol not in found]
    raw_response = dict(payload)
    raw_response["provider"] = "yahoo_quote"
    raw_response["quote_attempts"] = max(1, max_attempts)
    if include_premarket:
        raw_response["include_premarket"] = True
    return QuoteBatch(
        quotes=quotes,
        missing_symbols=missing,
        raw_response=raw_response,
    )
