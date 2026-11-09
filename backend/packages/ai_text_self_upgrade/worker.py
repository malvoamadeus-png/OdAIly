from __future__ import annotations

import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from .service import AITextSelfUpgradeService


BEIJING = ZoneInfo("Asia/Shanghai")


def current_run_key(now: datetime) -> str:
    local = now.astimezone(BEIJING)
    bucket_hour = local.hour - local.hour % 4
    bucket = local.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
    return bucket.isoformat(timespec="minutes")


class AITextSelfUpgradeWorker:
    def __init__(self, service: AITextSelfUpgradeService, *, interval_seconds: int = 30) -> None:
        self.service = service
        self.interval_seconds = max(10, interval_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="ai-text-self-upgrade-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_due(self, *, now: datetime | None = None) -> dict[str, int] | None:
        run_key = current_run_key(now or datetime.now(UTC))
        repository = self.service.repository
        if repository.has_run(run_key) or not repository.start_run(run_key):
            return None
        try:
            return self.service.run_batch(run_key=run_key)
        except Exception as exc:
            self.service.fail_run(run_key, f"{type(exc).__name__}: {exc}")
            return {"cases_found": 0, "cases_completed": 0, "cases_failed": 0, "cases_unlinked": 0}

    def _run(self) -> None:
        while not self._stop.is_set():
            result = self.run_due()
            if result and any(result.values()):
                print("[odaily] AI text self-upgrade " + " ".join(f"{key}={value}" for key, value in result.items()), flush=True)
            self._stop.wait(self.interval_seconds)
