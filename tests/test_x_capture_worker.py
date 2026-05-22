from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from packages.common.config import load_x_capture_worker_settings
from packages.x_capture.client import FXTwitterClient
from packages.x_capture.models import TimelineAttempt, TweetCandidate
from packages.x_capture.repository import CONFIG_NOTIFY_CHANNEL, InMemoryXCaptureRepository, PostgresXCaptureRepository
from packages.x_capture.worker import XCaptureWorker


def twitter_time(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).strftime("%a %b %d %H:%M:%S %z %Y")


def candidate(tweet_id: str, text: str = "text", *, created_at_raw: str | None = None) -> TweetCandidate:
    return TweetCandidate(
        tweet_id=tweet_id,
        author_username="ai_9684xtpa",
        author_display_name="Ai",
        text=text,
        created_at_raw=created_at_raw or twitter_time(),
        raw_payload={"id": tweet_id, "text": text, "created_at": created_at_raw or twitter_time()},
    )


class FakeClient(FXTwitterClient):
    def __init__(
        self,
        timelines: list[list[TweetCandidate]],
        *,
        detail_errors: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.timelines = timelines
        self.detail_errors = detail_errors or set()
        self.detail_calls: list[str] = []
        self.created_at_by_id = {
            item.tweet_id: item.created_at_raw
            for timeline in timelines
            for item in timeline
            if item.created_at_raw
        }

    def fetch_timeline(self, username: str, *, count: int = 20):
        items = self.timelines.pop(0)
        return items, TimelineAttempt("fxtwitter", "success", f"https://example.test/{username}", candidate_count=len(items))

    def fetch_detail(self, username: str, tweet_id: str) -> dict[str, Any]:
        self.detail_calls.append(tweet_id)
        if tweet_id in self.detail_errors:
            raise RuntimeError("detail failed")
        return {
            "id": tweet_id,
            "url": f"https://x.com/{username}/status/{tweet_id}",
            "text": f"detail {tweet_id}",
            "created_at": self.created_at_by_id.get(tweet_id) or twitter_time(),
            "author": {"screen_name": username, "name": "Ai"},
        }


def test_first_run_seeds_seen_without_tasks() -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="https://x.com/ai_9684xtpa")
    worker = XCaptureWorker(repository=repo, client=FakeClient([[candidate("1"), candidate("2")]]))

    stats = worker.run_once()

    assert stats[0].seeded_count == 2
    assert len(repo.seen) == 2
    assert repo.tasks == []
    assert repo.list_accounts()[0].seeded_at is not None


def test_second_run_saves_only_new_tweets_once() -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="ai_9684xtpa")
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient(
            [
                [candidate("1"), candidate("2")],
                [candidate("1"), candidate("2"), candidate("3")],
                [candidate("1"), candidate("2"), candidate("3")],
            ]
        ),
    )

    worker.run_once()
    second = worker.run_once()
    third = worker.run_once()

    assert second[0].new_count == 1
    assert second[0].saved_count == 1
    assert third[0].new_count == 0
    assert len(repo.tasks) == 1
    assert repo.tasks[0]["source_item_id"] == "3"


def test_detail_failure_still_saves_timeline_record() -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="ai_9684xtpa")
    fake = FakeClient(
        [[candidate("1")], [candidate("2", "timeline fallback")]],
        detail_errors={"2"},
    )
    worker = XCaptureWorker(repository=repo, client=fake)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    assert repo.tasks[0]["content"] == "timeline fallback"
    assert fake.detail_calls == ["2"]


def test_stale_tweets_are_marked_seen_without_tasks() -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="ai_9684xtpa")
    stale_time = twitter_time(datetime.now(UTC) - timedelta(minutes=30))
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient(
            [
                [candidate("1")],
                [candidate("1"), candidate("2", created_at_raw=stale_time)],
            ]
        ),
    )

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].new_count == 0
    assert stats[0].saved_count == 0
    assert stats[0].metadata["ignored_stale_count"] == 1
    assert stats[0].metadata["ignored_stale_tweet_ids"] == ["2"]
    assert "2" in repo.seen
    assert repo.tasks == []


def test_x_capture_freshness_window_uses_processing_env(monkeypatch) -> None:
    monkeypatch.setenv("PROCESSING_FRESHNESS_WINDOW_SECONDS", "300")

    settings = load_x_capture_worker_settings()

    assert settings.processing_freshness_window_seconds == 300


def test_x_capture_custom_freshness_window_marks_older_tweet_seen() -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="ai_9684xtpa")
    stale_time = twitter_time(datetime.now(UTC) - timedelta(minutes=6))
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient(
            [
                [candidate("1")],
                [candidate("1"), candidate("2", created_at_raw=stale_time)],
            ]
        ),
        freshness_window_seconds=300,
    )

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].metadata["freshness_window_seconds"] == 300
    assert stats[0].metadata["ignored_stale_tweet_ids"] == ["2"]
    assert "2" in repo.seen
    assert repo.tasks == []


def test_config_refresh_removes_disabled_account_from_schedule() -> None:
    repo = InMemoryXCaptureRepository()
    account = repo.create_account(username_or_url="ai_9684xtpa")
    worker = XCaptureWorker(repository=repo, client=FakeClient([]))

    worker.load_snapshot()
    assert "ai_9684xtpa" in worker._next_due

    repo.update_account(account.id, enabled=False)
    snapshot = worker.load_snapshot()

    assert snapshot.accounts == []
    assert "ai_9684xtpa" not in worker._next_due


def test_config_reload_signal_wakes_worker_loop() -> None:
    repo = InMemoryXCaptureRepository()
    worker = XCaptureWorker(repository=repo, client=FakeClient([]))

    worker.request_config_reload()

    assert worker._config_changed.is_set()
    assert worker._wake_event.is_set()


def test_attempt_prune_removes_only_old_rows() -> None:
    repo = InMemoryXCaptureRepository()
    account = repo.create_account(username_or_url="ai_9684xtpa")
    old_started = datetime.now(UTC) - timedelta(days=4)
    recent_started = datetime.now(UTC) - timedelta(days=2)
    repo.record_attempt(
        stats=repo_stats(account, "old"),
        started_at=old_started,
        finished_at=old_started + timedelta(seconds=1),
    )
    repo.record_attempt(
        stats=repo_stats(account, "recent"),
        started_at=recent_started,
        finished_at=recent_started + timedelta(seconds=1),
    )

    removed = repo.prune_attempts_before(datetime.now(UTC) - timedelta(days=3))

    assert removed == 1
    assert [item["status"] for item in repo.attempts] == ["recent"]


def test_worker_prunes_attempts_before_capture() -> None:
    repo = InMemoryXCaptureRepository()
    account = repo.create_account(username_or_url="ai_9684xtpa")
    old_started = datetime.now(UTC) - timedelta(days=5)
    repo.record_attempt(
        stats=repo_stats(account, "old"),
        started_at=old_started,
        finished_at=old_started + timedelta(seconds=1),
    )
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient([[candidate("1")]]),
        attempt_retention_days=3,
    )

    worker.run_once()

    assert all(item["started_at"] >= datetime.now(UTC) - timedelta(days=3) for item in repo.attempts)
    assert len(repo.attempts) == 1
    assert repo.attempts[0]["status"] == "success"


def test_worker_prune_failure_does_not_block_capture(monkeypatch) -> None:
    repo = InMemoryXCaptureRepository()
    repo.create_account(username_or_url="ai_9684xtpa")
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient([[candidate("1")]]),
        attempt_retention_days=3,
    )
    monkeypatch.setattr(repo, "prune_attempts_before", lambda cutoff: (_ for _ in ()).throw(RuntimeError("boom")))

    stats = worker.run_once()

    assert stats[0].status == "success"
    assert len(repo.attempts) == 1


def test_worker_prune_respects_hourly_interval() -> None:
    repo = InMemoryXCaptureRepository()
    worker = XCaptureWorker(
        repository=repo,
        client=FakeClient([]),
        attempt_retention_days=3,
        attempt_prune_interval_seconds=3600,
    )

    assert worker._prune_attempts_if_due(force=True) == 0
    worker.repository.prune_attempts_before = lambda cutoff: 99  # type: ignore[method-assign]

    assert worker._prune_attempts_if_due() == 0


def test_inmemory_repository_samples_noop_success_attempts_per_account_window() -> None:
    repo = InMemoryXCaptureRepository()
    account = repo.create_account(username_or_url="ai_9684xtpa")
    started = datetime.now(UTC)

    repo.record_attempt(
        stats=repo_stats(account, "success"),
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )
    repo.record_attempt(
        stats=repo_stats(account, "success"),
        started_at=started + timedelta(minutes=5),
        finished_at=started + timedelta(minutes=5, seconds=1),
    )

    assert len(repo.attempts) == 1


def test_inmemory_repository_keeps_interesting_success_attempts() -> None:
    from packages.x_capture.models import CaptureRunStats

    repo = InMemoryXCaptureRepository()
    account = repo.create_account(username_or_url="ai_9684xtpa")
    started = datetime.now(UTC)
    success = CaptureRunStats(account=account, status="success", new_count=1, saved_count=1)

    repo.record_attempt(
        stats=success,
        started_at=started,
        finished_at=started + timedelta(seconds=1),
    )
    repo.record_attempt(
        stats=success,
        started_at=started + timedelta(minutes=5),
        finished_at=started + timedelta(minutes=5, seconds=1),
    )

    assert len(repo.attempts) == 2


def test_attempt_retention_env_defaults_to_three_days(monkeypatch) -> None:
    monkeypatch.delenv("X_CAPTURE_ATTEMPT_RETENTION_DAYS", raising=False)

    settings = load_x_capture_worker_settings()

    assert settings.attempt_retention_days == 3


def test_attempt_retention_env_validates_range(monkeypatch) -> None:
    monkeypatch.setenv("X_CAPTURE_ATTEMPT_RETENTION_DAYS", "0")

    with pytest.raises(ValueError):
        load_x_capture_worker_settings()


def test_postgres_notify_listener_uses_psycopg3_notifies(monkeypatch) -> None:
    class FakeNotifyConnection:
        def __init__(self, on_notify) -> None:
            self.autocommit = False
            self.on_notify = on_notify
            self.executed: list[str] = []
            self.notify_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def execute(self, sql: str) -> None:
            self.executed.append(sql)

        def notifies(self, *, timeout: float | None = None, stop_after: int | None = None):
            self.notify_calls += 1
            self.on_notify()
            yield object()

    class FakePostgresRepository(PostgresXCaptureRepository):
        def __init__(self, conn: FakeNotifyConnection) -> None:
            self.conn = conn

        def _connect(self):
            return self.conn

    worker: XCaptureWorker
    conn = FakeNotifyConnection(lambda: worker.stop())
    repo = FakePostgresRepository(conn)
    worker = XCaptureWorker(repository=repo, client=FakeClient([]), notify_wait_seconds=0.01)
    monkeypatch.setattr("packages.x_capture.worker.select.select", lambda read, write, error, timeout: (read, [], []))

    worker._listen_for_config_changes()

    assert conn.executed == [f"LISTEN {CONFIG_NOTIFY_CHANNEL}"]
    assert conn.notify_calls == 1
    assert worker._config_changed.is_set()


def repo_stats(account, status: str):
    from packages.x_capture.models import CaptureRunStats

    return CaptureRunStats(account=account, status=status)
