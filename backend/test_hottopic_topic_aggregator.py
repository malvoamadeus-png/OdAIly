from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.hottopic.topic_aggregator import ContentItem, EventIdentity, ModelBriefWriter, TopicAggregator, stable_id


def profile(
    started_at: datetime,
    *,
    entities: set[str],
    event_kinds: set[str],
    participants: set[str],
    primary_assets: set[str] | None = None,
    identity: EventIdentity | None = None,
) -> dict[str, object]:
    return {
        "topic": {"started_at": started_at.astimezone(UTC).isoformat()},
        "identity": identity or EventIdentity(frozenset(), frozenset(entities), frozenset(entities), frozenset(), frozenset(entities)),
        "event_kinds": event_kinds,
        "participants": participants,
        "primary_assets": primary_assets or set(),
        "primary_contracts": set(),
        "asset_accounts": {},
        "launch_accounts": participants if "launch" in event_kinds else set(),
    }


def test_non_asset_event_identity_merges_across_independent_accounts() -> None:
    now = datetime.now(UTC)
    launch = profile(
        now,
        entities={"binance", "pancakeswap", "binancewallet"},
        event_kinds={"launch"},
        participants={"official", "commentator", "researcher"},
    )
    follow_up = profile(
        now + timedelta(minutes=20),
        entities={"binance", "pancakeswap", "polymarket", "binancewallet"},
        event_kinds={"launch"},
        participants={"analyst", "trader"},
        primary_assets={"ppoly"},
    )

    assert TopicAggregator._topics_share_continuous_subject(launch, follow_up) == (
        True,
        "shared named event identity=['binance', 'binancewallet', 'pancakeswap']; kind=launch",
    )


def test_model_brief_writer_uses_litellm_master_key_for_a_local_proxy(monkeypatch) -> None:
    monkeypatch.delenv("HOTTOPIC_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "upstream-key")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "local-proxy-key")

    writer = ModelBriefWriter("odaily-gpt-writer", base_url="http://127.0.0.1:4000/v1")

    assert writer.api_key == "local-proxy-key"


def test_aggregator_connection_matches_the_shared_worker_lock_policy(tmp_path: Path) -> None:
    aggregator = TopicAggregator(tmp_path / "topics.sqlite")
    try:
        assert aggregator.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert aggregator.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert aggregator.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        aggregator.close()


def test_model_brief_writer_uses_luna_then_terra_without_reasoning() -> None:
    calls: list[dict[str, object]] = []

    class Cursor:
        def fetchone(self) -> dict[str, str]:
            return {"working_title": "Example topic"}

    class Connection:
        def execute(self, *_args, **_kwargs) -> Cursor:
            return Cursor()

    writer = ModelBriefWriter(
        "gpt-5.6-luna",
        fallback_model="gpt-5.6-terra",
        base_url="https://example.test/v1",
        api_key="test-key",
        max_attempts=1,
    )

    def post(request):
        payload = json.loads(request.data.decode("utf-8"))
        calls.append(payload)
        if payload["model"] == "gpt-5.6-luna":
            raise RuntimeError("luna unavailable")
        return {
            "choices": [{"message": {"content": json.dumps({
                "title": "Example topic",
                "brief": "A concise evidence-grounded brief.",
                "source_claim_ids": ["claim:1"],
            })}}]
        }

    writer._post_json = post  # type: ignore[method-assign]
    result = writer(
        Connection(),  # type: ignore[arg-type]
        "topic:1",
        datetime.now(UTC).isoformat(),
        [{"claim_id": "claim:1", "claim_text": "Example evidence", "tweet_id": "1"}],
    )

    assert result["source_claim_ids"] == ["claim:1"]
    assert [call["model"] for call in calls] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert all(call["reasoning_effort"] == "none" for call in calls)


def test_process_batch_rebuilds_retrieval_cache_after_transaction_rollback(tmp_path: Path) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    aggregator = TopicAggregator(tmp_path / "topics.sqlite")

    def content(tweet_id: str, at: datetime) -> ContentItem:
        return ContentItem(
            content_item_id=f"tweet:{tweet_id}",
            tweet_id=tweet_id,
            activity_account=f"account_{tweet_id}",
            author=f"account_{tweet_id}",
            activity_type="original",
            content_text="$TEST launched",
            expanded_text="$TEST launched",
            created_at=at.isoformat(),
            source_url=f"https://x.com/account_{tweet_id}/status/{tweet_id}",
            metrics={},
            references={},
            raw_payload={},
        )

    original_refresh = aggregator._refresh_all_topics

    def fail_after_topic_state(_at: str):
        raise RuntimeError("forced failure after topic cache update")

    aggregator._refresh_all_topics = fail_after_topic_state  # type: ignore[method-assign]
    try:
        try:
            aggregator.process_batch([content("1", now)], now)
        except RuntimeError as exc:
            assert str(exc) == "forced failure after topic cache update"
        else:
            raise AssertionError("the first batch should fail after the topic state is cached")

        assert aggregator.connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 0

        aggregator._refresh_all_topics = original_refresh  # type: ignore[method-assign]
        result = aggregator.process_batch([content("2", now + timedelta(seconds=1))], now + timedelta(seconds=1))

        assert result["metrics"]["create_seed_count"] == 1
        assert aggregator.connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    finally:
        aggregator.close()


def test_process_batch_reuses_existing_seed_topic_when_cache_misses_it(tmp_path: Path) -> None:
    now = datetime(2026, 9, 25, tzinfo=UTC)
    aggregator = TopicAggregator(tmp_path / "topics.sqlite")
    payload = {
        "tweet_id": "1",
        "activity_type": "original",
        "author_screen_name": "account",
        "account_screen_name": "account",
        "created_at_iso": now.isoformat(),
        "text": "$TEST launched",
        "expanded_text": "$TEST launched",
    }
    item = ContentItem.from_any(payload)
    assert item is not None
    claim = aggregator._extract_claims(item)[0]
    aggregator._create_seed(claim, item, now.isoformat())
    aggregator.connection.commit()
    aggregator._retrieval_cache.clear()
    aggregator._feature_index.clear()

    try:
        result = aggregator.process_batch([payload], now + timedelta(seconds=1))

        assert result["metrics"]["create_seed_count"] == 1
        assert aggregator.connection.execute("SELECT COUNT(*) FROM topics").fetchone()[0] == 1
    finally:
        aggregator.close()


def test_event_identity_extracts_named_entities_and_launch_kind_from_ordinary_posts() -> None:
    announcement = {"claim_text": "Binance Wallet announced Pre-Access campaigns hosted by PancakeSwap."}
    follow_up = {"claim_text": "@BinanceWallet and @PancakeSwap introduced Pre-Access; Polymarket may be first."}

    announcement_identity = TopicAggregator._event_identity([announcement])
    follow_up_identity = TopicAggregator._event_identity([follow_up])

    assert {"binance", "pancakeswap"} <= announcement_identity.named_entities
    assert {"binancewallet", "pancakeswap"} <= follow_up_identity.named_entities
    assert TopicAggregator._event_kinds([announcement]) == {"launch"}
    assert TopicAggregator._event_kinds([follow_up]) == {"launch"}

    now = datetime.now(UTC)
    launch = profile(
        now,
        entities=set(),
        event_kinds=TopicAggregator._event_kinds([announcement]),
        participants={"official", "commentator"},
        identity=announcement_identity,
    )
    follow_up_topic = profile(
        now + timedelta(minutes=20),
        entities=set(),
        event_kinds=TopicAggregator._event_kinds([follow_up]),
        participants={"analyst"},
        identity=follow_up_identity,
    )
    assert TopicAggregator._topics_share_continuous_subject(launch, follow_up_topic) == (
        True,
        "shared named event identity=['binance', 'pancakeswap']; kind=launch",
    )


def test_non_asset_event_identity_rejects_weak_or_conflicting_matches() -> None:
    now = datetime.now(UTC)
    base = profile(
        now,
        entities={"binance", "pancakeswap", "binancewallet"},
        event_kinds={"launch"},
        participants={"one", "two"},
    )
    one_shared_entity = profile(
        now + timedelta(minutes=20),
        entities={"binance", "unrelatedproduct"},
        event_kinds={"launch"},
        participants={"three"},
    )
    different_event = profile(
        now + timedelta(minutes=20),
        entities={"binance", "pancakeswap", "binancewallet"},
        event_kinds={"listing"},
        participants={"three"},
    )
    conflicting_assets = profile(
        now + timedelta(minutes=20),
        entities={"binance", "pancakeswap", "binancewallet"},
        event_kinds={"launch"},
        participants={"three"},
        primary_assets={"otherasset"},
    )
    base["primary_assets"] = {"firstasset"}

    assert TopicAggregator._topics_share_continuous_subject(base, one_shared_entity)[0] is False
    assert TopicAggregator._topics_share_continuous_subject(base, different_event)[0] is False
    assert TopicAggregator._topics_share_continuous_subject(base, conflicting_assets)[0] is False


def test_reconcile_recent_topics_merges_existing_non_asset_seeds(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    aggregator = TopicAggregator(tmp_path / "topics.sqlite")
    try:
        seeds = [
            ("topic:announcement", "official", "Binance Wallet announced Pre-Access campaigns hosted by PancakeSwap."),
            ("topic:announcement", "commentator", "Binance and PancakeSwap launched Pre-Access for Binance Wallet users."),
            ("topic:announcement", "researcher", "PancakeSwap introduced Pre-Access through Binance Wallet."),
            ("topic:announcement", "trader", "Binance Wallet released the PancakeSwap Pre-Access campaign."),
            ("topic:follow-up", "analyst", "@BinanceWallet and @PancakeSwap introduced Pre-Access; Polymarket may be first."),
        ]
        created_topics: set[str] = set()
        with aggregator.connection:
            for index, (topic_id, account, text) in enumerate(seeds):
                item_id, claim_id = f"content:{index}", f"claim:{index}"
                at = (now + timedelta(minutes=index)).isoformat()
                aggregator.connection.execute(
                    "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, f"tweet:{index}", account, account, "original", text, text, at,
                     f"https://x.com/{account}/status/{index}", "{}", "{}", "{}", f"fingerprint:{index}"),
                )
                aggregator.connection.execute(
                    "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (claim_id, item_id, text, "official_statement", "[]", "发布/上线", "", "", 1.0, 1.0, at),
                )
                if topic_id not in created_topics:
                    aggregator.connection.execute(
                        "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
                        "seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,identity_revision,participant_count_1h,"
                        "participant_count_6h,participant_count_24h,participant_velocity,hotness_score,retention_tier) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (topic_id, text, text, "[]", "发布/上线", at, at, (now + timedelta(hours=24)).isoformat(), at, at,
                         "seed", "hidden", 1, 0, 0, 0, 0.0, 0.0, "transient"),
                    )
                    created_topics.add(topic_id)
                aggregator.connection.execute(
                    "INSERT INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (stable_id("membership", claim_id, topic_id), claim_id, topic_id, None, "primary", "new_fact", 1.0, "test", "test", at, None),
                )
                aggregator.connection.execute(
                    "INSERT INTO topic_participations VALUES (?,?,?,?)",
                    (topic_id, account, at, item_id),
                )
                aggregator._update_retrieval(topic_id, at)

        result = aggregator.reconcile_recent_topics(now + timedelta(minutes=5))

        assert result["topic_merges"]
        assert aggregator.connection.execute(
            "SELECT COUNT(*) FROM topics WHERE matching_status='active'"
        ).fetchone()[0] == 1
        assert aggregator.connection.execute(
            "SELECT COUNT(*) FROM topic_participations WHERE topic_id=("
            "SELECT topic_id FROM topics WHERE matching_status='active')"
        ).fetchone()[0] == 5
    finally:
        aggregator.close()


def test_lifecycle_qualifies_accounts_sharing_an_exact_contract(tmp_path: Path) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    aggregator = TopicAggregator(tmp_path / "topics.sqlite")
    topic_id = "topic:truman"
    contract = "0xabffa443547b34ab6c3b3173d26e233900527777"
    posts = [
        ("alpha", f"$TRUMAN is an AI world experiment {contract}"),
        ("bravo", f"$TRUMAN has a fixed CA {contract}"),
        ("charlie", f"Watching $TRUMAN at {contract}"),
        ("delta", f"The AI project contract is {contract}"),
        ("echo", f"This is the same BSC address: {contract}"),
    ]
    try:
        with aggregator.connection:
            for index, (account, text) in enumerate(posts):
                item_id, claim_id = f"content:{index}", f"claim:{index}"
                at = (now + timedelta(minutes=index)).isoformat()
                aggregator.connection.execute(
                    "INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, f"tweet:{index}", account, account, "original", text, text, at,
                     f"https://x.com/{account}/status/{index}", "{}", "{}", "{}", f"fingerprint:{index}"),
                )
                aggregator.connection.execute(
                    "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (claim_id, item_id, text, "reported_fact", "[]", "AI/模型", "", "", 1.0, 1.0, at),
                )
                aggregator.connection.execute(
                    "INSERT OR IGNORE INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
                    "seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,identity_revision,participant_count_1h,"
                    "participant_count_6h,participant_count_24h,participant_velocity,hotness_score,retention_tier) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (topic_id, "$TRUMAN", "$truman", "[]", "AI/模型", now.isoformat(), now.isoformat(),
                     (now + timedelta(hours=24)).isoformat(), at, at, "seed", "hidden", 1, 0, 0, 0, 0.0, 0.0, "transient"),
                )
                aggregator.connection.execute(
                    "INSERT INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (stable_id("membership", claim_id, topic_id), claim_id, topic_id, None, "primary", "new_fact", 1.0, "test", "test", at, None),
                )
                aggregator.connection.execute(
                    "INSERT INTO topic_participations VALUES (?,?,?,?)",
                    (topic_id, account, at, item_id),
                )

        assessment = aggregator._assess_topic(topic_id, now + timedelta(minutes=5))

        assert assessment.event_anchor == f"contract:{contract}"
        assert assessment.qualified_accounts == {account for account, _ in posts}
        assert assessment.qualifies is True
        aggregator._refresh_all_topics((now + timedelta(minutes=5)).isoformat())
        assert aggregator.connection.execute(
            "SELECT matching_status FROM topics WHERE topic_id=?", (topic_id,)
        ).fetchone()[0] == "active"
    finally:
        aggregator.close()
