from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from packages.common.config import XProcessingSettings
from packages.jin10_monitor import InMemoryJin10MonitorRepository, Jin10Item, Jin10MonitorWorker, parse_jin10_payload, parse_jin10_response_body
from packages.local_pipeline.processor import LocalPipelineProcessor
from packages.x_processing.models import JIN10_SOURCE, PromptTemplateVersion, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.worker import PUBLISHER_CHANNEL_BY_SOURCE, XProcessingWorker


class FakePipelineClient:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def submit_job(self, **kwargs) -> None:
        self.jobs.append(kwargs)


class FakeAiClient:
    def __init__(self, output: str) -> None:
        self.output = output
        self.prompts: list[str] = []

    def generate_text(self, *, prompt: str, **kwargs) -> str:
        self.prompts.append(prompt)
        return self.output


def test_parse_jin10_payload_supports_nested_flash_shape() -> None:
    items = parse_jin10_payload(
        {
            "data": [
                {
                    "id": 123,
                    "type": 0,
                    "time": "2026-06-24T10:20:30+00:00",
                    "data": {
                        "content": "美国副总统万斯：建立机制确保霍尔木兹通道保持开放。",
                        "pic": "",
                    },
                }
            ]
        }
    )

    assert len(items) == 1
    assert items[0].source_item_id == "123"
    assert items[0].title.startswith("美国副总统万斯")
    assert "霍尔木兹" in items[0].content
    assert items[0].source_url == "https://flash.jin10.com/detail/123"


def test_parse_jin10_payload_uses_stable_id_when_missing_id() -> None:
    items = parse_jin10_payload([{"title": "标题", "content": "正文", "time": "1719210000"}])

    assert len(items) == 1
    assert items[0].source_item_id
    assert items[0].title == "标题"


def test_parse_jin10_response_body_supports_static_newest_js() -> None:
    payload = parse_jin10_response_body(
        'var newest = [{"id":"20260624173740781800","time":"2026-06-24 17:37:40","data":{"content":"美国副总统万斯：确保霍尔木兹通道保持开放。"}}];'
    )
    items = parse_jin10_payload(payload)

    assert len(items) == 1
    assert items[0].source_item_id == "20260624173740781800"
    assert "霍尔木兹" in items[0].content


def test_jin10_worker_disabled_does_not_fetch() -> None:
    repository = InMemoryJin10MonitorRepository()
    called = False

    def fetch_items(settings, timeout_seconds):
        nonlocal called
        called = True
        return []

    worker = Jin10MonitorWorker(repository=repository, fetch_items=fetch_items)

    result = worker.run_once()

    assert result.status == "disabled"
    assert called is False


def test_jin10_worker_first_enabled_run_seeds_without_enqueue() -> None:
    repository = InMemoryJin10MonitorRepository()
    repository.update_settings(enabled=True)
    pipeline = FakePipelineClient()
    worker = Jin10MonitorWorker(
        repository=repository,
        pipeline_client=pipeline,  # type: ignore[arg-type]
        fetch_items=lambda settings, timeout_seconds: [Jin10Item("1", "标题", "正文")],
    )

    result = worker.run_once()

    assert result.status == "success"
    assert result.seeded == 1
    assert repository.tasks == []
    assert pipeline.jobs == []


def test_jin10_worker_saves_new_items_after_seed() -> None:
    repository = InMemoryJin10MonitorRepository()
    repository.update_settings(enabled=True)
    repository.mark_seeded(["old"])
    pipeline = FakePipelineClient()
    worker = Jin10MonitorWorker(
        repository=repository,
        pipeline_client=pipeline,  # type: ignore[arg-type]
        fetch_items=lambda settings, timeout_seconds: [Jin10Item("new", "标题", "正文")],
    )

    result = worker.run_once()

    assert result.status == "success"
    assert result.new == 1
    assert result.saved == 1
    assert repository.tasks[0]["source"] == JIN10_SOURCE
    assert pipeline.jobs[0]["job_type"] == "write_flow"
    assert pipeline.jobs[0]["source"] == JIN10_SOURCE


def test_jin10_worker_records_timeout_error() -> None:
    repository = InMemoryJin10MonitorRepository()
    repository.update_settings(enabled=True)

    def fetch_items(settings, timeout_seconds):
        raise TimeoutError("request timed out")

    worker = Jin10MonitorWorker(repository=repository, fetch_items=fetch_items)

    result = worker.run_once()

    assert result.status == "failed"
    assert repository.settings.last_error == "request timed out"
    assert repository.heartbeats[-1]["status"] == "failed"


def test_jin10_judge_publish_routes_to_regular() -> None:
    repository = InMemoryXProcessingRepository()
    repository.prompts["jin10_judge"] = PromptTemplateVersion(
        id=100,
        template_key="jin10_judge",
        version_number=1,
        content="只发布美伊谈判",
    )
    repository.add_task(
        TaskRecord(
            id=1,
            source=JIN10_SOURCE,
            source_item_id="1",
            source_url=None,
            title="万斯谈伊朗",
            content="确保霍尔木兹通道保持开放",
            published_at=datetime.now(UTC),
        )
    )
    ai_client = FakeAiClient('{"decision":"publish","reason":"命中美伊谈判","matched_topic":"美伊谈判"}')
    worker = XProcessingWorker(
        stage="judge_jin10",
        repository=repository,
        settings=XProcessingSettings(openai_api_key="test"),
        ai_client=ai_client,  # type: ignore[arg-type]
    )

    result = worker.run_once()

    assert result.exit_code == 0
    assert repository.tasks[1].status == "judged"
    assert repository.pipelines[1].news_type == "regular"
    assert "霍尔木兹" in ai_client.prompts[0]


def test_jin10_judge_non_json_fails_task() -> None:
    repository = InMemoryXProcessingRepository()
    repository.prompts["jin10_judge"] = PromptTemplateVersion(
        id=100,
        template_key="jin10_judge",
        version_number=1,
        content="只发布美伊谈判",
    )
    repository.add_task(
        TaskRecord(
            id=1,
            source=JIN10_SOURCE,
            source_item_id="1",
            source_url=None,
            title="无关标题",
            content="无关内容",
            published_at=datetime.now(UTC),
        )
    )
    worker = XProcessingWorker(
        stage="judge_jin10",
        repository=repository,
        settings=XProcessingSettings(openai_api_key="test"),
        ai_client=FakeAiClient("不是 JSON"),  # type: ignore[arg-type]
    )

    result = worker.run_once()

    assert result.exit_code == 1
    assert repository.tasks[1].status == "judge_failed"


def test_jin10_local_pipeline_sequence_and_publisher_channel() -> None:
    processor = object.__new__(LocalPipelineProcessor)
    task = TaskRecord(id=1, source=JIN10_SOURCE, source_item_id="1", source_url=None, title="标题", content="正文")

    assert processor._write_flow_sequence(task) == ["judge_jin10", "search", "write", "format_publish", "publish"]
    assert processor._remaining_write_flow_sequence(replace(task, status="judged")) == ["search", "write", "format_publish", "publish"]
    assert PUBLISHER_CHANNEL_BY_SOURCE[JIN10_SOURCE] == "jin10"
