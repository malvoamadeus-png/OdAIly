from __future__ import annotations

from datetime import UTC, datetime

from packages.competitor_monitor.fetchers import fetch_odaily, normalize_item_content, scrub_competitor_brands
from packages.competitor_monitor.events import EventAssignment, EventSourceRecord, NewsflashEventAggregator, NewsflashItemRecord
from packages.competitor_monitor.worker import CompetitorMonitorWorker, match_filter_terms


class FakeRepository:
    def __init__(self) -> None:
        self.saved = []
        self.newsflash_items = []
        self.assignments: list[EventAssignment] = []
        self.events: dict[str, NewsflashItemRecord] = {}
        self.keywords: list[str] = []
        self._next_id = 1
        self._next_event_id = 1

    def init_schema(self) -> None:
        return None

    def list_enabled_filter_keywords(self):
        return self.keywords

    def _find_assignment(self, item_id: int) -> EventAssignment | None:
        for assignment in reversed(self.assignments):
            if assignment.item_id == item_id:
                return assignment
        return None

    def save_items(self, items):
        self.saved.extend(items)
        task_count = sum(1 for item in items if item.source != "odaily")
        ref_count = sum(1 for item in items if item.source == "odaily")
        return task_count, ref_count

    def upsert_newsflash_items(self, items):
        records = []
        for item in items:
            existing = next(
                (record for record in self.newsflash_items if record.source == item.source and record.source_item_id == item.source_item_id),
                None,
            )
            if existing is None:
                record = NewsflashItemRecord(
                    id=self._next_id,
                    source=item.source,
                    source_item_id=item.source_item_id,
                    source_url=item.source_url,
                    title=item.title,
                    content=item.content,
                    published_at=datetime(2026, 5, 10, 10, self._next_id, tzinfo=UTC),
                    first_seen_at=datetime(2026, 5, 10, 10, self._next_id, tzinfo=UTC),
                )
                self._next_id += 1
                self.newsflash_items.append(record)
            else:
                record = existing
            records.append(record)
        return records

    def list_existing_event_sources(self, *, item_ids):
        rows = []
        for record in self.newsflash_items:
            if record.id not in item_ids:
                continue
            assignment = self._find_assignment(record.id)
            if assignment is not None:
                rows.append(EventSourceRecord(event_id=assignment.event_id, item=record))
        return rows

    def list_recent_event_sources(self, *, since, exclude_item_ids):  # noqa: ANN001
        rows = []
        for record in self.newsflash_items:
            if record.id in exclude_item_ids:
                continue
            assignment = self._find_assignment(record.id)
            if assignment is not None:
                rows.append(EventSourceRecord(event_id=assignment.event_id, item=record))
        return rows

    def create_event_for_item(self, item, *, needs_review=False):  # noqa: ANN001
        event_id = f"evt_{self._next_event_id}"
        self._next_event_id += 1
        self.events[event_id] = item
        return event_id

    def assign_item_to_event(self, assignment):
        self.assignments.append(assignment)

    def update_event_summaries(self, event_ids):
        self.updated_event_ids = set(event_ids)

    def record_worker_heartbeat(self, **kwargs):
        return None


def test_competitor_brand_scrub_removes_fixed_prefix_and_names() -> None:
    text = "机构：油价上涨 BlockBeats 消息，5 月 9 日，律动报道称金色财经报道，BTC 上涨。"

    cleaned = scrub_competitor_brands(text)

    assert "BlockBeats" not in cleaned
    assert "律动" not in cleaned
    assert "金色财经" not in cleaned


def test_normalize_content_removes_title_prefix() -> None:
    title = "某项目完成融资"
    content = "某项目完成融资 BlockBeats 消息，5 月 9 日，该项目完成融资。"

    assert normalize_item_content(title, content) == "该项目完成融资。"


def test_worker_saves_odaily_as_reference_and_competitors_as_tasks(monkeypatch) -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem
    from packages.common.config import CompetitorMonitorSettings

    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_blockbeats",
        lambda **kwargs: [NewsflashItem("blockbeats", "1", "竞品", "正文")],
    )
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_panews", lambda **kwargs: [])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_jinse", lambda **kwargs: [])
    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_odaily",
        lambda **kwargs: [NewsflashItem("odaily", "o1", "本方", "正文")],
    )
    repo = FakeRepository()
    worker = CompetitorMonitorWorker(repository=repo, settings=CompetitorMonitorSettings(blockbeats_api_key="key"))
    monkeypatch.setattr(worker, "_assign_events", lambda items: {"evt_1"})

    result = worker.run_once()

    assert result.task_inserted == 1
    assert result.reference_inserted == 1
    assert result.events_updated == 1
    assert result.filtered == 0
    assert [item.source for item in repo.saved] == ["blockbeats", "odaily"]


def test_filter_keywords_match_title_body_and_bitget_case_insensitive() -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem

    title_hit = NewsflashItem("blockbeats", "1", "BTC 跌破关键位置", "正文")
    body_hit = NewsflashItem("panews", "2", "标题", "某地址发生爆仓")
    case_hit = NewsflashItem("jinse", "3", "标题", "bitget 宣布上线新币")

    assert match_filter_terms(title_hit, ["跌破"]) == ["跌破"]
    assert match_filter_terms(body_hit, ["爆仓"]) == ["爆仓"]
    assert match_filter_terms(case_hit, ["Bitget"]) == ["Bitget"]


def test_worker_skips_filtered_competitor_but_keeps_odaily_reference(monkeypatch) -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_blockbeats",
        lambda **kwargs: [NewsflashItem("blockbeats", "1", "BTC 跌破 10 万美元", "正文")],
    )
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_panews", lambda **kwargs: [])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_jinse", lambda **kwargs: [])
    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_odaily",
        lambda **kwargs: [NewsflashItem("odaily", "o1", "本方突破", "正文含 Bitget")],
    )
    repo = FakeRepository()
    repo.keywords = ["跌破", "Bitget"]
    worker = CompetitorMonitorWorker(repository=repo, settings=CompetitorMonitorSettings(blockbeats_api_key="key"))
    monkeypatch.setattr(worker, "_assign_events", lambda items: {"evt_1"})

    result = worker.run_once()

    assert result.task_inserted == 0
    assert result.reference_inserted == 1
    assert result.filtered == 1
    assert [item.source for item in repo.saved] == ["odaily"]


def test_event_aggregator_groups_same_batch_items_and_keeps_embeddings_local() -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed(self, texts):
            vectors = []
            for text in texts:
                if "比特币 ETF 获批" in text:
                    vectors.append([1.0, 0.0])
                else:
                    vectors.append([0.0, 1.0])
            return vectors

    class FakeCache:
        def __init__(self) -> None:
            self.embeddings = {}
            self.documents = []

        def get_embedding(self, *, cache_key, model, text_hash):  # noqa: ANN001
            return self.embeddings.get((cache_key, model, text_hash))

        def set_embedding(self, *, cache_key, model, text_hash, vector):  # noqa: ANN001
            self.embeddings[(cache_key, model, text_hash)] = vector

        def upsert_document(self, document):
            self.documents.append(document)

    repo = FakeRepository()
    aggregator = NewsflashEventAggregator(
        repository=repo,
        settings=CompetitorMonitorSettings(blockbeats_api_key="key", event_duplicate_threshold=0.88),
        embedding_client=FakeEmbeddingClient(),
        ai_client=None,
        cache=FakeCache(),
    )

    event_ids = aggregator.assign_items(
        [
            NewsflashItem("blockbeats", "1", "比特币 ETF 获批", "比特币 ETF 获批 正文"),
            NewsflashItem("panews", "2", "比特币 ETF 获批", "比特币 ETF 获批 另一写法"),
            NewsflashItem("odaily", "3", "比特币 ETF 获批", "比特币 ETF 获批 本方写法"),
        ]
    )

    assert len(event_ids) == 1
    assert {assignment.event_id for assignment in repo.assignments} == event_ids
    assert len(repo.assignments) == 3
    assert all(not hasattr(record, "embedding") for record in repo.newsflash_items)


def test_event_aggregator_keeps_existing_assignment_when_item_reappears() -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class FakeCache:
        def __init__(self) -> None:
            self.embeddings = {}

        def get_embedding(self, *, cache_key, model, text_hash):  # noqa: ANN001
            return self.embeddings.get((cache_key, model, text_hash))

        def set_embedding(self, *, cache_key, model, text_hash, vector):  # noqa: ANN001
            self.embeddings[(cache_key, model, text_hash)] = vector

        def upsert_document(self, document):
            return None

    repo = FakeRepository()
    aggregator = NewsflashEventAggregator(
        repository=repo,
        settings=CompetitorMonitorSettings(blockbeats_api_key="key", event_duplicate_threshold=0.88),
        embedding_client=FakeEmbeddingClient(),
        ai_client=None,
        cache=FakeCache(),
    )
    item = NewsflashItem("odaily", "same-1", "比特币 ETF 获批", "比特币 ETF 获批 正文")

    first_event_ids = aggregator.assign_items([item])
    second_event_ids = aggregator.assign_items([item])

    assert first_event_ids == second_event_ids
    assert len(repo.events) == 1
    assert {assignment.event_id for assignment in repo.assignments} == first_event_ids
    assert len(repo.assignments) == 1


def test_fetch_odaily_uses_rss_host_fallback(monkeypatch) -> None:
    calls: list[str] = []

    class Response:
        def __init__(self, url: str) -> None:
            self.url = url

        def raise_for_status(self) -> None:
            if "api.odaily.news" in self.url:
                raise RuntimeError("dns failed")

        def json(self):
            return {
                "data": {
                    "list": [
                        {
                            "id": 1,
                            "title": "标题",
                            "content": "Odaily星球日报讯 正文",
                            "publishDate": "2026-05-10 02:00:00",
                        }
                    ]
                }
            }

    def fake_get(url, **kwargs):  # noqa: ANN001
        calls.append(url)
        return Response(url)

    monkeypatch.setattr("packages.competitor_monitor.fetchers.requests.get", fake_get)

    items = fetch_odaily(timeout_seconds=3)

    assert calls == [
        "https://api.odaily.news/api/v1/newsflash",
        "https://rss.odaily.news/api/v1/newsflash",
    ]
    assert len(items) == 1
    assert items[0].content == "正文"
