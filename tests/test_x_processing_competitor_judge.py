from __future__ import annotations

import json
from types import SimpleNamespace

from packages.x_processing.models import TaskRecord
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
