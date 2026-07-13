from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS = 1200


@dataclass(frozen=True, slots=True)
class FreshnessCheck:
    is_fresh: bool
    reason: str
    published_at: datetime | None
    reference_time: datetime
    delay_seconds: float | None
    window_seconds: int


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def evaluate_source_freshness(
    published_at: datetime | None,
    *,
    reference_time: datetime | None = None,
    window_seconds: int = DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
) -> FreshnessCheck:
    reference = ensure_utc(reference_time) or datetime.now(UTC)
    source_time = ensure_utc(published_at)
    window = max(1, int(window_seconds))
    if source_time is None:
        return FreshnessCheck(
            is_fresh=False,
            reason="missing_published_at",
            published_at=None,
            reference_time=reference,
            delay_seconds=None,
            window_seconds=window,
        )
    delay_seconds = (reference - source_time).total_seconds()
    if delay_seconds > window:
        return FreshnessCheck(
            is_fresh=False,
            reason="expired_by_freshness_gate",
            published_at=source_time,
            reference_time=reference,
            delay_seconds=delay_seconds,
            window_seconds=window,
        )
    return FreshnessCheck(
        is_fresh=True,
        reason="fresh",
        published_at=source_time,
        reference_time=reference,
        delay_seconds=delay_seconds,
        window_seconds=window,
    )


def freshness_error(check: FreshnessCheck) -> str:
    if check.reason == "missing_published_at":
        return f"expired_by_freshness_gate: missing_published_at window_seconds={check.window_seconds}"
    published_at = check.published_at.isoformat() if check.published_at else "-"
    delay = int(check.delay_seconds) if check.delay_seconds is not None else "-"
    return (
        "expired_by_freshness_gate: "
        f"reason={check.reason} published_at={published_at} "
        f"delay_seconds={delay} window_seconds={check.window_seconds}"
    )
