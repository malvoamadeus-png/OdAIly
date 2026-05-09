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
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

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
