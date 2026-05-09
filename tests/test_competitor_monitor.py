from __future__ import annotations

from packages.competitor_monitor.fetchers import fetch_odaily, normalize_item_content, scrub_competitor_brands
from packages.competitor_monitor.worker import CompetitorMonitorWorker


class FakeRepository:
    def __init__(self) -> None:
        self.saved = []

    def init_schema(self) -> None:
        return None

    def save_items(self, items):
        self.saved.extend(items)
        task_count = sum(1 for item in items if item.source != "odaily")
        ref_count = sum(1 for item in items if item.source == "odaily")
        return task_count, ref_count


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

    result = worker.run_once()

    assert result.task_inserted == 1
    assert result.reference_inserted == 1
    assert [item.source for item in repo.saved] == ["blockbeats", "odaily"]


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
