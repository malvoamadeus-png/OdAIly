from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from packages.common.config import load_x_processing_settings
from packages.common.freshness import DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS, evaluate_source_freshness
from packages.common.heartbeat import HeartbeatThrottle
from packages.local_pipeline.client import LocalPipelineClient
from .classifier import MixedSourceClassifier, build_mixed_source_classifier
from .fetcher import fetch_article, fetch_discovered_pages, get_site_registry
from .models import (
    NonMainstreamMediaSettings,
    NonMainstreamMediaSource,
    SOURCE_GROUP_MIXED_SOURCE,
    SiteDefinition,
    SourceRunStats,
)
from .repository import (
    NonMainstreamMediaRepository,
    alert_only_task_source,
    alert_only_task_source_for_target,
    utc_now,
    write_flow_task_source,
    write_flow_task_source_for_target,
)


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
        config_reload_interval_seconds: float = 300.0,
        request_timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        pipeline_client: LocalPipelineClient | None = None,
        mixed_classifier: MixedSourceClassifier | None = None,
    ) -> None:
        self.repository = repository
        self.site_registry = site_registry or get_site_registry()
        self.config_reload_interval_seconds = max(30.0, float(config_reload_interval_seconds))
        self.request_timeout_seconds = request_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.pipeline_client = pipeline_client
        self.mixed_classifier = mixed_classifier
        self.processing_freshness_window_seconds = DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS
        self.worker_id = f"non_mainstream_media-{os.getpid()}"
        self._stop_event = threading.Event()
        self._config_changed = threading.Event()
        self._wake_event = threading.Event()
        self._next_due: dict[str, float] = {}
        self._snapshot = WorkerSnapshot(NonMainstreamMediaSettings(), [])
        self._last_snapshot_loaded_monotonic: float | None = None
        self._heartbeat = HeartbeatThrottle(
            component="non_mainstream_media",
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
        self._mixed_classifier_init_error: str | None = None
        try:
            settings = load_x_processing_settings()
            self.processing_freshness_window_seconds = settings.processing_freshness_window_seconds
            if self.mixed_classifier is None and settings.openai_api_key:
                self.mixed_classifier = build_mixed_source_classifier(settings)
        except Exception as exc:
            self._mixed_classifier_init_error = str(exc)

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
        self._last_snapshot_loaded_monotonic = time.monotonic()
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
        print(
            "[odaily] non-mainstream media worker started. "
            f"sources={len(snapshot.sources)} interval={snapshot.settings.global_interval_seconds}s "
            f"config_reload_interval={int(self.config_reload_interval_seconds)}s"
        )
        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._config_changed.is_set() or self._snapshot_reload_due(now):
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
                            + source_interval_seconds(item.source, snapshot.settings)
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

    def _sleep_seconds(self, snapshot: WorkerSnapshot) -> float:
        now = time.monotonic()
        reload_due = self._seconds_until_snapshot_reload(now)
        if not snapshot.sources:
            return max(0.5, min(60.0, reload_due))
        next_due = min(self._next_due.get(source.site_key, now + 5.0) for source in snapshot.sources)
        return max(0.5, min(60.0, next_due - now, reload_due))

    def _snapshot_reload_due(self, now: float) -> bool:
        return self._seconds_until_snapshot_reload(now) <= 0.0

    def _seconds_until_snapshot_reload(self, now: float) -> float:
        if self._last_snapshot_loaded_monotonic is None:
            return 0.0
        deadline = self._last_snapshot_loaded_monotonic + self.config_reload_interval_seconds
        return deadline - now

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
        classified_counts = {
            "classified_crypto": 0,
            "classified_ai": 0,
            "classified_discard": 0,
        }
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
            classified_target = None
            if source.source_group == SOURCE_GROUP_MIXED_SOURCE:
                if self.mixed_classifier is None:
                    detail_errors[page.detail_url] = self._mixed_classifier_init_error or "mixed source classifier unavailable"
                    continue
                try:
                    classification = self.mixed_classifier.classify_fulltext(
                        site_display_name=source.display_name,
                        title=article.title,
                        content=article.content,
                        metadata={
                            **article.metadata,
                            "categories": article.categories,
                            "tags": article.tags,
                            "author_names": article.author_names,
                        },
                    )
                except Exception as exc:
                    detail_errors[page.detail_url] = f"mixed classification failed: {exc}"
                    continue
                classified_counts[f"classified_{classification.target}"] += 1
                if classification.target == "discard":
                    if self.repository.mark_seen(source, article.canonical_url, seeded=False):
                        new_count += 1
                    continue
                classified_target = classification.target
                article.metadata["classification_model"] = "gpt-5.4-mini"
                article.metadata["classification_input_mode"] = "fulltext"
                if classification.reason:
                    article.metadata["classification_reason"] = classification.reason
            task_id = self.repository.save_task(source, article, classified_target=classified_target)
            if task_id is None:
                if self.repository.mark_seen(source, article.canonical_url, seeded=False):
                    new_count += 1
                continue
            try:
                self._submit_pipeline_job(
                    job_type="write_flow",
                    task_id=task_id,
                    source=(
                        write_flow_task_source_for_target(classified_target)
                        if classified_target in {"crypto", "ai"}
                        else write_flow_task_source(source)
                    ),
                    source_item_id=article.canonical_url,
                )
            except Exception as exc:
                detail_errors[article.canonical_url] = f"local pipeline enqueue failed: {exc}"
                continue
            if self.repository.mark_seen(source, article.canonical_url, seeded=False):
                new_count += 1
                saved_count += 1
        status = "success" if not detail_errors else "parse_failed"
        return SourceRunStats(
            source=source,
            status=status,
            candidate_count=len(pages),
            new_count=new_count,
            saved_count=saved_count,
            error=f"{len(detail_errors)} detail page(s) failed" if detail_errors else None,
            metadata={
                "detail_errors": detail_errors,
                **(classified_counts if source.source_group == SOURCE_GROUP_MIXED_SOURCE else {}),
            },
        )

    def _save_alert_only_tasks(self, source: NonMainstreamMediaSource, pages: list[Any]) -> SourceRunStats:
        unseen = self.repository.unseen_source_item_ids(source.site_key, [page.source_item_id for page in pages])
        new_count = 0
        saved_count = 0
        enqueue_errors: dict[str, str] = {}
        stale_count = 0
        classified_counts = {
            "classified_crypto": 0,
            "classified_ai": 0,
            "classified_discard": 0,
        }
        for page in pages:
            if page.source_item_id not in unseen:
                continue
            classified_target = None
            classification_metadata: dict[str, Any] | None = None
            if source.source_group == SOURCE_GROUP_MIXED_SOURCE:
                if self.mixed_classifier is None:
                    enqueue_errors[page.source_item_id] = self._mixed_classifier_init_error or "mixed source classifier unavailable"
                    continue
                try:
                    classification = self.mixed_classifier.classify_headline_excerpt(
                        site_display_name=source.display_name,
                        title=page.title,
                        excerpt=page.excerpt,
                        detail_url=page.detail_url,
                        metadata={},
                    )
                except Exception as exc:
                    enqueue_errors[page.source_item_id] = f"mixed classification failed: {exc}"
                    continue
                classified_counts[f"classified_{classification.target}"] += 1
                if classification.target == "discard":
                    if self.repository.mark_seen(source, page.source_item_id, seeded=False):
                        new_count += 1
                    continue
                classified_target = classification.target
                classification_metadata = {
                    "classification_model": "gpt-5.4-mini",
                    "classification_input_mode": "headline_excerpt",
                }
                if classification.reason:
                    classification_metadata["classification_reason"] = classification.reason
            try:
                prepared_page = self._prepare_alert_page(source, page)
            except Exception as exc:
                enqueue_errors[page.source_item_id] = f"detail enrichment failed: {exc}"
                continue
            if prepared_page is None:
                if self.repository.mark_seen(source, page.source_item_id, seeded=False):
                    new_count += 1
                    stale_count += 1
                continue
            task_id = self.repository.save_alert_task(
                source,
                prepared_page,
                classified_target=classified_target,
                classification_metadata=classification_metadata,
            )
            if task_id is None:
                if self.repository.mark_seen(source, page.source_item_id, seeded=False):
                    new_count += 1
                continue
            try:
                self._submit_pipeline_job(
                    job_type="alert_only",
                    task_id=task_id,
                    source=(
                        alert_only_task_source_for_target(classified_target)
                        if classified_target in {"crypto", "ai"}
                        else alert_only_task_source(source)
                    ),
                    source_item_id=page.source_item_id,
                )
            except Exception as exc:
                enqueue_errors[page.source_item_id] = str(exc)
                continue
            if self.repository.mark_seen(source, page.source_item_id, seeded=False):
                new_count += 1
                saved_count += 1
        status = "success" if not enqueue_errors else "parse_failed"
        return SourceRunStats(
            source=source,
            status=status,
            candidate_count=len(pages),
            new_count=new_count,
            saved_count=saved_count,
            error=f"{len(enqueue_errors)} local pipeline enqueue(s) failed" if enqueue_errors else None,
            metadata={
                "enqueue_errors": enqueue_errors,
                "stale_count": stale_count,
                **(classified_counts if source.source_group == SOURCE_GROUP_MIXED_SOURCE else {}),
            },
        )

    def _prepare_alert_page(
        self,
        source: NonMainstreamMediaSource,
        page: Any,
    ) -> Any | None:
        if source.site_key != "ft_crypto":
            return page
        site = self.site_registry.get(source.site_key)
        if site is None:
            raise ValueError(f"site not registered: {source.site_key}")
        article = fetch_article(
            site,
            page,
            timeout_seconds=self.request_timeout_seconds,
            max_attempts=self.max_attempts,
            backoff_seconds=self.backoff_seconds,
        )
        check = evaluate_source_freshness(
            article.published_at,
            window_seconds=self.processing_freshness_window_seconds,
        )
        if not check.is_fresh:
            return None
        published_at_raw = str(article.metadata.get("published_at_raw") or page.published_at_raw or "").strip() or None
        return replace(
            page,
            detail_url=article.canonical_url or page.detail_url,
            title=article.title or page.title,
            excerpt=article.excerpt or page.excerpt,
            published_at=article.published_at,
            published_at_raw=published_at_raw,
        )

    def _submit_pipeline_job(self, *, job_type: str, task_id: int, source: str, source_item_id: str) -> None:
        if self.pipeline_client is None:
            return
        self.pipeline_client.submit_job(
            job_type=job_type,  # type: ignore[arg-type]
            task_id=task_id,
            source=source,
            source_item_id=source_item_id,
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
            "classified_crypto": sum(int(item.metadata.get("classified_crypto", 0)) for item in stats),
            "classified_ai": sum(int(item.metadata.get("classified_ai", 0)) for item in stats),
            "classified_discard": sum(int(item.metadata.get("classified_discard", 0)) for item in stats),
            "sites": [
                {
                    "site_key": item.source.site_key,
                    "status": item.status,
                    "candidate_count": item.candidate_count,
                    "seeded_count": item.seeded_count,
                    "new_count": item.new_count,
                    "saved_count": item.saved_count,
                    "error": item.error,
                    "classified_crypto": int(item.metadata.get("classified_crypto", 0)),
                    "classified_ai": int(item.metadata.get("classified_ai", 0)),
                    "classified_discard": int(item.metadata.get("classified_discard", 0)),
                }
                for item in stats
            ],
        }
        try:
            self._heartbeat.send(
                status="ok" if ok else "failed",
                success=ok,
                error=heartbeat_error,
                metadata=metadata,
            )
        except Exception as exc:
            print(f"[odaily] non-mainstream media heartbeat failed: {exc}")


def source_interval_seconds(source: NonMainstreamMediaSource, settings: NonMainstreamMediaSettings) -> int:
    return source.interval_seconds or settings.global_interval_seconds
