from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.common.config import XProcessingSettings
from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.searcher import SearchDocument, SearchMatch, build_ai_review_prompt
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import XProcessingWorker, build_structured_writer_prompt


def _vector(similarity: float) -> list[float]:
    return [similarity, math.sqrt(1.0 - similarity**2)]


class FakeEmbeddingService:
    cache = None

    def __init__(self, vectors: dict[tuple[str, str], list[float]]) -> None:
        self.vectors = vectors

    def embed_one(self, *, cache_key: str, text: str) -> list[float]:
        return [1.0, 0.0]

    def embed_documents(
        self,
        documents: list[SearchDocument],
    ) -> list[tuple[SearchDocument, list[float]]]:
        return [(document, self.vectors[(document.doc_type, document.doc_id)]) for document in documents]


class StrategyDuplicateAI:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate_text(self, **kwargs: object) -> str:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        if "斥资3.7亿美元，Strategy上周增持4603枚比特币" in prompt:
            return json.dumps(
                {
                    "decision": "duplicate",
                    "target_type": "recent_processed",
                    "target_id": "29558",
                    "new_facts": [],
                    "reason": "same_event_no_new_facts",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "decision": "new_event",
                "target_type": "none",
                "target_id": "",
                "new_facts": [],
                "reason": "different_event",
            },
            ensure_ascii=False,
        )


def _seed_task(database_path: Path, task: TaskRecord) -> None:
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks(
                id, source, source_item_id, source_url, title, content,
                published_at, raw_payload, metadata, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '{}', ?)
            """,
            (
                task.id,
                task.source,
                task.source_item_id,
                task.source_url,
                task.title,
                task.content,
                task.published_at.isoformat() if task.published_at else None,
                task.status,
            ),
        )
        conn.commit()


def _seed_candidate(database_path: Path, *, candidate_id: int, title: str, content: str) -> None:
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            """
            INSERT INTO search_event_candidates(
                id, status, title, content, content_hash, metadata, expires_at
            ) VALUES (?, 'active', ?, ?, ?, '{}', ?)
            """,
            (
                candidate_id,
                title,
                content,
                f"candidate-{candidate_id}",
                (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            ),
        )
        conn.commit()


def test_search_blocks_strategy_purchase_when_duplicate_is_not_top_one(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "odaily.sqlite"
    search_cache_path = tmp_path / "searcher.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    query = TaskRecord(
        id=598005,
        source="x",
        source_item_id="2094396214578139215",
        source_url="https://x.com/BTCtreasuries/status/2094396214578139215",
        title="持仓达84.505万枚比特币，Strategy 10周来首次增持4603枚",
        content="Strategy时隔10周首次增持4603枚比特币，持仓达到84.505万枚。",
        published_at=now,
        status="judged",
    )
    _seed_task(database_path, query)
    _seed_candidate(
        database_path,
        candidate_id=29558,
        title="斥资3.7亿美元，Strategy上周增持4603枚比特币",
        content="Strategy上周购买4603枚比特币，持仓达到84.505万枚。",
    )

    monkeypatch.setattr(
        "packages.x_processing.worker._search_cache_path_for_repository",
        lambda _repository: search_cache_path,
    )
    vectors = {
        ("odaily_reference", "514025"): _vector(0.745165),
        ("recent_processed", "29562"): _vector(0.671646),
        ("recent_processed", "29558"): _vector(0.639691),
    }
    ai_client = StrategyDuplicateAI()
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=XProcessingSettings(search_batch_ai_review_threshold=0.60),
        search_embedding_service=FakeEmbeddingService(vectors),
        search_ai_client=ai_client,
    )
    cache = worker._search_cache()
    assert cache is not None
    cache.upsert_document(
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="514025",
            title="Strategy比特币持仓盈利超28亿美元，持有84.04万枚比特币",
            content="Strategy持有84.04万枚比特币，当前浮盈超过28亿美元。",
            source="odaily",
            published_at=now - timedelta(hours=1),
        )
    )
    cache.upsert_document(
        SearchDocument(
            doc_type="recent_processed",
            doc_id="29562",
            title="Strive拟通过发行优先股筹集资金增持比特币",
            content="Strive计划募资并增持比特币。",
            source="candidate",
            task_id=597990,
            candidate_id=29562,
            status="active",
            created_at=now - timedelta(minutes=8),
            updated_at=now - timedelta(minutes=8),
            expires_at=now + timedelta(minutes=22),
        )
    )
    cache.upsert_document(
        SearchDocument(
            doc_type="recent_processed",
            doc_id="29558",
            title="斥资3.7亿美元，Strategy上周增持4603枚比特币",
            content="Strategy上周购买4603枚比特币，耗资约3.697亿美元，持仓达到84.505万枚。",
            source="candidate",
            task_id=597952,
            candidate_id=29558,
            status="active",
            created_at=now - timedelta(minutes=4),
            updated_at=now - timedelta(minutes=4),
            expires_at=now + timedelta(minutes=26),
        )
    )

    result = worker.run_once()
    assert result.failed == 0
    assert repository.get_task(query.id).status == "duplicate"
    assert repository.get_pipeline(query.id).candidate_id == 29558
    search_result = repository.get_pipeline(query.id).search_result
    assert search_result["duplicate_target"] == {
        "target_type": "recent_processed",
        "target_id": "29558",
        "candidate_id": 29558,
        "title": "斥资3.7亿美元，Strategy上周增持4603枚比特币",
        "source_url": None,
        "content": "Strategy上周购买4603枚比特币，耗资约3.697亿美元，持仓达到84.505万枚。",
        "similarity": 0.639691,
    }
    assert len(ai_client.prompts) == 1
    assert "514025" in ai_client.prompts[0]
    assert "29558" in ai_client.prompts[0]


def test_search_allows_strategy_purchase_when_only_old_holding_report_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "odaily.sqlite"
    search_cache_path = tmp_path / "searcher.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    query = TaskRecord(
        id=598005,
        source="x",
        source_item_id="2094396214578139215",
        source_url="https://x.com/BTCtreasuries/status/2094396214578139215",
        title="持仓达84.505万枚比特币，Strategy 10周来首次增持4603枚",
        content="Strategy时隔10周首次增持4603枚比特币，持仓达到84.505万枚。",
        published_at=now,
        status="judged",
    )
    _seed_task(database_path, query)
    monkeypatch.setattr(
        "packages.x_processing.worker._search_cache_path_for_repository",
        lambda _repository: search_cache_path,
    )
    ai_client = StrategyDuplicateAI()
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=XProcessingSettings(search_batch_ai_review_threshold=0.60),
        search_embedding_service=FakeEmbeddingService(
            {("odaily_reference", "514025"): _vector(0.745165)}
        ),
        search_ai_client=ai_client,
    )
    cache = worker._search_cache()
    assert cache is not None
    cache.upsert_document(
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="514025",
            title="Strategy比特币持仓盈利超28亿美元，持有84.04万枚比特币",
            content="Strategy持有84.04万枚比特币，当前浮盈超过28亿美元。",
            source="odaily",
            published_at=now - timedelta(hours=1),
        )
    )

    result = worker.run_once()

    assert result.failed == 0
    assert repository.get_task(query.id).status == "deduped"
    assert len(ai_client.prompts) == 1
    assert "514025" in ai_client.prompts[0]
    assert "29558" not in ai_client.prompts[0]


def test_search_reviews_medium_similarity_when_new_fact_is_fund_wallets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "odaily.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    query = TaskRecord(
        id=731763,
        source="x",
        source_item_id="2103290298638159941",
        source_url="https://x.com/EmberCN/status/2103290298638159941",
        title="Bitget 风险保护基金由 5500 枚 BTC 构成，分布在三个钱包",
        content="Bitget 价值 4.64 亿美元的风险保护基金由 5500 枚 BTC 构成，位于三个钱包。",
        published_at=now,
        status="judged",
    )
    _seed_task(database_path, query)
    monkeypatch.setattr(
        "packages.x_processing.worker._search_cache_path_for_repository",
        lambda _repository: tmp_path / "searcher.sqlite",
    )
    ai_client = StrategyDuplicateAI()
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=XProcessingSettings(search_batch_ai_review_threshold=0.60),
        search_embedding_service=FakeEmbeddingService(
            {("odaily_reference", "520198"): _vector(0.80)}
        ),
        search_ai_client=ai_client,
    )
    cache = worker._search_cache()
    assert cache is not None
    reference = SearchDocument(
        doc_type="odaily_reference",
        doc_id="520198",
        title="Bitget：部分热钱包发生异常转账，初步涉及约3.516亿美元",
        content="Bitget 被盗约 3.516 亿美元，相关损失在超过 4.64 亿美元的用户保护基金覆盖范围内。",
        source="odaily",
        published_at=now - timedelta(hours=2),
    )
    cache.upsert_document(reference)

    result = worker.run_once()

    assert result.message == f"processed task {query.id}", result
    assert result.failed == 0
    assert repository.get_task(query.id).status == "deduped"
    search_result = repository.get_pipeline(query.id).search_result
    assert search_result["is_duplicate"] is False
    assert search_result["reviewed_by_ai"] is True
    assert len(ai_client.prompts) == 1
    assert "钱包地址" in ai_client.prompts[0]
    assert "520198" in ai_client.prompts[0]

    single_prompt = build_ai_review_prompt(
        query=SearchDocument(
            doc_type="editor_plugin_query",
            doc_id="query",
            title=query.title,
            content=query.content,
            source="x",
        ),
        match=SearchMatch(document=reference, similarity=0.80),
    )
    assert "钱包地址" in single_prompt


def test_search_ai_reviews_very_high_similarity_instead_of_directly_blocking(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "odaily.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    query = TaskRecord(
        id=731764,
        source="x",
        source_item_id="2103290298638159942",
        source_url="https://x.com/EmberCN/status/2103290298638159942",
        title="Bitget 风险保护基金由 5500 枚 BTC 构成",
        content="Bitget 风险保护基金由 5500 枚 BTC 构成。",
        published_at=now,
        status="judged",
    )
    _seed_task(database_path, query)
    monkeypatch.setattr(
        "packages.x_processing.worker._search_cache_path_for_repository",
        lambda _repository: tmp_path / "searcher.sqlite",
    )
    ai_client = StrategyDuplicateAI()
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=XProcessingSettings(search_batch_ai_review_threshold=0.60),
        search_embedding_service=FakeEmbeddingService(
            {("odaily_reference", "520198"): _vector(0.90)}
        ),
        search_ai_client=ai_client,
    )
    cache = worker._search_cache()
    assert cache is not None
    cache.upsert_document(
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="520198",
            title="Bitget：部分热钱包发生异常转账",
            content="Bitget 热钱包发生异常转账。",
            source="odaily",
            published_at=now - timedelta(hours=2),
        )
    )

    result = worker.run_once()

    assert result.failed == 0
    assert repository.get_task(query.id).status == "deduped"
    assert len(ai_client.prompts) == 1
    search_result = repository.get_pipeline(query.id).search_result
    assert search_result["decision"] == "new_event"


def test_search_allows_same_event_update_with_explicit_new_facts_at_high_similarity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime.now(UTC)
    database_path = tmp_path / "odaily.sqlite"
    repository = SQLiteXProcessingRepository(database_path)
    query = TaskRecord(
        id=731765,
        source="x",
        source_item_id="bb-731765",
        source_url="https://www.theblockbeats.info/flash/731765",
        title="Justin Sun要求WLFI支付逾26万美元制裁费用，并确定听证日期",
        content="Justin Sun要求WLFI支付逾26万美元制裁费用，法院已确定于11月18日举行听证。",
        published_at=now,
        status="judged",
    )
    _seed_task(database_path, query)
    monkeypatch.setattr(
        "packages.x_processing.worker._search_cache_path_for_repository",
        lambda _repository: tmp_path / "searcher.sqlite",
    )

    class UpdateAI:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def generate_text(self, **kwargs: object) -> str:
            self.prompts.append(str(kwargs["prompt"]))
            return json.dumps(
                {
                    "decision": "same_event_update",
                    "target_type": "odaily_published",
                    "target_id": "520199",
                    "new_facts": ["制裁费用逾26万美元", "听证日期确定为11月18日"],
                    "reason": "same_event_new_facts",
                },
                ensure_ascii=False,
            )

    ai_client = UpdateAI()
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=XProcessingSettings(search_batch_ai_review_threshold=0.60),
        search_embedding_service=FakeEmbeddingService(
            {("odaily_reference", "520199"): _vector(0.93)}
        ),
        search_ai_client=ai_client,
    )
    cache = worker._search_cache()
    assert cache is not None
    cache.upsert_document(
        SearchDocument(
            doc_type="odaily_reference",
            doc_id="520199",
            title="Justin Sun要求WLFI支付制裁费用",
            content="Justin Sun要求WLFI承担制裁费用，法院正在安排相关诉讼程序。",
            source="odaily",
            published_at=now - timedelta(hours=1),
        )
    )

    result = worker.run_once()

    assert result.failed == 0
    assert repository.get_task(query.id).status == "deduped"
    search_result = repository.get_pipeline(query.id).search_result
    assert search_result["decision"] == "same_event_update"
    assert search_result["new_facts"] == ["制裁费用逾26万美元", "听证日期确定为11月18日"]
    assert search_result["update_target"]["target_id"] == "520199"
    assert search_result["update_target"]["content"] == "Justin Sun要求WLFI承担制裁费用，法院正在安排相关诉讼程序。"
    assert "same_event_update" in ai_client.prompts[0] or "制裁费用" in ai_client.prompts[0]


def test_search_duplicate_classification_requires_no_new_facts() -> None:
    from packages.x_processing.searcher import SearchDocument, SearchMatch

    query = SearchDocument(
        doc_type="task",
        doc_id="query",
        title="Justin Sun要求WLFI支付制裁费用",
        content="Justin Sun要求WLFI支付制裁费用。",
        source="blockbeats",
    )
    match = SearchMatch(
        document=SearchDocument(
            doc_type="odaily_reference",
            doc_id="520199",
            title="Justin Sun要求WLFI支付制裁费用",
            content="Justin Sun要求WLFI支付制裁费用。",
            source="odaily",
        ),
        similarity=0.95,
    )

    class DuplicateAI:
        def generate_text(self, **kwargs: object) -> str:
            return json.dumps(
                {
                    "decision": "duplicate",
                    "target_type": "odaily_published",
                    "target_id": "520199",
                    "new_facts": [],
                    "reason": "same_event_no_new_facts",
                }
            )

    worker = object.__new__(XProcessingWorker)
    worker.settings = XProcessingSettings(search_batch_ai_review_threshold=0.60)
    worker.search_ai_client = DuplicateAI()
    worker._record_attempt_context = lambda **_kwargs: None
    worker._record_attempt_response = lambda _output: None

    decision = worker._decide_matches(query=query, matches=[(match, "odaily_published")])

    assert decision is not None
    assert decision.decision == "duplicate"
    assert decision.is_duplicate is True
    assert decision.new_facts == []


def test_writer_prompt_receives_same_event_update_facts_and_prior_copy() -> None:
    task = TaskRecord(
        id=731766,
        source="blockbeats",
        source_item_id="bb-731766",
        source_url="https://www.theblockbeats.info/flash/731766",
        title="Justin Sun要求WLFI支付制裁费用",
        content="法院确定于11月18日举行听证。",
        status="deduped",
    )
    prompt = build_structured_writer_prompt(
        task=task,
        prompt=PromptTemplateVersion(
            id=1,
            template_key="mainstream_media_writer",
            version_number=1,
            content="撰写快讯。",
        ),
        known_subjects=[],
        search_result={
            "decision": "same_event_update",
            "new_facts": ["听证日期确定为11月18日"],
            "update_target": {
                "title": "Justin Sun要求WLFI承担制裁费用",
                "content": "Justin Sun要求WLFI承担相关制裁费用。",
            },
        },
    )

    assert "听证日期确定为11月18日" in prompt
    assert "Justin Sun要求WLFI承担相关制裁费用。" in prompt
    assert "本次稿件应以当前材料为准" in prompt
