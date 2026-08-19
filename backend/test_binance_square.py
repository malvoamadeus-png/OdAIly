from __future__ import annotations

from pathlib import Path

import pytest

from packages.binance_square.client import normalize_profile_url, parse_profile_response
from packages.binance_square.repository import BinanceSquareRepository
from packages.binance_square.worker import BinanceSquareWorker
from packages.common.storage import connect_sqlite
from packages.console_data_api import ConsoleDataApi
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import forced_manual_review_reason_code, publisher_manual_review_reason, resolve_publisher_channel
from packages.x_processing.models import TaskRecord


def _payload(*rows: dict) -> dict:
    return {"data": {"contentList": list(rows)}}


def _post(post_id: str, *, content_type: int = 1, timestamp: int = 1786889184000) -> dict:
    return {
        "id": post_id,
        "contentType": content_type,
        "username": "CZ",
        "displayName": "CZ",
        "bodyTextOnly": f"post {post_id}",
        "firstReleaseTime": timestamp,
        "webLink": f"https://www.binance.com/en/square/post/{post_id}",
        "imageList": [{"url": f"https://img.test/{post_id}.jpg"}],
    }


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("https://www.binance.com/zh-CN/square/profile/cz", "cz"),
        ("www.binance.com/en/square/profile/PhoenixToken0?ref=1", "PhoenixToken0"),
    ],
)
def test_normalize_profile_url(value: str, slug: str) -> None:
    assert normalize_profile_url(value) == (slug, f"https://www.binance.com/en/square/profile/{slug}")


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/en/square/profile/cz",
        "https://www.binance.com/en/square/post/123",
        "https://www.binance.com/en/square/profile/cz/extra",
    ],
)
def test_normalize_profile_url_rejects_non_profile_links(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_profile_url(value)


def test_parse_profile_response_keeps_only_normal_posts() -> None:
    posts = parse_profile_response(_payload(_post("2", content_type=4), _post("1")))
    assert [post.post_id for post in posts] == ["1"]
    assert posts[0].display_name == "CZ"
    assert posts[0].media_urls == ["https://img.test/1.jpg"]
    assert posts[0].published_at == "2026-08-16T14:06:24+00:00"


class FakeClient:
    def __init__(self, posts):
        self.posts = posts
        self.calls = 0

    def fetch_profile(self, profile_url: str):
        self.calls += 1
        return self.posts

    def close(self) -> None:
        return None


class FakePipeline:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[dict] = []

    def submit_job(self, **payload) -> None:
        if self.fail:
            raise RuntimeError("pipeline unavailable")
        self.jobs.append(payload)


def _repository(path: Path) -> BinanceSquareRepository:
    SQLiteXProcessingRepository(path)
    repository = BinanceSquareRepository(path)
    repository.init_schema()
    with connect_sqlite(path) as conn:
        conn.execute(
            "INSERT INTO binance_square_accounts(slug,slug_lower,profile_url) VALUES('cz','cz','https://www.binance.com/en/square/profile/cz')"
        )
        conn.commit()
    return repository


def _enable(repository: BinanceSquareRepository) -> None:
    with connect_sqlite(repository.path) as conn:
        conn.execute("UPDATE binance_square_settings SET enabled=1 WHERE singleton_key='global'")
        conn.commit()


def test_disabled_worker_does_not_launch_client(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "db.sqlite")
    client = FakeClient([])
    assert BinanceSquareWorker(repository=repository, client=client).run_once() == []
    assert client.calls == 0


def test_console_validates_profile_and_keeps_fixed_settings(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "db.sqlite")
    api = ConsoleDataApi(repository.path)
    with pytest.raises(ValueError, match="只支持"):
        api.execute({
            "table": "binance_square_accounts", "operation": "upsert", "on_conflict": "slug_lower",
            "data": {"profile_url": "https://example.com/square/profile/bad", "slug": "bad", "slug_lower": "bad"},
        })
    saved = api.execute({
        "table": "binance_square_accounts", "operation": "upsert", "on_conflict": "slug_lower",
        "data": {
            "profile_url": "https://www.binance.com/zh-CN/square/profile/NewUser?ref=1",
            "slug": "ignored", "slug_lower": "ignored", "enabled": True,
        },
    })
    assert saved[0]["slug"] == "NewUser"
    assert saved[0]["profile_url"] == "https://www.binance.com/en/square/profile/NewUser"
    with pytest.raises(ValueError, match="enabled switch"):
        api.execute({
            "table": "binance_square_settings", "operation": "update",
            "filters": [{"column": "singleton_key", "op": "eq", "value": "global"}],
            "data": {"interval_seconds": 60},
        })


def test_first_run_seeds_then_new_post_enters_pipeline(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "db.sqlite")
    _enable(repository)
    first_posts = parse_profile_response(_payload(_post("1"), _post("2")))
    client = FakeClient(first_posts)
    pipeline = FakePipeline()
    worker = BinanceSquareWorker(repository=repository, client=client, pipeline_client=pipeline)

    first = worker.run_once()[0]
    assert first.seeded_count == 2
    assert pipeline.jobs == []

    client.posts = parse_profile_response(_payload(_post("1"), _post("2"), _post("3", timestamp=1786889244000)))
    second = worker.run_once()[0]
    assert second.saved_count == 1
    assert pipeline.jobs[0]["source"] == "binance_square"
    with connect_sqlite(repository.path) as conn:
        task = conn.execute("SELECT id,source,source_item_id,status FROM tasks WHERE source='binance_square'").fetchone()
    assert {key: task[key] for key in ("source", "source_item_id", "status")} == {
        "source": "binance_square", "source_item_id": "3", "status": "pending"
    }
    processing_repository = SQLiteXProcessingRepository(repository.path)
    claimed = processing_repository.claim_task_by_id("judge_crypto", task_id=int(task["id"]), worker_id="test")
    assert claimed is not None
    assert claimed.source == "binance_square"


def test_pipeline_failure_leaves_post_retryable(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "db.sqlite")
    _enable(repository)
    client = FakeClient(parse_profile_response(_payload(_post("1"))))
    worker = BinanceSquareWorker(repository=repository, client=client, pipeline_client=FakePipeline())
    worker.run_once()
    client.posts = parse_profile_response(_payload(_post("1"), _post("2", timestamp=1786889244000)))
    worker.pipeline_client = FakePipeline(fail=True)
    assert worker.run_once()[0].saved_count == 0
    worker.pipeline_client = FakePipeline()
    assert worker.run_once()[0].saved_count == 1


def test_binance_square_publisher_contract_is_manual_review() -> None:
    task = TaskRecord(id=1, source="binance_square", source_item_id="1", source_url=None, title=None, content="正文")
    assert resolve_publisher_channel(task) == "x"
    assert forced_manual_review_reason_code(task) == "binance_square_manual_only"
    assert "不允许自动发布" in publisher_manual_review_reason("binance_square_manual_only", task=task)
