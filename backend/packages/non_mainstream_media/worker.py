from __future__ import annotations

import os
import random
import select
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .fetcher import fetch_article, fetch_discovered_pages, get_site_registry
from .models import NonMainstreamMediaSettings, NonMainstreamMediaSource, SiteDefinition, SourceRunStats
from .repository import CONFIG_NOTIFY_CHANNEL, NonMainstreamMediaRepository, PostgresNonMainstreamMediaRepository, utc_now


@dataclass(slots=True)
class WorkerSnapshot:
    settings: NonMainstreamMediaSettings
    sources: list[NonMainstreamMediaSource]


class NonMainstreamMediaWorker:
    def __init__(
        self,
        *,
        repository: NonMainstreamMediaRepository,
        site_registry: dict[str, SiteDefinition] | None = None,
        notify_wait_seconds: float = 5.0,
        notify_retry_seconds: float = 5.0,
        request_timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.repository = repository
        self.site_registry = site_registry or get_site_registry()
        self.notify_wait_seconds = notify_wait_seconds
        self.notify_retry_seconds = notify_retry_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.worker_id = f"non_mainstream_media-{os.getpid()}"
        self._stop_event = threading.Event()
        self._config_changed = threading.Event()
        self._wake_event = threading.Event()
        self._next_due: dict[str, float] = {}
        self._snapshot = WorkerSnapshot(NonMainstreamMediaSettings(), [])

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def request_config_reload(self) -> None:
        self._signal_config_changed()

    def _signal_config_changed(self) -> None:
        self._config_changed.set()
        self._wake_event.set()

    def load_snapshot(self) -> WorkerSnapshot:
        self.repository.sync_sources(list(self.site_registry.values()))
        settings = self.repository.get_settings()
        sources = self.repository.list_sources(include_disabled=False)
        self._snapshot = WorkerSnapshot(settings=settings, sources=sources)
        known = {source.site_key for source in sources}
        for site_key in list(self._next_due):
            if site_key not in known:
                del self._next_due[site_key]
        now = time.monotonic()
        for source in sources:
            self._next_due.setdefault(source.site_key, now + random.uniform(0, settings.jitter_seconds))
        return self._snapshot

    def run_once(self) -> list[SourceRunStats]:
        try:
            snapshot = self.load_snapshot()
            stats = self._process_sources(snapshot.sources)
            self._record_heartbeat(stats=stats, source_count=len(snapshot.sources))
            return stats
        except Exception as exc:
            self._record_heartbeat(stats=[], source_count=0, success=False, error=str(exc))
            raise

    def run_forever(self) -> None:
        snapshot = self.load_snapshot()
        notify_thread = self._start_notify_listener()
        print(
            "[odaily] non-mainstream media worker started. "
            f"sources={len(snapshot.sources)} interval={snapshot.settings.global_interval_seconds}s"
        )
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._config_changed.is_set():
                    self._config_changed.clear()
                    snapshot = self.load_snapshot()
                    print(f"[odaily] non-mainstream media config loaded. sources={len(snapshot.sources)}")
                due_sources = [source for source in snapshot.sources if self._next_due.get(source.site_key, 0) <= now]
                if due_sources:
                    stats = self._process_sources(due_sources)
                    self._record_heartbeat(stats=stats, source_count=len(snapshot.sources))
                    for item in stats:
                        self._next_due[item.source.site_key] = (
                            time.monotonic()
                            + snapshot.settings.global_interval_seconds
                            + random.uniform(0, snapshot.settings.jitter_seconds)
                        )
                        print(
                            "[odaily] non-mainstream media "
                            f"site={item.source.site_key} status={item.status} "
                            f"candidates={item.candidate_count} seeded={item.seeded_count} "
                            f"new={item.new_count} saved={item.saved_count} error={item.error or '-'}"
                        )
                else:
                    self._record_heartbeat(stats=[], source_count=len(snapshot.sources))
                self._wake_event.wait(self._sleep_seconds(snapshot))
                self._wake_event.clear()
        finally:
            self.stop()
            if notify_thread:
                notify_thread.join(timeout=2)

    def _sleep_seconds(self, snapshot: WorkerSnapshot) -> float:
        if not snapshot.sources:
            return 3600.0
        now = time.monotonic()
        next_due = min(self._next_due.get(source.site_key, now + 5.0) for source in snapshot.sources)
        return max(0.5, min(60.0, next_due - now))

    def _process_sources(self, sources: list[NonMainstreamMediaSource]) -> list[SourceRunStats]:
        return [self.process_source(source) for source in sources]

    def process_source(self, source: NonMainstreamMediaSource) -> SourceRunStats:
        started_at = utc_now()
        try:
            stats = self._process_source_inner(source)
        except Exception as exc:
            stats = SourceRunStats(source=source, status="fetch_failed", error=str(exc))
        finished_at = utc_now()
        self.repository.record_source_run(stats, started_at=started_at, finished_at=finished_at)
        return stats

    def _process_source_inner(self, source: NonMainstreamMediaSource) -> SourceRunStats:
        site = self.site_registry.get(source.site_key)
        if site is None:
            return SourceRunStats(source=source, status="unsupported_method", error=f"site not registered: {source.site_key}")
        if site.capture_method != "html_request":
            return SourceRunStats(
                source=source,
                status="unsupported_method",
                error=f"unsupported capture_method: {site.capture_method}",
            )
        pages = fetch_discovered_pages(
            site,
            timeout_seconds=self.request_timeout_seconds,
            max_attempts=self.max_attempts,
            backoff_seconds=self.backoff_seconds,
        )
        if not pages:
            return SourceRunStats(source=source, status="parse_empty", error="no detail pages discovered")
        discovered_ids = [page.source_item_id for page in pages]
        if source.seeded_at is None:
            self.repository.mark_source_seeded(source, discovered_ids)
            return SourceRunStats(
                source=source,
                status="success",
                candidate_count=len(pages),
                seeded_count=len(discovered_ids),
                metadata={"first_seen_seed": True},
            )
        if site.pipeline_mode == "alert_only":
            return self._save_alert_only_tasks(source, pages)
        unseen = self.repository.unseen_source_item_ids(source.site_key, discovered_ids)
        new_count = 0
        saved_count = 0
        detail_errors: dict[str, str] = {}
        for page in pages:
            if page.source_item_id not in unseen:
                continue
            try:
                article = fetch_article(
                    site,
                    page,
                    timeout_seconds=self.request_timeout_seconds,
                    max_attempts=self.max_attempts,
                    backoff_seconds=self.backoff_seconds,
                )
            except Exception as exc:
                detail_errors[page.detail_url] = str(exc)
                continue
            if self.repository.mark_seen(source, article.canonical_url, seeded=False):
                new_count += 1
            if self.repository.save_task(source, article):
                saved_count += 1
        status = "success" if not detail_errors else "parse_failed"
        return SourceRunStats(
            source=source,
            status=status,
            candidate_count=len(pages),
            new_count=new_count,
            saved_count=saved_count,
            error=f"{len(detail_errors)} detail page(s) failed" if detail_errors else None,
            metadata={"detail_errors": detail_errors},
        )

    def _save_alert_only_tasks(self, source: NonMainstreamMediaSource, pages: list[Any]) -> SourceRunStats:
        unseen = self.repository.unseen_source_item_ids(source.site_key, [page.source_item_id for page in pages])
        new_count = 0
        saved_count = 0
        for page in pages:
            if page.source_item_id not in unseen:
                continue
            if self.repository.mark_seen(source, page.source_item_id, seeded=False):
                new_count += 1
            if self.repository.save_alert_task(source, page):
                saved_count += 1
        return SourceRunStats(
            source=source,
            status="success",
            candidate_count=len(pages),
            new_count=new_count,
            saved_count=saved_count,
        )

    def _record_heartbeat(
        self,
        *,
        stats: list[SourceRunStats],
        source_count: int,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        failed = [item for item in stats if item.status != "success"]
        ok = success if success is not None else not failed
        heartbeat_error = error or ("; ".join(item.error or item.status for item in failed) if failed else None)
        metadata: dict[str, Any] = {
            "sources": source_count,
            "processed_sources": len(stats),
            "failed_sources": len(failed),
            "saved_count": sum(item.saved_count for item in stats),
            "new_count": sum(item.new_count for item in stats),
            "seeded_count": sum(item.seeded_count for item in stats),
            "sites": [
                {
                    "site_key": item.source.site_key,
                    "status": item.status,
                    "candidate_count": item.candidate_count,
                    "seeded_count": item.seeded_count,
                    "new_count": item.new_count,
                    "saved_count": item.saved_count,
                    "error": item.error,
                }
                for item in stats
            ],
        }
        try:
            self.repository.record_worker_heartbeat(
                component="non_mainstream_media",
                worker_id=self.worker_id,
                status="ok" if ok else "failed",
                success=ok,
                error=heartbeat_error,
                metadata=metadata,
            )
        except Exception as exc:
            print(f"[odaily] non-mainstream media heartbeat failed: {exc}")

    def _start_notify_listener(self) -> threading.Thread | None:
        if not isinstance(self.repository, PostgresNonMainstreamMediaRepository):
            return None
        thread = threading.Thread(
            target=self._listen_for_config_changes,
            name="non-mainstream-media-config-listener",
            daemon=True,
        )
        thread.start()
        return thread

    def _listen_for_config_changes(self) -> None:
        repository = self.repository
        if not isinstance(repository, PostgresNonMainstreamMediaRepository):
            return
        while not self._stop_event.is_set():
            try:
                with repository._connect() as conn:
                    conn.autocommit = True
                    conn.execute(f"LISTEN {CONFIG_NOTIFY_CHANNEL}")
                    self._signal_config_changed()
                    print(f"[odaily] non-mainstream media listening for {CONFIG_NOTIFY_CHANNEL}")
                    while not self._stop_event.is_set():
                        if select.select([conn], [], [], self.notify_wait_seconds)[0]:
                            for _notify in conn.notifies(timeout=0, stop_after=100):
                                self._signal_config_changed()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._signal_config_changed()
                print(
                    "[odaily] non-mainstream media config listener reconnecting "
                    f"in {self.notify_retry_seconds:g}s: {exc}"
                )
                self._stop_event.wait(self.notify_retry_seconds)
