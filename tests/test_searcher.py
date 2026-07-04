from __future__ import annotations

from pathlib import Path
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.x_processing.searcher import (
    CachedEmbeddingService,
    DashScopeEmbeddingClient,
    SearchCache,
    SearchDocument,
    cosine_similarity,
    is_dashscope_arrearage_error,
    normalize_for_embedding,
)


class FakeResponse:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200, text: str = "ok") -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class CountingEmbeddingClient:
    model = "fake-embedding"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


def test_dashscope_embedding_client_uses_compatible_endpoint(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return FakeResponse({"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

    monkeypatch.setattr("packages.x_processing.searcher.requests.post", fake_post)
    client = DashScopeEmbeddingClient(
        api_key="dash-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v4",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert client.embed(["hello"]) == [[0.1, 0.2]]
    assert calls[0]["url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer dash-key"
    assert calls[0]["json"] == {"model": "text-embedding-v4", "input": ["hello"]}


def test_dashscope_embedding_client_batches_requests(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_post(url, **kwargs):
        texts = kwargs["json"]["input"]
        calls.append(texts)
        return FakeResponse({"data": [{"index": index, "embedding": [float(index)]} for index in range(len(texts))]})

    monkeypatch.setattr("packages.x_processing.searcher.requests.post", fake_post)
    client = DashScopeEmbeddingClient(
        api_key="dash-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v4",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    vectors = client.embed([f"text {index}" for index in range(21)])

    assert len(vectors) == 21
    assert [len(batch) for batch in calls] == [10, 10, 1]


def test_dashscope_embedding_client_includes_error_body(monkeypatch) -> None:
    def fake_post(url, **kwargs):
        return FakeResponse({}, status_code=400, text='{"message":"too many inputs"}')

    monkeypatch.setattr("packages.x_processing.searcher.requests.post", fake_post)
    client = DashScopeEmbeddingClient(
        api_key="dash-key",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="text-embedding-v4",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    try:
        client.embed(["hello"])
    except RuntimeError as exc:
        assert "too many inputs" in str(exc)
    else:
        raise AssertionError("DashScope 400 should fail with response body")


def test_dashscope_arrearage_error_detection() -> None:
    assert is_dashscope_arrearage_error("400 Client Error from DashScope embeddings: Arrearage")
    assert is_dashscope_arrearage_error('{"error":{"message":"Access denied","code":"Arrearage"}} from DashScope')
    assert not is_dashscope_arrearage_error("requests timeout while calling embeddings")


def test_search_cache_reuses_embedding_by_content_hash(tmp_path: Path) -> None:
    client = CountingEmbeddingClient()
    service = CachedEmbeddingService(client=client, cache=SearchCache(tmp_path / "searcher.sqlite"))
    doc = SearchDocument(doc_type="task", doc_id="1", title="标题", content="正文", source="x", task_id=1)

    first = service.embed_documents([doc])[0][1]
    second = service.embed_documents([doc])[0][1]

    assert first == second
    assert client.calls == 1


def test_embedding_text_uses_title_and_content_only() -> None:
    assert normalize_for_embedding(title="标题", content="正文") == "标题：标题\n正文：正文"
    assert cosine_similarity([1, 0], [1, 0]) == 1.0


def test_search_cache_reads_local_odaily_candidates_and_alert_history(tmp_path: Path) -> None:
    cache = SearchCache(tmp_path / "searcher.sqlite")
    now = datetime(2099, 5, 22, 12, 0, tzinfo=UTC)
    cache.upsert_documents(
        [
            SearchDocument(
                doc_type="odaily_reference",
                doc_id="od-1",
                title="Odaily",
                content="Reference",
                source="odaily",
                published_at=now,
            ),
            SearchDocument(
                doc_type="candidate",
                doc_id="1",
                title="Candidate",
                content="In flight",
                source="candidate",
                task_id=101,
                candidate_id=1,
                status="active",
                created_at=now,
                expires_at=now + timedelta(hours=24),
            ),
            SearchDocument(
                doc_type="external_media_alert_history",
                doc_id="alert-1",
                title="Alert",
                content="Already notified",
                source="external_media_alert",
                task_id=201,
                status="notified",
                created_at=now,
            ),
        ]
    )

    odaily = cache.list_odaily_reference_documents(since=now - timedelta(hours=1))
    candidates = cache.list_active_candidate_documents()
    alerts = cache.list_notified_alert_documents(since=now - timedelta(hours=1))

    assert [doc.doc_id for doc in odaily] == ["od-1"]
    assert [doc.doc_id for doc in candidates] == ["1"]
    assert [doc.doc_id for doc in alerts] == ["alert-1"]


def test_search_cache_can_mark_candidate_inactive(tmp_path: Path) -> None:
    cache = SearchCache(tmp_path / "searcher.sqlite")
    now = datetime(2099, 5, 22, 12, 0, tzinfo=UTC)
    cache.upsert_document(
        SearchDocument(
            doc_type="candidate",
            doc_id="7",
            title="Candidate",
            content="Body",
            source="candidate",
            task_id=7,
            candidate_id=7,
            status="active",
            created_at=now,
            expires_at=now + timedelta(hours=24),
        )
    )

    cache.mark_document_status(
        cache_key="candidate:7",
        status="inactive",
        expires_at=now,
        metadata_updates={"released_by_task_status": "discarded"},
    )

    assert cache.list_active_candidate_documents() == []


def test_search_cache_ignores_candidate_without_expiry(tmp_path: Path) -> None:
    cache = SearchCache(tmp_path / "searcher.sqlite")
    now = datetime(2099, 5, 22, 12, 0, tzinfo=UTC)
    cache.upsert_document(
        SearchDocument(
            doc_type="candidate",
            doc_id="9",
            title="Candidate",
            content="Body",
            source="candidate",
            task_id=9,
            candidate_id=9,
            status="active",
            created_at=now,
        )
    )

    assert cache.list_active_candidate_documents() == []


def test_search_cache_ignores_candidate_older_than_ttl_even_if_expiry_is_future(tmp_path: Path) -> None:
    cache = SearchCache(tmp_path / "searcher.sqlite")
    now = datetime.now(UTC)
    cache.upsert_document(
        SearchDocument(
            doc_type="candidate",
            doc_id="10",
            title="Old Candidate",
            content="Body",
            source="candidate",
            task_id=10,
            candidate_id=10,
            status="active",
            created_at=now - timedelta(hours=25),
            expires_at=now + timedelta(hours=23),
        )
    )

    assert cache.list_active_candidate_documents() == []
