from __future__ import annotations

import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.hottopic.capture import AccountRow, ContentItem
from packages.hottopic.service import CollectRequestPacer, HotTopicService, open_worker_database


def item(handle: str, tweet_id: str, created_at: datetime) -> ContentItem:
    return ContentItem(
        account_screen_name=handle,
        account_name=handle,
        tweet_id=tweet_id,
        activity_type="original",
        author_screen_name=handle,
        author_name=handle,
        created_at=created_at.strftime("%a %b %d %H:%M:%S +0000 %Y"),
        created_at_iso=created_at.isoformat(),
        url=f"https://x.com/{handle}/status/{tweet_id}",
        text="A shared event update",
        expanded_text="A shared event update",
        quote_id=None,
        quote_author_screen_name=None,
        quote_text=None,
        reply_to=None,
        replies=0,
        reposts=0,
        quotes=0,
        likes=0,
        views=0,
    )


@pytest.fixture
def service(tmp_path: Path):
    value = HotTopicService(tmp_path / "hottopic.sqlite", workers=1)
    try:
        yield value
    finally:
        value.close()


def test_seed_defaults_every_account_to_hot_topic_enabled(service: HotTopicService) -> None:
    service.seed_accounts([
        {"screen_name": "hellojintao", "display_name": "hello"},
        {"screen_name": "TeamTrump", "display_name": "Trump"},
    ])
    accounts = {row["screen_name_lower"]: row for row in service.list_accounts()}
    assert accounts["hellojintao"]["status"] == "followed"
    assert accounts["hellojintao"]["hot_topic_enabled"] == 1
    assert accounts["teamtrump"]["status"] == "followed"
    assert accounts["teamtrump"]["hot_topic_enabled"] == 1


def test_legacy_status_endpoint_cannot_create_a_hidden_blacklist(service: HotTopicService) -> None:
    service.add_account("TeamTrump")
    with pytest.raises(ValueError, match="不支持"):
        service.set_account_status("TeamTrump", "blacklisted")
    account = service.list_x_agent_accounts()["items"][0]
    assert account["status"] == "followed"
    assert account["hotTopicEnabled"] is True


def test_watermark_rejects_history_and_content_counter_survives_cleanup(service: HotTopicService) -> None:
    service.add_account("alice")
    account = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
    watermark = datetime.fromisoformat(service.deployment_started_at())
    old = item("alice", "old", watermark - timedelta(seconds=1))
    fresh = item("alice", "fresh", watermark + timedelta(seconds=1))

    assert service._store_poll(account, [old], None, watermark.isoformat()) == 0
    assert service._store_poll(account, [fresh], None, watermark.isoformat()) == 1
    assert service.list_accounts()[0]["cumulative_content_count"] == 1

    with service.db:
        service.db.execute("UPDATE hottopic_inbox SET processed_at=?,collected_at=?", ((watermark - timedelta(hours=49)).isoformat(), (watermark - timedelta(hours=49)).isoformat()))
    service.maintain(force=True)
    assert service.list_accounts()[0]["cumulative_content_count"] == 1


def test_legacy_unfollow_maps_to_subscription_state(service: HotTopicService) -> None:
    service.add_account("alice")
    assert service.set_account_status("alice", "unfollowed")["status"] == "unfollowed"
    state = service.list_x_agent_accounts()["items"][0]
    assert state["hotTopicEnabled"] is False
    assert state["marketSentimentEnabled"] is False
    assert state["projectPromotionEnabled"] is False
    assert service.set_account_status("alice", "followed")["status"] == "followed"


def test_account_cleanup_job_preserves_permanent_evidence(service: HotTopicService) -> None:
    """Legacy cleanup rows remain bounded when an older database contains them."""
    service.add_account("alice")
    now = service.deployment_started_at()
    with service.aggregator.connection:
        service.aggregator.connection.executemany(
            "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (f"content:{number}", f"tweet:{number}", "alice", "alice", "original", "text", "text", now,
                 f"https://x.com/alice/status/{number}", "{}", "{}", "{}", f"fingerprint:{number}")
                for number in range(3)
            ],
        )
        service.aggregator.connection.executemany(
            "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (f"claim:{number}", f"content:{number}", "text", "statement", "[]", "", "", "", 1.0, 1.0, now)
                for number in range(3)
            ],
        )
        service.aggregator.connection.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
            "seed_expires_at,matching_status,visibility,identity_revision,participant_count_1h,participant_count_6h,"
            "participant_count_24h,participant_velocity,hotness_score,retention_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("topic:permanent", "Shared event", "Shared event", "[]", "event", now, now, now,
             "active", "visible", 1, 1, 1, 1, 1.0, 1.0, "permanent"),
        )
        service.aggregator.connection.execute(
            "INSERT INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("membership:permanent", "claim:0", "topic:permanent", None, "primary", "new_fact", 1.0, "test", "test", now, None),
        )

    with service.db:
        service.db.execute(
            "INSERT INTO hottopic_account_cleanup(screen_name_lower,queued_at,last_error) VALUES(?,?,NULL)",
            ("alice", now),
        )
    assert service.aggregator.connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 3
    assert service.db.execute("SELECT COUNT(*) FROM hottopic_account_cleanup").fetchone()[0] == 1

    service.process_account_cleanup_jobs()

    assert service.aggregator.connection.execute("SELECT COUNT(*) FROM content_items").fetchone()[0] == 1
    assert service.aggregator.connection.execute(
        "SELECT content_item_id FROM content_items"
    ).fetchone()[0] == "content:0"
    assert service.db.execute("SELECT COUNT(*) FROM hottopic_account_cleanup").fetchone()[0] == 0


def test_ai_brief_generation_does_not_hold_the_sqlite_write_lock(service: HotTopicService) -> None:
    """Network-bound brief generation must run after the aggregation commit."""
    now = service.deployment_started_at()
    writer_started = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []

    def blocking_writer(*_args):
        writer_started.set()
        assert release_writer.wait(timeout=2)
        return {"title": "Shared event", "brief": "A confirmed shared event.", "source_claim_ids": ["claim:brief"]}

    service.aggregator.brief_writer = blocking_writer
    service.aggregator._refresh_all_topics = lambda _at: {"transitions": {}, "affected_topic_ids": []}  # type: ignore[method-assign]
    with service.aggregator.connection:
        service.aggregator.connection.execute(
            "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("content:brief", "tweet:brief", "alice", "alice", "original", "Shared event announced", "Shared event announced", now,
             "https://x.com/alice/status/brief", "{}", "{}", "{}", "fingerprint:brief"),
        )
        service.aggregator.connection.execute(
            "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("claim:brief", "content:brief", "Shared event announced", "official_statement", "[]", "event", "", "", 1.0, 1.0, now),
        )
        service.aggregator.connection.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
            "seed_expires_at,matching_status,visibility,identity_revision,participant_count_1h,participant_count_6h,"
            "participant_count_24h,participant_velocity,hotness_score,retention_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("topic:brief", "Shared event", "Shared event", "[]", "event", now, now, now,
             "active", "hidden", 1, 1, 1, 1, 1.0, 1.0, "permanent"),
        )
        service.aggregator.connection.execute(
            "INSERT INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("membership:brief", "claim:brief", "topic:brief", None, "primary", "new_fact", 1.0, "test", "test", now, None),
        )

    def aggregate() -> None:
        try:
            service.aggregator.process_batch([], now)
        except BaseException as exc:  # Surface worker failures in the test thread.
            errors.append(exc)

    worker = threading.Thread(target=aggregate)
    worker.start()
    assert writer_started.wait(timeout=1)
    blocker = open_worker_database(service.path)
    try:
        release_timer = threading.Timer(0.35, release_writer.set)
        release_timer.start()
        started = time.perf_counter()
        blocker.execute("BEGIN IMMEDIATE")
        elapsed = time.perf_counter() - started
        blocker.rollback()
        assert elapsed < 0.2
        release_timer.join()
    finally:
        release_writer.set()
        blocker.close()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not errors


def test_aggregation_does_not_hold_the_account_directory_lock(service: HotTopicService) -> None:
    service.add_account("alice")
    account = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
    watermark = datetime.fromisoformat(service.deployment_started_at())
    service._store_poll(account, [item("alice", "slow-aggregate", watermark + timedelta(seconds=1))], None, watermark.isoformat())
    aggregation_started = threading.Event()
    release_aggregation = threading.Event()
    errors: list[BaseException] = []

    def slow_process(*_args, **_kwargs):
        aggregation_started.set()
        assert release_aggregation.wait(timeout=2)
        return {"metrics": {}}

    service.aggregator.process_batch = slow_process  # type: ignore[method-assign]

    def aggregate() -> None:
        try:
            service.aggregate()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=aggregate)
    worker.start()
    assert aggregation_started.wait(timeout=1)
    started = time.perf_counter()
    service.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
    assert time.perf_counter() - started < 0.2
    release_aggregation.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert not errors


def test_permanent_topic_participation_updates_cumulative_metric(service: HotTopicService) -> None:
    service.add_account("alice")
    now = service.deployment_started_at()
    with service.db:
        service.db.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
            "seed_expires_at,matching_status,visibility,identity_revision,participant_count_1h,participant_count_6h,"
            "participant_count_24h,participant_velocity,hotness_score,retention_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("topic:one", "Shared event", "Shared event", "[]", "event", now, now, now,
             "active", "visible", 1, 1, 1, 1, 1.0, 1.0, "permanent"),
        )
        service.db.execute(
            "INSERT INTO topic_participations(topic_id,activity_account,last_participation_at,last_content_item_id) VALUES(?,?,?,?)",
            ("topic:one", "Alice", now, "missing-content-is-not-referenced"),
        )
    service._refresh_hot_topic_counts()
    assert service.list_accounts()[0]["cumulative_hot_topic_count"] == 1


def test_retention_constants_are_applied(service: HotTopicService) -> None:
    started = datetime.fromisoformat(service.deployment_started_at())
    with service.db:
        service.db.execute("INSERT INTO hottopic_events(at,kind,detail_json) VALUES(?,?,?)", ((started - timedelta(days=6)).isoformat(), "old", "{}"))
    service.maintain(force=True)
    assert service.db.execute("SELECT COUNT(*) FROM hottopic_events WHERE kind='old'").fetchone()[0] == 0


def test_zero_interval_pacer_still_honors_a_shared_rate_limit(monkeypatch) -> None:
    clock = [0.0]
    sleeps: list[float] = []

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr("packages.hottopic.service.time.monotonic", monotonic)
    monkeypatch.setattr("packages.hottopic.service.time.sleep", sleep)
    pacer = CollectRequestPacer(0)
    pacer.defer(1.2)
    pacer.wait()

    assert sum(sleeps) >= 1.2


def test_collection_failures_back_off_per_account(service: HotTopicService) -> None:
    transient = {"kind": "fetch_failed", "error": "timed out"}
    rate_limited = {"kind": "rate_limited", "error": "HTTP Error 429", "retry_after_seconds": 900}

    assert service._next_poll_delay_seconds(transient, 1) == 600
    assert service._next_poll_delay_seconds(transient, 2) == 1200
    assert service._next_poll_delay_seconds(rate_limited, 1) == 900
    assert service._next_poll_delay_seconds(rate_limited, 2) == 1800


def test_maintain_records_sqlite_lock_and_leaves_the_worker_alive(service: HotTopicService) -> None:
    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    service.aggregator.reconcile_recent_topics = locked  # type: ignore[method-assign]

    assert service.maintain(force=True) is None
    event = service.db.execute(
        "SELECT kind,detail_json FROM hottopic_events WHERE kind='maintain_busy' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert event is not None
    assert "database is locked" in event["detail_json"]
