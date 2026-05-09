from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.x_processing.searcher import (
    CachedEmbeddingService,
    DashScopeEmbeddingClient,
    SearchCache,
    SearchDocument,
    cosine_similarity,
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
