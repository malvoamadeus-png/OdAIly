from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.common.config import CompetitorMonitorSettings, XProcessingSettings
from packages.competitor_monitor.events import EventAssignment, EventSourceRecord, NewsflashEventAggregator, NewsflashItemRecord
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.worker import CompetitorMonitorWorker
from packages.x_processing.models import PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.worker import XProcessingWorker


class FailingAiClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error or ValueError("Expecting value: line 1 column 1 (char 0)")

    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        raise self.error


def test_competitor_judge_falls_back_when_ai_returns_empty_json() -> None:
    repo = InMemoryXProcessingRepository()
    repo.add_task(
        TaskRecord(
            id=1,
            source="panews",
            source_item_id="p1",
            source_url="https://www.panewslab.com/zh/articledetails/p1.html",
            title="Gravity Bridge被盗资金中，9.1万美元已被冻结",
            content="跨链桥 Gravity Bridge 被盗资金中的 9.1 万美元已被冻结，攻击者仍持有大部分被盗资金。",
            published_at=datetime.now(UTC),
            status="searched",
            metadata={"source_kind": "competitor"},
        )
    )
    worker = XProcessingWorker(
        stage="judge_crypto",
        repository=repo,
        settings=XProcessingSettings(openai_api_key="key"),
        ai_client=FailingAiClient(),
    )

    result = worker.run_once()

    assert result.processed == 1
    assert result.failed == 0
    assert repo.tasks[1].status == "deduped"
    assert repo.pipelines[1].news_type == "onchain"
    assert repo.pipelines[1].last_error is None


def test_event_aggregator_skips_ai_review_failure_without_failing_round() -> None:
    existing_record = NewsflashItemRecord(
        id=1,
        source="blockbeats",
        source_item_id="existing",
        source_url=None,
        title="比特币 ETF 获批",
        content="比特币 ETF 获批 正文",
        published_at=datetime.now(UTC),
        first_seen_at=datetime.now(UTC),
    )

    class FakeRepository:
        def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]:
            return [
                NewsflashItemRecord(
                    id=2,
                    source=items[0].source,
                    source_item_id=items[0].source_item_id,
                    source_url=items[0].source_url,
                    title=items[0].title,
                    content=items[0].content,
                    published_at=datetime.now(UTC),
                    first_seen_at=datetime.now(UTC),
                )
            ]

        def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]:
            return []

        def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]:
            return [EventSourceRecord(event_id="evt_existing", item=existing_record)]

        def create_event_with_source(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str:
            return "evt_new"

        def assign_item_to_event(self, assignment: EventAssignment) -> None:
            self.assignment = assignment

        def update_event_summaries(self, event_ids: set[str]) -> None:
            self.updated_event_ids = event_ids

    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed(self, texts: list[str]) -> list[list[float]]:
            vectors: list[list[float]] = []
            for text in texts:
                vectors.append([1.0, 0.0] if "另一写法" in text else [0.8, 0.6])
            return vectors

    class FakeCache:
        def get_embedding(self, *, cache_key: str, model: str, text_hash: str) -> list[float] | None:
            return None

        def set_embedding(self, *, cache_key: str, model: str, text_hash: str, vector: list[float]) -> None:
            return None

        def upsert_document(self, document) -> None:
            return None

    repo = FakeRepository()
    aggregator = NewsflashEventAggregator(
        repository=repo,
        settings=CompetitorMonitorSettings(
            blockbeats_api_key="key",
            event_duplicate_threshold=0.99,
            event_ai_review_threshold=0.5,
        ),
        embedding_client=FakeEmbeddingClient(),
        ai_client=FailingAiClient(),
        cache=FakeCache(),
    )

    event_ids = aggregator.assign_items([NewsflashItem("panews", "new", "比特币 ETF 获批", "比特币 ETF 获批 另一写法")])

    assert event_ids == {"evt_new"}
    assert repo.updated_event_ids == {"evt_new"}


def test_competitor_worker_keeps_success_heartbeat_when_event_aggregation_fails(monkeypatch) -> None:
    class FakeRepository:
        def __init__(self) -> None:
            self.saved: list[NewsflashItem] = []
            self.heartbeats: list[dict[str, Any]] = []

        def list_enabled_filter_keywords(self) -> list[str]:
            return []

        def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]:
            self.saved.extend(items)
            return sum(1 for item in items if item.source != "odaily"), sum(1 for item in items if item.source == "odaily")

        def record_worker_heartbeat(self, **kwargs) -> None:
            self.heartbeats.append(kwargs)

    fresh_time = datetime.now(UTC).isoformat()
    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_blockbeats",
        lambda **kwargs: [NewsflashItem("blockbeats", "b1", "竞品快讯", "正文", published_at=fresh_time)],
    )
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_panews", lambda **kwargs: [])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_jinse", lambda **kwargs: [])
    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_odaily",
        lambda **kwargs: [NewsflashItem("odaily", "o1", "本方快讯", "正文", published_at=fresh_time)],
    )
    repo = FakeRepository()
    worker = CompetitorMonitorWorker(repository=repo, settings=CompetitorMonitorSettings(blockbeats_api_key="key"))
    monkeypatch.setattr(worker, "_assign_events", lambda items: (_ for _ in ()).throw(ValueError("Expecting value: line 1 column 1 (char 0)")))

    result = worker.run_once()

    assert result.failed_sources == {}
    assert result.event_error == "Expecting value: line 1 column 1 (char 0)"
    assert result.task_inserted == 1
    assert result.reference_inserted == 1
    assert repo.heartbeats[-1]["success"] is True
    assert repo.heartbeats[-1]["metadata"]["event_error"] == "Expecting value: line 1 column 1 (char 0)"
