from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.hottopic.topic_aggregator import TopicAggregator


NOW = datetime(2026, 10, 8, 12, tzinfo=UTC)


def add_topic(aggregator: TopicAggregator, topic_id: str, title: str, brief: str, index: int) -> None:
    at = (NOW - timedelta(minutes=30 - index)).isoformat()
    with aggregator.connection:
        aggregator.connection.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,"
            "seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,brief_status,identity_revision,"
            "participant_count_1h,participant_count_6h,participant_count_24h,participant_velocity,hotness_score,retention_tier) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (topic_id, title, title, "[]", "发布/上线", at, at, (NOW + timedelta(days=1)).isoformat(), at, at,
             "active", "visible", "ready", 1, 5, 5, 5, 5.0, 10.0, "permanent"),
        )
        aggregator.connection.execute(
            "INSERT INTO brief_revisions VALUES(?,?,?,?,?,?,?,?,?)",
            (topic_id, 1, title, brief, "[]", "[]", at, "became_visible", title + brief),
        )
        for actor in range(5):
            item_id, claim_id = f"content:{index}:{actor}", f"claim:{index}:{actor}"
            account = f"account:{index}:{actor}"
            aggregator.connection.execute(
                "INSERT INTO content_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, item_id, account, account, "original", brief, brief, at,
                 "https://x.com/example", "{}", "{}", "{}", item_id),
            )
            aggregator.connection.execute(
                "INSERT INTO claims VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (claim_id, item_id, brief, "official_statement", "[]", "发布/上线", "", "", 1.0, 1.0, at),
            )
            aggregator.connection.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"membership:{index}:{actor}", claim_id, topic_id, None, "primary", "new_fact", 1.0, "test", "test", at, None),
            )
            aggregator.connection.execute(
                "INSERT INTO topic_participations VALUES(?,?,?,?)", (topic_id, account, at, item_id),
            )


class Reviewer:
    def __init__(self, groups: list[list[str]]) -> None:
        self.groups = groups
        self.coarse_calls: list[list[dict[str, str]]] = []
        self.deep_calls: list[list[str]] = []

    def propose(self, cards):
        self.coarse_calls.append(cards)
        return [{"ids": ids, "reason": "same reader story"} for ids in self.groups]

    def review(self, cards):
        self.deep_calls.append([card["id"] for card in cards])
        return {
            "ids": [card["id"] for card in cards], "decision": "merge", "confidence": 0.95,
            "reason": "same concrete event", "shared_fact": "same announcement",
            "distinct_facts": "different details", "risk": "preserve attribution",
            "evidence_claim_ids": {card["id"]: [card["evidence"][0]["claim_id"]] for card in cards},
        }


def test_full_visible_scan_merges_groups_and_keeps_periodic_schedule(tmp_path: Path) -> None:
    drake = ["topic:drake:1", "topic:drake:2", "topic:drake:3"]
    coinbase = ["topic:coinbase:1", "topic:coinbase:2"]
    reviewer = Reviewer([drake, coinbase])
    written_evidence: list[list[str]] = []

    def write_brief(_db, _id, _at, evidence):
        written_evidence.append([row["claim_id"] for row in evidence])
        return {"title": "Combined topic", "brief": "Combined evidence", "source_claim_ids": written_evidence[-1]}

    aggregator = TopicAggregator(tmp_path / "topics.sqlite", visible_merge_reviewer=reviewer,
                                 brief_writer=write_brief)
    try:
        for index, topic_id in enumerate([*drake, *coinbase, "topic:other"]):
            subject = "Justin Drake announced bunker mode ECDSA" if topic_id in drake else (
                "Coinbase announced Deribit Global Exchange" if topic_id in coinbase else "Anthropic announced Claude Haiku"
            )
            add_topic(aggregator, topic_id, subject, subject, index)

        first = aggregator.scan_visible_topics(NOW)

        assert first is not None and len(first["merges"]) == 3
        assert len(reviewer.coarse_calls) == 1
        assert len(reviewer.coarse_calls[0]) == 6
        assert len(reviewer.deep_calls) == 2
        assert [len(evidence) for evidence in written_evidence] == [2, 3]
        assert aggregator.connection.execute(
            "SELECT working_title FROM topics WHERE topic_id=?", (coinbase[0],)
        ).fetchone()[0].startswith("Coinbase")
        assert aggregator.connection.execute(
            "SELECT COUNT(*) FROM topics WHERE matching_status='active' AND visibility='visible'"
        ).fetchone()[0] == 3
        assert aggregator.connection.execute("SELECT status FROM topic_merge_scans").fetchone()[0] == "succeeded"
        assert aggregator.scan_visible_topics(NOW + timedelta(hours=1)) is None

        aggregator.queue_visible_merge_scan()
        immediate = aggregator.scan_visible_topics(NOW + timedelta(hours=2))
        assert immediate is not None and immediate["groups"] == 0
        assert aggregator.visible_merge_scan_due(NOW + timedelta(hours=4)) == "periodic"
        repaired = aggregator.repair_visible_merge_brief(first["merges"][0]["merge_id"], NOW + timedelta(hours=2))
        assert repaired["brief_status"] == "ready"
        assert len(written_evidence[-1]) == 2
    finally:
        aggregator.close()


def test_overlapping_coarse_groups_are_reviewed_as_one_group() -> None:
    groups = [{"ids": ["a", "b"]}, {"ids": ["b", "c"]}, {"ids": ["d", "e"]}]
    assert TopicAggregator._combine_merge_groups(groups, {"a", "b", "c", "d", "e"}) == [
        ["a", "b", "c"], ["d", "e"],
    ]


def test_empty_scan_and_failed_scan_retry(tmp_path: Path) -> None:
    class FailingReviewer:
        def propose(self, _cards):
            raise RuntimeError("model unavailable")

    aggregator = TopicAggregator(tmp_path / "topics.sqlite", visible_merge_reviewer=Reviewer([]))
    try:
        add_topic(aggregator, "topic:a", "Justin Drake announced bunker mode", "Justin Drake announced bunker mode", 0)
        add_topic(aggregator, "topic:b", "Coinbase announced Global Exchange", "Coinbase announced Global Exchange", 1)
        empty = aggregator.scan_visible_topics(NOW)
        assert empty is not None and empty["groups"] == 0 and empty["merges"] == []

        aggregator.visible_merge_reviewer = FailingReviewer()
        aggregator.queue_visible_merge_scan()
        failed = aggregator.scan_visible_topics(NOW + timedelta(hours=1))
        assert failed is not None and "model unavailable" in failed["error"]
        assert aggregator.visible_merge_scan_due(NOW + timedelta(hours=1, minutes=14)) is None
        assert aggregator.visible_merge_scan_due(NOW + timedelta(hours=1, minutes=15)) == "new_visible"
        assert aggregator.connection.execute(
            "SELECT status FROM topic_merge_scans ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0] == "failed"
    finally:
        aggregator.close()


def test_changed_input_cannot_be_merged(tmp_path: Path) -> None:
    reviewer = Reviewer([["topic:a", "topic:b"]])
    aggregator = TopicAggregator(tmp_path / "topics.sqlite", visible_merge_reviewer=reviewer)
    try:
        add_topic(aggregator, "topic:a", "Justin Drake announced bunker mode", "Justin Drake announced bunker mode", 0)
        add_topic(aggregator, "topic:b", "Justin Drake announced ECDSA risk", "Justin Drake announced ECDSA risk", 1)
        original_review = reviewer.review

        def changed_review(cards):
            result = original_review(cards)
            with aggregator.connection:
                aggregator.connection.execute(
                    "UPDATE brief_revisions SET content_hash='new-hash' WHERE topic_id='topic:b'"
                )
            return result

        reviewer.review = changed_review  # type: ignore[method-assign]
        result = aggregator.scan_visible_topics(NOW)
        assert result is not None and "changed during merge review" in result["error"]
        assert aggregator.connection.execute("SELECT COUNT(*) FROM topic_merges").fetchone()[0] == 0
    finally:
        aggregator.close()


def test_hidden_and_archived_topics_are_excluded(tmp_path: Path) -> None:
    reviewer = Reviewer([])
    aggregator = TopicAggregator(tmp_path / "topics.sqlite", visible_merge_reviewer=reviewer)
    try:
        for index, topic_id in enumerate(("visible", "hidden", "archived")):
            add_topic(aggregator, topic_id, topic_id, topic_id, index)
        with aggregator.connection:
            aggregator.connection.execute("UPDATE topics SET visibility='hidden' WHERE topic_id='hidden'")
            aggregator.connection.execute("UPDATE topics SET matching_status='archived' WHERE topic_id='archived'")
        result = aggregator.scan_visible_topics(NOW)
        assert result is not None and result["topics"] == 1
        assert reviewer.coarse_calls == []
    finally:
        aggregator.close()


def test_merge_requires_evidence_from_every_topic(tmp_path: Path) -> None:
    reviewer = Reviewer([["topic:a", "topic:b"]])
    aggregator = TopicAggregator(tmp_path / "topics.sqlite", visible_merge_reviewer=reviewer)
    try:
        add_topic(aggregator, "topic:a", "Drake bunker mode", "Drake bunker mode", 0)
        add_topic(aggregator, "topic:b", "堡垒模式与 ECDSA", "堡垒模式与 ECDSA", 1)
        original_review = reviewer.review

        def incomplete_review(cards):
            result = original_review(cards)
            result["evidence_claim_ids"].pop("topic:b")
            return result

        reviewer.review = incomplete_review  # type: ignore[method-assign]
        result = aggregator.scan_visible_topics(NOW)
        assert result is not None and "lacks evidence" in result["error"]
        assert aggregator.connection.execute("SELECT COUNT(*) FROM topic_merges").fetchone()[0] == 0
        assert aggregator.visible_merge_scan_due(NOW + timedelta(minutes=15)) == "periodic"
    finally:
        aggregator.close()
