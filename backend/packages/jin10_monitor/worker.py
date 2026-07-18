from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from datetime import datetime

from packages.common.heartbeat import HeartbeatThrottle
from packages.common.source_exclusions import SourceExclusionMatcher
from packages.local_pipeline.client import LocalPipelineClient

from .fetcher import fetch_jin10_items
from .models import JIN10_SOURCE, Jin10Item, Jin10RunResult, Jin10Settings
from .repository import Jin10MonitorRepository, utc_now


FetchJin10Items = Callable[[Jin10Settings, float], list[Jin10Item]]


class Jin10MonitorWorker:
    def __init__(
        self,
        *,
        repository: Jin10MonitorRepository,
        request_timeout_seconds: float = 20.0,
        pipeline_client: LocalPipelineClient | None = None,
        fetch_items: FetchJin10Items | None = None,
        worker_id: str | None = None,
        exclusion_matcher: SourceExclusionMatcher | None = None,
    ) -> None:
        self.repository = repository
        self.request_timeout_seconds = request_timeout_seconds
        self.pipeline_client = pipeline_client
        self.fetch_items = fetch_items or default_fetch_items
        self.worker_id = worker_id or f"jin10_monitor-{os.getpid()}"
        self.exclusion_matcher = exclusion_matcher
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._heartbeat = HeartbeatThrottle(
            component="jin10_monitor",
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

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run_once(self) -> Jin10RunResult:
        finished_at: datetime | None = None
        try:
            settings = self.repository.get_settings()
            if not settings.enabled:
                result = Jin10RunResult(status="disabled")
                self._record_heartbeat(result)
                return result
            items = self.fetch_items(settings, self.request_timeout_seconds)
            result = self._process_items(items)
            finished_at = utc_now()
            self.repository.record_run(result, finished_at=finished_at)
            self._record_heartbeat(result)
            return result
        except Exception as exc:
            result = Jin10RunResult(status="failed", error=str(exc))
            finished_at = utc_now()
            try:
                self.repository.record_run(result, finished_at=finished_at)
            finally:
                self._record_heartbeat(result)
            return result

    def run_forever(self) -> None:
        print("[odaily] jin10 monitor started")
        while not self._stop_event.is_set():
            sleep_seconds = 60
            try:
                settings = self.repository.get_settings()
                sleep_seconds = max(10, settings.interval_seconds)
                result = self.run_once()
                print(
                    "[odaily] jin10 monitor round "
                    f"status={result.status} fetched={result.fetched} seeded={result.seeded} "
                    f"new={result.new} saved={result.saved} error={result.error or '-'}"
                )
            except Exception as exc:
                print(f"[odaily] jin10 monitor round failed: {exc}")
            self._wake_event.wait(sleep_seconds)
            self._wake_event.clear()

    def _process_items(self, items: list[Jin10Item]) -> Jin10RunResult:
        source_item_ids = [item.source_item_id for item in items]
        if not self.repository.has_seen_items():
            self.repository.mark_seeded(source_item_ids)
            return Jin10RunResult(
                status="success",
                fetched=len(items),
                seeded=len(source_item_ids),
                sample_titles=[item.title for item in items[:3]],
            )
        unseen = self.repository.unseen_source_item_ids(source_item_ids)
        new_count = 0
        saved_count = 0
        enqueue_errors: dict[str, str] = {}
        for item in items:
            if item.source_item_id not in unseen:
                continue
            if self.exclusion_matcher is not None and self.exclusion_matcher.is_excluded(
                scopes=["jin10"],
                texts=[item.title, item.content],
            ):
                self.repository.mark_seen(item.source_item_id, seeded=False)
                continue
            task_id = self.repository.save_task(item)
            if task_id is None:
                if self.repository.mark_seen(item.source_item_id, seeded=False):
                    new_count += 1
                continue
            try:
                self._submit_pipeline_job(task_id=task_id, source_item_id=item.source_item_id)
            except Exception as exc:
                enqueue_errors[item.source_item_id] = str(exc)
                continue
            if self.repository.mark_seen(item.source_item_id, seeded=False):
                new_count += 1
                saved_count += 1
        status = "success" if not enqueue_errors else "enqueue_failed"
        return Jin10RunResult(
            status=status,
            fetched=len(items),
            new=new_count,
            saved=saved_count,
            error=f"{len(enqueue_errors)} item(s) failed to enqueue" if enqueue_errors else None,
            sample_titles=[item.title for item in items[:3]],
        )

    def _submit_pipeline_job(self, *, task_id: int, source_item_id: str) -> None:
        if self.pipeline_client is None:
            return
        self.pipeline_client.submit_job(
            job_type="write_flow",
            task_id=task_id,
            source=JIN10_SOURCE,
            source_item_id=source_item_id,
        )

    def _record_heartbeat(self, result: Jin10RunResult) -> None:
        success = result.status in {"success", "disabled"}
        self._heartbeat.send(
            status="ok" if success else "failed",
            success=success,
            error=result.error if not success else None,
            metadata={
                "status": result.status,
                "fetched": result.fetched,
                "seeded": result.seeded,
                "new": result.new,
                "saved": result.saved,
                "sample_titles": result.sample_titles,
            },
        )


def default_fetch_items(settings: Jin10Settings, timeout_seconds: float) -> list[Jin10Item]:
    return fetch_jin10_items(
        endpoint_url=settings.endpoint_url,
        headers=settings.request_headers,
        channel=settings.channel,
        timeout_seconds=timeout_seconds,
    )
