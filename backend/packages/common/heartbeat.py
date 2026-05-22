from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


HeartbeatWriter = Callable[[str, str, str, bool, str | None, dict[str, Any] | None], None]


@dataclass(slots=True)
class HeartbeatThrottle:
    component: str
    worker_id: str
    writer: HeartbeatWriter
    interval_seconds: float = 60.0
    _last_flush_monotonic: float | None = field(default=None, init=False, repr=False)
    _last_status: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.interval_seconds = max(1.0, float(self.interval_seconds))

    def send(
        self,
        *,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        force: bool = False,
    ) -> bool:
        now = time.monotonic()
        should_flush = force or not success
        if self._last_flush_monotonic is None:
            should_flush = True
        if self._last_status is not None and self._last_status != status:
            should_flush = True
        if not should_flush and now - self._last_flush_monotonic >= self.interval_seconds:
            should_flush = True
        if not should_flush:
            return False
        self.writer(self.component, self.worker_id, status, success, error, metadata)
        self._last_flush_monotonic = now
        self._last_status = status
        return True
