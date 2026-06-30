from __future__ import annotations

import json
from datetime import UTC, datetime

from packages.external_media_alert import (
    AI_SOURCE_ALERT_TASK_SOURCE,
    ALERT_PROMPT_KEY,
    InMemoryExternalMediaAlertRepository,
    MAINSTREAM_MEDIA_TASK_SOURCE,
    PostgresExternalMediaAlertRepository,
)
from packages.external_media_alert.fetcher import get_site_registry, parse_feed_items
from packages.external_media_alert.models import ExternalMediaSourceDefinition, MediaNewsflashItem
from packages.external_media_alert.repository import SCHEMA_SQL as EXTERNAL_MEDIA_ALERT_SCHEMA_SQL
from packages.external_media_alert.worker import ExternalMediaAlertWorker, build_alert_notice, parse_domain_route
from packages.common.config import ExternalMediaAlertSettings, RetrySettings, load_external_media_alert_settings
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
    source: str = "external_media_alert",
) -> TaskRecord:
    now = datetime(2026, 5, 19, 12, 0, tzinfo=UTC)
    return TaskRecord(
        id=task_id,
        source=source,
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
            "source_kind": source,
        },
    )


def test_external_media_alert_settings_can_disable_notify_listener(monkeypatch) -> None:
    monkeypatch.setenv("EXTERNAL_MEDIA_ALERT_ENABLE_NOTIFY_LISTENER", "false")

    loaded = load_external_media_alert_settings()

    assert loaded.enable_notify_listener is False


def test_external_media_alert_notify_listener_defaults_disabled(monkeypatch) -> None:
    monkeypatch.delenv("EXTERNAL_MEDIA_ALERT_ENABLE_NOTIFY_LISTENER", raising=False)

    loaded = load_external_media_alert_settings()

    assert loaded.enable_notify_listener is False


def test_external_media_alert_schema_drops_task_notify_trigger() -> None:
    assert "DROP TRIGGER IF EXISTS trg_tasks_external_media_alert_queue_notify ON tasks" in EXTERNAL_MEDIA_ALERT_SCHEMA_SQL
    assert "DROP FUNCTION IF EXISTS notify_external_media_alert_task_queue_changed()" in EXTERNAL_MEDIA_ALERT_SCHEMA_SQL
    assert "CREATE TRIGGER trg_tasks_external_media_alert_queue_notify" not in EXTERNAL_MEDIA_ALERT_SCHEMA_SQL


def test_external_media_alert_worker_skips_notify_listener_when_disabled(capsys) -> None:
    repo = PostgresExternalMediaAlertRepository(database_url="postgresql://example")
    worker = ExternalMediaAlertWorker(
        stage="notify",
        repository=repo,
        settings=ExternalMediaAlertSettings(enable_notify_listener=False),
    )

    thread = worker._start_notify_listener()

    assert thread is None
    assert "notify listener disabled" in capsys.readouterr().out


def test_build_alert_notice_avoids_duplicate_url_when_title_is_url() -> None:
    notice = build_alert_notice(
        site_display_name="The Block",
        title="https://www.theblock.co/post/406731/new-zcash-nonprofit-sovright-unveils-zec-wallet-recovery-tool",
        source_url="https://www.theblock.co/post/406731/new-zcash-nonprofit-sovright-unveils-zec-wallet-recovery-tool",
    )

    assert notice.count("https://www.theblock.co/post/406731/new-zcash-nonprofit-sovright-unveils-zec-wallet-recovery-tool") == 1


def test_the_block_uses_official_rss_feed() -> None:
    site = get_site_registry()["the_block"]

    assert site.feed_url == "https://www.theblock.co/rss.xml"


def test_parse_feed_items_handles_the_block_rss_payload() -> None:
    site = ExternalMediaSourceDefinition(
        site_key="the_block",
        display_name="The Block",
        homepage_url="https://www.theblock.co/",
        feed_url="https://www.theblock.co/rss.xml",
        list_url="https://www.theblock.co/latest-crypto-news",
    )
    xml_text = """
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <item>
          <title>First The Block Title</title>
          <link>https://www.theblock.co/post/123/first-story?utm_source=rss&amp;utm_medium=rss</link>
          <description><![CDATA[First The Block Title - Lead paragraph from rss.]]></description>
          <pubDate>Sat, 24 May 2026 10:00:00 +0000</pubDate>
        </item>
        <item>
          <title>Duplicate Should Collapse</title>
          <link>https://www.theblock.co/post/123/first-story?utm_source=other</link>
          <description><![CDATA[Different query string but same article.]]></description>
          <pubDate>Sat, 24 May 2026 10:01:00 +0000</pubDate>
        </item>
      </channel>
    </rss>
    """

    items = parse_feed_items(site, xml_text, timeout_seconds=1)

    assert len(items) == 1
    assert items[0].title == "First The Block Title"
    assert items[0].content == "Lead paragraph from rss."
    assert items[0].source_url == "https://www.theblock.co/post/123/first-story"
    assert items[0].metadata["site_key"] == "the_block"


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
    ai_client = FakeAIClient(['{"route":"crypto","discard_reason":"none"}'])
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


def test_ai_source_alert_domain_judge_uses_ai_source_prompt_label() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="thelec_china",
            site_display_name="TheElec CHINA",
            title="AI chip supplier expands packaging capacity",
            excerpt="AI chip supply chain update",
            source_url="https://www.thelec.net/news/articleView.html?idxno=10961",
            source=AI_SOURCE_ALERT_TASK_SOURCE,
        )
    )
    ai_client = FakeAIClient(['{"route":"crypto","discard_reason":"none"}'])
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=ai_client,
    )

    result = worker.run_once()

    assert result.processed == 1
    assert repo.tasks[1].status == "classified"
    assert "任务类型：AI信源标题提醒" in ai_client.calls[0]["prompt"]
    assert "来源媒体：TheElec CHINA" in ai_client.calls[0]["prompt"]


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
    ai_client = FakeAIClient(['{"route":"discard","discard_reason":"non_crypto"}'])
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
        ai_client=FakeAIClient(['{"route":"discard","discard_reason":"non_crypto"}']),
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
    ai_client = FakeAIClient(['{"route":"discard","discard_reason":"non_crypto"}'])
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
    ai_client = FakeAIClient(['{"route":"crypto","discard_reason":"none"}'])
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
    assert '"enum": ["none", "non_crypto", "market_analysis"]' in json.dumps(ai_client.calls[0]["text_format"], ensure_ascii=False)


def test_domain_judge_discards_mainstream_market_analysis() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="pending",
            site_key="coindesk",
            site_display_name="CoinDesk",
            title="Bitcoin price outlook shows resistance near $110K",
            excerpt="Analysts expect BTC to move between support and resistance while major indices stay mixed.",
            source_url="https://www.coindesk.com/markets/2026/05/24/bitcoin-price-outlook",
            source=MAINSTREAM_MEDIA_TASK_SOURCE,
        )
    )
    worker = ExternalMediaAlertWorker(
        stage="domain_judge",
        repository=repo,
        settings=settings(),
        ai_client=FakeAIClient(['{"route":"discard","discard_reason":"market_analysis"}']),
    )

    worker.run_once()

    assert repo.tasks[1].status == "discarded"
    assert repo.pipelines[1].discard_reason == "market_analysis"
    assert repo.pipelines[1].domain_output["discard_reason"] == "market_analysis"


def test_parse_domain_route_defaults_missing_discard_reason_for_discard() -> None:
    route, discard_reason = parse_domain_route('{"route":"discard"}')

    assert route == "discard"
    assert discard_reason == "non_crypto"


def test_parse_domain_route_defaults_missing_discard_reason_for_crypto() -> None:
    route, discard_reason = parse_domain_route('{"route":"crypto"}')

    assert route == "crypto"
    assert discard_reason == "none"


def test_media_newsflash_save_enqueues_mainstream_media_task() -> None:
    repo = InMemoryExternalMediaAlertRepository()

    saved, duplicate = repo.save_media_newsflash_items(
        [
            MediaNewsflashItem(
                source="coindesk",
                title="Bitcoin ETFs add to inflow streak",
                content="Spot Bitcoin ETFs extended their inflow streak for a fifth day.",
                source_url="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak",
                metadata={"site_key": "coindesk", "site_display_name": "CoinDesk"},
            )
        ]
    )

    tasks = list(repo.tasks.values())

    assert (saved, duplicate) == (1, 0)
    assert len(tasks) == 1
    assert tasks[0].source == MAINSTREAM_MEDIA_TASK_SOURCE
    assert tasks[0].metadata["site_display_name"] == "CoinDesk"
    assert tasks[0].metadata["original_title"] == "Bitcoin ETFs add to inflow streak"


def test_media_newsflash_duplicate_title_does_not_enqueue_task() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    first = MediaNewsflashItem(
        source="coindesk",
        title="Bitcoin ETFs add to inflow streak",
        content="First version.",
        source_url="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak",
        metadata={"site_key": "coindesk", "site_display_name": "CoinDesk"},
    )
    duplicate_by_title = MediaNewsflashItem(
        source="coindesk",
        title="Bitcoin ETFs add to inflow streak",
        content="Second version.",
        source_url="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak-update",
        metadata={"site_key": "coindesk", "site_display_name": "CoinDesk"},
    )

    assert repo.save_media_newsflash_items([first]) == (1, 0)
    assert repo.save_media_newsflash_items([duplicate_by_title]) == (0, 1)

    tasks = list(repo.tasks.values())
    assert len(tasks) == 1
    assert tasks[0].source_item_id == "coindesk:https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak"


def test_media_newsflash_duplicate_url_does_not_enqueue_task() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    first = MediaNewsflashItem(
        source="decrypt",
        title="Bitcoin dips as traders de-risk",
        content="First version.",
        source_url="https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion",
        metadata={"site_key": "decrypt", "site_display_name": "Decrypt"},
    )
    duplicate_by_url = MediaNewsflashItem(
        source="decrypt",
        title="A different title",
        content="Second version.",
        source_url="https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion",
        metadata={"site_key": "decrypt", "site_display_name": "Decrypt"},
    )

    assert repo.save_media_newsflash_items([first]) == (1, 0)
    assert repo.save_media_newsflash_items([duplicate_by_url]) == (0, 1)

    tasks = list(repo.tasks.values())
    assert len(tasks) == 1
    assert tasks[0].source_item_id == "decrypt:https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion"


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


def test_ai_source_alert_notify_uses_ai_source_notice_label() -> None:
    repo = InMemoryExternalMediaAlertRepository()
    repo.add_task(
        build_task(
            task_id=1,
            status="deduped",
            site_key="thelec_china",
            site_display_name="TheElec CHINA",
            title="AI chip supplier expands packaging capacity",
            excerpt="AI chip supply chain update",
            source_url="https://www.thelec.net/news/articleView.html?idxno=10961",
            source=AI_SOURCE_ALERT_TASK_SOURCE,
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
        "AI信源标题提醒：TheElec CHINA｜AI chip supplier expands packaging capacity\nhttps://www.thelec.net/news/articleView.html?idxno=10961"
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
