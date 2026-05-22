from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any


NOOP_SUCCESS_WINDOW = timedelta(minutes=10)


def x_capture_attempt_fingerprint(
    *,
    status: str,
    candidate_count: int,
    seeded_count: int,
    new_count: int,
    saved_count: int,
    error: str | None,
    metadata: dict[str, Any],
) -> str:
    payload = {
        "status": status,
        "candidate_count": int(candidate_count),
        "seeded_count": int(seeded_count),
        "new_count": int(new_count),
        "saved_count": int(saved_count),
        "error": error or None,
        "metadata": metadata,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def should_sample_x_capture_attempt(
    *,
    status: str,
    new_count: int,
    saved_count: int,
    fingerprint: str,
    finished_at: datetime,
    previous_finished_at: datetime | None,
    previous_fingerprint: str | None,
    window: timedelta = NOOP_SUCCESS_WINDOW,
) -> bool:
    if status != "success":
        return True
    if int(new_count) > 0 or int(saved_count) > 0:
        return True
    if previous_finished_at is None:
        return True
    if previous_fingerprint != fingerprint:
        return True
    return finished_at - previous_finished_at >= window
