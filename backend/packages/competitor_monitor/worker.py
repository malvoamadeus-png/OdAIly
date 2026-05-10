from __future__ import annotations

import os
import time
from dataclasses import dataclass

from packages.common.config import CompetitorMonitorSettings
from packages.x_processing.models import COMPETITOR_SOURCES

from .fetchers import NewsflashItem
from .fetchers import fetch_blockbeats, fetch_jinse, fetch_odaily, fetch_panews
from .events import NewsflashEventAggregator
from .repository import CompetitorMonitorRepository


@dataclass(frozen=True, slots=True)
class CompetitorRunResult:
    fetched: int
    task_inserted: int
    reference_inserted: int
    events_updated: int
    filtered: int
    failed_sources: dict[str, str]


class CompetitorMonitorWorker:
    def __init__(self, *, repository: CompetitorMonitorRepository, settings: CompetitorMonitorSettings) -> None:
        self.repository = repository
        self.settings = settings
        self.worker_id = f"competitor_monitor-{os.getpid()}"
        self._event_aggregator: NewsflashEventAggregator | None = None

    def run_once(self) -> CompetitorRunResult:
        items = []
        failed: dict[str, str] = {}
        try:
            fetchers = {
                "blockbeats": lambda: fetch_blockbeats(
                    api_key=self.settings.blockbeats_api_key,
                    timeout_seconds=self.settings.request_timeout_seconds,
                ),
                "panews": lambda: fetch_panews(timeout_seconds=self.settings.request_timeout_seconds),
                "jinse": lambda: fetch_jinse(timeout_seconds=self.settings.request_timeout_seconds),
                "odaily": lambda: fetch_odaily(timeout_seconds=self.settings.request_timeout_seconds),
            }
            for source, fn in fetchers.items():
                try:
                    items.extend(fn())
                except Exception as exc:
                    failed[source] = str(exc)
            filter_terms = self._load_filter_terms()
            filtered_items, filtered_count = filter_competitor_items(items, filter_terms)
            try:
                event_ids = self._assign_events(filtered_items)
            except Exception as exc:
                failed["event_aggregator"] = str(exc)
                event_ids = set()
            task_count, reference_count = self.repository.save_items(filtered_items)
            result = CompetitorRunResult(
                fetched=len(items),
                task_inserted=task_count,
                reference_inserted=reference_count,
                events_updated=len(event_ids),
                filtered=filtered_count,
                failed_sources=failed,
            )
        except Exception as exc:
            result = CompetitorRunResult(
                fetched=len(items),
                task_inserted=0,
                reference_inserted=0,
                events_updated=0,
                filtered=0,
                failed_sources={"worker": str(exc)},
            )
            self._record_heartbeat(result)
            raise
        self._record_heartbeat(result)
        return result

    def run_forever(self) -> None:
        print("[odaily] competitor monitor started")
        while True:
            result = self.run_once()
            print(
                "[odaily] competitor monitor round "
                f"fetched={result.fetched} tasks={result.task_inserted} references={result.reference_inserted} "
                f"events={result.events_updated} "
                f"filtered={result.filtered} "
                f"failed={result.failed_sources}"
            )
            time.sleep(self.settings.fetch_interval_seconds)

    def _load_filter_terms(self) -> list[str]:
        if not hasattr(self.repository, "list_enabled_filter_keywords"):
            return []
        try:
            return self.repository.list_enabled_filter_keywords()
        except Exception as exc:
            print(f"[odaily] competitor monitor filter keywords load failed: {exc}")
            return []

    def _record_heartbeat(self, result: CompetitorRunResult) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self.repository.record_worker_heartbeat(
                component="competitor_monitor",
                worker_id=self.worker_id,
                status="ok" if not result.failed_sources else "failed",
                success=not bool(result.failed_sources),
                error=str(result.failed_sources) if result.failed_sources else None,
                metadata={
                    "fetched": result.fetched,
                    "task_inserted": result.task_inserted,
                    "reference_inserted": result.reference_inserted,
                    "events_updated": result.events_updated,
                    "filtered": result.filtered,
                    "failed_sources": result.failed_sources,
                },
            )
        except Exception as exc:
            print(f"[odaily] competitor monitor heartbeat failed: {exc}")

    def _assign_events(self, items: list[NewsflashItem]) -> set[str]:
        if not hasattr(self.repository, "upsert_newsflash_items"):
            return set()
        if not items:
            return set()
        if self._event_aggregator is None:
            self._event_aggregator = NewsflashEventAggregator(repository=self.repository, settings=self.settings)
        return self._event_aggregator.assign_items(items)


def normalize_filter_term(value: str) -> str:
    return " ".join(value.strip().lower().split())


def match_filter_terms(item: NewsflashItem, terms: list[str]) -> list[str]:
    haystack = normalize_filter_term(f"{item.title}\n{item.content}")
    matches: list[str] = []
    for term in terms:
        normalized = normalize_filter_term(term)
        if normalized and normalized in haystack:
            matches.append(term)
    return matches


def filter_competitor_items(items: list[NewsflashItem], terms: list[str]) -> tuple[list[NewsflashItem], int]:
    if not terms:
        return items, 0
    kept: list[NewsflashItem] = []
    filtered = 0
    for item in items:
        if item.source not in COMPETITOR_SOURCES:
            kept.append(item)
            continue
        matches = match_filter_terms(item, terms)
        if matches:
            filtered += 1
            print(
                "[odaily] competitor monitor filtered "
                f"source={item.source} source_item_id={item.source_item_id} "
                f"matched_terms={matches} title={item.title}"
            )
            continue
        kept.append(item)
    return kept, filtered
