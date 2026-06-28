from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Iterator
from uuid import uuid4

from packages.common.config import CompetitorMonitorSettings
from packages.common.freshness import evaluate_source_freshness
from packages.common.heartbeat import HeartbeatThrottle
from packages.common.paths import get_paths
from packages.local_pipeline.client import LocalPipelineClient
from packages.x_processing.searcher import SearchCache, SearchDocument

from .fetchers import NewsflashItem
from .fetchers import fetch_blockbeats, fetch_jinse, fetch_odaily, fetch_panews
from .events import NewsflashEventAggregator
from .repository import CompetitorMonitorRepository, parse_datetime


NEWSFLASH_SOURCES = ("blockbeats", "panews", "jinse", "odaily")


class EventAssignmentTimeout(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompetitorRunResult:
    fetched: int
    task_inserted: int
    reference_inserted: int
    events_updated: int
    filtered: int
    event_elapsed_seconds: float
    event_error: str | None
    expired_for_tasks: int
    expired_for_tasks_by_source: dict[str, int]
    failed_sources: dict[str, str]
    fetched_by_source: dict[str, int]
    filtered_by_source: dict[str, int]
    sample_titles_by_source: dict[str, list[str]]


class CompetitorMonitorWorker:
    def __init__(
        self,
        *,
        repository: CompetitorMonitorRepository,
        settings: CompetitorMonitorSettings,
        pipeline_client: LocalPipelineClient | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.pipeline_client = pipeline_client
        self.worker_id = f"competitor_monitor-{os.getpid()}"
        self._event_aggregator: NewsflashEventAggregator | None = None
        self._search_cache = SearchCache(_search_cache_path_for_repository(self.repository))
        self._heartbeat = HeartbeatThrottle(
            component="competitor_monitor",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )

    def run_once(self) -> CompetitorRunResult:
        items = []
        failed: dict[str, str] = {}
        fetched_by_source = {source: 0 for source in NEWSFLASH_SOURCES}
        sample_titles_by_source: dict[str, list[str]] = {source: [] for source in NEWSFLASH_SOURCES}
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
                    source_items = fn()
                    fetched_by_source[source] = len(source_items)
                    sample_titles_by_source[source] = [item.title for item in source_items[:3]]
                    if not source_items:
                        print(f"[odaily] competitor monitor source empty source={source}")
                    items.extend(source_items)
                except Exception as exc:
                    failed[source] = str(exc)
                    print(f"[odaily] competitor monitor source failed source={source} error={exc}")
            exclude_terms = self._load_exclude_terms()
            filtered_items, filtered_count, filtered_by_source = exclude_newsflash_items(items, exclude_terms)
            event_elapsed_seconds = 0.0
            event_error = None
            event_started = time.monotonic()
            try:
                event_ids = self._assign_events(filtered_items)
            except Exception as exc:
                event_error = str(exc)
                event_ids = set()
                print(f"[odaily] competitor event aggregation failed error={exc}")
            finally:
                event_elapsed_seconds = time.monotonic() - event_started
            task_items, expired_for_tasks, expired_for_tasks_by_source = self._filter_items_for_tasks(filtered_items)
            if self.pipeline_client is None:
                task_count, reference_count = self.repository.save_items(task_items)
            else:
                task_records, reference_count = self.repository.save_items_for_pipeline(task_items)
                for item, task_id in task_records:
                    self.pipeline_client.submit_job(
                        job_type="write_flow",
                        task_id=task_id,
                        source=item.source,
                        source_item_id=item.source_item_id,
                    )
                task_count = len(task_records)
            self._mirror_odaily_references(task_items)
            result = CompetitorRunResult(
                fetched=len(items),
                task_inserted=task_count,
                reference_inserted=reference_count,
                events_updated=len(event_ids),
                filtered=filtered_count,
                event_elapsed_seconds=event_elapsed_seconds,
                event_error=event_error,
                expired_for_tasks=expired_for_tasks,
                expired_for_tasks_by_source=expired_for_tasks_by_source,
                failed_sources=failed,
                fetched_by_source=fetched_by_source,
                filtered_by_source=filtered_by_source,
                sample_titles_by_source=sample_titles_by_source,
            )
        except Exception as exc:
            result = CompetitorRunResult(
                fetched=len(items),
                task_inserted=0,
                reference_inserted=0,
                events_updated=0,
                filtered=0,
                event_elapsed_seconds=0.0,
                event_error=None,
                expired_for_tasks=0,
                expired_for_tasks_by_source={source: 0 for source in NEWSFLASH_SOURCES},
                failed_sources={"worker": str(exc)},
                fetched_by_source=fetched_by_source,
                filtered_by_source={source: 0 for source in NEWSFLASH_SOURCES},
                sample_titles_by_source=sample_titles_by_source,
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
                f"expired_for_tasks={result.expired_for_tasks} "
                f"event_elapsed_seconds={result.event_elapsed_seconds:.1f} "
                f"event_error={result.event_error or '-'} "
                f"fetched_by_source={result.fetched_by_source} "
                f"filtered_by_source={result.filtered_by_source} "
                f"expired_for_tasks_by_source={result.expired_for_tasks_by_source} "
                f"failed={result.failed_sources}"
            )
            time.sleep(self.settings.fetch_interval_seconds)

    def _load_exclude_terms(self) -> list[str]:
        if not hasattr(self.repository, "list_enabled_filter_keywords"):
            return []
        try:
            return self.repository.list_enabled_filter_keywords()
        except Exception as exc:
            print(f"[odaily] competitor monitor exclude keywords load failed: {exc}")
            return []

    def _filter_items_for_tasks(self, items: list[NewsflashItem]) -> tuple[list[NewsflashItem], int, dict[str, int]]:
        kept: list[NewsflashItem] = []
        expired_by_source = {source: 0 for source in NEWSFLASH_SOURCES}
        expired_count = 0
        reference_time = None
        for item in items:
            if item.source == "odaily":
                kept.append(item)
                continue
            published_at = parse_datetime(item.published_at)
            check = evaluate_source_freshness(
                published_at,
                reference_time=reference_time,
                window_seconds=self.settings.processing_freshness_window_seconds,
            )
            reference_time = check.reference_time
            if check.is_fresh:
                kept.append(item)
                continue
            expired_count += 1
            expired_by_source[item.source] = expired_by_source.get(item.source, 0) + 1
            delay = int(check.delay_seconds) if check.delay_seconds is not None else "-"
            published = check.published_at.isoformat() if check.published_at else "-"
            print(
                "[odaily] competitor monitor freshness skipped task "
                f"source={item.source} source_item_id={item.source_item_id} "
                f"published_at={published} delay_seconds={delay} "
                f"window_seconds={check.window_seconds} action=skip_tasks_keep_newsflash"
            )
        return kept, expired_count, expired_by_source

    def _record_heartbeat(self, result: CompetitorRunResult) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self._heartbeat.send(
                status="ok" if not result.failed_sources else "failed",
                success=not bool(result.failed_sources),
                error=str(result.failed_sources) if result.failed_sources else None,
                metadata={
                    "fetched": result.fetched,
                    "task_inserted": result.task_inserted,
                    "reference_inserted": result.reference_inserted,
                    "events_updated": result.events_updated,
                    "filtered": result.filtered,
                    "expired_for_tasks": result.expired_for_tasks,
                    "event_elapsed_seconds": round(result.event_elapsed_seconds, 3),
                    "event_error": result.event_error,
                    "fetched_by_source": result.fetched_by_source,
                    "filtered_by_source": result.filtered_by_source,
                    "expired_for_tasks_by_source": result.expired_for_tasks_by_source,
                    "sample_titles_by_source": result.sample_titles_by_source,
                    "failed_sources": result.failed_sources,
                    "event_assignment_timeout_seconds": self.settings.event_assignment_timeout_seconds,
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
        print(
            "[odaily] competitor event aggregation started "
            f"items={len(items)} timeout_seconds={self.settings.event_assignment_timeout_seconds}"
        )
        started = time.monotonic()
        try:
            with _event_assignment_deadline(self.settings.event_assignment_timeout_seconds):
                event_ids = self._event_aggregator.assign_items(items)
        finally:
            elapsed = time.monotonic() - started
            print(f"[odaily] competitor event aggregation finished elapsed_seconds={elapsed:.1f}")
        return event_ids

    def _mirror_odaily_references(self, items: list[NewsflashItem]) -> None:
        odaily_documents = [
            SearchDocument(
                doc_type="odaily_reference",
                doc_id=item.source_item_id,
                title=item.title,
                content=item.content,
                source="odaily",
                source_url=item.source_url,
                published_at=parse_datetime(item.published_at),
                status="published",
                metadata=item.metadata,
            )
            for item in items
            if item.source == "odaily"
        ]
        if odaily_documents:
            self._search_cache.upsert_documents(odaily_documents)


@contextmanager
def _event_assignment_deadline(timeout_seconds: int) -> Iterator[None]:
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _raise_timeout(signum: int, frame: FrameType | None) -> None:
        raise EventAssignmentTimeout(f"event aggregation exceeded {timeout_seconds}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def normalize_exclude_term(value: str) -> str:
    return " ".join(value.strip().lower().split())


def match_exclude_terms(item: NewsflashItem, terms: list[str]) -> list[str]:
    if item.source == "odaily":
        return []
    haystack = normalize_exclude_term(f"{item.title}\n{item.content}")
    matches: list[str] = []
    for term in terms:
        normalized = normalize_exclude_term(term)
        if normalized and normalized in haystack:
            matches.append(term)
    return matches


def exclude_newsflash_items(items: list[NewsflashItem], terms: list[str]) -> tuple[list[NewsflashItem], int, dict[str, int]]:
    filtered_by_source = {source: 0 for source in NEWSFLASH_SOURCES}
    if not terms:
        return items, 0, filtered_by_source
    kept: list[NewsflashItem] = []
    filtered = 0
    for item in items:
        matches = match_exclude_terms(item, terms)
        if matches:
            filtered += 1
            filtered_by_source[item.source] = filtered_by_source.get(item.source, 0) + 1
            print(
                "[odaily] competitor monitor excluded "
                f"source={item.source} source_item_id={item.source_item_id} "
                f"matched_terms={matches} title={item.title}"
            )
            continue
        kept.append(item)
    return kept, filtered, filtered_by_source


def filter_competitor_items(items: list[NewsflashItem], terms: list[str]) -> tuple[list[NewsflashItem], int]:
    kept, filtered, _ = exclude_newsflash_items(items, terms)
    return kept, filtered


def match_filter_terms(item: NewsflashItem, terms: list[str]) -> list[str]:
    return match_exclude_terms(item, terms)


def _search_cache_path_for_repository(repository) -> Any:
    paths = get_paths()
    if type(repository).__name__.startswith("Postgres"):
        return paths.searcher_cache_path
    return paths.processed_dir / "searcher" / f"test-competitor-searcher-{uuid4().hex}.sqlite"
