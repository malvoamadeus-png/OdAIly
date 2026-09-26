from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from packages.hottopic.capture import AccountRow, ContentItem
from packages.hottopic.service import HotTopicService, iso, utc_now
from packages.x_agent.analysis import AnalysisResult, XAgentAnalyzer


def content(handle: str, tweet_id: str, text: str) -> ContentItem:
    created = utc_now() + timedelta(seconds=1)
    return ContentItem(
        account_screen_name=handle,
        account_name=handle,
        tweet_id=tweet_id,
        activity_type="original",
        author_screen_name=handle,
        author_name=handle,
        created_at=created.strftime("%a %b %d %H:%M:%S +0000 %Y"),
        created_at_iso=iso(created),
        url=f"https://x.com/{handle}/status/{tweet_id}",
        text=text,
        expanded_text=text,
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


class FakeAnalyzer:
    def analyze(self, module: str, source: dict[str, str]) -> AnalysisResult:
        if module == "market_sentiment":
            return AnalysisResult(
                module,
                "gpt-5.6-luna",
                None,
                [{"scope": "Crypto具体标的", "instrument_name": "Bitcoin", "ticker": "BTC", "sentiment": "偏多/乐观", "reason": "作者明确看多 BTC。"}],
            )
        return AnalysisResult(
            module,
            "gpt-5.6-luna",
            None,
            [{"project_name": "Example", "ticker": "EX", "chain": "Base", "contract_address": "0xabcdef0123456789", "official_url": "", "logic": "产品即将上线并有空投催化。"}],
        )


class BrokenAnalyzer:
    def analyze(self, _module: str, _source: dict[str, str]) -> AnalysisResult:
        raise RuntimeError("upstream unavailable")


def service(tmp_path: Path) -> HotTopicService:
    return HotTopicService(tmp_path / "x-agent.sqlite", workers=1)


def test_existing_hottopic_sqlite_receives_additive_x_agent_migration(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite"
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript(
            """
            CREATE TABLE hottopic_accounts (
              screen_name TEXT PRIMARY KEY,
              screen_name_lower TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL DEFAULT '',
              protected INTEGER NOT NULL DEFAULT 0,
              status TEXT NOT NULL,
              next_due_at TEXT,
              last_polled_at TEXT,
              last_success_at TEXT,
              last_error TEXT,
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              last_item_count INTEGER NOT NULL DEFAULT 0,
              cumulative_content_count INTEGER NOT NULL DEFAULT 0,
              cumulative_hot_topic_count INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            INSERT INTO hottopic_accounts(screen_name,screen_name_lower,status,created_at,updated_at)
            VALUES ('alice','alice','followed','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO hottopic_accounts(screen_name,screen_name_lower,status,created_at,updated_at)
            VALUES ('blocked','blocked','blacklisted','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    value = HotTopicService(path, workers=1)
    try:
        columns = {row["name"] for row in value.db.execute("PRAGMA table_info(hottopic_accounts)").fetchall()}
        assert {
            "hot_topic_enabled",
            "market_sentiment_enabled",
            "project_promotion_enabled",
            "last_x_agent_analyzed_at",
            "last_x_agent_error",
        } <= columns
        followed = value.db.execute(
            "SELECT status,hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled "
            "FROM hottopic_accounts WHERE screen_name_lower='alice'"
        ).fetchone()
        blocked = value.db.execute(
            "SELECT status,hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled "
            "FROM hottopic_accounts WHERE screen_name_lower='blocked'"
        ).fetchone()
        assert tuple(followed) == ("followed", 1, 0, 0)
        assert tuple(blocked) == ("followed", 1, 0, 0)
        assert value.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_agent_analysis_jobs'"
        ).fetchone()
    finally:
        value.close()


def test_existing_explicitly_disabled_legacy_row_stays_disabled_without_blacklist(tmp_path: Path) -> None:
    path = tmp_path / "existing.sqlite"
    initial = HotTopicService(path, workers=1)
    initial.close()
    legacy = sqlite3.connect(path)
    try:
        legacy.execute(
            "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,hot_topic_enabled,"
            "market_sentiment_enabled,project_promotion_enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("alice", "alice", "Alice", "blacklisted", 0, 0, 0, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
        legacy.commit()
    finally:
        legacy.close()

    value = HotTopicService(path, workers=1)
    try:
        row = value.list_x_agent_accounts()["items"][0]
        assert row["status"] == "unfollowed"
        assert row["hotTopicEnabled"] is False
        assert row["marketSentimentEnabled"] is False
        assert row["projectPromotionEnabled"] is False
    finally:
        value.close()


def test_subscriptions_are_independent_and_do_not_need_a_main_x_account(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        account = value.add_x_agent_account("alice", "Alice")
        assert account["hotTopicEnabled"] is True
        assert account["marketSentimentEnabled"] is False
        assert account["projectPromotionEnabled"] is False

        result = value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})[0]
        assert result["hotTopicEnabled"] is True
        assert result["marketSentimentEnabled"] is True
        assert value.x_agent_dashboard()["accounts"]["marketSentimentEnabled"] == 1
    finally:
        value.close()


def test_single_collected_post_fans_out_to_both_internal_modules(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True, "project_promotion_enabled": True})
        value.x_agent_analyzer = FakeAnalyzer()  # type: ignore[assignment]
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        assert value._store_poll(row, [content("alice", "1", "BTC bullish; Base token launch airdrop")], None, iso(utc_now())) == 1

        assert value.db.execute("SELECT COUNT(*) FROM hottopic_inbox").fetchone()[0] == 1
        assert value.db.execute("SELECT COUNT(*) FROM x_agent_analysis_jobs").fetchone()[0] == 2
        assert value.process_x_agent_jobs() == 2

        market = value.list_market_sentiment()
        projects = value.list_project_promotions()
        assert market["total"] == 1
        assert market["items"][0]["ticker"] == "BTC"
        assert projects["total"] == 1
        assert projects["items"][0]["projectName"] == "Example"
    finally:
        value.close()


def test_model_failure_stays_visible_and_is_not_interpreted_as_no_result(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        value.x_agent_analyzer = BrokenAnalyzer()  # type: ignore[assignment]
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "2", "BTC bullish with a long position")], None, iso(utc_now()))

        value.process_x_agent_jobs()
        job = value.db.execute("SELECT status,last_error FROM x_agent_analysis_jobs WHERE tweet_id='2'").fetchone()
        account = value.list_x_agent_accounts()["items"][0]
        assert job["status"] == "pending"
        assert "upstream unavailable" in job["last_error"]
        assert "upstream unavailable" in account["lastAnalysisError"]
        assert value.list_market_sentiment()["total"] == 0
    finally:
        value.close()


def test_project_same_identity_and_logic_updates_instead_of_creating_a_second_row(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        item = {"project_name": "Example", "ticker": "EX", "chain": "Base", "contract_address": "0xabcdef0123456789", "official_url": "", "logic": "产品即将上线并有空投催化。"}
        source = {"account_screen_name": "alice", "tweet_id": "first", "url": "https://x.com/alice/status/first", "expanded_text": "first", "created_at_iso": iso(utc_now())}
        result = AnalysisResult("project_promotion", "gpt-5.6-luna", None, [item])
        with value.db:
            value._save_project_results(source, result, iso(utc_now()))
            source["tweet_id"] = "second"
            source["url"] = "https://x.com/alice/status/second"
            value._save_project_results(source, result, iso(utc_now() + timedelta(minutes=1)))
        assert value.db.execute("SELECT COUNT(*) FROM x_agent_project_observations").fetchone()[0] == 1
        assert value.db.execute("SELECT source_tweet_id FROM x_agent_project_observations").fetchone()[0] == "second"
    finally:
        value.close()


def test_luna_failure_uses_terra_with_reasoning_disabled() -> None:
    calls: list[dict[str, object]] = []

    def post(_base: str, _key: str, body: bytes, _timeout: float) -> dict[str, object]:
        request = __import__("json").loads(body)
        calls.append(request)
        if request["model"] == "gpt-5.6-luna":
            raise RuntimeError("luna unavailable")
        return {"choices": [{"message": {"content": '{"items":[{"scope":"Crypto具体标的","instrument_name":"Bitcoin","ticker":"BTC","sentiment":"偏多/乐观","reason":"作者明确看多。"}]}'}}]}

    analyzer = XAgentAnalyzer(
        primary_model="gpt-5.6-luna",
        fallback_model="gpt-5.6-terra",
        base_url="https://example.test/v1",
        api_key="test-key",
        max_attempts=1,
        post_json=post,
    )
    result = analyzer.analyze("market_sentiment", {"tweet_id": "x", "account_screen_name": "alice", "text": "BTC bullish"})
    assert result.actual_model == "gpt-5.6-terra"
    assert result.fallback_reason == "RuntimeError: luna unavailable"
    assert [call["model"] for call in calls] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert all(call["reasoning_effort"] == "none" for call in calls)


def test_reimport_without_explicit_apply_does_not_override_manual_switch(tmp_path: Path) -> None:
    report_path = tmp_path / "screening.json"
    report_path.write_text(json.dumps({"accounts": [{
        "username": "alice",
        "display_name": "Alice",
        "judgment": {
            "market_sentiment": {"decision": "include"},
            "project_promotion": {"decision": "exclude"},
        },
    }]}), encoding="utf-8")
    value = service(tmp_path)
    try:
        assert value.import_x_agent_screening_report(report_path, apply_suggestions=True)["inserted"] == 1
        assert value.list_x_agent_accounts()["items"][0]["marketSentimentEnabled"] is True
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": False})
        assert value.import_x_agent_screening_report(report_path, apply_suggestions=False)["skipped"] == 1
        assert value.list_x_agent_accounts()["items"][0]["marketSentimentEnabled"] is False
    finally:
        value.close()


def test_readding_an_existing_account_preserves_manual_subscription_switches(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice", "Old name")
        value.set_x_agent_subscriptions(["alice"], {
            "hot_topic_enabled": False,
            "market_sentiment_enabled": False,
            "project_promotion_enabled": True,
        })

        account = value.add_x_agent_account("alice", "New name")
        assert account["displayName"] == "New name"
        assert account["hotTopicEnabled"] is False
        assert account["marketSentimentEnabled"] is False
        assert account["projectPromotionEnabled"] is True
        assert account["status"] == "followed"
    finally:
        value.close()


def test_missing_model_route_creates_a_visible_failed_job(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        value.x_agent_analyzer = None
        value.x_agent_analyzer_error = "RuntimeError: missing test model route"
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "missing-route", "BTC bullish")], None, iso(utc_now()))

        assert value.process_x_agent_jobs() == 1
        job = value.db.execute("SELECT status,last_error FROM x_agent_analysis_jobs WHERE tweet_id='missing-route'").fetchone()
        account = value.list_x_agent_accounts()["items"][0]
        assert job["status"] == "failed"
        assert "missing test model route" in job["last_error"]
        assert "missing test model route" in account["lastAnalysisError"]
    finally:
        value.close()


def test_missing_model_route_reclaims_an_abandoned_processing_job(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "abandoned", "BTC bullish")], None, iso(utc_now()))
        with value.db:
            value.db.execute(
                "UPDATE x_agent_analysis_jobs SET status='processing',attempts=1,updated_at=? WHERE tweet_id='abandoned'",
                (iso(utc_now() - timedelta(minutes=6)),),
            )
        value.x_agent_analyzer = None
        value.x_agent_analyzer_error = "RuntimeError: missing test model route"

        assert value.process_x_agent_jobs() == 1
        job = value.db.execute("SELECT status,last_error FROM x_agent_analysis_jobs WHERE tweet_id='abandoned'").fetchone()
        assert job["status"] == "failed"
        assert "missing test model route" in job["last_error"]
    finally:
        value.close()


def test_disabling_a_subscription_skips_its_pending_work(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "unsubscribe", "BTC bullish")], None, iso(utc_now()))

        value.set_x_agent_subscriptions(
            ["alice"],
            {"hot_topic_enabled": False, "market_sentiment_enabled": False},
        )
        inbox = value.db.execute("SELECT processed_at FROM hottopic_inbox WHERE tweet_id='unsubscribe'").fetchone()
        job = value.db.execute("SELECT status FROM x_agent_analysis_jobs WHERE tweet_id='unsubscribe'").fetchone()
        assert inbox["processed_at"] is not None
        assert job["status"] == "ignored"
    finally:
        value.close()


def test_disabling_a_subscription_clears_its_failed_work_and_retention_pin(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "failed-unsubscribe", "BTC bullish")], None, iso(utc_now()))
        with value.db:
            value.db.execute(
                "UPDATE x_agent_analysis_jobs SET status='failed',last_error='upstream failed' WHERE tweet_id='failed-unsubscribe'"
            )

        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": False})
        job = value.db.execute(
            "SELECT status,last_error FROM x_agent_analysis_jobs WHERE tweet_id='failed-unsubscribe'"
        ).fetchone()
        assert job["status"] == "ignored"
        assert job["last_error"] is None
        account = value.list_x_agent_accounts()["items"][0]
        assert account["lastAnalysisError"] is None
        assert value.x_agent_dashboard()["accounts"]["errors"] == 0
    finally:
        value.close()


def test_rate_limited_collection_defers_the_shared_pacer_and_account(tmp_path: Path) -> None:
    class Pacer:
        def __init__(self) -> None:
            self.deferred: list[float] = []

        def defer(self, seconds: float) -> None:
            self.deferred.append(seconds)

    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        pacer = Pacer()
        value.collect_request_pacer = pacer  # type: ignore[assignment]
        before = utc_now()
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(
            row,
            [],
            {"kind": "rate_limited", "error": "HTTP Error 429", "retry_after_seconds": 900},
            iso(before),
        )

        account = value.db.execute(
            "SELECT next_due_at,consecutive_failures FROM hottopic_accounts WHERE screen_name_lower='alice'"
        ).fetchone()
        assert pacer.deferred == [900.0]
        assert account["consecutive_failures"] == 1
        assert datetime.fromisoformat(account["next_due_at"]) >= before + timedelta(seconds=890)
    finally:
        value.close()


def test_stale_processing_job_is_reclaimed_after_worker_restart(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        value.x_agent_analyzer = FakeAnalyzer()  # type: ignore[assignment]
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "stale", "BTC bullish")], None, iso(utc_now()))
        with value.db:
            value.db.execute(
                "UPDATE x_agent_analysis_jobs SET status='processing',attempts=1,updated_at=? WHERE tweet_id='stale'",
                (iso(utc_now() - timedelta(minutes=6)),),
            )

        assert value.process_x_agent_jobs() == 1
        job = value.db.execute("SELECT status,attempts FROM x_agent_analysis_jobs WHERE tweet_id='stale'").fetchone()
        assert job["status"] == "succeeded"
        assert job["attempts"] == 2
    finally:
        value.close()


def test_failed_job_keeps_its_inbox_source_for_manual_retry(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [content("alice", "retained", "BTC bullish")], None, iso(utc_now()))
        old = iso(utc_now() - timedelta(hours=49))
        with value.db:
            value.db.execute("UPDATE hottopic_inbox SET processed_at=?,collected_at=? WHERE tweet_id='retained'", (old, old))
            value.db.execute("UPDATE x_agent_analysis_jobs SET status='failed',completed_at=? WHERE tweet_id='retained'", (old,))

        value.maintain(force=True)
        assert value.db.execute("SELECT COUNT(*) FROM hottopic_inbox WHERE tweet_id='retained'").fetchone()[0] == 1
        value.x_agent_analyzer = FakeAnalyzer()  # type: ignore[assignment]
        assert value.retry_failed_x_agent_jobs() == 1
        assert value.process_x_agent_jobs() == 1
        assert value.db.execute("SELECT status FROM x_agent_analysis_jobs WHERE tweet_id='retained'").fetchone()[0] == "succeeded"
    finally:
        value.close()


def test_quote_only_content_is_not_attributed_to_the_tracking_account(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        value.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})
        value.x_agent_analyzer = FakeAnalyzer()  # type: ignore[assignment]
        item = content("alice", "quoted", "")
        item.activity_type = "quote"
        item.expanded_text = "引用 @other: BTC bullish with a long position"
        item.quote_text = "BTC bullish with a long position"
        row = AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice")
        value._store_poll(row, [item], None, iso(utc_now()))

        assert value.process_x_agent_jobs() == 1
        assert value.db.execute("SELECT status FROM x_agent_analysis_jobs WHERE tweet_id='quoted'").fetchone()[0] == "ignored"
        assert value.list_market_sentiment()["total"] == 0
    finally:
        value.close()


def test_concurrent_subscription_updates_share_the_sqlite_connection_safely(tmp_path: Path) -> None:
    value = service(tmp_path)
    try:
        value.add_x_agent_account("alice")
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [
                pool.submit(value.set_x_agent_subscriptions, ["alice"], {"market_sentiment_enabled": bool(index % 2)})
                for index in range(120)
            ]
            for future in futures:
                future.result()
        assert value.list_x_agent_accounts()["total"] == 1
    finally:
        value.close()
