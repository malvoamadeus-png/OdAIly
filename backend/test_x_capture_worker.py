from __future__ import annotations

import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from packages.x_capture.models import CaptureRecord, CaptureRunStats, TimelineAttempt, TweetCandidate, XCaptureAccount
from packages.x_capture.repository import InMemoryXCaptureRepository
from packages.x_capture.sqlite_repository import SQLiteXCaptureRepository
from packages.x_capture.worker import CollectRequestPacer, XCaptureWorker


def _tweet_candidate(username: str, tweet_id: str, text: str = "New post") -> TweetCandidate:
    return TweetCandidate(
        tweet_id=tweet_id,
        author_username=username,
        author_display_name=username,
        text=text,
        created_at_raw=datetime.now(UTC).strftime("%a %b %d %H:%M:%S +0000 %Y"),
    )


class _StubClient:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.detail_calls: list[tuple[str, str]] = []
        self.closed = False

    def fetch_timeline(self, username: str, *, count: int = 20, before_request=None):
        if before_request is not None:
            before_request()
        return self._handler(username, count)

    def fetch_detail(self, username: str, tweet_id: str) -> dict:
        self.detail_calls.append((username, tweet_id))
        return {
            "id": tweet_id,
            "text": f"detail-{tweet_id}",
            "created_at": datetime.now(UTC).strftime("%a %b %d %H:%M:%S +0000 %Y"),
            "author": {"screen_name": username, "name": username},
        }

    def collect_context_chain(self, detail, *, root_id: str, author_hint: str = "", raw_layers=None):
        return [], "complete", None

    def build_record(self, username: str, candidate: TweetCandidate, **kwargs) -> CaptureRecord:
        return CaptureRecord(
            platform="x",
            tweet_id=candidate.tweet_id,
            author_username=candidate.author_username,
            author_display_name=candidate.author_display_name,
            url=f"https://x.com/{username}/status/{candidate.tweet_id}",
            text=candidate.text,
            created_at=datetime.now(UTC).isoformat(),
            reply_count=0,
            retweet_count=0,
            like_count=0,
            bookmark_count=0,
            view_count=0,
            media_urls=[],
            metadata={"source": "fxtwitter"},
            raw_payload={"timeline": {}},
        )

    def close(self) -> None:
        self.closed = True


class _CountingRepo(InMemoryXCaptureRepository):
    def __init__(self) -> None:
        super().__init__()
        self.unseen_queries: list[list[str]] = []
        self.heartbeats: list[tuple[str, bool, str | None, dict]] = []

    def unseen_tweet_ids(self, tweet_ids: list[str]) -> set[str]:
        self.unseen_queries.append(list(tweet_ids))
        return super().unseen_tweet_ids(tweet_ids)

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error=None, metadata=None) -> None:
        self.heartbeats.append((status, success, error, dict(metadata or {})))


def test_collect_request_pacer_spaces_requests() -> None:
    pacer = CollectRequestPacer(0.03)
    timestamps: list[float] = []
    for _ in range(3):
        pacer.wait()
        timestamps.append(time.monotonic())
    assert timestamps[-1] - timestamps[0] >= 0.05


def test_sliding_scheduler_keeps_cadence_despite_slow_account_and_failures() -> None:
    repo = _CountingRepo()
    repo.update_settings(global_interval_seconds=1, max_concurrency=4, jitter_seconds=0)
    fast = repo.create_account(username_or_url="fastacct")
    slow = repo.create_account(username_or_url="slowacct")
    repo.mark_account_seeded(fast, ["1"])
    repo.mark_account_seeded(slow, ["2"])

    lock = threading.Lock()
    fast_calls: list[float] = []
    slow_calls: list[float] = []
    slow_release = threading.Event()

    def handle_timeline(username: str, _count: int):
        if username == "fastacct":
            with lock:
                fast_calls.append(time.monotonic())
                idx = len(fast_calls)
            if idx == 1:
                return [], TimelineAttempt("fxtwitter", "fetch_failed", "https://api.fxtwitter.com/fastacct", "404 Not Found")
            return [_tweet_candidate("fastacct", "1")], TimelineAttempt("fxtwitter", "success", "https://api.fxtwitter.com/fastacct", None, 1)
        with lock:
            slow_calls.append(time.monotonic())
        slow_release.wait(timeout=1.5)
        return [_tweet_candidate("slowacct", "2")], TimelineAttempt("fxtwitter", "success", "https://api.fxtwitter.com/slowacct", None, 1)

    client = _StubClient(handle_timeline)
    worker = XCaptureWorker(
        repository=repo,
        client=client,  # type: ignore[arg-type]
        config_reload_interval_seconds=60.0,
        min_request_interval_seconds=0.0,
        detail_workers=2,
    )
    worker._next_due = {"fastacct": time.monotonic(), "slowacct": time.monotonic()}
    repo.accounts[fast.id] = replace(fast, seeded_at=datetime.now(UTC), interval_seconds=0.12)  # type: ignore[arg-type]
    repo.accounts[slow.id] = replace(slow, seeded_at=datetime.now(UTC), interval_seconds=0.12)  # type: ignore[arg-type]

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with lock:
                if len(fast_calls) >= 3:
                    break
            time.sleep(0.02)
        assert len(fast_calls) >= 3
        assert len(slow_calls) == 1
        assert client.detail_calls == []
    finally:
        slow_release.set()
        worker.stop()
        thread.join(timeout=2.0)

    assert client.closed is True
    assert repo.unseen_queries == []
    statuses = [attempt["status"] for attempt in repo.attempts if attempt["username_lower"] == "fastacct"]
    assert "fetch_failed" in statuses
    assert "success" in statuses


def test_detail_pipeline_runs_asynchronously_and_prevents_duplicate_inflight_processing() -> None:
    repo = _CountingRepo()
    repo.update_settings(global_interval_seconds=1, max_concurrency=4, jitter_seconds=0)
    acct = repo.create_account(username_or_url="OnchainLens")
    repo.mark_account_seeded(acct, ["10"])
    repo.accounts[acct.id] = replace(acct, seeded_at=datetime.now(UTC), interval_seconds=0.08)  # type: ignore[arg-type]

    detail_started = threading.Event()
    detail_release = threading.Event()
    detail_entered: list[str] = []
    timeline_calls = 0

    class _AsyncDetailClient(_StubClient):
        def fetch_detail(self, username: str, tweet_id: str) -> dict:
            detail_entered.append(tweet_id)
            detail_started.set()
            detail_release.wait(timeout=2.0)
            return super().fetch_detail(username, tweet_id)

    def handle_timeline(username: str, _count: int):
        nonlocal timeline_calls
        timeline_calls += 1
        return [
            _tweet_candidate(username, "11", "ABRAXAS CAPITAL BUYS 11,000 ETH"),
            _tweet_candidate(username, "10", "Old post"),
        ], TimelineAttempt("fxtwitter", "success", "https://api.fxtwitter.com/OnchainLens", None, 2)

    submitted_jobs: list[dict] = []

    class _Pipeline:
        def submit_job(self, **kwargs):
            submitted_jobs.append(kwargs)

    client = _AsyncDetailClient(handle_timeline)
    worker = XCaptureWorker(
        repository=repo,
        client=client,  # type: ignore[arg-type]
        pipeline_client=_Pipeline(),  # type: ignore[arg-type]
        config_reload_interval_seconds=60.0,
        min_request_interval_seconds=0.0,
        detail_workers=2,
    )

    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    try:
        assert detail_started.wait(timeout=1.5)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and timeline_calls < 3:
            time.sleep(0.02)
        assert timeline_calls >= 3
        assert detail_entered == ["11"]
        detail_release.set()

        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and not submitted_jobs:
            time.sleep(0.02)
    finally:
        detail_release.set()
        worker.stop()
        thread.join(timeout=2.0)

    assert len(submitted_jobs) == 1
    assert submitted_jobs[0]["source_item_id"] == "11"
    assert len(repo.tasks) == 1
    assert len(client.detail_calls) == 1


def test_pipeline_failure_retries_on_next_poll_and_partial_failure_heartbeat_is_degraded() -> None:
    repo = _CountingRepo()
    repo.update_settings(global_interval_seconds=1, max_concurrency=4, jitter_seconds=0)
    ok_acct = repo.create_account(username_or_url="OnchainLens")
    bad_acct = repo.create_account(username_or_url="BrokenAcct")
    repo.mark_account_seeded(ok_acct, [])
    repo.mark_account_seeded(bad_acct, [])

    pipeline_attempts = 0

    class _FlakyPipeline:
        def submit_job(self, **kwargs):
            nonlocal pipeline_attempts
            pipeline_attempts += 1
            if pipeline_attempts == 1:
                raise RuntimeError("local pipeline busy")

    def handle_timeline(username: str, _count: int):
        if username == "BrokenAcct":
            return [], TimelineAttempt("fxtwitter", "fetch_failed", "https://api.fxtwitter.com/BrokenAcct", "404 Not Found")
        return [_tweet_candidate(username, "777", "Fresh signal")], TimelineAttempt(
            "fxtwitter", "success", "https://api.fxtwitter.com/OnchainLens", None, 1
        )

    worker = XCaptureWorker(
        repository=repo,
        client=_StubClient(handle_timeline),  # type: ignore[arg-type]
        pipeline_client=_FlakyPipeline(),  # type: ignore[arg-type]
        min_request_interval_seconds=0.0,
    )

    first = worker.run_once()
    assert len(first) == 2
    assert "777" in repo.unseen_tweet_ids(["777"])
    assert repo.heartbeats[-1][0] == "degraded"
    assert repo.heartbeats[-1][1] is True

    second = worker.run_once()
    assert len(second) == 2
    assert "777" not in repo.unseen_tweet_ids(["777"])
    assert pipeline_attempts == 2


def test_sqlite_repository_batches_attempts_and_caches_noop_sampling(tmp_path) -> None:
    repo = SQLiteXCaptureRepository(tmp_path / "capture.sqlite")
    acct = repo.create_account(username_or_url="OnchainLens")
    repo.mark_account_seeded(acct, ["100"])
    assert repo.list_recent_seen_tweet_ids(limit=10) == ["100"]

    t0 = datetime.now(UTC)
    noop = CaptureRunStats(
        account=acct,
        status="success",
        candidate_count=16,
        new_count=0,
        saved_count=0,
        metadata={"attempt_url": "https://api.fxtwitter.com/OnchainLens"},
    )
    repo.record_attempts_batch([(noop, t0, t0 + timedelta(seconds=1))])
    repo.record_attempts_batch([(noop, t0 + timedelta(seconds=10), t0 + timedelta(seconds=11))])
    repo.record_attempts_batch([(noop, t0 + timedelta(seconds=20), t0 + timedelta(seconds=21))])

    attempts = repo.list_recent_attempts(limit=10)
    assert len(attempts) == 1
    refreshed = repo.get_account_by_username("OnchainLens")
    assert refreshed is not None
    assert refreshed.last_polled_at is not None
    assert refreshed.last_polled_at >= t0 + timedelta(seconds=20)
