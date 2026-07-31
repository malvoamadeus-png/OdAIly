from __future__ import annotations

from packages.publisher.push_client import PushClient
from packages.x_processing.models import TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository


class _Response:
    status_code = 200
    text = "ok"

    def raise_for_status(self) -> None:
        return None


def _publish_task(task_id: int) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        source="x",
        source_item_id=f"item-{task_id}",
        source_url=None,
        title=f"title-{task_id}",
        content="content",
        status="publisher_pending",
    )


def test_claim_task_by_id_locks_only_the_requested_publish_task() -> None:
    repository = InMemoryXProcessingRepository()
    repository.add_task(_publish_task(1))
    repository.add_task(_publish_task(2))

    claimed = repository.claim_task_by_id("publish", task_id=2, worker_id="local-pipeline")

    assert claimed is not None
    assert claimed.id == 2
    assert claimed.status == "publishing"
    assert repository.tasks[1].status == "publisher_pending"
    assert repository.claim_task_by_id("publish", task_id=2, worker_id="legacy-worker") is None


def test_push_client_sends_stable_idempotency_header(monkeypatch) -> None:
    captured: list[dict] = []

    def fake_post(endpoint, *, json, headers, timeout):
        captured.append({"endpoint": endpoint, "json": json, "headers": headers, "timeout": timeout})
        return _Response()

    monkeypatch.setattr("packages.publisher.push_client.requests.post", fake_post)
    client = PushClient(endpoint="https://example.test/push", timeout_seconds=3, max_attempts=1, backoff_seconds=0)

    result = client.push(
        title="title",
        content="content",
        dry_run=False,
        is_publish=True,
        idempotency_key="odaily-task-42",
    )

    assert result.ok is True
    assert len(captured) == 1
    assert captured[0]["headers"]["Idempotency-Key"] == "odaily-task-42"
