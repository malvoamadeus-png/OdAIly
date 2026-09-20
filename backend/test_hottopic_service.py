from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.hottopic.capture import AccountRow, ContentItem
from packages.hottopic.service import BLACKLISTED_HANDLES, HotTopicService


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


def test_seed_blacklists_named_accounts_and_keeps_hellojintao(service: HotTopicService) -> None:
    service.seed_accounts([
        {"screen_name": "hellojintao", "display_name": "hello"},
        {"screen_name": "TeamTrump", "display_name": "Trump"},
    ])
    accounts = {row["screen_name_lower"]: row for row in service.list_accounts()}
    assert accounts["hellojintao"]["status"] == "followed"
    assert accounts["teamtrump"]["status"] == "blacklisted"
    assert "teamtrump" in BLACKLISTED_HANDLES


def test_named_blacklist_cannot_be_added_before_the_seed_runs(service: HotTopicService) -> None:
    with pytest.raises(ValueError, match="已拉黑"):
        service.add_account("TeamTrump")
    assert service.list_accounts(status="blacklisted")[0]["screen_name_lower"] == "teamtrump"


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


def test_blacklist_stops_tracking_and_requires_explicit_follow_to_restore(service: HotTopicService) -> None:
    service.add_account("alice")
    assert service.set_account_status("alice", "blacklisted")["status"] == "blacklisted"
    with pytest.raises(ValueError, match="先解除"):
        service.add_account("alice")
    assert service.set_account_status("alice", "unfollowed")["status"] == "unfollowed"
    assert service.set_account_status("alice", "followed")["status"] == "followed"


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
