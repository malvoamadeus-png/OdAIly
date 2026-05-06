from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from packages.briefing.generator import build_brief
from packages.common.config import BriefKind, MarketBriefSettings
from packages.common.paths import AppPaths, ensure_runtime_dirs
from packages.common.storage import append_brief_result, save_market_quotes
from packages.common.time_utils import EASTERN_TZ, is_weekend_in_eastern, now_iso, now_shanghai, today_key
from packages.market.yahoo import fetch_quotes
from packages.publisher import PushClient, PushResult


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

    batch = fetch_quotes(
        symbols=settings.watchlist,
        overrides=settings.yahoo_symbol_overrides,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
        include_premarket=kind == "premarket",
    )
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
        "dry-run completed" if dry_run else "pushed with isPublish=false",
        run_id,
        pushed=not dry_run,
    )
