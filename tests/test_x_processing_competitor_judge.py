from __future__ import annotations

import json
from types import SimpleNamespace

from packages.publisher import PushResult
from packages.x_processing.models import PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.worker import XProcessingWorker


class RecordingAiClient:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[dict] = []

    def generate_text(self, **kwargs):
        self.calls.append(kwargs)
        return self.output


class RecordingRepository:
    def __init__(self) -> None:
        self.discards: list[dict] = []

    def complete_judge_discard(self, task_id: int, **kwargs) -> None:
        self.discards.append({"task_id": task_id, **kwargs})


class RecordingPushClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def push(self, **kwargs):
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text="ok")


class RecordingFeedWriter:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def upsert_newsflash(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_competitor_non_crypto_ai_uses_one_judge_call_and_specialized_rules() -> None:
    client = RecordingAiClient(
        json.dumps({"route": "discard", "discard_type": "non_crypto_ai"})
    )
    repository = RecordingRepository()
    worker = object.__new__(XProcessingWorker)
    worker.repository = repository
    worker.settings = SimpleNamespace(judge_model="judge-model", judge_reasoning_effort="low")
    worker.judge_ai_client = client
    worker._search_cache_store = None
    task = TaskRecord(
        id=42,
        source="blockbeats",
        source_item_id="bb-42",
        source_url="https://example.test/42",
        title="Alibaba launches enterprise AI application platform Miaowu Team Edition",
        content="The product helps enterprises create general AI applications.",
        metadata={"site_display_name": "BlockBeats"},
    )

    worker._run_judge(task)

    assert len(client.calls) == 1
    assert client.calls[0]["text_format"]["name"] == "competitor_judge_route"
    assert "加密行业存在实质" in client.calls[0]["prompt"]
    assert len(repository.discards) == 1
    assert repository.discards[0]["discard_type"] == "non_crypto_ai"
    assert repository.discards[0]["rule_set"] == "competitor"


def test_publisher_hard_blocks_magne_ai_even_when_model_passes() -> None:
    repository = InMemoryXProcessingRepository()
    task = TaskRecord(
        id=1,
        source="blockbeats",
        source_item_id="bb-1",
        source_url="https://example.test/news",
        title="Project announces update",
        content="Original competitor body.",
        status="publisher_pending",
    )
    repository.add_task(task)
    repository.pipelines[task.id] = PipelineRecord(
        task_id=task.id,
        final_title="Project announces update",
        final_content="Odaily news body mentions magne.ai.",
    )

    worker = object.__new__(XProcessingWorker)
    worker.repository = repository
    worker.settings = SimpleNamespace(
        dry_run=False,
        publisher_model="publisher-model",
        publisher_reasoning_effort="low",
    )
    worker.ai_client = RecordingAiClient('{"decision":"pass","reason":"model would publish"}')
    worker.push_client = RecordingPushClient()
    worker.feed_writer = RecordingFeedWriter()
    worker._search_cache_store = None

    worker._run_publish(task)

    pipeline = repository.get_pipeline(task.id)
    assert repository.tasks[task.id].status == "ready_review"
    assert pipeline.publisher_decision == "manual_review"
    assert pipeline.publisher_reason_code == "hard_blocked_term"
    assert pipeline.publisher_output["hard_blocked_term"] == "MAGNE.AI"
    assert worker.push_client.calls[0]["is_publish"] is False
    assert worker.feed_writer.calls[0]["publisher_reason_code"] == "hard_blocked_term"
