from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from packages.common.config import CompetitorMonitorSettings
from packages.common.storage import connect_sqlite
from packages.competitor_monitor.events import NewsflashEventAggregator
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.sqlite_repository import SQLiteCompetitorMonitorRepository
from packages.x_processing.searcher import SearchCache


class FakeEmbeddingClient:
    model = "test-embedding"

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [next(vector for title, vector in self.vectors.items() if f"标题：{title}" in text) for text in texts]


class FakeReviewClient:
    def __init__(self, decisions: dict[frozenset[str], bool], default: bool = False) -> None:
        self.decisions = decisions
        self.default = default
        self.prompts: list[str] = []

    def generate_text(self, *, model: str, prompt: str, text_format: dict | None = None, reasoning_effort: str | None = None) -> str:
        del model, text_format, reasoning_effort
        self.prompts.append(prompt)
        titles = [line.removeprefix("标题：") for line in prompt.splitlines() if line.startswith("标题：")]
        decision = self.decisions.get(frozenset(titles), self.default)
        return json.dumps({"is_same_event": decision}, ensure_ascii=False)


class CompetitorEventAggregationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "odaily.sqlite"
        self.repository = SQLiteCompetitorMonitorRepository(self.path)
        self.settings = CompetitorMonitorSettings(
            openai_api_key="test",
            event_duplicate_threshold=0.88,
            event_ai_review_threshold=0.65,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def aggregator(self, vectors: dict[str, list[float]], review: FakeReviewClient) -> NewsflashEventAggregator:
        return NewsflashEventAggregator(
            repository=self.repository,
            settings=self.settings,
            embedding_client=FakeEmbeddingClient(vectors),
            ai_client=review,
            cache=SearchCache(Path(self.temp_dir.name) / "searcher.sqlite"),
        )

    @staticmethod
    def item(source: str, source_item_id: str, title: str, published_at: str, source_url: str | None = None) -> NewsflashItem:
        return NewsflashItem(
            source=source,
            source_item_id=source_item_id,
            title=title,
            content=f"正文：{title} 的完整报道内容。",
            source_url=source_url,
            published_at=published_at,
        )

    def event_ids_for_items(self) -> dict[str, str]:
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(
                "SELECT source_item_id,event_id FROM newsflash_event_sources ORDER BY item_id"
            ).fetchall()
        return {str(row["source_item_id"]): str(row["event_id"]) for row in rows}

    def test_same_original_url_assigns_without_ai(self) -> None:
        first = self.item("blockbeats", "b1", "同一原始报道", "2026-08-27T01:00:00+00:00", "https://www.cnbc.com/story?id=1")
        second = self.item(
            "odaily",
            "o1",
            "同一消息的改写标题",
            "2026-08-27T01:05:00+00:00",
            "https://www.cnbc.com/story/?id=1&utm_source=feed",
        )
        review = FakeReviewClient({}, default=False)
        aggregator = self.aggregator({"同一原始报道": [1.0, 0.0], "同一消息的改写标题": [0.0, 1.0]}, review)

        aggregator.assign_items([first])
        aggregator.assign_items([second])

        event_ids = self.event_ids_for_items()
        self.assertEqual(event_ids["b1"], event_ids["o1"])
        self.assertEqual(review.prompts, [])

    def test_ai_review_receives_title_body_and_source_without_similarity_or_reason(self) -> None:
        first = self.item("blockbeats", "b1", "原始报道标题", "2026-08-27T01:00:00+00:00", "https://example.com/one")
        second = self.item("odaily", "o1", "可能不同报道", "2026-08-27T01:05:00+00:00", "https://example.com/two")
        review = FakeReviewClient({}, default=False)
        aggregator = self.aggregator({"原始报道标题": [1.0, 0.0], "可能不同报道": [0.8, 0.6]}, review)

        aggregator.assign_items([first])
        aggregator.assign_items([second])

        self.assertEqual(len(review.prompts), 1)
        prompt = review.prompts[0]
        self.assertIn("来源：blockbeats", prompt)
        self.assertIn("原始链接：https://example.com/one", prompt)
        self.assertIn("正文：原始报道标题 的完整报道内容。", prompt)
        self.assertNotIn("相似度：", prompt)
        self.assertNotIn('"reason"', prompt)
        self.assertNotEqual(self.event_ids_for_items()["b1"], self.event_ids_for_items()["o1"])

    def test_high_similarity_still_assigns_without_ai(self) -> None:
        first = self.item("blockbeats", "b1", "高相似原始报道", "2026-08-27T01:00:00+00:00", "https://example.com/one")
        second = self.item("odaily", "o1", "高相似改写", "2026-08-27T01:05:00+00:00", "https://example.com/two")
        review = FakeReviewClient({}, default=False)
        aggregator = self.aggregator({"高相似原始报道": [1.0, 0.0], "高相似改写": [1.0, 0.0]}, review)

        aggregator.assign_items([first])
        aggregator.assign_items([second])

        self.assertEqual(self.event_ids_for_items()["b1"], self.event_ids_for_items()["o1"])
        self.assertEqual(review.prompts, [])

    def test_new_items_use_first_item_as_anchor_instead_of_transitive_chain(self) -> None:
        first = self.item("jinse", "j1", "基准报道", "2026-08-27T01:00:00+00:00")
        second = self.item("blockbeats", "b1", "基准报道改写", "2026-08-27T01:05:00+00:00")
        third = self.item("odaily", "o1", "另一篇相似报道", "2026-08-27T01:10:00+00:00")
        review = FakeReviewClient(
            {frozenset({"基准报道", "基准报道改写"}): True, frozenset({"基准报道", "另一篇相似报道"}): False},
            default=True,
        )
        aggregator = self.aggregator(
            {"基准报道": [1.0, 0.0], "基准报道改写": [0.8, 0.6], "另一篇相似报道": [0.8, 0.6]},
            review,
        )

        aggregator.assign_items([first, second, third])

        event_ids = self.event_ids_for_items()
        self.assertEqual(event_ids["j1"], event_ids["b1"])
        self.assertNotEqual(event_ids["j1"], event_ids["o1"])


if __name__ == "__main__":
    unittest.main()
