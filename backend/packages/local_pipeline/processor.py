from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from packages.common.config import (
    ExternalMediaAlertSettings,
    XProcessingSettings,
)
from packages.common.heartbeat import HeartbeatThrottle
from packages.common.paths import ensure_runtime_dirs, get_paths
from packages.external_media_alert import PostgresExternalMediaAlertRepository
from packages.external_media_alert.models import (
    AI_SOURCE_ALERT_TASK_SOURCE,
    ALERT_TASK_SOURCE,
)
from packages.external_media_alert.worker import ExternalMediaAlertWorker, HandledStageError as AlertHandledStageError
from packages.x_capture.repository import PostgresXCaptureRepository
from packages.x_processing.models import (
    AI_SOURCE,
    COMPETITOR_SOURCES,
    JIN10_SOURCE,
    NON_MAINSTREAM_MEDIA_SOURCE,
    SEARCH_FIRST_SOURCES,
    TaskRecord,
)
from packages.x_processing.repository import PostgresXProcessingRepository
from packages.x_processing.odaily_reference_source import fetch_odaily_reference_documents_from_api
from packages.x_processing.searcher import SearchCache
from packages.x_processing.worker import HandledStageError as XHandledStageError
from packages.x_processing import XProcessingWorker

from .queue import LocalPipelineJob


X_TERMINAL_STATUSES: set[str] = {
    "auto_published",
    "ready_review",
    "discarded",
    "duplicate",
    "expired",
    "publisher_failed",
    "judge_failed",
    "search_failed",
    "write_failed",
    "format_failed",
    "legacy_skipped",
}

ALERT_TERMINAL_STATUSES: set[str] = {
    "discarded",
    "duplicate",
    "notified",
    "domain_failed",
    "search_failed",
    "notify_failed",
    "legacy_skipped",
}


@dataclass(frozen=True)
class LocalPipelineRunResult:
    task_id: int
    status: str
    message: str


class LocalPipelineProcessor:
    def __init__(
        self,
        *,
        database_url: str | None,
        x_settings: XProcessingSettings,
        alert_settings: ExternalMediaAlertSettings,
    ) -> None:
        self.database_url = database_url
        self.paths = get_paths()
        ensure_runtime_dirs(self.paths)
        self.x_repository = PostgresXProcessingRepository(database_url)
        self.alert_repository = PostgresExternalMediaAlertRepository(database_url)
        self.x_capture_repository = PostgresXCaptureRepository(database_url)
        self.x_settings = x_settings
        self.alert_settings = alert_settings
        self.worker_id = f"local_pipeline-{os.getpid()}"
        self._x_workers: dict[str, XProcessingWorker] = {}
        self._alert_workers: dict[str, ExternalMediaAlertWorker] = {}
        if self.paths.searcher_cache_path is None:
            raise ValueError("searcher cache path is not configured")
        self._search_cache = SearchCache(self.paths.searcher_cache_path)
        self._search_cache_warmer_stop = threading.Event()
        self._search_cache_warmer_thread: threading.Thread | None = None
        self._heartbeat = HeartbeatThrottle(
            component="local_pipeline",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.x_repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )

    def init_remote_schema(self) -> None:
        self.x_capture_repository.init_schema()
        self.x_repository.init_schema()
        self.x_repository.seed_prompt_templates(root_dir=self.paths.root_dir)
        self.alert_repository.init_schema()

    def start_background_tasks(self) -> None:
        if self._search_cache_warmer_thread is not None and self._search_cache_warmer_thread.is_alive():
            return
        self._search_cache_warmer_stop.clear()
        self._search_cache_warmer_thread = threading.Thread(
            target=self._run_search_cache_warmer,
            name="search-cache-warmer",
            daemon=True,
        )
        self._search_cache_warmer_thread.start()

    def stop_background_tasks(self) -> None:
        self._search_cache_warmer_stop.set()
        thread = self._search_cache_warmer_thread
        if thread is not None:
            thread.join(timeout=5)

    def warm_search_cache_once(self) -> int:
        since = datetime.now(UTC) - timedelta(hours=self.x_settings.search_window_hours)
        try:
            documents = fetch_odaily_reference_documents_from_api(
                since=since,
                timeout_seconds=self.x_settings.request_timeout_seconds,
            )
        except Exception as exc:
            print(f"[odaily] search cache warm odaily api failed; fallback=supabase error={exc}")
            documents = self.x_repository.list_odaily_reference_documents(since=since)
        self._search_cache.upsert_documents(documents)
        return len(documents)

    def _run_search_cache_warmer(self) -> None:
        interval_seconds = self.x_settings.search_cache_refresh_seconds
        while not self._search_cache_warmer_stop.is_set():
            try:
                count = self.warm_search_cache_once()
                self.record_heartbeat(
                    success=True,
                    error=None,
                    metadata={"search_cache_warm": True, "odaily_reference_count": count},
                )
            except Exception as exc:
                print(f"[odaily] search cache warm failed: {exc}")
                self.record_heartbeat(
                    success=False,
                    error=str(exc),
                    metadata={"search_cache_warm": True},
                )
            self._search_cache_warmer_stop.wait(interval_seconds)

    def process(self, job: LocalPipelineJob) -> LocalPipelineRunResult:
        if job.job_type == "write_flow":
            return self._process_write_flow(job)
        if job.job_type == "alert_only":
            return self._process_alert_only(job)
        raise ValueError(f"unknown local pipeline job_type: {job.job_type}")

    def record_heartbeat(self, *, success: bool, error: str | None, metadata: dict | None = None) -> None:
        self._heartbeat.send(
            status="ok" if success else "failed",
            success=success,
            error=error,
            metadata=metadata or {},
        )

    def _process_write_flow(self, job: LocalPipelineJob) -> LocalPipelineRunResult:
        self.x_repository.ensure_pipeline(job.task_id)
        task = self.x_repository.get_task(job.task_id)
        if task.status in X_TERMINAL_STATUSES:
            return LocalPipelineRunResult(task.id, task.status, f"stopped at {task.status}")
        task = self._x_worker("judge_crypto")._resolve_task_author_name(task)
        sequence = self._remaining_write_flow_sequence(task)
        for stage in sequence:
            task = self.x_repository.get_task(job.task_id)
            if task.status in X_TERMINAL_STATUSES:
                return LocalPipelineRunResult(task.id, task.status, f"stopped at {task.status}")
            worker = self._x_worker(stage)
            task = worker._resolve_task_author_name(task)
            if worker._expire_task_if_stale(task):
                task = self.x_repository.get_task(job.task_id)
                return LocalPipelineRunResult(task.id, task.status, "expired")
            try:
                worker._process_task(task)
            except XHandledStageError as exc:
                task = self.x_repository.get_task(job.task_id)
                raise RuntimeError(str(exc) or task.status) from exc
            except Exception as exc:
                self.x_repository.fail_task(task.id, stage=stage, error=str(exc))
                raise
        task = self.x_repository.get_task(job.task_id)
        return LocalPipelineRunResult(task.id, task.status, "write_flow completed")

    def _process_alert_only(self, job: LocalPipelineJob) -> LocalPipelineRunResult:
        self.alert_repository.ensure_pipeline(job.task_id)
        task = self.alert_repository.get_task(job.task_id)
        if task.status in ALERT_TERMINAL_STATUSES:
            return LocalPipelineRunResult(task.id, task.status, f"stopped at {task.status}")
        for stage in self._remaining_alert_sequence(task.status):
            task = self.alert_repository.get_task(job.task_id)
            if task.status in ALERT_TERMINAL_STATUSES:
                return LocalPipelineRunResult(task.id, task.status, f"stopped at {task.status}")
            worker = self._alert_worker(stage)
            try:
                worker._process_task(task)
            except AlertHandledStageError as exc:
                task = self.alert_repository.get_task(job.task_id)
                raise RuntimeError(str(exc) or task.status) from exc
            except Exception as exc:
                self.alert_repository.fail_task(task.id, stage=stage, error=str(exc))
                raise
        task = self.alert_repository.get_task(job.task_id)
        return LocalPipelineRunResult(task.id, task.status, "alert_only completed")

    def _write_flow_sequence(self, task: TaskRecord) -> list[str]:
        if task.source == "x":
            judge_stage = "judge_ai" if bool(task.metadata.get("x_account_is_ai_source")) else "judge_crypto"
            return [judge_stage, "search", "write", "format_publish", "publish"]
        if task.source == AI_SOURCE:
            return ["search", "judge_ai", "write", "format_publish", "publish"]
        if task.source == JIN10_SOURCE:
            return ["judge_jin10", "search", "write", "format_publish", "publish"]
        if task.source == NON_MAINSTREAM_MEDIA_SOURCE or task.source in COMPETITOR_SOURCES or task.source in SEARCH_FIRST_SOURCES:
            return ["search", "judge_crypto", "write", "format_publish", "publish"]
        return ["judge_crypto", "search", "write", "format_publish", "publish"]

    def _remaining_write_flow_sequence(self, task: TaskRecord) -> list[str]:
        sequence = self._write_flow_sequence(task)
        next_stage = self._write_flow_next_stage(task.status, sequence)
        if next_stage is None:
            return sequence
        try:
            return sequence[sequence.index(next_stage) :]
        except ValueError:
            return sequence

    def _write_flow_next_stage(self, status: str, sequence: list[str]) -> str | None:
        if status == "pending":
            return sequence[0]
        if status == "judging":
            for judge_stage in ("judge_crypto", "judge_ai", "judge_jin10", "judge"):
                if judge_stage in sequence:
                    return judge_stage
            return sequence[0]
        if status == "judged":
            return "search" if "search" in sequence else "write"
        if status == "deduping":
            return "search"
        if status == "searched":
            search_index = sequence.index("search") if "search" in sequence else -1
            for judge_stage in ("judge_crypto", "judge_ai", "judge_jin10", "judge"):
                if judge_stage in sequence and sequence.index(judge_stage) > search_index:
                    return judge_stage
            return "write"
        if status in {"deduped", "writing"}:
            return "write"
        if status in {"written", "formatting"}:
            return "format_publish"
        if status in {"publisher_pending", "publishing"}:
            return "publish"
        return None

    def _remaining_alert_sequence(self, status: str) -> list[str]:
        sequence = ["domain_judge", "search", "notify"]
        if status in {"pending", "classifying"}:
            return sequence
        if status in {"classified", "deduping"}:
            return sequence[1:]
        if status in {"deduped", "notifying"}:
            return sequence[2:]
        return sequence

    def _x_worker(self, stage: str) -> XProcessingWorker:
        worker = self._x_workers.get(stage)
        if worker is None:
            worker = XProcessingWorker(
                stage=stage,  # type: ignore[arg-type]
                repository=self.x_repository,
                settings=self.x_settings,
                x_capture_repository=self.x_capture_repository,
                worker_id=f"{self.worker_id}-{stage}",
            )
            self._x_workers[stage] = worker
        return worker

    def _alert_worker(self, stage: str) -> ExternalMediaAlertWorker:
        worker = self._alert_workers.get(stage)
        if worker is None:
            worker = ExternalMediaAlertWorker(
                stage=stage,  # type: ignore[arg-type]
                repository=self.alert_repository,
                settings=self.alert_settings,
                worker_id=f"{self.worker_id}-{stage}",
            )
            self._alert_workers[stage] = worker
        return worker


def is_alert_source(source: str) -> bool:
    return source in {ALERT_TASK_SOURCE, AI_SOURCE_ALERT_TASK_SOURCE, MAINSTREAM_MEDIA_TASK_SOURCE}
