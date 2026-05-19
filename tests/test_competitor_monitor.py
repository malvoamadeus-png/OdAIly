from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.common.time_utils import SHANGHAI_TZ
from packages.competitor_monitor.fetchers import fetch_jinse, fetch_odaily, normalize_item_content, scrub_competitor_brands
from packages.competitor_monitor.events import EventAssignment, EventSourceRecord, NewsflashEventAggregator, NewsflashItemRecord
from packages.competitor_monitor.repository import parse_datetime
from packages.competitor_monitor.worker import CompetitorMonitorWorker, match_filter_terms


class FakeRepository:
    def __init__(self) -> None:
        self.saved = []
        self.newsflash_items = []
        self.assignments: list[EventAssignment] = []
        self.events: dict[str, NewsflashItemRecord] = {}
        self.keywords: list[str] = []
        self.updated_event_ids = set()
        self.heartbeats = []
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

    def create_event_with_source(self, item, *, needs_review=False):  # noqa: ANN001
        event_id = f"evt_{self._next_event_id}"
        self._next_event_id += 1
        self.events[event_id] = item
        self.assignments.append(
            EventAssignment(
                item_id=item.id,
                event_id=event_id,
                role="primary",
                match_method="new_event",
                needs_review=needs_review,
            )
        )
        return event_id

    def assign_item_to_event(self, assignment):
        previous = self._find_assignment(assignment.item_id)
        if assignment.match_method == "new_event":
            if previous is not None and previous.event_id == assignment.event_id:
                return
        if previous is not None and previous.event_id != assignment.event_id:
            remaining_old_sources = [
                item for item in self.assignments if item.item_id != assignment.item_id and item.event_id == previous.event_id
            ]
            if not remaining_old_sources:
                self.events.pop(previous.event_id, None)
        self.assignments.append(assignment)

    def update_event_summaries(self, event_ids):
        self.updated_event_ids = set(event_ids)

    def prune_excluded_event_sources(self, terms=None):
        from packages.competitor_monitor.worker import match_filter_terms

        active_terms = self.keywords if terms is None else terms
        matched_item_ids = {record.id for record in self.newsflash_items if match_filter_terms(_record_to_item(record), active_terms)}
        affected_event_ids = {assignment.event_id for assignment in self.assignments if assignment.item_id in matched_item_ids}
        before = len(self.assignments)
        self.assignments = [assignment for assignment in self.assignments if assignment.item_id not in matched_item_ids]
        remaining_event_ids = {assignment.event_id for assignment in self.assignments}
        deleted_event_ids = affected_event_ids - remaining_event_ids
        for event_id in deleted_event_ids:
            self.events.pop(event_id, None)
        self.updated_event_ids = affected_event_ids - deleted_event_ids
        return {
            "matched_items": len(matched_item_ids),
            "removed_sources": before - len(self.assignments),
            "deleted_events": len(deleted_event_ids),
            "updated_events": len(self.updated_event_ids),
        }

    def prune_orphan_events(self):
        assigned_event_ids = {assignment.event_id for assignment in self.assignments}
        orphan_event_ids = set(self.events) - assigned_event_ids
        for event_id in orphan_event_ids:
            self.events.pop(event_id, None)
        return len(orphan_event_ids)

    def repair_newsflash_timestamps(self):
        return {"updated_items": 0, "updated_events": 0}

    def record_worker_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)


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


def test_competitor_api_style_can_override_x_process_style(monkeypatch) -> None:
    from packages.common.config import load_competitor_monitor_settings

    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("X_PROCESS_OPENAI_API_STYLE", "responses")
    monkeypatch.setenv("COMPETITOR_OPENAI_API_STYLE", "chat_completions")

    settings = load_competitor_monitor_settings()

    assert settings.openai_api_style == "chat_completions"


def test_worker_saves_unexcluded_odaily_as_reference_and_competitors_as_tasks(monkeypatch) -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem
    from packages.common.config import CompetitorMonitorSettings

    monkeypatch.setattr(
        "packages.competitor_monitor.worker.fetch_blockbeats",
        lambda **kwargs: [NewsflashItem("blockbeats", "1", "竞品", "正文", published_at=datetime.now(UTC).isoformat())],
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
    assert result.fetched_by_source == {"blockbeats": 1, "panews": 0, "jinse": 0, "odaily": 1}
    assert result.filtered_by_source == {"blockbeats": 0, "panews": 0, "jinse": 0, "odaily": 0}
    assert repo.heartbeats[-1]["metadata"]["fetched_by_source"]["blockbeats"] == 1
    assert [item.source for item in repo.saved] == ["blockbeats", "odaily"]


def test_worker_keeps_stale_competitor_in_events_but_skips_tasks(monkeypatch) -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    fresh_time = datetime.now(UTC).isoformat()
    stale_time = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
    stale_item = NewsflashItem("blockbeats", "old", "旧竞品", "旧正文", published_at=stale_time)
    fresh_item = NewsflashItem("panews", "new", "新竞品", "新正文", published_at=fresh_time)
    odaily_item = NewsflashItem("odaily", "o1", "本方", "本方正文", published_at=stale_time)
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_blockbeats", lambda **kwargs: [stale_item])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_panews", lambda **kwargs: [fresh_item])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_jinse", lambda **kwargs: [])
    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_odaily", lambda **kwargs: [odaily_item])
    repo = FakeRepository()
    worker = CompetitorMonitorWorker(repository=repo, settings=CompetitorMonitorSettings(blockbeats_api_key="key"))
    assigned_batches = []

    def fake_assign_events(items):
        assigned_batches.append(items)
        return {"evt_1"}

    monkeypatch.setattr(worker, "_assign_events", fake_assign_events)

    result = worker.run_once()

    assert [item.source_item_id for item in assigned_batches[0]] == ["old", "new", "o1"]
    assert [item.source_item_id for item in repo.saved] == ["new", "o1"]
    assert result.task_inserted == 1
    assert result.reference_inserted == 1
    assert result.events_updated == 1
    assert result.expired_for_tasks == 1
    assert result.expired_for_tasks_by_source["blockbeats"] == 1
    assert repo.heartbeats[-1]["metadata"]["expired_for_tasks"] == 1


def test_filter_keywords_match_title_body_and_bitget_case_insensitive() -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem

    title_hit = NewsflashItem("blockbeats", "1", "BTC 跌破关键位置", "正文")
    body_hit = NewsflashItem("panews", "2", "标题", "某地址发生爆仓")
    case_hit = NewsflashItem("jinse", "3", "标题", "bitget 宣布上线新币")

    assert match_filter_terms(title_hit, ["跌破"]) == ["跌破"]
    assert match_filter_terms(body_hit, ["爆仓"]) == ["爆仓"]
    assert match_filter_terms(case_hit, ["Bitget"]) == ["Bitget"]


def test_worker_excludes_all_sources_from_tasks_references_and_events(monkeypatch) -> None:
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
    assigned_batches = []

    def fake_assign_events(items):
        assigned_batches.append(items)
        return {"evt_1"} if items else set()

    monkeypatch.setattr(worker, "_assign_events", fake_assign_events)

    result = worker.run_once()

    assert result.task_inserted == 0
    assert result.reference_inserted == 0
    assert result.events_updated == 0
    assert result.filtered == 2
    assert result.filtered_by_source["blockbeats"] == 1
    assert result.filtered_by_source["odaily"] == 1
    assert assigned_batches == [[]]
    assert repo.saved == []
    assert repo.heartbeats[-1]["metadata"]["filtered_by_source"]["odaily"] == 1


def test_fetch_jinse_uses_live_id_and_title_fallback(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "data": [
                    {
                        "title": "美国参议院银行委员会将对法案进行首次投票",
                        "published_at": 1778283366,
                        "jump_url": "https://www.jinse2.com/lives/512001.html",
                    }
                ]
            }

    monkeypatch.setattr("packages.competitor_monitor.fetchers.requests.get", lambda *args, **kwargs: Response())

    items = fetch_jinse(timeout_seconds=3)

    assert len(items) == 1
    assert items[0].source_item_id == "512001"
    assert items[0].content == "美国参议院银行委员会将对法案进行首次投票"
    assert items[0].published_at == "1778283366"


def test_fetch_jinse_reads_coinmeta_grouped_lives(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "news": 2,
                "count": 2,
                "top_id": 512327,
                "bottom_id": 512326,
                "list": [
                    {
                        "date": "2026-05-12",
                        "lives": [
                            {
                                "id": 512327,
                                "content": "【半导体股市值占标普 500 总市值比例达创纪录的 23%】金色财经报道，5月12日消息，半导体股市值占标普 500 总市值比例达创纪录的 23%。",
                                "link": "https://x.com/Cointelegraph/status/2054069277632741773",
                                "created_at": 1778563741,
                            },
                            {
                                "id": 512326,
                                "content": "【BIT关联地址卖出最后持有的99,612枚HYPE】金色财经报道，5月12日，据Onchain Lens监测，BIT关联地址卖出最后99,612枚HYPE。",
                                "created_at": 1778563583,
                            },
                        ],
                    }
                ],
            }

    monkeypatch.setattr("packages.competitor_monitor.fetchers.requests.get", lambda *args, **kwargs: Response())

    items = fetch_jinse(timeout_seconds=3)

    assert len(items) == 2
    assert items[0].source_item_id == "512327"
    assert items[0].title == "半导体股市值占标普 500 总市值比例达创纪录的 23%"
    assert "金色财经报道" not in items[0].content
    assert items[0].source_url == "https://www.jinse2.com/lives/512327.html"
    assert items[0].published_at == "1778563741"


def test_parse_datetime_treats_naive_newsflash_time_as_shanghai() -> None:
    parsed = parse_datetime("2026-05-11 10:36:58")

    assert parsed == datetime(2026, 5, 11, 10, 36, 58, tzinfo=SHANGHAI_TZ)
    assert parsed.astimezone(UTC) == datetime(2026, 5, 11, 2, 36, 58, tzinfo=UTC)


def test_parse_datetime_keeps_explicit_utc_and_unix_timestamp() -> None:
    assert parse_datetime("2026-05-11T02:38:00.000Z") == datetime(2026, 5, 11, 2, 38, tzinfo=UTC)
    assert parse_datetime(1778283366) == datetime(2026, 5, 8, 23, 36, 6, tzinfo=UTC)


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


def test_event_aggregator_new_event_is_created_with_primary_source_atomically() -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    class FakeCache:
        def get_embedding(self, *, cache_key, model, text_hash):  # noqa: ANN001
            return None

        def set_embedding(self, *, cache_key, model, text_hash, vector):  # noqa: ANN001
            return None

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

    event_ids = aggregator.assign_items([NewsflashItem("jinse", "j1", "某项目完成融资", "某项目完成融资 正文")])

    assert len(event_ids) == 1
    event_id = next(iter(event_ids))
    assert event_id in repo.events
    assert len(repo.assignments) == 1
    assert repo.assignments[0].event_id == event_id
    assert repo.assignments[0].role == "primary"
    assert repo.assignments[0].match_method == "new_event"


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


def test_event_aggregator_skips_embedding_when_all_items_already_assigned() -> None:
    from packages.common.config import CompetitorMonitorSettings
    from packages.competitor_monitor.fetchers import NewsflashItem

    class FailingEmbeddingClient:
        model = "fake-embedding"

        def embed(self, texts):
            raise AssertionError("existing assignments should not be embedded again")

    class FakeCache:
        def get_embedding(self, *, cache_key, model, text_hash):  # noqa: ANN001
            return None

        def set_embedding(self, *, cache_key, model, text_hash, vector):  # noqa: ANN001
            return None

        def upsert_document(self, document):
            return None

    repo = FakeRepository()
    item = NewsflashItem("blockbeats", "same-1", "标题", "正文")
    record = repo.upsert_newsflash_items([item])[0]
    repo.events = {"evt_existing": record}
    repo.assignments = [
        EventAssignment(item_id=record.id, event_id="evt_existing", role="primary", match_method="new_event")
    ]
    aggregator = NewsflashEventAggregator(
        repository=repo,
        settings=CompetitorMonitorSettings(blockbeats_api_key="key"),
        embedding_client=FailingEmbeddingClient(),
        ai_client=None,
        cache=FakeCache(),
    )

    event_ids = aggregator.assign_items([item])

    assert event_ids == {"evt_existing"}
    assert repo.updated_event_ids == {"evt_existing"}
    assert len(repo.assignments) == 1


def test_event_aggregator_can_match_new_item_to_reappearing_assigned_item() -> None:
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
    existing_item = NewsflashItem("blockbeats", "existing", "比特币 ETF 获批", "比特币 ETF 获批 正文")
    existing_record = repo.upsert_newsflash_items([existing_item])[0]
    repo.events = {"evt_existing": existing_record}
    repo.assignments = [
        EventAssignment(item_id=existing_record.id, event_id="evt_existing", role="primary", match_method="new_event")
    ]
    new_item = NewsflashItem("panews", "new", "比特币 ETF 获批", "比特币 ETF 获批 另一写法")
    aggregator = NewsflashEventAggregator(
        repository=repo,
        settings=CompetitorMonitorSettings(blockbeats_api_key="key", event_duplicate_threshold=0.88),
        embedding_client=FakeEmbeddingClient(),
        ai_client=None,
        cache=FakeCache(),
    )

    event_ids = aggregator.assign_items([existing_item, new_item])

    assert event_ids == {"evt_existing"}
    new_record = next(record for record in repo.newsflash_items if record.source_item_id == "new")
    new_assignment = repo._find_assignment(new_record.id)
    assert new_assignment is not None
    assert new_assignment.event_id == "evt_existing"
    assert new_assignment.matched_item_id == existing_record.id


def test_prune_excluded_event_sources_removes_matches_and_keeps_remaining_event() -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem

    repo = FakeRepository()
    blockbeats, odaily, jinse = repo.upsert_newsflash_items(
        [
            NewsflashItem("blockbeats", "b1", "BTC 突破 10 万美元", "正文"),
            NewsflashItem("odaily", "o1", "ETH 现货 ETF 获批", "正文"),
            NewsflashItem("jinse", "j1", "Bitget 宣布上线新币", "正文"),
        ]
    )
    repo.events = {"evt_1": blockbeats, "evt_2": jinse}
    repo.assignments = [
        EventAssignment(item_id=blockbeats.id, event_id="evt_1", role="primary", match_method="new_event"),
        EventAssignment(item_id=odaily.id, event_id="evt_1", role="supporting", match_method="embedding_high"),
        EventAssignment(item_id=jinse.id, event_id="evt_2", role="primary", match_method="new_event"),
    ]
    repo.keywords = ["突破", "Bitget"]

    result = repo.prune_excluded_event_sources()

    assert result == {"matched_items": 2, "removed_sources": 2, "deleted_events": 1, "updated_events": 1}
    assert [assignment.item_id for assignment in repo.assignments] == [odaily.id]
    assert set(repo.events) == {"evt_1"}
    assert repo.updated_event_ids == {"evt_1"}


def test_prune_orphan_events_deletes_only_events_without_sources() -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem

    repo = FakeRepository()
    item = repo.upsert_newsflash_items([NewsflashItem("odaily", "o1", "标题", "正文")])[0]
    repo.events = {"evt_keep": item, "evt_orphan": item}
    repo.assignments = [EventAssignment(item_id=item.id, event_id="evt_keep", role="primary", match_method="new_event")]

    deleted = repo.prune_orphan_events()

    assert deleted == 1
    assert set(repo.events) == {"evt_keep"}


def test_reassigning_last_source_deletes_previous_empty_event() -> None:
    from packages.competitor_monitor.fetchers import NewsflashItem

    repo = FakeRepository()
    item = repo.upsert_newsflash_items([NewsflashItem("odaily", "o1", "标题", "正文")])[0]
    repo.events = {"evt_old": item, "evt_new": item}
    repo.assignments = [EventAssignment(item_id=item.id, event_id="evt_old", role="primary", match_method="new_event")]

    repo.assign_item_to_event(
        EventAssignment(
            item_id=item.id,
            event_id="evt_new",
            role="supporting",
            match_method="embedding_high",
            similarity=0.95,
        )
    )

    assert set(repo.events) == {"evt_new"}
    assert repo._find_assignment(item.id).event_id == "evt_new"


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


def _record_to_item(record: NewsflashItemRecord):
    from packages.competitor_monitor.fetchers import NewsflashItem

    return NewsflashItem(
        record.source,
        record.source_item_id,
        record.title or "",
        record.content,
        record.source_url,
        record.published_at.isoformat() if record.published_at else None,
        metadata=record.metadata,
    )
