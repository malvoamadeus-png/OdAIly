from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from packages.common.config import GateBriefKind, GateTradfiSettings
from packages.common.paths import AppPaths, ensure_runtime_dirs
from packages.common.storage import append_brief_result, save_gate_quotes
from packages.common.time_utils import is_weekend_in_eastern, now_iso, now_shanghai, today_key
from packages.publisher import PushClient, PushResult

from .client import GateClient
from .generator import build_gate_brief


@dataclass(slots=True)
class GateRunResult:
    exit_code: int
    status: str
    kind: GateBriefKind
    message: str
    run_id: str
    pushed: bool = False


def _push_client(settings: GateTradfiSettings) -> PushClient:
    return PushClient(
        endpoint=str(settings.push_endpoint),
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )


def run_gate_once(
    *,
    kind: GateBriefKind,
    settings: GateTradfiSettings,
    paths: AppPaths,
    dry_run_override: bool | None = None,
    force: bool = False,
) -> GateRunResult:
    ensure_runtime_dirs(paths)
    run_id = f"gate-{kind}-{now_shanghai().strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:8]}"
    date_key = today_key()
    dry_run = settings.dry_run if dry_run_override is None else dry_run_override

    if not force and is_weekend_in_eastern():
        message = "skipped: weekend in America/New_York"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "task": "gate-tradfi",
                "run_id": run_id,
                "kind": kind,
                "status": "skipped",
                "message": message,
                "created_at": now_iso(),
            },
        )
        return GateRunResult(0, "skipped", kind, message, run_id)

    client = GateClient(
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )
    futures_symbols = {
        key: item.model_dump(mode="json") for key, item in settings.futures_symbols.items()
    }
    batch = client.fetch_batch(
        tradfi_symbols=settings.tradfi_symbols,
        futures_symbols=futures_symbols,
    )
    quote_path = save_gate_quotes(
        paths,
        run_id=run_id,
        payload={
            "task": "gate-tradfi",
            "run_id": run_id,
            "kind": kind,
            "created_at": now_iso(),
            "quotes": {key: quote.model_dump(mode="json") for key, quote in batch.quotes.items()},
            "raw_response": batch.raw_response,
            "errors": batch.errors,
        },
    )

    brief = build_gate_brief(quotes=batch.quotes)
    if brief is None:
        message = "skipped: no valid title candidate data"
        append_brief_result(
            paths,
            date_key=date_key,
            payload={
                "task": "gate-tradfi",
                "run_id": run_id,
                "kind": kind,
                "status": "skipped",
                "message": message,
                "quote_path": str(quote_path),
                "errors": batch.errors,
                "created_at": now_iso(),
            },
        )
        return GateRunResult(0, "skipped", kind, message, run_id)

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
            "task": "gate-tradfi",
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
        return GateRunResult(1, "error", kind, push_result.error or "push failed", run_id)
    return GateRunResult(
        0,
        "success",
        kind,
        "dry-run completed" if dry_run else "pushed with isPublish=false",
        run_id,
        pushed=not dry_run,
    )
