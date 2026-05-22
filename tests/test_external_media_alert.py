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


class CountingAlertRepository(InMemoryExternalMediaAlertRepository):
    def __init__(self) -> None:
        super().__init__()
        self.odaily_reads = 0
        self.history_reads = 0

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        self.odaily_reads += 1
        return super().list_odaily_reference_documents(since=since)

    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]:
        self.history_reads += 1
        return super().list_notified_alert_documents(since=since)


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


def test_domain_judge_sends_strong_crypto_title_to_model() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="ft_crypto",
            site_display_name="FT Crypto",
            title="Investors weigh crypto token issuance plans",
            excerpt="Section story",
            source_url="https://www.ft.com/content/one/",
        )
    )
    ai_client = FakeAIClient(['{"route":"crypto"}'])
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
    assert repo.pipelines[1].domain_model == "gpt-5.4-mini"
    assert len(ai_client.calls) == 1


def test_domain_judge_sends_fortune_non_crypto_title_to_model() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="fortune_crypto",
            site_display_name="Fortune Crypto",
            title="How Coach became Gen Z’s favorite affordable luxury handbag brand",
            excerpt="Retail feature",
            source_url="https://fortune.com/2026/05/19/coach-handbags-comeback-millennials-gen-z/",
        )
    )
    ai_client = FakeAIClient(['{"route":"discard"}'])
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=ai_client,
    )

    worker.run_once()

    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].discard_reason == "non_crypto"
    assert len(ai_client.calls) == 1


def test_domain_judge_sends_strong_non_crypto_title_to_model() -> None:
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
        ai_client=FakeAIClient(['{"route":"discard"}']),
    )

    worker.run_once()

    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].discard_reason == "non_crypto"
    assert repo.pipelines[1].domain_model == "gpt-5.4-mini"


def test_domain_judge_sends_short_ticker_substring_to_model() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="wsj_finance",
            site_display_name="WSJ Finance",
            title=(
                "Heard on the Street: While everyone obsesses over what happens to oil flows in the Strait of "
                "Hormuz, a solar-power revolution is under way in some emerging countries"
            ),
            excerpt="Solar-power trade-barrier story.",
            source_url="https://www.wsj.com/finance/investing/to-cash-in-on-solar-stocks-look-for-trade-barriers-ad33e94f/",
        )
    )
    ai_client = FakeAIClient(['{"route":"discard"}'])
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=ai_client,
    )

    worker.run_once()

    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].discard_reason == "non_crypto"
    assert len(ai_client.calls) == 1


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


def test_search_uses_local_mirrored_alert_history_after_warmup() -> None:
    repo = CountingAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
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
    worker._search_cache().upsert_document(
        SearchDocument(
            doc_type="external_media_alert_history",
            doc_id="https://fortune.com/2026/05/18/stablecoin-bill/",
            title="Stablecoin bill clears key hurdle in the Senate",
            content="Earlier reminder",
            source="external_media_alert",
            status="notified",
            created_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        )
    )

    worker.run_once()

    assert repo.tasks[1].status == "duplicate"
    assert repo.history_reads == 0


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


def test_notify_mirrors_history_into_local_cache() -> None:
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
    worker = ExternalMediaAlertWorker(
        stage="notify",
        repository=repo,
        settings=settings(),
        telegram_client=FakeTelegramClient(ok=True),
    )

    worker.run_once()

    cached = worker._search_cache().list_notified_alert_documents()
    assert [document.doc_id for document in cached] == ["https://www.ft.com/content/notice/"]
