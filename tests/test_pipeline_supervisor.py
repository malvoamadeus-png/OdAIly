from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.config import PipelineSupervisorSettings, RetrySettings
from packages.pipeline_supervisor.repository import EXPECTED_HEARTBEAT_COMPONENTS, to_json_safe
from packages.pipeline_supervisor.worker import PipelineSupervisorWorker
from packages.x_processing.telegram import TelegramResult


class FakeSupervisorRepository:
    def __init__(self) -> None:
        self.stale_heartbeats: list[dict[str, Any]] = []
        self.stale_success_heartbeats: list[dict[str, Any]] = []
        self.old_tasks: list[dict[str, Any]] = []
        self.stuck_tasks: list[dict[str, Any]] = []
        self.failed_tasks: list[dict[str, Any]] = []
        self.x_success_count = 1
        self.x_success_heartbeats = 0
        self.claimed: set[str] = set()

    def init_schema(self) -> None:
        return None

    def list_stale_heartbeats(self, *, cutoff: datetime):
        return self.stale_heartbeats

    def list_stale_success_heartbeats(self, *, cutoff: datetime):
        return self.stale_success_heartbeats

    def list_old_claimable_tasks(self, *, cutoff: datetime):
        return self.old_tasks

    def list_stuck_processing_tasks(self, *, cutoff: datetime):
        return self.stuck_tasks

    def list_recent_failed_tasks(self, *, since: datetime, threshold: int):
        return [item for item in self.failed_tasks if item["count"] >= threshold]

    def count_recent_x_success_attempts(self, *, since: datetime) -> int:
        return self.x_success_count

    def count_recent_x_capture_success_heartbeats(self, *, since: datetime) -> int:
        return self.x_success_heartbeats

    def claim_alert(self, *, alert_key: str, message: str, dedup_cutoff: datetime, metadata: dict[str, Any] | None = None) -> bool:
        if alert_key in self.claimed:
            return False
        self.claimed.add(alert_key)
        return True


class FakeTelegramClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_message(self, text: str) -> TelegramResult:
        self.calls.append(text)
        return TelegramResult(ok=True)


def settings() -> PipelineSupervisorSettings:
    return PipelineSupervisorSettings(
        telegram_bot_token="token",
        telegram_chat_id="chat",
        retry=RetrySettings(max_attempts=1, backoff_seconds=0),
    )


def test_supervisor_detects_stale_heartbeat_and_deduplicates_alert() -> None:
    repo = FakeSupervisorRepository()
    repo.stale_heartbeats = [
        {
            "component": "x_process_search",
            "worker_id": "worker-1",
            "status": "ok",
            "last_seen_at": datetime.now(UTC) - timedelta(minutes=20),
        }
    ]
    telegram = FakeTelegramClient()
    worker = PipelineSupervisorWorker(repository=repo, settings=settings(), telegram_client=telegram)

    first = worker.run_once()
    second = worker.run_once()

    assert first.checked == 1
    assert first.sent == 1
    assert second.checked == 1
    assert second.suppressed == 1
    assert len(telegram.calls) == 1
    assert "worker 心跳超时" in telegram.calls[0]
    assert "x_process_search" in telegram.calls[0]


def test_supervisor_detects_task_backlog_stuck_failed_and_x_attempt_gap() -> None:
    repo = FakeSupervisorRepository()
    repo.old_tasks = [{"source": "x", "status": "judged", "count": 2, "oldest_updated_at": "old"}]
    repo.stuck_tasks = [{"source": "blockbeats", "status": "deduping", "count": 1, "oldest_updated_at": "old"}]
    repo.failed_tasks = [{"source": "panews", "status": "search_failed", "count": 3}]
    repo.x_success_count = 0
    telegram = FakeTelegramClient()
    worker = PipelineSupervisorWorker(repository=repo, settings=settings(), telegram_client=telegram)

    result = worker.run_once()

    assert result.checked == 4
    assert result.sent == 4
    assert any("任务积压" in call and "x/judged" in call for call in telegram.calls)
    assert any("处理中任务卡住" in call and "blockbeats/deduping" in call for call in telegram.calls)
    assert any("失败任务异常" in call and "panews/search_failed" in call for call in telegram.calls)
    assert any("X 抓取无成功记录" in call for call in telegram.calls)


def test_supervisor_detects_worker_without_recent_success() -> None:
    repo = FakeSupervisorRepository()
    repo.stale_success_heartbeats = [
        {
            "component": "competitor_monitor",
            "worker_id": "worker-1",
            "status": "failed",
            "last_seen_at": datetime.now(UTC),
            "last_success_at": datetime.now(UTC) - timedelta(hours=1),
            "last_error": "{'blockbeats': 'timeout'}",
        }
    ]
    telegram = FakeTelegramClient()
    worker = PipelineSupervisorWorker(repository=repo, settings=settings(), telegram_client=telegram)

    result = worker.run_once()

    assert result.checked == 1
    assert "无近期成功心跳" in telegram.calls[0]
    assert "competitor_monitor" in telegram.calls[0]


def test_supervisor_accepts_recent_x_capture_heartbeat_when_attempts_are_sampled() -> None:
    repo = FakeSupervisorRepository()
    repo.x_success_count = 0
    repo.x_success_heartbeats = 1
    telegram = FakeTelegramClient()
    worker = PipelineSupervisorWorker(repository=repo, settings=settings(), telegram_client=telegram)

    result = worker.run_once()

    assert result.checked == 0
    assert telegram.calls == []


def test_supervisor_metadata_is_json_safe() -> None:
    now = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)

    payload = to_json_safe({"last_seen_at": now, "items": ({"at": now},), "ids": {2, 1}})

    assert payload == {
        "last_seen_at": "2026-05-11T14:00:00+00:00",
        "items": [{"at": "2026-05-11T14:00:00+00:00"}],
        "ids": [1, 2],
    }


def test_supervisor_expected_heartbeats_exclude_external_media_fetcher() -> None:
    assert "external_media_alert_fetcher" not in EXPECTED_HEARTBEAT_COMPONENTS
