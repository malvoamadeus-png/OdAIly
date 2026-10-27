from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.common.storage import connect_sqlite
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.editor_plugin_local_store import LocalEditorPluginStore
from packages.external_media_alert.models import BLOOMBERG_PROMPT_KEY
from packages.external_media_alert.models import ExternalMediaAlertPipelineRecord
from packages.external_media_alert.repository import InMemoryExternalMediaAlertRepository
from packages.external_media_alert.sqlite_repository import SQLiteExternalMediaAlertRepository
from packages.external_media_alert.worker import (
    BLOOMBERG_DOMAIN_JSON_SCHEMA,
    ExternalMediaAlertWorker,
    parse_bloomberg_domain_result,
)
from packages.non_mainstream_media import (
    InMemoryNonMainstreamMediaRepository,
    NonMainstreamMediaWorker,
    get_site_registry,
)
from packages.non_mainstream_media.fetcher import discover_bloomberg_rss_pages
from packages.non_mainstream_media.models import DiscoveredPage
from packages.x_processing.models import PromptTemplateVersion, TaskRecord


RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Bitcoin ETF sees record inflows</title>
    <link>https://www.bloomberg.com/news/articles/2026-10-27/bitcoin-etf-inflows</link>
    <description><![CDATA[Funds added bitcoin exposure as demand accelerated.]]></description>
    <pubDate>Tue, 27 Oct 2026 01:00:00 GMT</pubDate>
  </item>
  <item>
    <title>Duplicate Bloomberg headline</title>
    <link>https://www.bloomberg.com/news/articles/2026-10-27/bitcoin-etf-inflows?utm_source=rss</link>
    <description>Duplicate URL after normalization</description>
  </item>
  <item>
    <title>Unrelated site link with a long enough headline</title>
    <link>https://example.com/news/articles/other</link>
  </item>
</channel></rss>"""


def test_bloomberg_rss_parser_extracts_fields_and_deduplicates_urls() -> None:
    pages = discover_bloomberg_rss_pages(RSS_SAMPLE)

    assert len(pages) == 1
    page = pages[0]
    assert page.source_item_id == "https://www.bloomberg.com/news/articles/2026-10-27/bitcoin-etf-inflows/"
    assert page.detail_url == page.source_item_id
    assert page.title == "Bitcoin ETF sees record inflows"
    assert page.excerpt == "Funds added bitcoin exposure as demand accelerated."
    assert page.published_at == datetime(2026, 10, 27, 1, 0, tzinfo=UTC)
    assert page.published_at_raw == "Tue, 27 Oct 2026 01:00:00 GMT"


def test_bloomberg_site_is_registered_in_mixed_alert_only_group() -> None:
    site = get_site_registry()["bloomberg_news"]

    assert site.list_url == "https://www.bloomberg.com/feeds/news.rss"
    assert site.pipeline_mode == "alert_only"
    assert site.source_group == "mixed_source"


class RecordingPipelineClient:
    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def submit_job(self, **kwargs: object) -> None:
        self.jobs.append(kwargs)


class MustNotClassifyMixedSource:
    model = "unused"

    def classify_headline_excerpt(self, **kwargs: object):
        raise AssertionError("Bloomberg must use its own alert judge")


def test_bloomberg_first_poll_seeds_then_new_items_enter_alert_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    site = get_site_registry()["bloomberg_news"]
    repository = InMemoryNonMainstreamMediaRepository()
    repository.sync_sources([site])
    pipeline = RecordingPipelineClient()
    worker = NonMainstreamMediaWorker(
        repository=repository,
        site_registry={"bloomberg_news": site},
        pipeline_client=pipeline,
        mixed_classifier=MustNotClassifyMixedSource(),
    )
    old = DiscoveredPage(
        source_item_id="https://www.bloomberg.com/news/articles/old",
        detail_url="https://www.bloomberg.com/news/articles/old",
        title="Old title",
        excerpt="Old summary",
    )
    new = DiscoveredPage(
        source_item_id="https://www.bloomberg.com/news/articles/new",
        detail_url="https://www.bloomberg.com/news/articles/new",
        title="New title",
        excerpt="New summary",
    )
    pages = [old]
    monkeypatch.setattr(
        "packages.non_mainstream_media.worker.fetch_discovered_pages",
        lambda *args, **kwargs: list(pages),
    )

    first = worker.process_source(repository.list_sources()[0])
    assert first.seeded_count == 1
    assert first.saved_count == 0
    assert not pipeline.jobs

    pages[:] = [old, new]
    second = worker.process_source(repository.list_sources()[0])

    assert second.saved_count == 1
    assert len(repository.tasks) == 1
    task = repository.tasks[0]
    assert task["source"] == "external_media_alert"
    assert task["source_url"] == new.detail_url
    assert task["content"] == new.excerpt
    assert task["metadata"]["site_key"] == "bloomberg_news"
    assert task["metadata"]["source_group"] == "mixed_source"
    assert pipeline.jobs[0]["job_type"] == "alert_only"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"route": "eligible", "categories": ["crypto"]}, (True, ("crypto",))),
        ({"route": "eligible", "categories": ["trump"]}, (True, ("trump",))),
        ({"route": "eligible", "categories": ["ai"]}, (True, ("ai",))),
        (
            {"route": "eligible", "categories": ["ai", "crypto", "trump"]},
            (True, ("crypto", "trump", "ai")),
        ),
        ({"route": "discard", "categories": []}, (False, ())),
    ],
)
def test_bloomberg_domain_result_supports_multi_label_eligibility(payload, expected) -> None:
    assert parse_bloomberg_domain_result(json.dumps(payload)) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"route": "eligible", "categories": []},
        {"route": "discard", "categories": ["crypto"]},
        {"route": "eligible", "categories": ["politics"]},
    ],
)
def test_bloomberg_domain_result_rejects_inconsistent_categories(payload) -> None:
    with pytest.raises(ValueError):
        parse_bloomberg_domain_result(json.dumps(payload))


class RecordingAlertRepository:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompt_keys: list[str] = []
        self.completed: list[dict[str, object]] = []
        self.discarded: list[dict[str, object]] = []

    def get_active_prompt(self, key: str):
        self.prompt_keys.append(key)
        return PromptTemplateVersion(
            id=1,
            template_key=key,
            version_number=1,
            content="Prompt content",
        )

    def complete_domain(self, task_id: int, **kwargs: object) -> None:
        self.completed.append({"task_id": task_id, **kwargs})

    def complete_domain_discard(self, task_id: int, **kwargs: object) -> None:
        self.discarded.append({"task_id": task_id, **kwargs})


class RecordingAIClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def generate_text(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.response


def make_domain_worker(repository: RecordingAlertRepository, client: RecordingAIClient):
    worker = object.__new__(ExternalMediaAlertWorker)
    worker.repository = repository
    worker.ai_client = client
    worker.settings = SimpleNamespace(domain_judge_model="judge-model")
    worker._prompt_cache = {}
    return worker


def test_bloomberg_domain_judge_uses_own_prompt_and_saves_categories() -> None:
    response = json.dumps({"route": "eligible", "categories": ["crypto", "ai"]})
    repository = RecordingAlertRepository(response)
    client = RecordingAIClient(response)
    worker = make_domain_worker(repository, client)
    task = SimpleNamespace(
        id=7,
        title="Bitcoin and AI companies",
        content="",
        source_url="https://www.bloomberg.com/news/articles/test",
        metadata={
            "site_key": "bloomberg_news",
            "site_display_name": "Bloomberg",
            "excerpt": "A summary from RSS.",
        },
    )

    worker._run_domain_judge(task)

    assert repository.prompt_keys == [BLOOMBERG_PROMPT_KEY]
    assert repository.completed[0]["route"] == "bloomberg"
    assert repository.completed[0]["categories"] == ("crypto", "ai")
    assert client.calls[0]["text_format"] == BLOOMBERG_DOMAIN_JSON_SCHEMA
    assert "RSS 摘要：A summary from RSS." in client.calls[0]["prompt"]


def test_non_bloomberg_alerts_keep_existing_domain_prompt() -> None:
    response = json.dumps({"route": "crypto", "discard_reason": "none"})
    repository = RecordingAlertRepository(response)
    client = RecordingAIClient(response)
    worker = make_domain_worker(repository, client)
    task = SimpleNamespace(
        id=8,
        source="external_media_alert",
        title="Crypto headline",
        content="",
        source_url="https://example.test/story",
        metadata={"site_key": "the_block", "site_display_name": "The Block"},
    )

    worker._run_domain_judge(task)

    assert repository.prompt_keys == ["external_media_alert_domain_judge"]
    assert repository.completed[0]["route"] == "crypto"
    assert "categories" not in repository.completed[0]


def test_bloomberg_categories_persist_in_alert_pipeline_sqlite(tmp_path: Path) -> None:
    repository = SQLiteExternalMediaAlertRepository(tmp_path / "alerts.sqlite")
    with connect_sqlite(repository.path) as conn:
        cursor = conn.execute(
            "INSERT INTO tasks(source, source_item_id, content, metadata) VALUES (?, ?, ?, ?)",
            (
                "external_media_alert",
                "bloomberg:test",
                "A Bloomberg alert",
                json.dumps({"site_key": "bloomberg_news"}),
            ),
        )
        task_id = int(cursor.lastrowid)
    repository.ensure_pipeline(task_id)

    repository.complete_domain(
        task_id,
        route="bloomberg",
        categories=("crypto", "ai"),
        prompt=None,
        model="judge-model",
        raw_output='{"route":"eligible","categories":["crypto","ai"]}',
    )

    record = repository.get_pipeline(task_id)
    assert record.domain_route == "bloomberg"
    assert record.domain_output["categories"] == ["crypto", "ai"]


def test_bloomberg_categories_persist_in_memory_alert_repository() -> None:
    repository = InMemoryExternalMediaAlertRepository()
    repository.add_task(
        TaskRecord(
            id=1,
            source="external_media_alert",
            source_item_id="bloomberg:test",
            source_url=None,
            title="A Bloomberg alert",
            content="Summary",
        )
    )
    repository.pipelines[1] = ExternalMediaAlertPipelineRecord(task_id=1)

    repository.complete_domain(
        1,
        route="bloomberg",
        categories=("crypto", "trump"),
        prompt=None,
        model="judge-model",
        raw_output='{"route":"eligible","categories":["crypto","trump"]}',
    )

    record = repository.get_pipeline(1)
    assert record.domain_route == "bloomberg"
    assert record.domain_output["categories"] == ["crypto", "trump"]


def test_bloomberg_feed_shows_matching_topic_badge(tmp_path: Path) -> None:
    store = LocalEditorPluginStore(tmp_path / "feed.sqlite")
    writer = LocalEditorPluginFeedWriter(store)

    writer.upsert_external_media_alert(
        task_id=23,
        source="external_media_alert",
        source_url="https://www.bloomberg.com/news/articles/test",
        title="Bloomberg headline",
        content="RSS summary",
        metadata={
            "site_key": "bloomberg_news",
            "site_display_name": "Bloomberg",
            "bloomberg_categories": ["crypto", "trump", "ai"],
        },
    )

    row = store.list_feed_items(limit=10, max_age_hours=2)[0]
    assert {badge["value"] for badge in row["badges"]} == {
        "Bloomberg",
        "标题提醒",
        "Crypto / 特朗普 / AI",
    }
    assert row["meta_json"]["bloomberg_categories"] == ["crypto", "trump", "ai"]
