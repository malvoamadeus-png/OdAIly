from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.common.config import XProcessingSettings
from packages.x_processing.models import TaskRecord
from packages.x_processing.searcher import SearchDocument, SearchMatch, build_ai_review_prompt
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import XProcessingWorker


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
                    "is_duplicate": True,
                    "duplicate_target_type": "recent_processed",
                    "duplicate_target_id": "29558",
                    "reason": "same_event",
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "is_duplicate": False,
                "duplicate_target_type": "none",
                "duplicate_target_id": "",
                "reason": "update_of_existing_event",
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


def test_search_keeps_direct_duplicate_for_very_high_similarity(
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
    assert repository.get_task(query.id).status == "duplicate"
    assert len(ai_client.prompts) == 0
