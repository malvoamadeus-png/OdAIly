from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from uuid import uuid4

from dotenv import load_dotenv

from packages.common.paths import AppPaths, ensure_runtime_dirs
from packages.common.storage import append_brief_result
from packages.common.time_utils import now_iso, today_key
from packages.okx_stock_brief import (
    OKXPublicClient,
    _default_as_of_date,
    generate_brief,
)
from packages.publisher import PushClient, PushResult


DEFAULT_PUSH_ENDPOINT = "http://47.113.217.70:8501/push/data"


@dataclass(slots=True)
class OKXStockBriefRunResult:
    exit_code: int
    status: str
    kind: str
    message: str
    run_id: str
    pushed: bool = False


def build_publish_title(total_volume: object) -> str:
    """Build the fixed title using whole 100-million-unit buckets."""
    try:
        total = Decimal(str(total_volume))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("total_volume must be numeric") from exc
    if total < 0:
        raise ValueError("total_volume must be non-negative")
    whole_hundred_million = (total / Decimal("100000000")).to_integral_value(rounding=ROUND_FLOOR)
    return f"OKX美股产品过去30日成交额超{whole_hundred_million}亿美元"


def _push_client() -> PushClient:
    load_dotenv()
    return PushClient(
        endpoint=os.getenv("ODAILY_PUSH_ENDPOINT") or DEFAULT_PUSH_ENDPOINT,
        timeout_seconds=float(os.getenv("OKX_STOCK_BRIEF_PUSH_TIMEOUT_SECONDS") or 30.0),
        max_attempts=int(os.getenv("OKX_STOCK_BRIEF_PUSH_MAX_ATTEMPTS") or 3),
        backoff_seconds=float(os.getenv("OKX_STOCK_BRIEF_PUSH_BACKOFF_SECONDS") or 1.0),
    )


def run_okx_stock_brief_once(
    *,
    kind: str,
    config_path: str | None,
    paths: AppPaths,
    dry_run_override: bool | None = None,
    force: bool = False,
) -> OKXStockBriefRunResult:
    del config_path, force
    ensure_runtime_dirs(paths)
    run_id = f"okx-stock-brief-{now_iso().replace(':', '').replace('+', '-')}-{uuid4().hex[:8]}"
    dry_run = bool(dry_run_override) if dry_run_override is not None else False

    try:
        result = generate_brief(OKXPublicClient(), as_of_date=_default_as_of_date())
        title = build_publish_title(result.total_volume)
        push_result: PushResult = _push_client().push(
            title=title,
            content=result.render(),
            dry_run=dry_run,
            is_publish=True,
            is_push=False,
            idempotency_key=f"okx-stock-brief:{result.as_of_date.isoformat()}",
            origin_key="scheduled_brief",
            outlet_category="market",
            priority_status="not_applicable",
        )
    except Exception as exc:
        message = f"error: {exc}"
        append_brief_result(
            paths,
            date_key=today_key(),
            payload={
                "task": "okx-stock-brief",
                "run_id": run_id,
                "kind": kind,
                "status": "error",
                "message": message,
                "created_at": now_iso(),
            },
        )
        return OKXStockBriefRunResult(1, "error", kind, message, run_id)

    status = "success" if push_result.ok else "error"
    message = "dry-run completed" if dry_run and push_result.ok else (
        "published with isPublish=true,isPush=false" if push_result.ok else push_result.error or "push failed"
    )
    append_brief_result(
        paths,
        date_key=today_key(),
        payload={
            "task": "okx-stock-brief",
            "run_id": run_id,
            "kind": kind,
            "status": status,
            "dry_run": dry_run,
            "title": title,
            "content": result.render(),
            "isPublish": True,
            "isPush": False,
            "as_of_date": result.as_of_date.isoformat(),
            "window_start": result.window_start.isoformat(),
            "brief": result.to_json(),
            "push_result": push_result.model_dump(mode="json"),
            "created_at": now_iso(),
        },
    )
    if not push_result.ok:
        return OKXStockBriefRunResult(1, "error", kind, message, run_id)
    return OKXStockBriefRunResult(0, "success", kind, message, run_id, pushed=not dry_run)
