from __future__ import annotations

import json
from datetime import UTC, datetime

from packages.common.config import XProcessingSettings
from packages.x_processing.models import PipelineRecord, TaskRecord
from packages.x_processing.odaily_reference_source import fetch_odaily_reference_documents_from_api
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.searcher import SearchDocument, top_match
from packages.x_processing.worker import XProcessingWorker


class StubEmbeddingService:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.cache = None

    def embed_one(self, *, cache_key: str, text: str) -> list[float]:
        return self.vectors[text]

    def embed_documents(self, documents: list[SearchDocument]) -> list[tuple[SearchDocument, list[float]]]:
        return [(document, self.vectors[document.embedding_text]) for document in documents]


class StubAIClient:
    def __init__(self, raw_output: str) -> None:
        self.raw_output = raw_output
        self.prompts: list[str] = []

    def generate_text(self, *, model: str, prompt: str, text_format, reasoning_effort: str | None = None) -> str:
        self.prompts.append(prompt)
        return self.raw_output


def build_settings(*, search_ai_review_threshold: float = 0.65) -> XProcessingSettings:
    return XProcessingSettings.model_validate(
        {
            "openai_api_key": "test-key",
            "dashscope_api_key": "test-key",
            "push_endpoint": "http://127.0.0.1:9/push",
            "search_window_hours": 72,
            "search_duplicate_threshold": 0.88,
            "search_ai_review_threshold": search_ai_review_threshold,
        }
    )


def build_task(*, task_id: int = 1) -> TaskRecord:
    now = datetime(2026, 7, 13, 12, 47, tzinfo=UTC)
    return TaskRecord(
        id=task_id,
        source="x",
        source_item_id="2076649521183097131",
        source_url="https://x.com/BTCtreasuries/status/2076649521183097131",
        title="@BTCtreasuries: JUST IN: Strategy $MSTR now has over 20.4 months of preferred stock dividend cov",
        content=(
            "JUST IN: Strategy $MSTR now has over 20.4 months of preferred stock dividend coverage, "
            "surpassing Strive's, after increasing its USD reserve to $3 billion."
        ),
        published_at=now,
        status="judged",
        created_at=now,
        updated_at=now,
    )


def build_reference(*, doc_id: str = "500139") -> SearchDocument:
    return SearchDocument(
        doc_type="odaily_reference",
        doc_id=doc_id,
        title="Strategy宣布旗下美元储备规模已增至30亿美元",
        content="Strategy 宣布旗下美元储备已增加了 4.5 亿美元，截至 7 月 12 日美元储备规模已增至 30 亿美元，同时比特币持仓量为 843,775 枚。",
        source="odaily",
        source_url="https://x.com/Strategy/status/2076638779528388781",
        published_at=datetime(2026, 7, 13, 12, 5, 22, tzinfo=UTC),
    )


def test_top_match_returns_highest_cosine_similarity() -> None:
    query_vector = [1.0, 0.0, 0.0]
    weak = build_reference(doc_id="weak")
    strong = build_reference(doc_id="strong")

    match = top_match(
        query_vector,
        [
            (weak, [0.2, 0.9, 0.0]),
            (strong, [0.95, 0.1, 0.0]),
        ],
    )

    assert match is not None
    assert match.document.doc_id == "strong"
    assert match.similarity > 0.99


def test_odaily_api_reference_source_filters_window_and_converts_documents(monkeypatch) -> None:
    payload = {
        "data": {
            "list": [
                {
                    "id": 101,
                    "title": "测试快讯",
                    "content": "Odaily星球日报讯 测试正文",
                    "publishTimestamp": 1_784_073_600_000,
                },
                {
                    "id": 100,
                    "title": "旧快讯",
                    "content": "旧正文",
                    "publishTimestamp": 1_700_000_000_000,
                },
            ],
            "hasMore": False,
        }
    }

    def fake_fetch_page(*, page: int, size: int, timeout_seconds: float):
        assert page == 1
        assert size == 100
        assert timeout_seconds == 12.0
        return payload

    monkeypatch.setattr("packages.x_processing.odaily_reference_source.fetch_odaily_page", fake_fetch_page)

    documents = fetch_odaily_reference_documents_from_api(
        since=datetime(2026, 7, 14, tzinfo=UTC),
        timeout_seconds=12.0,
    )

    assert [document.doc_id for document in documents] == ["101"]
    assert documents[0].source == "odaily"
    assert documents[0].content == "测试正文"
    assert documents[0].metadata["reference_source"] == "odaily_api"


def test_run_search_falls_back_to_supabase_references_when_odaily_api_fails(monkeypatch) -> None:
    repository = InMemoryXProcessingRepository()
    task = build_task()
    repository.add_task(task)
    repository.pipelines[task.id] = PipelineRecord(task_id=task.id)
    reference = build_reference()
    repository.odaily_references.append(reference)

    def fail_fetch(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr("packages.x_processing.worker.fetch_odaily_reference_documents_from_api", fail_fetch)
    vectors = {
        SearchDocument(
            doc_type="task",
            doc_id=str(task.id),
            title=task.title,
            content=task.content,
            source=task.source,
            source_url=task.source_url,
            task_id=task.id,
            published_at=task.published_at,
            metadata=task.metadata,
        ).embedding_text: [1.0, 0.0],
        reference.embedding_text: [0.95, (1 - 0.95**2) ** 0.5],
    }
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=build_settings(),
        search_embedding_service=StubEmbeddingService(vectors),
    )

    worker._run_search(task)

    assert repository.tasks[task.id].status == "duplicate"
    assert repository.pipelines[task.id].search_result["duplicate_target_id"] == reference.doc_id


def test_run_search_marks_duplicate_after_ai_review_for_lower_similarity_match(monkeypatch) -> None:
    repository = InMemoryXProcessingRepository()
    task = build_task()
    repository.add_task(task)
    repository.pipelines[task.id] = PipelineRecord(task_id=task.id)
    reference = build_reference()
    repository.odaily_references.append(reference)

    def fail_fetch(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr("packages.x_processing.worker.fetch_odaily_reference_documents_from_api", fail_fetch)
    vectors = {
        SearchDocument(
            doc_type="task",
            doc_id=str(task.id),
            title=task.title,
            content=task.content,
            source=task.source,
            source_url=task.source_url,
            task_id=task.id,
            published_at=task.published_at,
            metadata=task.metadata,
        ).embedding_text: [1.0, 0.0],
        reference.embedding_text: [0.6541330513767348, (1 - 0.6541330513767348**2) ** 0.5],
    }
    ai_client = StubAIClient(
        json.dumps(
            {
                "is_duplicate": True,
                "duplicate_target_type": "odaily_published",
                "duplicate_target_id": reference.doc_id,
                "reason": "same_event",
            }
        )
    )
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=build_settings(),
        search_embedding_service=StubEmbeddingService(vectors),
        search_ai_client=ai_client,
    )

    worker._run_search(task)

    assert repository.tasks[task.id].status == "duplicate"
    assert repository.pipelines[task.id].search_result["duplicate_target_id"] == reference.doc_id
    assert ai_client.prompts


def test_run_search_keeps_match_context_when_ai_review_rejects_duplicate(monkeypatch) -> None:
    repository = InMemoryXProcessingRepository()
    task = build_task()
    repository.add_task(task)
    repository.pipelines[task.id] = PipelineRecord(task_id=task.id)
    reference = build_reference()
    repository.odaily_references.append(reference)

    def fail_fetch(*args, **kwargs):
        raise RuntimeError("api unavailable")

    monkeypatch.setattr("packages.x_processing.worker.fetch_odaily_reference_documents_from_api", fail_fetch)
    query_doc = SearchDocument(
        doc_type="task",
        doc_id=str(task.id),
        title=task.title,
        content=task.content,
        source=task.source,
        source_url=task.source_url,
        task_id=task.id,
        published_at=task.published_at,
        metadata=task.metadata,
    )
    vectors = {
        query_doc.embedding_text: [1.0, 0.0],
        reference.embedding_text: [0.7, (1 - 0.7**2) ** 0.5],
    }
    raw_output = json.dumps(
        {
            "is_duplicate": False,
            "duplicate_target_type": "none",
            "duplicate_target_id": "",
            "reason": "same_topic_different_event",
        }
    )
    worker = XProcessingWorker(
        stage="search",
        repository=repository,
        settings=build_settings(),
        search_embedding_service=StubEmbeddingService(vectors),
        search_ai_client=StubAIClient(raw_output),
    )

    worker._run_search(task)

    result = repository.pipelines[task.id].search_result
    assert repository.tasks[task.id].status == "deduped"
    assert result["reason"] == "same_topic_different_event"
    assert result["reviewed_by_ai"] is True
    assert result["raw_ai_output"] == raw_output
    assert result["observed_matches"][0]["target_id"] == reference.doc_id
