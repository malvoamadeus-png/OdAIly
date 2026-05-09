from __future__ import annotations

from typing import Any

from packages.common.config import RetrySettings, XProcessingSettings
from packages.publisher import PushResult
from packages.x_processing.ai_client import OpenAIResponsesClient, to_chat_response_format
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief, PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.searcher import SearchDocument
from packages.x_processing.telegram import TelegramResult
from packages.x_processing.worker import XProcessingWorker, build_telegram_notice, parse_judge_route, parse_news_type


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
        self.calls: list[dict[str, str | bool | None]] = []

    def push(self, *, title: str, content: str, dry_run: bool, source_url: str | None = None) -> PushResult:
        self.calls.append({"title": title, "content": content, "dry_run": dry_run, "source_url": source_url})
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
        dashscope_api_key="dash",
        dry_run=False,
        retry=RetrySettings(max_attempts=1, backoff_seconds=0),
    )


class FakeEmbeddingService:
    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.vectors = vectors or {}

    def embed_one(self, *, cache_key: str, text: str) -> list[float]:
        return self.vectors.get(cache_key, [1.0, 0.0])

    def embed_documents(self, documents: list[SearchDocument]):
        return [(doc, self.vectors.get(f"{doc.doc_type}:{doc.doc_id}", [1.0, 0.0])) for doc in documents]


class FakeOpenAIResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


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


def competitor_task(task_id: int, status: str = "pending") -> TaskRecord:
    return TaskRecord(
        id=task_id,
        source="blockbeats",
        source_item_id=f"bb-{task_id}",
        source_url="https://www.theblockbeats.info/flash/1",
        title="某项目完成融资",
        content="某项目完成融资，金额为 500 万美元。",
        status=status,
        metadata={"source_kind": "competitor"},
    )


def test_parse_news_type_accepts_only_known_values() -> None:
    assert parse_news_type('{"news_type":"regular"}') == "regular"
    assert parse_news_type('{"route":"regular","discard_type":"none"}') == "regular"
    assert parse_news_type('```json\n{"news_type":"onchain"}\n```') == "onchain"
    try:
        parse_news_type('{"news_type":"market"}')
    except ValueError as exc:
        assert "invalid route" in str(exc)
    else:
        raise AssertionError("invalid news_type should fail")


def test_parse_judge_route_validates_discard_type() -> None:
    assert parse_judge_route('{"route":"discard","discard_type":"pure_emotion"}') == ("discard", "pure_emotion")
    assert parse_judge_route('{"route":"discard","discard_type":"non_crypto_ai"}') == ("discard", "non_crypto_ai")
    try:
        parse_judge_route('{"route":"discard","discard_type":"none"}')
    except ValueError as exc:
        assert "requires a discard_type" in str(exc)
    else:
        raise AssertionError("discard without discard_type should fail")


def test_judge_routes_news_to_judged() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="pending"))
    fake_ai = FakeAiClient(['{"route":"funding","discard_type":"none"}'])
    worker = XProcessingWorker(stage="judge", repository=repo, settings=settings(), ai_client=fake_ai)

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "judged"
    assert repo.pipelines[1].news_type == "funding"
    assert "可丢弃内容只有四类" in fake_ai.calls[0]["prompt"]
    assert fake_ai.calls[0]["text_format"]["name"] == "x_judge_route"


def test_judge_discards_garbage_expression() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="pending"))
    fake_ai = FakeAiClient(['{"route":"discard","discard_type":"baseless_trading_call"}'])
    worker = XProcessingWorker(stage="judge", repository=repo, settings=settings(), ai_client=fake_ai)

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].news_type is None


def test_openai_client_uses_configured_responses_base_url(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeOpenAIResponse({"output_text": "ok"})

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)
    client = OpenAIResponsesClient(
        api_key="key",
        base_url="https://relay.example.com/v1",
        api_style="responses",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert client.generate_text(model="gpt", prompt="hello") == "ok"
    assert calls[0]["url"] == "https://relay.example.com/v1/responses"


def test_openai_client_supports_chat_completions_style(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeOpenAIResponse({"choices": [{"message": {"content": '{"news_type":"regular"}'}}]})

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)
    client = OpenAIResponsesClient(
        api_key="key",
        base_url="https://relay.example.com/v1",
        api_style="chat_completions",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert client.generate_text(model="gpt", prompt="hello", text_format={"type": "json_schema", "name": "x", "schema": {}})
    assert calls[0]["url"] == "https://relay.example.com/v1/chat/completions"
    assert calls[0]["json"]["messages"] == [{"role": "user", "content": "hello"}]
    assert calls[0]["json"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "x", "schema": {}, "strict": True},
    }


def test_chat_response_format_converts_responses_json_schema() -> None:
    assert to_chat_response_format({"type": "json_schema", "name": "x", "schema": {"type": "object"}}) == {
        "type": "json_schema",
        "json_schema": {"name": "x", "schema": {"type": "object"}, "strict": True},
    }


def test_telegram_notice_includes_source_url_on_next_line() -> None:
    assert build_telegram_notice(title="标题", source_url="https://x.com/a/status/1") == (
        "有新快讯：标题\n原文链接：https://x.com/a/status/1"
    )


def test_searcher_creates_candidate_for_x_and_advances_to_deduped() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="judged"))
    worker = XProcessingWorker(
        stage="search",
        repository=repo,
        settings=settings(),
        ai_client=None,
        search_embedding_service=FakeEmbeddingService({"task:1": [0.0, 1.0]}),
    )

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "deduped"
    assert repo.pipelines[1].candidate_id == 1


def test_searcher_links_duplicate_to_active_candidate() -> None:
    repo = InMemoryXProcessingRepository()
    primary = task(1, status="deduped")
    repo.add_task(primary)
    repo.create_candidate_for_task(primary, search_result={})
    duplicate = competitor_task(2, status="pending")
    repo.add_task(duplicate)
    worker = XProcessingWorker(
        stage="search",
        repository=repo,
        settings=settings(),
        ai_client=None,
        search_embedding_service=FakeEmbeddingService({"task:2": [1.0, 0.0], "candidate:1": [1.0, 0.0]}),
    )

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[2].status == "duplicate"
    assert repo.event_sources[-1]["candidate_id"] == 1
    assert repo.event_sources[-1]["role"] == "supporting"


def test_competitor_flows_search_then_judge() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(competitor_task(1, status="pending"))
    search_worker = XProcessingWorker(
        stage="search",
        repository=repo,
        settings=settings(),
        ai_client=None,
        search_embedding_service=FakeEmbeddingService({"task:1": [0.0, 1.0]}),
    )
    assert search_worker.run_once().processed == 1
    assert repo.tasks[1].status == "searched"

    fake_ai = FakeAiClient(['{"route":"regular","discard_type":"none"}'])
    judge_worker = XProcessingWorker(stage="judge", repository=repo, settings=settings(), ai_client=fake_ai)
    assert judge_worker.run_once().processed == 1
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


def test_competitor_writer_uses_source_material_without_brand_or_url() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(competitor_task(1, status="deduped"))
    repo.pipelines[1] = PipelineRecord(task_id=1, news_type="regular")
    fake_ai = FakeAiClient(["标题\n\n正文"])
    worker = XProcessingWorker(stage="write", repository=repo, settings=settings(), ai_client=fake_ai)

    result = worker.run_once()

    assert result.processed == 1
    prompt = fake_ai.calls[0]["prompt"]
    assert "【信源材料】" in prompt
    assert "来源类型：信源" in prompt
    assert "theblockbeats" not in prompt.lower()
    assert "BlockBeats" not in prompt
    assert "律动" not in prompt


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
    assert push.calls[0]["source_url"] == "https://x.com/a/status/1"
    assert telegram.calls == ["有新快讯：标题\n原文链接：https://x.com/a/status/1"]


def test_competitor_publish_hides_source_url() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(competitor_task(1, status="written"))
    repo.pipelines[1] = PipelineRecord(task_id=1, draft_title="标题", draft_content="正文")
    push = FakePushClient(ok=True)
    telegram = FakeTelegramClient(TelegramResult(ok=True))
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
    assert push.calls[0]["source_url"] is None
    assert telegram.calls == ["有新快讯：标题"]


def test_claim_skips_locked_task() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(task(1, status="pending"))

    first = repo.claim_task("judge", worker_id="a")
    second = repo.claim_task("judge", worker_id="b")

    assert first is not None
    assert second is None
