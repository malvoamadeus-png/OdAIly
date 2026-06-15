from __future__ import annotations

from packages.local_pipeline.processor import (
    ALERT_TERMINAL_STATUSES,
    LocalPipelineProcessor,
    X_TERMINAL_STATUSES,
)
from packages.local_pipeline.queue import LocalPipelineQueue
from packages.x_processing.models import TaskRecord


def test_local_pipeline_queue_deduplicates_jobs(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / "local_pipeline.sqlite")

    first = queue.enqueue(job_type="write_flow", task_id=1, source="x", source_item_id="tweet-1")
    second = queue.enqueue(job_type="write_flow", task_id=1, source="x", source_item_id="tweet-1")

    assert first.id == second.id
    assert queue.stats()["pending"] == 1


def test_local_pipeline_queue_claim_succeed_and_retry(tmp_path) -> None:
    queue = LocalPipelineQueue(tmp_path / "local_pipeline.sqlite")
    queue.enqueue(job_type="alert_only", task_id=2, source="external_media_alert", source_item_id="story-1")

    job = queue.claim_next(worker_id="test-worker")

    assert job is not None
    assert job.status == "running"
    queue.mark_failed(job.id, error="boom", attempt_count=job.attempt_count)
    assert queue.stats()["failed"] == 1

    queue.enqueue(job_type="alert_only", task_id=2, source="external_media_alert", source_item_id="story-1")
    retry = queue.claim_next(worker_id="test-worker")

    assert retry is not None
    assert retry.id == job.id
    queue.mark_succeeded(retry.id)
    assert queue.stats()["succeeded"] == 1


def test_local_pipeline_processor_write_flow_sequences_by_source() -> None:
    processor = object.__new__(LocalPipelineProcessor)

    x_task = TaskRecord(id=1, source="x", source_item_id="1", source_url=None, title=None, content="x")
    blockbeats_task = TaskRecord(
        id=3,
        source="blockbeats",
        source_item_id="3",
        source_url=None,
        title=None,
        content="x",
    )
    ai_source_task = TaskRecord(
        id=4,
        source="ai_source",
        source_item_id="4",
        source_url=None,
        title=None,
        content="x",
    )

    assert processor._write_flow_sequence(x_task) == [
        "judge_crypto",
        "search",
        "write",
        "format_publish",
        "publish",
    ]
    assert processor._write_flow_sequence(
        TaskRecord(
            id=2,
            source="x",
            source_item_id="2",
            source_url=None,
            title=None,
            content="x",
            metadata={"x_account_is_ai_source": True},
        )
    )[0] == "judge_ai"
    assert processor._write_flow_sequence(blockbeats_task)[:2] == ["search", "judge_crypto"]
    assert processor._write_flow_sequence(ai_source_task)[:2] == ["search", "judge_ai"]


def test_local_pipeline_processor_resumes_write_flow_from_current_status() -> None:
    processor = object.__new__(LocalPipelineProcessor)
    search_first = TaskRecord(
        id=1,
        source="blockbeats",
        source_item_id="1",
        source_url=None,
        title=None,
        content="x",
        status="searched",
    )
    publishing = TaskRecord(
        id=2,
        source="x",
        source_item_id="2",
        source_url=None,
        title=None,
        content="x",
        status="publisher_pending",
    )

    assert processor._remaining_write_flow_sequence(search_first) == [
        "judge_crypto",
        "write",
        "format_publish",
        "publish",
    ]
    assert processor._remaining_write_flow_sequence(publishing) == ["publish"]
    assert processor._remaining_alert_sequence("classified") == ["search", "notify"]
    assert processor._remaining_alert_sequence("deduped") == ["notify"]


def test_local_pipeline_processor_treats_legacy_skipped_as_terminal() -> None:
    assert "legacy_skipped" in X_TERMINAL_STATUSES
    assert "legacy_skipped" in ALERT_TERMINAL_STATUSES
