from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from packages.common.config import XProcessingSettings
from packages.hottopic.event_tracking import SQLiteEventTrackingTaskDispatcher
from packages.local_pipeline.processor import LocalPipelineProcessor
from packages.publisher.push_client import PushResult
from packages.x_processing.models import EVENT_TRACKING_SOURCE, PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from packages.x_processing.worker import XProcessingWorker


class FakePushClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def push(self, **kwargs):
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text="ok")


class FakeFeedWriter:
    def upsert_newsflash(self, **_kwargs) -> None:
        return None


class FakePipelineClient:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def submit_job(self, **kwargs) -> None:
        self.jobs.append(kwargs)


def test_event_tracking_source_starts_at_search_and_auto_publishes_with_tweet_idempotency() -> None:
    task = TaskRecord(
        id=73,
        source=EVENT_TRACKING_SOURCE,
        source_item_id="official-tweet-73",
        source_url="https://x.com/GIWAofficial/status/official-tweet-73",
        title="GIWA：官方进展",
        content="GIWA confirms a material recovery update.",
        published_at=datetime.now(UTC),
        metadata={"event_tracking": {"auto_publish": True, "classification": "material_progress"}},
        status="publisher_pending",
    )
    assert LocalPipelineProcessor._write_flow_sequence(None, task) == ["search", "write", "format_publish", "publish"]  # type: ignore[arg-type]

    repository = InMemoryXProcessingRepository()
    repository.add_task(task)
    repository.pipelines[task.id] = PipelineRecord(
        task_id=task.id,
        news_type="onchain",
        final_title="GIWA 公布资金冻结进展",
        final_content="GIWA 表示已冻结部分涉案资金，调查仍在进行。",
    )
    push_client = FakePushClient()
    worker = XProcessingWorker(
        stage="publish",
        repository=repository,
        settings=XProcessingSettings(dry_run=False),
        push_client=push_client,  # type: ignore[arg-type]
    )
    worker.feed_writer = FakeFeedWriter()  # type: ignore[assignment]

    worker._run_publish(task)

    assert repository.tasks[task.id].status == "auto_published"
    pipeline = repository.pipelines[task.id]
    assert pipeline.publisher_decision == "auto_publish"
    assert pipeline.publisher_reason_code == "event_tracking_material_progress"
    assert len(push_client.calls) == 1
    assert push_client.calls[0]["is_publish"] is True
    assert push_client.calls[0]["idempotency_key"] == "event-tracking:official-tweet-73"


def test_event_tracking_dispatcher_creates_one_prejudged_task_and_reuses_tweet_identity(tmp_path: Path) -> None:
    database_path = tmp_path / "odaily.sqlite"
    client = FakePipelineClient()
    dispatcher = SQLiteEventTrackingTaskDispatcher(database_path, pipeline_client=client)
    event = {"event_id": "event:giwa", "title": "GIWA 假桥事件", "tracking_type": "security_asset_incident"}
    cycle = {"cycle_id": "cycle:one", "started_at": "2026-09-28T08:00:00+00:00"}
    update = {
        "update_id": "update:one",
        "classification": "material_progress",
        "news_type": "onchain",
        "fact_summary": "已冻结部分资金",
        "difference_text": "首次确认冻结处置",
        "confirmed_facts_json": "[\"已冻结部分资金\"]",
        "unconfirmed_claims_json": "[]",
        "handle_lower": "giwaofficial",
    }
    post = {
        "tweet_id": "official-tweet-91",
        "account_screen_name": "GIWAofficial",
        "created_at_iso": "2026-09-28T08:01:00+00:00",
        "url": "https://x.com/GIWAofficial/status/official-tweet-91",
        "expanded_text": "We froze a portion of the funds.",
    }

    first = dispatcher.ensure_task(event=event, cycle=cycle, update=update, post=post)
    second = dispatcher.ensure_task(event=event, cycle=cycle, update=update, post=post)
    dispatcher.submit(task_id=first, tweet_id=post["tweet_id"])

    assert first == second
    assert client.jobs == [{"job_type": "write_flow", "task_id": first, "source": EVENT_TRACKING_SOURCE, "source_item_id": "official-tweet-91"}]
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT source,source_item_id,status,metadata FROM tasks").fetchone()
        pipeline = connection.execute("SELECT news_type,judge_output FROM x_task_pipeline WHERE task_id=?", (first,)).fetchone()
    assert row[:3] == (EVENT_TRACKING_SOURCE, "official-tweet-91", "judged")
    assert json.loads(row[3])["event_tracking"]["auto_publish"] is True
    assert pipeline[0] == "onchain"
    assert json.loads(pipeline[1])["rule_set"] == "event_tracking_material_progress"


def test_event_tracking_prejudged_task_is_claimable_by_sqlite_search_worker(tmp_path: Path) -> None:
    database_path = tmp_path / "odaily.sqlite"
    dispatcher = SQLiteEventTrackingTaskDispatcher(database_path)
    task_id = dispatcher.ensure_task(
        event={"event_id": "event:giwa", "title": "GIWA 假桥事件", "tracking_type": "security_asset_incident"},
        cycle={"cycle_id": "cycle:one", "started_at": "2026-09-28T08:00:00+00:00"},
        update={
            "update_id": "update:one",
            "classification": "material_progress",
            "news_type": "onchain",
            "fact_summary": "已冻结部分资金",
            "difference_text": "首次确认冻结处置",
            "confirmed_facts_json": "[\"已冻结部分资金\"]",
            "unconfirmed_claims_json": "[]",
            "handle_lower": "giwaofficial",
        },
        post={
            "tweet_id": "official-tweet-92",
            "account_screen_name": "GIWAofficial",
            "created_at_iso": "2026-09-28T08:01:00+00:00",
            "url": "https://x.com/GIWAofficial/status/official-tweet-92",
            "expanded_text": "We froze a portion of the funds.",
        },
    )

    repository = SQLiteXProcessingRepository(database_path)
    claimed = repository.claim_task_by_id("search", task_id=task_id, worker_id="test-search")

    assert claimed is not None
    assert claimed.id == task_id
    assert claimed.source == EVENT_TRACKING_SOURCE
    assert claimed.status == "deduping"
