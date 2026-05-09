from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from packages.briefing.generator import CRYPTO_STOCK_SYMBOLS, BriefPayload, build_brief
from packages.common.config import BriefKind, MarketBriefSettings
from packages.common.paths import AppPaths, ensure_runtime_dirs
from packages.common.storage import append_brief_result, save_market_quotes
from packages.common.time_utils import (
    EASTERN_TZ,
    is_weekend_in_eastern,
    now_iso,
    now_shanghai,
    today_key,
)
from packages.market.models import MarketQuote, QuoteBatch
from packages.market.providers import fetch_finnhub_quotes
from packages.market.yahoo import fetch_chart_quotes, fetch_quotes
from packages.publisher import PushClient, PushResult
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief


@dataclass(slots=True)
class BriefRunResult:
    exit_code: int
    status: str
    kind: BriefKind
    message: str
    run_id: str
    pushed: bool = False


def _push_client(settings: MarketBriefSettings) -> PushClient:
    return PushClient(
        endpoint=str(settings.push_endpoint),
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )


def _apply_writer2_format(brief: BriefPayload) -> BriefPayload:
    formatted = format_brief(DraftBrief(title=brief.title, content=brief.content))
    return brief.model_copy(update={"title": formatted.title, "content": formatted.content})


def _value_for_kind(quote: MarketQuote, kind: BriefKind) -> float | None:
    if kind == "premarket":
        return quote.pre_market_change_percent
    return quote.regular_market_change_percent


def _time_for_kind(quote: MarketQuote, kind: BriefKind) -> str | None:
    if kind == "premarket":
        return quote.pre_market_time
    return quote.regular_market_time


def _parse_quote_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _quality_error(
    *,
    kind: BriefKind,
    settings: MarketBriefSettings,
    batch: QuoteBatch,
) -> str | None:
    valid_quotes = [
        quote
        for quote in batch.quotes
        if quote.source_error is None and _value_for_kind(quote, kind) is not None
    ]
    quality_quotes = valid_quotes
    if kind in {"premarket", "open"}:
        now = now_shanghai()
        max_age_seconds = settings.max_quote_age_minutes * 60
        quality_quotes = []
        for quote in valid_quotes:
            quote_time = _parse_quote_time(_time_for_kind(quote, kind))
            if quote_time is None:
                continue
            age_seconds = abs((now - quote_time.astimezone(now.tzinfo)).total_seconds())
            if age_seconds <= max_age_seconds:
                quality_quotes.append(quote)

    valid_crypto_count = sum(1 for quote in quality_quotes if quote.symbol in CRYPTO_STOCK_SYMBOLS)
    if valid_crypto_count < settings.min_valid_crypto_stocks:
        return (
            "insufficient valid crypto stock quotes: "
            f"{valid_crypto_count}/{settings.min_valid_crypto_stocks}"
        )

    if kind in {"close", "open"}:
        valid_index_count = sum(1 for quote in quality_quotes if quote.symbol in {"SPX", "IXIC", "DJI"})
        if valid_index_count < settings.min_valid_indices:
            return f"insufficient valid index quotes: {valid_index_count}/{settings.min_valid_indices}"
    return None


def _source_attempt(
    *,
    source: str,
    batch: QuoteBatch | None = None,
    status: str,
    message: str,
) -> dict:
    return {
        "source": source,
        "status": status,
        "message": message,
        "raw_response": batch.raw_response if batch is not None else {},
        "missing_symbols": batch.missing_symbols if batch is not None else [],
    }


def _fetch_market_quotes(
    *,
    kind: BriefKind,
    settings: MarketBriefSettings,
) -> QuoteBatch:
    attempts: list[dict] = []
    last_batch: QuoteBatch | None = None

    for source in settings.market_data_sources:
        if source == "yahoo_quote":
            batch = fetch_quotes(
                symbols=settings.watchlist,
                overrides=settings.yahoo_symbol_overrides,
                timeout_seconds=settings.request_timeout_seconds,
                max_attempts=settings.retry.max_attempts,
                backoff_seconds=settings.retry.backoff_seconds,
                include_premarket=kind == "premarket",
            )
            last_batch = batch
            source_error = batch.raw_response.get("quote_error")
            if source_error:
                attempts.append(
                    _source_attempt(
                        source=source,
                        batch=batch,
                        status="error",
                        message=str(source_error),
                    )
                )
                continue
        elif source == "yahoo_chart":
            try:
                batch = fetch_chart_quotes(
                    symbols=settings.watchlist,
                    kind=kind,
                    overrides=settings.yahoo_symbol_overrides,
                    timeout_seconds=settings.request_timeout_seconds,
                    max_attempts=settings.retry.max_attempts,
                    backoff_seconds=settings.retry.backoff_seconds,
                )
            except Exception as exc:
                attempts.append(_source_attempt(source=source, status="error", message=str(exc)))
                continue
            last_batch = batch
        elif source == "finnhub":
            if not settings.finnhub_api_key:
                attempts.append(
                    _source_attempt(
                        source=source,
                        status="skipped",
                        message="FINNHUB_API_KEY is not configured.",
                    )
                )
                continue
            try:
                batch = fetch_finnhub_quotes(
                    symbols=settings.watchlist,
                    kind=kind,
                    api_key=settings.finnhub_api_key,
                    overrides=settings.finnhub_symbol_overrides,
                    timeout_seconds=settings.request_timeout_seconds,
                    max_attempts=settings.retry.max_attempts,
                    backoff_seconds=settings.retry.backoff_seconds,
                )
            except Exception as exc:
                attempts.append(_source_attempt(source=source, status="error", message=str(exc)))
                continue
            last_batch = batch
        else:
            attempts.append(_source_attempt(source=source, status="skipped", message="Unknown source."))
            continue

        quality_error = _quality_error(kind=kind, settings=settings, batch=batch)
        if quality_error:
            attempts.append(
                _source_attempt(
                    source=source,
                    batch=batch,
                    status="skipped",
                    message=quality_error,
                )
            )
            continue

        raw_response = dict(batch.raw_response)
        raw_response["selected_source"] = source
        raw_response["source_attempts"] = attempts
        return QuoteBatch(
            quotes=batch.quotes,
            missing_symbols=batch.missing_symbols,
            raw_response=raw_response,
        )

    has_transport_error = any(attempt["status"] == "error" for attempt in attempts)
    message = "all market data sources failed or were skipped"
    error_key = "quote_error" if has_transport_error else "quality_error"
    return QuoteBatch(
        quotes=last_batch.quotes if last_batch else [],
        missing_symbols=last_batch.missing_symbols if last_batch else list(settings.watchlist),
        raw_response={
            error_key: message,
            "source_attempts": attempts,
            "last_raw_response": last_batch.raw_response if last_batch else {},
        },
    )


def run_brief_once(
    *,
    kind: BriefKind,
    settings: MarketBriefSettings,
    paths: AppPaths,
    dry_run_override: bool | None = None,
    force: bool = False,
) -> BriefRunResult:
    ensure_runtime_dirs(paths)
    run_id = f"{kind}-{now_shanghai().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    date_key = today_key()
    dry_run = settings.dry_run if dry_run_override is None else dry_run_override

    if not force and is_weekend_in_eastern():
        message = "skipped: weekend in America/New_York"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "run_id": run_id,
                "kind": kind,
                "status": "skipped",
                "message": message,
                "created_at": now_iso(),
            },
        )
        return BriefRunResult(0, "skipped", kind, message, run_id)

    batch = _fetch_market_quotes(kind=kind, settings=settings)
    quote_path = save_market_quotes(
        paths,
        run_id=run_id,
        payload={
            "run_id": run_id,
            "kind": kind,
            "created_at": now_iso(),
            "timezone": str(EASTERN_TZ),
            "quotes": [quote.model_dump(mode="json") for quote in batch.quotes],
            "missing_symbols": batch.missing_symbols,
            "raw_response": batch.raw_response,
        },
    )

    quote_error = batch.raw_response.get("quote_error")
    if quote_error:
        message = f"error: market data request failed after retries: {quote_error}"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "run_id": run_id,
                "kind": kind,
                "status": "error",
                "message": message,
                "quote_path": str(quote_path),
                "missing_symbols": batch.missing_symbols,
                "created_at": now_iso(),
            },
        )
        return BriefRunResult(1, "error", kind, message, run_id)

    quality_error = batch.raw_response.get("quality_error")
    if quality_error:
        message = f"skipped: {quality_error}"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "run_id": run_id,
                "kind": kind,
                "status": "skipped",
                "message": message,
                "quote_path": str(quote_path),
                "missing_symbols": batch.missing_symbols,
                "created_at": now_iso(),
            },
        )
        return BriefRunResult(0, "skipped", kind, message, run_id)

    source_errors = [quote.symbol for quote in batch.quotes if quote.source_error]
    skipped = list(dict.fromkeys([*batch.missing_symbols, *source_errors]))
    brief = build_brief(kind=kind, quotes=batch.quotes, skipped_symbols=skipped)
    if brief is None:
        message = "skipped: no valid market data for this brief kind"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "run_id": run_id,
                "kind": kind,
                "status": "skipped",
                "message": message,
                "quote_path": str(quote_path),
                "missing_symbols": batch.missing_symbols,
                "created_at": now_iso(),
            },
        )
        return BriefRunResult(0, "skipped", kind, message, run_id)
    brief = _apply_writer2_format(brief)

    push_result: PushResult = _push_client(settings).push(
        title=brief.title,
        content=brief.content,
        dry_run=dry_run,
    )
    status = "success" if push_result.ok else "error"
    append_brief_result(
        paths,
        date_key=date_key,
        payload={
            "run_id": run_id,
            "kind": kind,
            "status": status,
            "dry_run": dry_run,
            "title": brief.title,
            "content": brief.content,
            "isPublish": brief.isPublish,
            "used_symbols": brief.used_symbols,
            "skipped_symbols": brief.skipped_symbols,
            "quote_path": str(quote_path),
            "push_result": push_result.model_dump(mode="json"),
            "created_at": now_iso(),
        },
    )
    if not push_result.ok:
        return BriefRunResult(1, "error", kind, push_result.error or "push failed", run_id)
    return BriefRunResult(
        0,
        "success",
        kind,
        "dry-run completed" if dry_run else "pushed with isPublish=false,isPush=false",
        run_id,
        pushed=not dry_run,
    )
