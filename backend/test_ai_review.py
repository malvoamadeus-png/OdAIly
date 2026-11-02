from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from packages.ai_review import (
    ATTITUDE_RE,
    DAILY_TOKEN_LIMIT,
    AIReviewRepository,
    AIReviewService,
    _compile_alias_matcher,
    _find_alias_matches,
    _match_alias,
)
from packages.hottopic.service import HotTopicService


class FakeAnalyzer:
    def analyze(self, item, examples):
        return {
            "recommendation": "consider",
            "reason": "已有多条独立原文证据。",
            "model": "gpt-6-luna",
            "input_tokens": 100,
            "output_tokens": 20,
        }


def _create_main_database(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE tasks(id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,source_url TEXT,title TEXT,
          content TEXT,published_at TEXT,created_at TEXT,metadata TEXT,raw_payload TEXT,status TEXT);
        CREATE TABLE newsflash_items(id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,source_url TEXT,
          title TEXT,content TEXT,published_at TEXT);
        CREATE TABLE newsflash_events(event_id TEXT PRIMARY KEY,representative_title TEXT,event_time TEXT,
          first_published_at TEXT,first_source TEXT,first_sources TEXT,competitor_source_count INTEGER,
          has_odaily INTEGER,status TEXT);
        CREATE TABLE newsflash_event_sources(event_id TEXT,item_id INTEGER,source TEXT,source_item_id TEXT);
        CREATE TABLE newsflash_production_metadata(delivery_key TEXT,odaily_id TEXT,origin_key TEXT,
          outlet_category TEXT,pushed_at TEXT);
        CREATE TABLE x_capture_accounts(username_lower TEXT PRIMARY KEY,enabled INTEGER DEFAULT 1);
        """
    )
    connection.commit()
    return connection


def _add_event(conn, event_id, sources, *, odaily_publish=None):
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO newsflash_events VALUES(?,?,?,?,?,?,?,?,?)",
        (event_id, f"Event {event_id}", now.isoformat(), now.isoformat(), sources[0][0],
         json.dumps([item[0] for item in sources]), sum(item[0] != "odaily" for item in sources),
         int(any(item[0] == "odaily" for item in sources)), "active"),
    )
    for index, (source, item_id, published) in enumerate(sources):
        source_row_id = int(event_id[-1]) * 10 + index + 1
        conn.execute(
            "INSERT INTO newsflash_items VALUES(?,?,?,?,?,?,?)",
            (source_row_id, source, item_id, f"https://{source}.example/news/{item_id}",
             f"{source} report", f"Body from {source} @editor https://example.org/story", published),
        )
        conn.execute(
            "INSERT INTO newsflash_event_sources VALUES(?,?,?,?)",
            (event_id, source_row_id, source, item_id),
        )
    if odaily_publish:
        status, odaily_id = odaily_publish
        task_id = int(event_id[-1])
        conn.execute("INSERT INTO tasks(id,status) VALUES(?,?)", (task_id, status))
        conn.execute(
            "INSERT INTO newsflash_production_metadata VALUES(?,?,?,?,?)",
            (f"odaily-task-{task_id}", odaily_id, "competitor", "ai_regular", now.isoformat()),
        )


def _create_hot_database(path: Path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE x_agent_sentiment_results(instrument_key TEXT,instrument_name TEXT,ticker TEXT,scope TEXT,posted_at TEXT);
        CREATE TABLE x_agent_project_observations(identity_key TEXT,project_name TEXT,ticker TEXT,chain_name TEXT,last_mentioned_at TEXT);
        CREATE TABLE hottopic_accounts(screen_name_lower TEXT PRIMARY KEY,status TEXT DEFAULT 'followed',
          hot_topic_enabled INTEGER DEFAULT 0,market_sentiment_enabled INTEGER,
          project_promotion_enabled INTEGER,investment_leads_enabled INTEGER DEFAULT 0);
        CREATE TABLE hottopic_inbox(tweet_id TEXT PRIMARY KEY,screen_name_lower TEXT,payload_json TEXT,collected_at TEXT);
        """
    )
    connection.commit()
    return connection


def test_ticker_matching_requires_complete_word_boundary():
    assert _match_alias("BTC is bullish", "BTC")
    assert _match_alias("$BTC is bullish", "btc")
    assert not _match_alias("BTCXYZ is bullish", "BTC")
    assert not _match_alias("ABTC is bullish", "BTC")
    assert _match_alias("比特币看多", "比特币")
    assert _match_alias("Bitcoin is bullish", "bitcoin")
    assert ATTITUDE_RE.search("Bitcoin is bullish")
    assert not ATTITUDE_RE.search("a longer story about a shortage")
    matcher = _compile_alias_matcher({"btc": "bitcoin", "bitcoin": "bitcoin", "比特币": "bitcoin"})
    assert _find_alias_matches("$BTC, Bitcoin and 比特币; BTCXYZ", matcher) == {"btc", "bitcoin", "比特币"}
    assert _find_alias_matches("bitcoin", _compile_alias_matcher({"coin": "coin", "bitcoin": "bitcoin"})) == {"bitcoin"}
    assert _find_alias_matches("STRASSE", _compile_alias_matcher({"straße": "street"})) == {"strasse"}


def test_event_queue_includes_only_three_supported_event_scenarios():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        now = datetime.now(UTC)
        _add_event(main, "event-1", [
            ("blockbeats", "bb1", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily1", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily1"))
        _add_event(main, "event-2", [
            ("blockbeats", "bb2", (now - timedelta(hours=2)).isoformat()),
            ("panews", "pa2", (now - timedelta(hours=1)).isoformat()),
        ])
        _add_event(main, "event-3", [("odaily", "odaily3", now.isoformat())])
        _add_event(main, "event-4", [
            ("blockbeats", "bb4", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily4", (now - timedelta(hours=1)).isoformat()),
        ])
        _add_event(main, "event-5", [("panews", "pa5", now.isoformat())])
        _add_event(main, "event-6", [
            ("blockbeats", "bb6", (now - timedelta(hours=2)).isoformat()),
            ("panews", "pa6", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily6", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("ready_review", "odaily6"))
        main.commit()
        main.close()
        hot.close()

        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            assert service._queue_events(now - timedelta(days=7)) == 4
            items = [
                service.get_item(f"event:event-{index}:" + next(
                    category for category in ("competitor_first_ai", "multiple_competitors_no_odaily", "odaily_only")
                    if service.get_item(f"event:event-{index}:{category}")
                ))
                for index in (1, 2, 3, 6)
            ]
            categories = {item["category"] for item in items}
            assert categories == {"competitor_first_ai", "multiple_competitors_no_odaily", "odaily_only"}
            first = next(item for item in items if "直发" in item["title"])
            assert any("挂后台" in item["title"] for item in items)
            assert "editor" in first["clues"]["xAccounts"]
            assert "example.org" in first["clues"]["domains"]
        finally:
            service.close()


def test_event_queue_marks_reviewed_publication_and_ignores_parallel_first_publish():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        now = datetime.now(UTC)
        _add_event(main, "event-1", [
            ("blockbeats", "bb-first", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily-first", (now - timedelta(hours=2)).isoformat()),
        ], odaily_publish=("auto_published", "odaily-first"))
        main.commit()
        main.close()
        hot.close()

        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            assert service._queue_events(now - timedelta(days=7)) == 0
            assert service.get_item("event:event-1:competitor_first_ai") is None
        finally:
            service.close()


def test_event_queue_marks_ai_background_publish_and_keeps_source_attribution():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        now = datetime.now(UTC)
        _add_event(main, "event-2", [
            ("blockbeats", "bb-first", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily-second", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("ready_review", "odaily-second"))
        main.commit()
        main.close()
        hot.close()
        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            assert service._queue_events(now - timedelta(days=7)) == 1
            item = service.get_item("event:event-2:competitor_first_ai")
            assert "挂后台" in item["title"]
            assert all("blockbeats" in evidence["url"] or "odaily" in evidence["url"] for evidence in item["evidence"])
        finally:
            service.close()


def test_event_queue_requires_an_untracked_x_clue_and_skips_tracked_or_hyperbot_sources():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        now = datetime.now(UTC)
        _add_event(main, "event-1", [
            ("hyperbot", "hb1", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily1", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily1"))
        _add_event(main, "event-2", [
            ("cointelegraph", "ct2", (now - timedelta(hours=2)).isoformat()),
            ("panews", "pa2", (now - timedelta(hours=1)).isoformat()),
        ])
        main.execute("UPDATE newsflash_items SET content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-2')")
        main.execute("UPDATE newsflash_items SET source_url='https://cointelegraph.com/story',content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-2' AND source='cointelegraph')")
        main.execute("UPDATE newsflash_items SET source_url='https://www.panewslab.com/story',content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-2' AND source='panews')")
        _add_event(main, "event-3", [
            ("cointelegraph", "ct3", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily3", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily3"))
        main.execute("UPDATE newsflash_items SET content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-3')")
        main.execute("UPDATE newsflash_items SET source_url='https://cointelegraph.com/story',content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-3' AND source='cointelegraph')")
        _add_event(main, "event-4", [
            ("cointelegraph", "ct4", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily4", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily4"))
        main.execute("UPDATE newsflash_items SET content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-4')")
        main.execute("UPDATE newsflash_items SET source_url='https://cointelegraph.com/story',content='Story cites @tracked_account' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-4' AND source='cointelegraph')")
        main.execute("INSERT INTO x_capture_accounts(username_lower) VALUES('tracked_account')")
        _add_event(main, "event-5", [
            ("cointelegraph", "ct5", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily5", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily5"))
        main.execute("UPDATE newsflash_items SET content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-5')")
        main.execute("UPDATE newsflash_items SET source_url='https://cointelegraph.com/story',content='Report from @new_source' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-5' AND source='cointelegraph')")
        main.commit()
        main.close()
        hot.close()
        service = AIReviewService(
            database_path=root / "review.sqlite", primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite", analyzer=FakeAnalyzer(),
        )
        try:
            assert service._queue_events(now - timedelta(days=3)) == 1
            item = service.get_item("event:event-5:competitor_first_ai")
            assert item is not None
            assert item["clues"]["xAccounts"] == ["new_source"]
            assert "cointelegraph.com" not in item["clues"]["domains"]
            assert "panewslab.com" not in item["clues"]["domains"]
            assert service.get_item("event:event-1:competitor_first_ai") is None
            assert service.get_item("event:event-2:multiple_competitors_no_odaily") is None
            assert service.get_item("event:event-3:competitor_first_ai") is None
            assert service.get_item("event:event-4:competitor_first_ai") is None
        finally:
            service.close()


def test_account_scan_uses_distinct_assets_and_skips_reposts_and_quote_text():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        now = datetime.now(UTC)
        for key, name, ticker in (
            ("Crypto具体标的:bitcoin", "Bitcoin", "BTC"),
            ("Crypto具体标的:ethereum", "Ethereum", "ETH"),
            ("Crypto具体标的:solana", "Solana", "SOL"),
        ):
            hot.execute(
                "INSERT INTO x_agent_sentiment_results VALUES(?,?,?,?,?)",
                (key, name, ticker, "Crypto具体标的", now.isoformat()),
            )
        posts = [
            ("alpha", "tweet-1", "BTC and Bitcoin bullish because the chain grows"),
            ("alpha", "tweet-2", "ETH bullish on protocol adoption"),
            ("alpha", "tweet-3", "SOL bullish and DeFi users grow"),
            ("two_only", "tweet-4", "BTC Bitcoin bullish"),
            ("two_only", "tweet-5", "ETH bullish"),
            ("retweeted", "tweet-6", "BTC ETH SOL bullish because price rises"),
            ("market_on", "tweet-7", "BTC bullish because users grow"),
            ("market_on", "tweet-8", "ETH bullish because adoption grows"),
            ("market_on", "tweet-9", "SOL bullish because usage grows"),
        ]
        for index, (account, tweet_id, content) in enumerate(posts):
            meta = {"account_username": account, "author_username": account}
            raw = {}
            if account == "retweeted":
                raw = {"raw_payload": {"timeline": {
                    "reposted_by": {"screen_name": "retweeted"},
                    "author": {"screen_name": "original_author"},
                }}}
            main.execute(
                "INSERT INTO tasks(id,source,source_item_id,source_url,title,content,published_at,created_at,metadata,raw_payload,status) "
                "VALUES(?, 'x', ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (index + 1, tweet_id, f"https://x.com/{account}/status/{tweet_id}", tweet_id, content,
                 now.isoformat(), now.isoformat(), json.dumps(meta), json.dumps(raw)),
            )
        hot.execute(
            "INSERT INTO hottopic_inbox VALUES(?,?,?,?)",
            ("quoted-tweet", "quoted", json.dumps({
                "activity_type": "quote", "text": "I disagree with this take",
                "quote_text": "BTC ETH SOL bullish because blockchain is great",
                "url": "https://x.com/quoted/status/quoted-tweet", "created_at_iso": now.isoformat(),
            }), now.isoformat()),
        )
        for account in ("alpha", "two_only", "retweeted", "quoted"):
            hot.execute(
                "INSERT INTO hottopic_accounts(screen_name_lower,hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled) "
                "VALUES(?,1,0,0)", (account,),
            )
        hot.execute(
            "INSERT INTO hottopic_accounts(screen_name_lower,market_sentiment_enabled,project_promotion_enabled) "
            "VALUES('market_on',1,1)"
        )
        main.commit()
        hot.commit()
        main.close()
        hot.close()
        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            assert _match_alias("BTCXYZ", "BTC") is False
            assert service._queue_accounts(now - timedelta(days=7)) == 2
            items = [
                item for key in ("account:alpha:market_sentiment", "account:alpha:project_promotion")
                if (item := service.get_item(key))
            ]
            categories = {(item["account_lower"], item["category"]) for item in items}
            assert ("alpha", "market_sentiment") in categories
            assert ("alpha", "project_promotion") in categories
            assert not any(item["account_lower"] in {"two_only", "retweeted", "quoted", "market_on"} for item in items)
            assert service._queue_accounts(now - timedelta(days=7)) == 0
            outsider = {
                "account_username": "not_agent",
                "author_username": "not_agent",
            }
            main = sqlite3.connect(root / "main.sqlite")
            main.execute(
                "INSERT INTO tasks(id,source,source_item_id,source_url,title,content,published_at,created_at,metadata,raw_payload,status) "
                "VALUES(99,'x','outsider','https://x.com/not_agent/status/outsider','outsider','BTC ETH SOL bullish because users grow',?,?,?,?, 'pending')",
                (now.isoformat(), now.isoformat(), json.dumps(outsider), "{}"),
            )
            main.commit()
            main.close()
            assert service._queue_accounts(now - timedelta(days=7)) == 0
        finally:
            service.close()


def test_daily_budget_reservation_and_usage_settlement():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            day = "2026-11-02"
            assert repository.update_usage(day, "account", reserve=DAILY_TOKEN_LIMIT - 100)
            assert not repository.update_usage(day, "account", reserve=101)
            repository.update_usage(day, "account", settle=(DAILY_TOKEN_LIMIT - 100, 20))
            usage = repository.usage(day)["account"]
            assert usage["usedTokens"] == 20
            assert usage["reservedTokens"] == 0
        finally:
            repository.close()


def test_history_examples_are_limited_to_matching_review_category():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            for index, category in enumerate(("odaily_only", "odaily_only", "multiple_competitors_no_odaily")):
                key = f"event:history-{index}:{category}"
                repository.insert_candidate(
                    key=key, item_type="event", category=category, title=category, evidence=[],
                )
                repository.decide(key, decision="accepted", actor="editor@example.com", note=category)
            examples = repository.history_examples("event", "odaily_only")
            assert len(examples) == 2
            assert all(example["category"] == "odaily_only" for example in examples)
            assert all(repository.get_item(f"event:history-{index}:{category}") for index, category in enumerate(("odaily_only", "odaily_only", "multiple_competitors_no_odaily")))
        finally:
            repository.close()


def test_queue_hides_unanalyzed_and_drop_recommendations():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            for key in ("visible", "drop", "queued"):
                repository.insert_candidate(
                    key=f"event:{key}:odaily_only", item_type="event", category="odaily_only",
                    title=key, evidence=[],
                )
            visible = repository.db.execute("SELECT id FROM ai_review_items WHERE item_key='event:visible:odaily_only'").fetchone()[0]
            dropped = repository.db.execute("SELECT id FROM ai_review_items WHERE item_key='event:drop:odaily_only'").fetchone()[0]
            repository.complete_analysis(visible, recommendation="consider", reason="可持续追踪", model="gpt-6-luna", input_tokens=10, output_tokens=2)
            repository.complete_analysis(dropped, recommendation="drop", reason="不跟进", model="gpt-6-luna", input_tokens=10, output_tokens=2)
            page = repository.list_items(item_type="event")
            assert page["total"] == 1
            assert [item["title"] for item in page["items"]] == ["visible"]
        finally:
            repository.close()


def test_daily_run_uses_three_day_window_and_continues_beyond_one_batch():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _create_main_database(root / "main.sqlite").close()
        _create_hot_database(root / "hot.sqlite").close()
        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            for index in range(501):
                service.repository.insert_candidate(
                    key=f"account:batch{index}:market_sentiment", item_type="account", category="market_sentiment",
                    title=f"@batch{index}", evidence=[], account_lower=f"batch{index}",
                )
            before_six = datetime(2026, 11, 2, 21, 59, tzinfo=UTC)
            at_six = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
            assert service.run_if_due(now=before_six)["status"] == "not_due"
            first = service.run_if_due(now=at_six)
            assert first["status"] == "in_progress"
            assert first["analyzed"] == 500
            assert service.repository.get_state("last_scan_date") == "2026-11-03"
            second = service.run_if_due(now=at_six + timedelta(seconds=2))
            assert second["status"] == "completed"
            assert second["analyzed"] == 1
            assert service.run_if_due(now=at_six + timedelta(hours=1))["status"] == "already_ran"
        finally:
            service.close()


def test_account_recommendation_can_be_accepted_without_changing_source_configuration():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = _create_main_database(root / "main.sqlite")
        hot = _create_hot_database(root / "hot.sqlite")
        main.close()
        hot.close()
        service = AIReviewService(
            database_path=root / "review.sqlite",
            primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite",
            analyzer=FakeAnalyzer(),
        )
        try:
            service.repository.insert_candidate(
                key="account:test:market_sentiment", item_type="account", category="market_sentiment",
                title="@test · market", evidence=[], account_lower="test",
            )
            accepted = service.decide("account:test:market_sentiment", decision="accepted", actor="editor@example.com")
            assert accepted["status"] == "accepted"
            assert accepted["humanDecision"] == "accepted"
            assert service.get_item("account:test:market_sentiment")["account_lower"] == "test"
        finally:
            service.close()


def test_new_hot_topic_review_account_only_opens_selected_modules():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE hottopic_accounts(screen_name TEXT,screen_name_lower TEXT PRIMARY KEY,display_name TEXT,
          status TEXT,next_due_at TEXT,hot_topic_enabled INTEGER,market_sentiment_enabled INTEGER,
          project_promotion_enabled INTEGER,investment_leads_enabled INTEGER,investment_leads_enabled_at TEXT,
          created_at TEXT,updated_at TEXT,cumulative_content_count INTEGER DEFAULT 0,cumulative_hot_topic_count INTEGER DEFAULT 0,
          last_polled_at TEXT,last_success_at TEXT,last_error TEXT,last_x_agent_analyzed_at TEXT,last_x_agent_error TEXT);
        """
    )
    service = HotTopicService.__new__(HotTopicService)
    service.db = connection
    service.lock = threading.RLock()
    service._update_x_agent_account_state = lambda *_args, **_kwargs: None
    service._update_x_agent_account_state = lambda *_args, **_kwargs: None
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,hot_topic_enabled,"
        "market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled,investment_leads_enabled_at,created_at,updated_at) "
        "VALUES('existing','existing','Existing','followed',?,1,0,0,1,?,?,?)",
        (now, now, now, now),
    )
    result = service.ensure_ai_review_account("candidate", market_sentiment=True, project_promotion=False)
    row = connection.execute(
        "SELECT hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled "
        "FROM hottopic_accounts WHERE screen_name_lower='candidate'"
    ).fetchone()
    assert result["marketSentimentEnabled"] is True
    assert tuple(row) == (0, 1, 0, 0)
    service.ensure_ai_review_account("existing", market_sentiment=True, project_promotion=False)
    existing = connection.execute(
        "SELECT hot_topic_enabled,market_sentiment_enabled,project_promotion_enabled,investment_leads_enabled "
        "FROM hottopic_accounts WHERE screen_name_lower='existing'"
    ).fetchone()
    assert tuple(existing) == (1, 1, 0, 1)
    connection.close()
