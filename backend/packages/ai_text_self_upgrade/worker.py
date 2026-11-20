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
        recover_retention = getattr(self.service.repository, "recover_storage_retention_runs", None)
        if recover_retention is not None:
            recover_retention()
        recover_manual = getattr(self.service.repository, "recover_manual_rejections", None)
        if recover_manual is not None:
            recovered = recover_manual()
            if recovered:
                print(f"[odaily] AI text self-upgrade recovered manual rejections count={recovered}", flush=True)
        self._thread = threading.Thread(target=self._run, name="ai-text-self-upgrade-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def run_due(self, *, now: datetime | None = None) -> dict[str, int] | None:
        outbox_result = self._sync_manual_rejection_outbox()
        manual_result: dict[str, int] = {}
        process_manual = getattr(self.service.repository, "process_manual_rejection_queue", None)
        if process_manual is not None:
            manual_result = process_manual()
            if manual_result.get("processed"):
                self._sync_manual_rejection_outbox()
        manual_result.update(outbox_result)
        run_key = current_run_key(now or datetime.now(UTC))
        repository = self.service.repository
        if repository.has_run(run_key) or not repository.start_run(run_key):
            if not any(manual_result.values()):
                return None
            return {f"manual_rejections_{key}": value for key, value in manual_result.items()}
        try:
            result = self.service.run_batch(run_key=run_key)
            if manual_result.get("processed"):
                result.update({f"manual_rejections_{key}": value for key, value in manual_result.items()})
            return result
        except Exception as exc:
            self.service.fail_run(run_key, f"{type(exc).__name__}: {exc}")
            return {"cases_found": 0, "cases_completed": 0, "cases_failed": 0, "cases_unlinked": 0}

    def _sync_manual_rejection_outbox(self) -> dict[str, int]:
        outbox = getattr(self.service, "manual_rejection_outbox", None)
        if outbox is None:
            return {}
        try:
            rows = outbox.pending_manual_rejections(limit=100)
        except Exception as exc:
            print(f"[odaily] manual rejection outbox read failed: {type(exc).__name__}: {exc}", flush=True)
            return {"outbox_errors": 1}
        result = {"outbox_queued": 0, "outbox_completed": 0, "outbox_deleted": 0, "outbox_errors": 0}
        for row in rows:
            try:
                if row.status == "pending":
                    queue_id = self.service.repository.queue_manual_rejection(
                        row.auditor_check_id,
                        actor=row.actor_email,
                    )
                    if not queue_id:
                        raise RuntimeError("manual rejection queue did not persist the intent")
                    outbox.mark_manual_rejection_queued(row.id)
                    result["outbox_queued"] += 1
                elif row.status == "queued":
                    status = self.service.repository.manual_rejection_status(row.auditor_check_id)
                    if status == "completed":
                        outbox.mark_manual_rejection_complete(row.id)
                        result["outbox_completed"] += 1
                    elif status == "deleted":
                        outbox.mark_manual_rejection_complete(row.id, deleted=True)
                        result["outbox_deleted"] += 1
            except Exception as exc:
                result["outbox_errors"] += 1
                try:
                    outbox.mark_manual_rejection_failed(row.id, error=f"{type(exc).__name__}: {exc}")
                except Exception as persist_error:
                    print(f"[odaily] manual rejection outbox recovery persistence failed: {type(persist_error).__name__}: {persist_error}", flush=True)
        return {key: value for key, value in result.items() if value}

    def run_storage_retention_due(self, *, now: datetime | None = None) -> dict[str, int] | None:
        local_now = (now or datetime.now(UTC)).astimezone(BEIJING)
        if local_now.hour < 6:
            return None
        repository = self.service.repository
        run_key = repository.oldest_unfinished_storage_retention_run() or f"storage_retention:{local_now.date().isoformat()}:v1"
        status = repository.run_status(run_key)
        if status is not None:
            if status == "completed":
                return None
            if status == "failed":
                retry = getattr(repository, "retry_failed_run", None)
                if retry is None or not retry(run_key, retry_after_seconds=300):
                    return None
        elif not repository.start_run(run_key):
            return None

        try:
            from packages.maintenance import create_maintenance_repository

            maintenance = create_maintenance_repository().cleanup_main_pipeline_fields(
                dry_run=False,
                completed_field_retention_days=5,
            )
        except Exception as exc:
            try:
                repository.finish_run(run_key, status="failed", error=f"{type(exc).__name__}: {exc}")
            except Exception as persist_error:
                print(f"[odaily] storage retention failure state persistence failed: {type(persist_error).__name__}: {persist_error}", flush=True)
            print(f"[odaily] storage retention failed: {type(exc).__name__}: {exc}", flush=True)
            return {"failed": 1}

        counts = {**maintenance.deleted, **maintenance.cleared}
        total = sum(maintenance.deleted.values()) + sum(maintenance.cleared.values())
        try:
            repository.finish_run(run_key, status="running" if maintenance.has_more else "completed", cases_found=total)
        except Exception as exc:
            print(f"[odaily] storage retention completion persistence failed: {type(exc).__name__}: {exc}", flush=True)
            return {"failed": 1}
        print("[odaily] storage retention " + " ".join(f"{key}={value}" for key, value in counts.items()), flush=True)
        return counts

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_storage_retention_due()
            except Exception as exc:
                print(f"[odaily] storage retention scheduler failed: {type(exc).__name__}: {exc}", flush=True)
            result = self.run_due()
            if result and any(result.values()):
                print("[odaily] AI text self-upgrade " + " ".join(f"{key}={value}" for key, value in result.items()), flush=True)
            self._stop.wait(self.interval_seconds)
