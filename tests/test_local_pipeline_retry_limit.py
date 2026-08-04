from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.local_pipeline.queue import LocalPipelineQueue
from packages.x_processing.models import TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository


def _make_due(queue: LocalPipelineQueue, job_id: int) -> None:
    now = datetime.now(UTC).isoformat()
    with queue._connect() as conn:
        conn.execute("UPDATE local_pipeline_jobs SET next_attempt_at = ? WHERE id = ?", (now, job_id))


def test_queue_exhausts_after_three_total_attempts(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / "queue.sqlite", max_attempts=3)
    queue.enqueue(job_type="write_flow", task_id=101, source="x", source_item_id="item-101")

    for expected_attempt in (1, 2, 3):
        job = queue.claim_next(worker_id=f"worker-{expected_attempt}")
        assert job is not None
        assert job.attempt_count == expected_attempt
        queue.mark_failed(job.id, error=f"failure-{expected_attempt}", attempt_count=job.attempt_count)
        if expected_attempt < 3:
            _make_due(queue, job.id)

    assert queue.stats() == {"exhausted": 1}
    assert queue.claim_next(worker_id="worker-after-limit") is None
    assert queue.list_exhausted_jobs(limit=1)[0]["task_id"] == 101


def test_stale_running_job_over_limit_becomes_exhausted(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / "queue.sqlite", max_attempts=3)
    job = queue.enqueue(job_type="write_flow", task_id=102, source="x", source_item_id="item-102")
    with queue._connect() as conn:
        conn.execute(
            """
            UPDATE local_pipeline_jobs
            SET status='running', attempt_count=3, locked_by='dead-worker',
                locked_at=?, next_attempt_at=?
            WHERE id=?
            """,
            (
                (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                datetime.now(UTC).isoformat(),
                job.id,
            ),
        )

    assert queue.requeue_stale_running_jobs(stale_before=datetime.now(UTC) - timedelta(minutes=30)) == 1
    assert queue.stats() == {"exhausted": 1}


def test_over_limit_maintenance_is_dry_run_until_execute(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / "queue.sqlite", max_attempts=3)
    job = queue.enqueue(job_type="alert_only", task_id=103, source="external_media_alert", source_item_id="item-103")
    with queue._connect() as conn:
        conn.execute(
            "UPDATE local_pipeline_jobs SET status='failed', attempt_count=3 WHERE id=?",
            (job.id,),
        )

    assert len(queue.list_over_limit_jobs()) == 1
    assert queue.stats() == {"failed": 1}
    assert queue.exhaust_over_limit_jobs() == 1
    assert queue.stats() == {"exhausted": 1}


def test_failed_x_stage_statuses_are_claimable_for_the_same_stage(tmp_path) -> None:
    repository = SQLiteXProcessingRepository(tmp_path / "odaily.sqlite")
    assert repository._eligible("judge_crypto", {"source": "x", "status": "judge_failed"}, False)
    assert repository._eligible("search", {"source": "x", "status": "search_failed"}, False)
    assert repository._eligible("write", {"source": "x", "status": "write_failed"}, False)
    assert repository._eligible("format_publish", {"source": "x", "status": "format_failed"}, False)
    assert repository._eligible("publish", {"source": "x", "status": "publisher_failed"}, False)


def test_in_memory_failed_stage_can_be_reclaimed_by_id() -> None:
    repository = InMemoryXProcessingRepository()
    repository.add_task(
        TaskRecord(
            id=104,
            source="x",
            source_item_id="item-104",
            source_url=None,
            title="title",
            content="content",
            status="write_failed",
        )
    )

    claimed = repository.claim_task_by_id("write", task_id=104, worker_id="local-pipeline-write")

    assert claimed is not None
    assert claimed.status == "writing"
