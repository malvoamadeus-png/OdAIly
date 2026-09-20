from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.hottopic.topic_aggregator import EventIdentity, ModelBriefWriter, TopicAggregator, stable_id


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

    writer = ModelBriefWriter("gpt-5.6-terra", base_url="http://127.0.0.1:4000/v1")

    assert writer.api_key == "local-proxy-key"


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
