from __future__ import annotations

import json
from datetime import UTC, datetime

from packages.external_media_alert import ALERT_PROMPT_KEY, InMemoryExternalMediaAlertRepository
from packages.external_media_alert.worker import ExternalMediaAlertWorker
from packages.common.config import ExternalMediaAlertSettings, RetrySettings
from packages.x_processing.searcher import SearchDocument
from packages.x_processing.telegram import TelegramResult
from packages.x_processing.models import TaskRecord


class FakeAIClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, object]] = []

    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, object] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "prompt": prompt,
                "text_format": text_format,
                "reasoning_effort": reasoning_effort,
            }
        )
        return self.outputs.pop(0)


class FakeSearchCache:
    def upsert_document(self, document) -> None:
        return None

    def upsert_documents(self, documents) -> None:
        return None


class FakeEmbeddingService:
    def __init__(self) -> None:
        self.cache = FakeSearchCache()
        self.embed_one_calls: list[tuple[str, str]] = []

    def embed_one(self, *, cache_key: str, text: str) -> list[float]:
        self.embed_one_calls.append((cache_key, text))
        return [1.0, 0.0]

    def embed_documents(self, documents: list[SearchDocument]) -> list[tuple[SearchDocument, list[float]]]:
        return [(document, [0.0, 1.0]) for document in documents]


class FakeTelegramClient:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def send_message(self, text: str) -> TelegramResult:
        self.calls.append(text)
        if self.ok:
            return TelegramResult(ok=True, status_code=200)
        return TelegramResult(ok=False, error="telegram down")


def settings() -> ExternalMediaAlertSettings:
    return ExternalMediaAlertSettings(
        openai_api_key="token",
        dashscope_api_key="dashscope",
        retry=RetrySettings(max_attempts=1, backoff_seconds=0),
    )


def build_task(
    *,
    task_id: int,
    status: str,
    site_key: str,
    site_display_name: str,
    title: str,
    excerpt: str,
    source_url: str,
) -> TaskRecord:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    return TaskRecord(
        id=task_id,
        source="external_media_alert",
        source_item_id=source_url,
        source_url=source_url,
        title=title,
        content=excerpt,
        status=status,
        created_at=now,
        updated_at=now,
        metadata={
            "site_key": site_key,
            "site_display_name": site_display_name,
            "excerpt": excerpt,
            "source_kind": "external_media_alert",
        },
    )


def test_domain_judge_keeps_crypto_section_without_model() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="ft_crypto",
            site_display_name="FT Crypto",
            title="Investors weigh token issuance plans",
            excerpt="Section story",
            source_url="https://www.ft.com/content/one/",
        )
    )
    ai_client = FakeAIClient([])
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=ai_client,
    )

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "classified"
    assert repo.pipelines[1].domain_route == "crypto"
    assert repo.pipelines[1].domain_model == "deterministic-domain-precheck"
    assert ai_client.calls == []


def test_domain_judge_discards_strong_non_crypto_title_without_model() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="wsj_business",
            site_display_name="WSJ Business",
            title="Hollywood studios bet on summer movie slate",
            excerpt="Entertainment outlook",
            source_url="https://www.wsj.com/articles/movie-slate/",
        )
    )
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=FakeAIClient([]),
    )

    worker.run_once()

    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].discard_reason == "non_crypto"


def test_domain_judge_uses_model_for_ambiguous_title() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.prompts[ALERT_PROMPT_KEY] = repo.prompts[ALERT_PROMPT_KEY].__class__(
        id=9,
        template_key=ALERT_PROMPT_KEY,
        version_number=3,
        content="Judge whether the title is crypto-related.",
    )
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="wsj_finance",
            site_display_name="WSJ Finance",
            title="Treasury market tension spills into risk assets",
            excerpt="Macro pressure builds across markets.",
            source_url="https://www.wsj.com/articles/macro-risk-assets/",
        )
    )
    ai_client = FakeAIClient(['{"route":"crypto"}'])
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=ai_client,
    )

    worker.run_once()

    assert repo.tasks[1].status == "classified"
    assert repo.pipelines[1].domain_route == "crypto"
    assert repo.pipelines[1].prompt_template_key == ALERT_PROMPT_KEY
    assert repo.pipelines[1].prompt_version_id == 9
    assert "来源媒体：WSJ Finance" in str(ai_client.calls[0]["prompt"])
    assert '"enum": ["crypto", "discard"]' in json.dumps(ai_client.calls[0]["text_format"], ensure_ascii=False)


def test_search_marks_duplicate_when_matching_odaily_history() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="classified",
            site_key="wsj_business",
            site_display_name="WSJ Business",
            title="Bitcoin treasury wave spreads to small caps",
            excerpt="Corporate buyers accelerate treasury strategy.",
            source_url="https://www.wsj.com/articles/bitcoin-wave/",
        )
    )
    repo.odaily_references = [
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="odaily-1",
            title="Bitcoin treasury wave spreads to small caps",
            content="Odaily already covered this event.",
            source="odaily",
            source_url="https://www.odaily.news/post/1",
        )
    ]
    worker = ExternalMediaAlertWorker(
        stage="search",
        repository=repo,
        settings=settings(),
        ai_client=FakeAIClient([]),
        search_embedding_service=FakeEmbeddingService(),
        search_ai_client=FakeAIClient([]),
    )

    worker.run_once()

    assert repo.tasks[1].status == "duplicate"
    assert repo.pipelines[1].search_result["duplicate_target_type"] == "odaily_published"


def test_search_marks_duplicate_when_matching_prior_alert_history() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="notified",
            site_key="fortune_crypto",
            site_display_name="Fortune Crypto",
            title="Stablecoin bill clears key hurdle in the Senate",
            excerpt="Earlier reminder",
            source_url="https://fortune.com/2026/05/18/stablecoin-bill/",
        )
    )
    repo.add_task(
        build_task(
            task_id=2,
            status="classified",
            site_key="ft_crypto",
            site_display_name="FT Crypto",
            title="Stablecoin bill clears key hurdle in the Senate",
            excerpt="New source, same title",
            source_url="https://www.ft.com/content/two/",
        )
    )
    worker = ExternalMediaAlertWorker(
        stage="search",
        repository=repo,
        settings=settings(),
        ai_client=FakeAIClient([]),
        search_embedding_service=FakeEmbeddingService(),
        search_ai_client=FakeAIClient([]),
    )

    worker.run_once()

    assert repo.tasks[2].status == "duplicate"
    assert repo.pipelines[2].search_result["duplicate_target_type"] == "external_media_alert_history"


def test_notify_sends_separate_telegram_notice() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="deduped",
            site_key="ft_crypto",
            site_display_name="FT Crypto",
            title="Token market makers face tighter oversight",
            excerpt="Alert content",
            source_url="https://www.ft.com/content/notice/",
        )
    )
    telegram = FakeTelegramClient(ok=True)
    worker = ExternalMediaAlertWorker(
        stage="notify",
        repository=repo,
        settings=settings(),
        telegram_client=telegram,
    )

    worker.run_once()

    assert repo.tasks[1].status == "notified"
    assert telegram.calls == [
        "外媒标题提醒：FT Crypto｜Token market makers face tighter oversight\nhttps://www.ft.com/content/notice/"
    ]
