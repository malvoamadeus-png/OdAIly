from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from packages.hottopic.capture import ContentItem
from packages.hottopic.event_tracking import EventModelResult, EventTracker, OpenAIEventTrackingAI
from packages.hottopic.service import open_worker_database
from packages.hottopic.topic_aggregator import TopicAggregator


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class FakeDispatcher:
    def __init__(self) -> None:
        self.next_task_id = 41
        self.created: list[dict[str, Any]] = []
        self.submitted: list[tuple[int, str]] = []
        self.statuses: dict[int, str] = {}
        self.dismissed: list[str] = []

    def ensure_task(self, **kwargs: Any) -> int:
        task_id = self.next_task_id
        self.next_task_id += 1
        self.created.append(kwargs)
        self.statuses[task_id] = "judged"
        return task_id

    def submit(self, *, task_id: int, tweet_id: str) -> None:
        self.submitted.append((task_id, tweet_id))

    def task_status(self, task_id: int) -> str | None:
        return self.statuses.get(task_id)

    def dismiss_event_tasks(self, *, event_id: str, dismissed_at: str, dismissed_by: str) -> dict[str, Any]:
        self.dismissed.append(event_id)
        return {"cancelledTaskIds": [], "publishedTaskIds": [], "terminalTaskIds": []}


class FakeAI:
    def __init__(self, *, include_citations: bool = True) -> None:
        self.include_citations = include_citations
        self.web_search_calls = 0

    @staticmethod
    def _result(text: str, *, model: str, citations: list[str] | None = None, tool_calls: list[dict[str, Any]] | None = None) -> EventModelResult:
        return EventModelResult(
            text=text,
            model=model,
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            duration_ms=4.0,
            raw_payload={},
            citations=citations or [],
            tool_calls=tool_calls or [],
        )

    def generate_json(self, *, model: str, prompt: str) -> EventModelResult:
        if "新闻值班追踪判断器" in prompt:
            topics = json.loads(prompt.rsplit("输入热点：", 1)[1])
            decisions = [
                {
                    "topic_id": topic["topic_id"],
                    "decision": "track",
                    "reader_value": "asset_trading",
                    "tracking_type": "security_asset_incident",
                    "event_identity": "GIWA fake bridge security incident",
                    "confirmed_facts": ["GIWA 已确认假链与假桥风险"],
                    "unconfirmed_claims": ["损失范围仍在核验"],
                    "reason": "官方仍会披露资金追踪与补偿进展",
                    "official_response_hypothesis": {
                        "entities": ["GIWA"],
                        "why_likely": "项目方正在调查并需要向用户说明处置",
                        "next_information": "资金追踪、影响范围与补偿安排",
                    },
                    "recommended_initial_window_hours": 72,
                    "stop_conditions": ["事件解决", "24 小时无实质官方进展"],
                    "confidence": 0.92,
                }
                for topic in topics
            ]
            return self._result(json.dumps(decisions, ensure_ascii=False), model=model)
        if "官方事件进展判断器" in prompt:
            return self._result(
                json.dumps(
                    {
                        "classification": "material_progress",
                        "news_type": "onchain",
                        "fact_summary": "GIWA 公布了新的涉案资金冻结进展。",
                        "difference_from_timeline": "此前只确认假桥风险，本帖首次确认冻结处置。",
                        "confirmed_facts": ["已冻结部分涉案资金"],
                        "unconfirmed_claims": ["最终补偿范围尚未公布"],
                        "confidence": 0.96,
                        "reason": "新增了影响用户资产处置的官方事实。",
                    },
                    ensure_ascii=False,
                ),
                model=model,
            )
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    def web_search_json(self, *, model: str, prompt: str) -> EventModelResult:
        self.web_search_calls += 1
        accounts = [
            {
                "handle": "lookonchain",
                "display_name": "Lookonchain",
                "official_entity": "Lookonchain",
                "official_relation": "official_project",
                "role": "third-party observer",
                "x_profile_url": "https://x.com/lookonchain",
                "official_evidence": "不是事件主体",
                "official_evidence_urls": ["https://lookonchain.com"],
                "person_involvement": "",
            },
            {
                "handle": "GIWAofficial",
                "display_name": "GIWA",
                "official_entity": "GIWA",
                "official_relation": "official_project",
                "role": "事件主体会继续发布调查与资金处置进展",
                "x_profile_url": "https://x.com/GIWAofficial",
                "official_evidence": "官网与本次 Web Search 结果将该账号列为项目官方账号",
                "official_evidence_urls": ["https://giwa.example/official-x"],
                "person_involvement": "",
            },
        ]
        return self._result(
            json.dumps({"accounts": accounts, "reason": "仅项目官方账号应被追踪"}, ensure_ascii=False),
            model=model,
            citations=["https://giwa.example/official-x"] if self.include_citations else [],
            tool_calls=[{"type": "web_search_call", "id": "ws_1"}],
        )


def _seed_topic(connection, clock: Clock, topic_id: str = "topic:giwa") -> None:
    now = clock().isoformat()
    with connection:
        connection.execute(
            """
            INSERT INTO topics(
              topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,
              seed_expires_at,matching_status,visibility,identity_revision,participant_count_1h,participant_count_6h,
              participant_count_24h,participant_velocity,hotness_score,retention_tier
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                topic_id, "GIWA 假桥资金风险", "GIWA 假桥资金风险", json.dumps(["GIWA", "$FAKER"]), "security incident",
                now, now, (clock() + timedelta(hours=2)).isoformat(), "active", "visible", 1, 3, 4, 5, 1.0, 9.0, "permanent",
            ),
        )


def _tracker(tmp_path: Path, *, ai: FakeAI | None = None) -> tuple[EventTracker, Any, Clock, FakeDispatcher]:
    database_path = tmp_path / "hottopic.sqlite"
    aggregator = TopicAggregator(database_path)
    aggregator.close()
    connection = open_worker_database(database_path)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS hottopic_inbox(tweet_id TEXT PRIMARY KEY,screen_name_lower TEXT,payload_json TEXT,collected_at TEXT,processed_at TEXT,error TEXT)"
    )
    clock = Clock()
    dispatcher = FakeDispatcher()
    tracker = EventTracker(
        connection,
        primary_database_path=tmp_path / "odaily.sqlite",
        ai=ai or FakeAI(),
        dispatcher=dispatcher,
        now=clock,
    )
    return tracker, connection, clock, dispatcher


def _official_update(clock: Clock, tweet_id: str = "official-progress") -> ContentItem:
    return ContentItem(
        account_screen_name="GIWAofficial",
        account_name="GIWA",
        tweet_id=tweet_id,
        activity_type="original",
        author_screen_name="GIWAofficial",
        author_name="GIWA",
        created_at=clock().strftime("%a %b %d %H:%M:%S +0000 %Y"),
        created_at_iso=clock().isoformat(),
        url=f"https://x.com/GIWAofficial/status/{tweet_id}",
        text="We froze a portion of the funds and continue the investigation.",
        expanded_text="We froze a portion of the funds and continue the investigation.",
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


def test_official_only_discovery_material_progress_outbox_and_silence_lifecycle(tmp_path: Path) -> None:
    tracker, connection, clock, dispatcher = _tracker(tmp_path)
    try:
        _seed_topic(connection, clock)
        assert tracker.observe_topics(["topic:giwa"]) == {"assessed": 1, "tracked": 1, "failed": 0}
        assert tracker.discover_official_accounts() == {"discovered": 1, "failed": 0, "bound": 1}

        handles = [row["handle_lower"] for row in connection.execute("SELECT handle_lower FROM event_tracking_accounts")]
        assert handles == ["giwaofficial"]
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_bindings").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM hottopic_inbox").fetchone()[0] == 0

        def scanner(_account, _cutoff):
            return [_official_update(clock)], None, None

        assert tracker.poll_due_accounts(scanner) == {"polled": 1, "accepted": 1, "errors": 0}
        assert tracker.classify_official_updates() == {"classified": 1, "material": 1, "failed": 0}
        dispatched = tracker.dispatch_publications()
        assert dispatched["submitted"] == 1
        assert dispatcher.submitted == [(41, "official-progress")]
        assert dispatcher.created[0]["post"]["tweet_id"] == "official-progress"
        assert connection.execute("SELECT COUNT(*) FROM hottopic_inbox").fetchone()[0] == 0

        dispatcher.statuses[41] = "duplicate"
        assert tracker.refresh_publication_statuses() == {"duplicate": 1, "published": 0}
        assert connection.execute("SELECT status FROM event_tracking_publication_outbox").fetchone()[0] == "duplicate"

        clock.advance(hours=25)
        assert tracker.maintain() == {"ended": 1}
        event = connection.execute("SELECT status,end_reason FROM event_tracking_events").fetchone()
        assert dict(event) == {"status": "ended", "end_reason": "silence_24h"}
        assert connection.execute("SELECT status FROM event_tracking_bindings").fetchone()[0] == "released"
    finally:
        connection.close()


def test_web_search_without_citations_never_creates_a_manual_or_third_party_tracking_path(tmp_path: Path) -> None:
    ai = FakeAI(include_citations=False)
    tracker, connection, clock, _dispatcher = _tracker(tmp_path, ai=ai)
    try:
        _seed_topic(connection, clock)
        tracker.observe_topics(["topic:giwa"])
        outcome = tracker.discover_official_accounts()

        assert outcome == {"discovered": 1, "failed": 1, "bound": 0}
        assert ai.web_search_calls == 2
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_bindings").fetchone()[0] == 0
        event = connection.execute("SELECT status,end_reason FROM event_tracking_events").fetchone()
        assert dict(event) == {"status": "discovery_failed", "end_reason": "discovery_failed"}
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_account_discoveries WHERE status='failed'").fetchone()[0] == 1
    finally:
        connection.close()


def test_distinct_topics_never_share_tracking_event_from_model_identity(tmp_path: Path) -> None:
    tracker, connection, clock, _dispatcher = _tracker(tmp_path)
    try:
        _seed_topic(connection, clock, "topic:first")
        _seed_topic(connection, clock, "topic:second")
        assert tracker.observe_topics(["topic:first", "topic:second"]) == {
            "assessed": 2, "tracked": 2, "failed": 0,
        }
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_events").fetchone()[0] == 2
    finally:
        connection.close()


def test_topic_merge_retires_duplicate_tracking_cycle(tmp_path: Path) -> None:
    tracker, connection, clock, dispatcher = _tracker(tmp_path)
    try:
        _seed_topic(connection, clock, "topic:first")
        _seed_topic(connection, clock, "topic:second")
        tracker.observe_topics(["topic:first", "topic:second"])
        events = {row["topic_id"]: row["event_id"] for row in connection.execute("SELECT topic_id,event_id FROM event_tracking_topic_links")}
        with connection:
            connection.execute(
                "INSERT INTO topic_merges VALUES(?,?,?,?,?)",
                ("merge:1", "topic:second", "topic:first", "same incident", clock().isoformat()),
            )
            connection.execute("UPDATE topics SET matching_status='archived',visibility='hidden' WHERE topic_id='topic:second'")
        result = tracker.reconcile_topic_merges()
        assert result["retired"] == 1
        assert len(dispatcher.dismissed) == 1
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_events WHERE status='discovering'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_events WHERE end_reason='topic_merged'").fetchone()[0] == 1
        assert tracker.reconcile_topic_merges() == {"moved": 0, "retired": 0}
        assert set(events.values()) == {row["event_id"] for row in connection.execute("SELECT event_id FROM event_tracking_events")}
    finally:
        connection.close()


def test_legacy_multi_topic_event_is_not_propagated_by_merge_history(tmp_path: Path) -> None:
    tracker, connection, clock, dispatcher = _tracker(tmp_path)
    try:
        for topic_id in ("topic:first", "topic:second", "topic:unrelated"):
            _seed_topic(connection, clock, topic_id)
        tracker.observe_topics(["topic:first"])
        event_id = connection.execute("SELECT event_id FROM event_tracking_events").fetchone()[0]
        with connection:
            connection.execute(
                "INSERT INTO event_tracking_topic_links VALUES(?,?,?,?,?,?)",
                (event_id, "topic:unrelated", "old", "{}", clock().isoformat(), clock().isoformat()),
            )
            connection.execute(
                "INSERT INTO topic_merges VALUES(?,?,?,?,?)",
                ("merge:legacy", "topic:first", "topic:second", "same incident", clock().isoformat()),
            )
        assert tracker.reconcile_topic_merges() == {"moved": 0, "retired": 0}
        assert connection.execute("SELECT COUNT(*) FROM event_tracking_topic_links WHERE topic_id='topic:second'").fetchone()[0] == 0
        assert dispatcher.dismissed == []
    finally:
        connection.close()


def test_operator_tracking_starts_after_automatic_rejection_and_survives_rescan(tmp_path: Path) -> None:
    tracker, connection, clock, _dispatcher = _tracker(tmp_path)
    try:
        _seed_topic(connection, clock)
        snapshot = tracker._topic_snapshots(["topic:giwa"])[0]
        with connection:
            connection.execute(
                "INSERT INTO event_tracking_topic_assessments(topic_id,snapshot_hash,snapshot_json,decision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                ("topic:giwa", snapshot["snapshot_hash"], json.dumps(snapshot), "do_not_track", clock().isoformat(), clock().isoformat()),
            )
        event_id = tracker.start_tracking_topic(
            "topic:giwa", actor="operator", reason="Follow the official response to the security incident",
            tracking_type="security_asset_incident", reader_value="asset_trading",
            confirmed_facts=["GIWA has acknowledged the bridge incident"],
            unconfirmed_claims=["Loss amount is not confirmed"],
            official_response_hypothesis={"entities": ["GIWA"], "why_likely": "Investigation is ongoing", "next_information": "Impact and remediation"},
        )
        assert tracker.start_tracking_topic(
            "topic:giwa", actor="operator", reason="Follow the official response to the security incident",
            tracking_type="security_asset_incident", reader_value="asset_trading",
            confirmed_facts=["GIWA has acknowledged the bridge incident"],
            unconfirmed_claims=["Loss amount is not confirmed"],
            official_response_hypothesis={"entities": ["GIWA"], "why_likely": "Investigation is ongoing", "next_information": "Impact and remediation"},
        ) == event_id
        with connection:
            connection.execute("UPDATE topics SET working_title='GIWA bridge investigation continues' WHERE topic_id='topic:giwa'")
        assert tracker.observe_topics(["topic:giwa"])["assessed"] == 0
        assert connection.execute("SELECT status FROM event_tracking_events WHERE event_id=?", (event_id,)).fetchone()[0] == "discovering"
        assert connection.execute("SELECT actual_model FROM event_tracking_topic_assessments WHERE topic_id='topic:giwa'").fetchone()[0] == "operator_override"
        assert tracker.discover_official_accounts(event_id=event_id)["bound"] == 1
    finally:
        connection.close()


def test_openai_web_search_adapter_requires_real_tool_call_and_citation(monkeypatch) -> None:
    requests: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                "model": "gpt-web",
                "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
                "output": [
                    {"type": "web_search_call", "id": "ws_1", "action": {"type": "search", "query": "GIWA official X"}},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "{\"accounts\":[]}",
                                "annotations": [{"type": "url_citation", "url": "https://giwa.example/official-x"}],
                            }
                        ],
                    },
                ],
            }

    def post(_url: str, **kwargs: Any) -> Response:
        requests.append(kwargs["json"])
        return Response()

    monkeypatch.setattr("packages.hottopic.event_tracking.requests.post", post)
    client = OpenAIEventTrackingAI(api_key="test", base_url="https://api.openai.com/v1", timeout_seconds=1)
    result = client.web_search_json(model="gpt-web", prompt="find official account")

    assert requests[0]["tools"] == [{"type": "web_search"}]
    assert result.tool_calls[0]["type"] == "web_search_call"
    assert result.citations == ["https://giwa.example/official-x"]

    class NoCitationResponse(Response):
        def json(self) -> dict[str, Any]:
            payload = super().json()
            payload["output"][1]["content"][0]["annotations"] = []
            return payload

    monkeypatch.setattr("packages.hottopic.event_tracking.requests.post", lambda *_args, **_kwargs: NoCitationResponse())
    with pytest.raises(ValueError, match="citations"):
        client.web_search_json(model="gpt-web", prompt="find official account")
