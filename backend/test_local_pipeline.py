from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3
import threading
import time

from packages.local_pipeline.queue import LocalPipelineQueue
from packages.local_pipeline.server import LocalPipelineService


class _Processor:
    worker_id = "test-worker"

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def init_remote_schema(self) -> None:
        self.calls.append("init_remote_schema")

    def start_background_tasks(self) -> None:
        self.calls.append("start_background_tasks")

    def record_heartbeat(self, *, success: bool, error: str | None, metadata: dict | None = None) -> None:
        return None


def test_local_pipeline_initializes_schema_before_starting_workers(monkeypatch) -> None:
    calls: list[str] = []
    processor = _Processor(calls)
    service = LocalPipelineService(queue=object(), processor=processor)  # type: ignore[arg-type]
    monkeypatch.setattr(service, "_ensure_worker_running", lambda: calls.append("ensure_worker"))

    service.start()

    assert calls == ["init_remote_schema", "start_background_tasks", "ensure_worker"]


def test_abandoned_job_recovery_excludes_current_worker_and_recent_jobs(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / 'queue.sqlite', storage_epoch='current')
    for n in range(1, 5):
        queue.enqueue(job_type='write_flow', task_id=n, source='x', source_item_id=str(n))
    old = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    recent = datetime.now(UTC).isoformat()
    with sqlite3.connect(queue.path) as conn:
        for n, owner, locked, epoch in [(1, 'old-worker', old, 'current'), (2, 'current-worker', old, 'current'),
                                        (3, 'old-worker', recent, 'current'), (4, 'old-worker', old, 'other')]:
            conn.execute("UPDATE local_pipeline_jobs SET status='running',attempt_count=1,locked_by=?,locked_at=?,storage_epoch=? WHERE task_id=?", (owner, locked, epoch, n))
    count = queue.requeue_stale_running_jobs(stale_before=datetime.now(UTC)-timedelta(minutes=30), exclude_worker_id='current-worker')
    assert count == 1
    with sqlite3.connect(queue.path) as conn:
        states = conn.execute('SELECT task_id,status,locked_by FROM local_pipeline_jobs ORDER BY task_id').fetchall()
    assert states == [(1, 'failed', None), (2, 'running', 'current-worker'), (3, 'running', 'old-worker'), (4, 'running', 'old-worker')]


def test_worker_checks_for_abandoned_jobs_periodically(monkeypatch) -> None:
    class Queue:
        calls = []
        def requeue_stale_running_jobs(self, **kwargs):
            self.calls.append(kwargs)
            return 0
        def claim_next(self, **kwargs):
            return None
        def stats(self):
            return {}
    queue = Queue()
    service = LocalPipelineService(queue=queue, processor=_Processor([]))  # type: ignore[arg-type]
    monkeypatch.setattr(service._wake_event, 'wait', lambda _: None)
    service._run_worker_once()
    service._run_worker_once()
    assert len(queue.calls) == 1
    assert queue.calls[0]['exclude_worker_id'] == 'test-worker'


def test_stale_lease_cannot_complete_reclaimed_job(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / 'queue.sqlite', storage_epoch='current')
    queue.enqueue(job_type='write_flow', task_id=1, source='x', source_item_id='lease-test')

    first = queue.claim_next(worker_id='worker-a')
    assert first is not None and first.lease_token
    stale_before = datetime.now(UTC) + timedelta(seconds=1)
    assert queue.requeue_stale_running_jobs(stale_before=stale_before) == 1

    second = queue.claim_next(worker_id='worker-b')
    assert second is not None and second.lease_token != first.lease_token
    assert queue.mark_succeeded(first.id, lease_token=first.lease_token) is False
    assert queue.mark_succeeded(second.id, lease_token=second.lease_token) is True
    assert queue.stats() == {'succeeded': 1}


def test_shutdown_drains_active_job_and_rejects_new_enqueue(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()
    ledger: list[str] = []

    class BlockingProcessor(_Processor):
        def process(self, job):
            entered.set()
            assert release.wait(5)
            ledger.append("external-result")
            return type("Result", (), {"task_id": job.task_id, "status": "published", "message": "done"})()

        def stop_background_tasks(self):
            ledger.append("background-stopped")

    queue = LocalPipelineQueue(tmp_path / "queue.sqlite", storage_epoch="drain")
    service = LocalPipelineService(queue=queue, processor=BlockingProcessor([]))  # type: ignore[arg-type]
    service.start()
    service.enqueue({"job_type": "write_flow", "task_id": 1, "source": "x", "source_item_id": "drain-1"})
    assert entered.wait(2)

    stopper = threading.Thread(target=service.stop)
    stopper.start()
    time.sleep(0.05)
    assert stopper.is_alive()
    health = service.health()
    assert health["ok"] is False
    assert health["state"] == "draining"
    assert health["accepting"] is False
    assert health["draining"] is True
    assert health["inflight"] == 1
    try:
        service.enqueue({"job_type": "write_flow", "task_id": 2, "source": "x", "source_item_id": "drain-2"})
    except RuntimeError as exc:
        assert "shutting down" in str(exc)
    else:
        raise AssertionError("shutdown must reject new jobs")

    release.set()
    stopper.join(2)
    assert not stopper.is_alive()
    assert ledger == ["external-result", "background-stopped"]
    assert queue.stats() == {"succeeded": 1}
