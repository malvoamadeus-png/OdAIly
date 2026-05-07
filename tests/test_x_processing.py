from __future__ import annotations

from typing import Any

from packages.common.config import RetrySettings, XProcessingSettings
from packages.publisher import PushResult
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief, PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.telegram import TelegramResult
from packages.x_processing.worker import XProcessingWorker, parse_news_type


class FakeAiClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def generate_text(self, *, model: str, prompt: str, text_format: dict[str, Any] | None = None) -> str:
        self.calls.append({"model": model, "prompt": prompt, "text_format": text_format})
        return self.outputs.pop(0)


class FakePushClient:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[dict[str, str | bool]] = []

    def push(self, *, title: str, content: str, dry_run: bool) -> PushResult:
        self.calls.append({"title": title, "content": content, "dry_run": dry_run})
        return PushResult(ok=self.ok, status_code=200 if self.ok else None, error=None if self.ok else "push failed")


class FakeTelegramClient:
    def __init__(self, result: TelegramResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def send_message(self, text: str) -> TelegramResult:
        self.calls.append(text)
        return self.result


def settings() -> XProcessingSettings:
    return XProcessingSettings(
        openai_api_key="test",
        dry_run=False,
        retry=RetrySettings(max_attempts=1, backoff_seconds=0),
    )


def task(task_id: int, status: str = "pending") -> TaskRecord:
    return TaskRecord(
        id=task_id,
        source="x",
        source_item_id=f"tweet-{task_id}",
        source_url="https://x.com/a/status/1",
        title="@a: text",
        content="Binance raised 5000USDT",
        status=status,
        metadata={"author_display_name": "Alice"},
    )


def test_parse_news_type_accepts_only_known_values() -> None:
    assert parse_news_type('{"news_type":"regular"}') == "regular"
    assert parse_news_type('```json\n{"news_type":"onchain"}\n```') == "onchain"
    try:
        parse_news_type('{"news_type":"market"}')
    except ValueError as exc:
        assert "invalid news_type" in str(exc)
    else:
        raise AssertionError("invalid news_type should fail")


def test_searcher_noop_advances_to_deduped() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="judged"))
    worker = XProcessingWorker(stage="search", repository=repo, settings=settings(), ai_client=None)

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "deduped"


def test_writer_uses_prompt_for_news_type_and_records_draft() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="deduped"))
    repo.pipelines[1] = PipelineRecord(task_id=1, news_type="funding")
    fake_ai = FakeAiClient(["融资标题\n\n融资正文"])
    worker = XProcessingWorker(stage="write", repository=repo, settings=settings(), ai_client=fake_ai)

    result = worker.run_once()

    pipeline = repo.pipelines[1]
    assert result.processed == 1
    assert repo.tasks[1].status == "written"
    assert pipeline.prompt_template_key == "x_funding_writer"
    assert pipeline.prompt_version_id == repo.prompts["x_funding_writer"].id
    assert pipeline.draft_title == "融资标题"
    assert pipeline.draft_content == "融资正文"
    assert fake_ai.calls[0]["model"] == "gpt-5.5"


def test_formatter_applies_writer2_rules() -> None:
    formatted = format_brief(
        DraftBrief(
            title="币安 上涨至 8000美元",
            content="Binance推出全新的dapp，成交5000USDT，价值500万美金。。",
        )
    )

    assert formatted.title == "币安上涨至8000美元"
    assert formatted.content == "Odaily星球日报讯 币安推出全新的DApp，成交5000 USDT，价值500万美元。"


def test_format_publish_keeps_ready_review_when_telegram_fails() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="written"))
    repo.pipelines[1] = PipelineRecord(task_id=1, draft_title="标题", draft_content="正文")
    push = FakePushClient(ok=True)
    telegram = FakeTelegramClient(TelegramResult(ok=False, error="telegram failed"))
    worker = XProcessingWorker(
        stage="format_publish",
        repository=repo,
        settings=settings(),
        ai_client=None,
        push_client=push,
        telegram_client=telegram,
    )

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "ready_review"
    assert repo.pipelines[1].telegram_result["ok"] is False
    assert push.calls[0]["dry_run"] is False
    assert telegram.calls == ["有新快讯：标题"]


def test_claim_skips_locked_task() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="pending"))

    first = repo.claim_task("judge", worker_id="a")
    second = repo.claim_task("judge", worker_id="b")

    assert first is not None
    assert second is None
