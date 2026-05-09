from __future__ import annotations

import time
from dataclasses import dataclass

from packages.common.config import CompetitorMonitorSettings

from .fetchers import fetch_blockbeats, fetch_jinse, fetch_odaily, fetch_panews
from .repository import CompetitorMonitorRepository


@dataclass(frozen=True, slots=True)
class CompetitorRunResult:
    fetched: int
    task_inserted: int
    reference_inserted: int
    failed_sources: dict[str, str]


class CompetitorMonitorWorker:
    def __init__(self, *, repository: CompetitorMonitorRepository, settings: CompetitorMonitorSettings) -> None:
        self.repository = repository
        self.settings = settings

    def run_once(self) -> CompetitorRunResult:
        items = []
        failed: dict[str, str] = {}
        fetchers = {
            "blockbeats": lambda: fetch_blockbeats(api_key=self.settings.blockbeats_api_key, timeout_seconds=self.settings.request_timeout_seconds),
            "panews": lambda: fetch_panews(timeout_seconds=self.settings.request_timeout_seconds),
            "jinse": lambda: fetch_jinse(timeout_seconds=self.settings.request_timeout_seconds),
            "odaily": lambda: fetch_odaily(timeout_seconds=self.settings.request_timeout_seconds),
        }
        for source, fn in fetchers.items():
            try:
                items.extend(fn())
            except Exception as exc:
                failed[source] = str(exc)
        task_count, reference_count = self.repository.save_items(items)
        return CompetitorRunResult(
            fetched=len(items),
            task_inserted=task_count,
            reference_inserted=reference_count,
            failed_sources=failed,
        )

    def run_forever(self) -> None:
        print("[odaily] competitor monitor started")
        while True:
            result = self.run_once()
            print(
                "[odaily] competitor monitor round "
                f"fetched={result.fetched} tasks={result.task_inserted} references={result.reference_inserted} "
                f"failed={result.failed_sources}"
            )
            time.sleep(self.settings.fetch_interval_seconds)
