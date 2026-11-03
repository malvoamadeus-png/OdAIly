from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from packages.ai_review import DAILY_TOKEN_LIMIT, AIReviewRepository, AIReviewService, _event_source_key


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
          title TEXT,content TEXT,published_at TEXT,raw_payload TEXT,metadata TEXT);
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
        publisher_domains = {
            "odaily": "odaily.news", "blockbeats": "theblockbeats.info", "panews": "panewslab.com",
            "jinse": "jinse.cn", "cointelegraph": "cointelegraph.com",
        }
        article_url = f"https://{publisher_domains.get(source, source + '.example')}/news/{item_id}"
        conn.execute(
            "INSERT INTO newsflash_items VALUES(?,?,?,?,?,?,?,?,?)",
            (source_row_id, source, item_id, article_url,
             f"{source} report", f"Body from {source} @editor https://example.org/story", published,
             json.dumps({"link": f"https://origin.example/{source}/{item_id}"}), "{}"),
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


def _event_candidates(service, event_id, category=None):
    rows = service.repository.db.execute(
        "SELECT item_key,category FROM ai_review_items WHERE item_type='event' AND item_key LIKE ?",
        (f"event-source:{event_id}:%",),
    ).fetchall()
    return [
        service.get_item(row["item_key"])
        for row in rows
        if category is None or row["category"] == category
    ]


def test_event_queue_includes_competitor_scenarios_and_extracts_original_sources():
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
        main.execute(
            "UPDATE newsflash_items SET raw_payload=? WHERE source='blockbeats' AND source_item_id='bb1'",
            (json.dumps({"link": "https://x.com/editor/status/123"}),),
        )
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
            service.repository.insert_candidate(
                key="event:event-3:competitor_first_ai", category="competitor_first_ai",
                title="Legacy event candidate", evidence=[],
            )
            assert service._queue_events(now - timedelta(days=7)) == 6
            items = [item for index in (1, 2, 6) for item in _event_candidates(service, f"event-{index}")]
            categories = {item["category"] for item in items}
            assert categories == {"competitor_first_ai", "multiple_competitors_no_odaily"}
            first = service.get_item(_event_source_key("event-1", "competitor_first_ai", "x:editor"))
            assert any("挂后台" in item["title"] for item in items)
            assert "editor" in first["clues"]["xAccounts"]
            assert first["clues"]["domains"] == []
            domain_candidate = service.get_item(
                _event_source_key("event-1", "competitor_first_ai", "domain:example.org")
            )
            assert domain_candidate["clues"]["domains"] == ["example.org"]
            assert all(evidence["sourceKind"] == "competitor" for item in items for evidence in item["evidence"])
            assert all(
                len(evidence["originSources"]) == 1
                for item in items for evidence in item["evidence"]
            )
            assert service.get_item("event:event-3:competitor_first_ai") is None
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
            assert _event_candidates(service, "event-1") == []
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
            assert service._queue_events(now - timedelta(days=7)) == 2
            item = service.get_item(_event_source_key("event-2", "competitor_first_ai", "domain:origin.example"))
            assert "挂后台" in item["title"]
            assert all("blockbeats" in evidence["url"] for evidence in item["evidence"])
            assert any(origin["url"].startswith("https://origin.example/") for evidence in item["evidence"] for origin in evidence["originSources"])
        finally:
            service.close()


def test_event_queue_requires_linked_original_sources_and_skips_unlinked_mentions():
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
        main.execute("UPDATE newsflash_items SET raw_payload='{}' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id IN ('event-2','event-3','event-4'))")
        main.execute("INSERT INTO x_capture_accounts(username_lower) VALUES('tracked_account')")
        _add_event(main, "event-5", [
            ("cointelegraph", "ct5", (now - timedelta(hours=2)).isoformat()),
            ("odaily", "odaily5", (now - timedelta(hours=1)).isoformat()),
        ], odaily_publish=("auto_published", "odaily5"))
        main.execute("UPDATE newsflash_items SET content='Source report' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-5')")
        main.execute("UPDATE newsflash_items SET source_url='https://cointelegraph.com/story',content='Report from @new_source' WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-5' AND source='cointelegraph')")
        main.execute("UPDATE newsflash_items SET raw_payload=? WHERE id IN (SELECT item_id FROM newsflash_event_sources WHERE event_id='event-5' AND source='cointelegraph')", (json.dumps({"link": "https://x.com/new_source/status/456"}),))
        main.commit()
        main.close()
        hot.close()
        service = AIReviewService(
            database_path=root / "review.sqlite", primary_database_path=root / "main.sqlite",
            hottopic_database_path=root / "hot.sqlite", analyzer=FakeAnalyzer(),
        )
        try:
            assert service._queue_events(now - timedelta(days=3)) == 1
            item = service.get_item(_event_source_key("event-5", "competitor_first_ai", "x:new_source"))
            assert item is not None
            assert item["clues"]["xAccounts"] == ["new_source"]
            assert "cointelegraph.com" not in item["clues"]["domains"]
            assert "panewslab.com" not in item["clues"]["domains"]
            assert _event_candidates(service, "event-1") == []
            assert _event_candidates(service, "event-2") == []
            assert _event_candidates(service, "event-3") == []
            assert _event_candidates(service, "event-4") == []
        finally:
            service.close()


def test_daily_budget_reservation_and_usage_settlement():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            day = "2026-11-02"
            assert repository.update_usage(day, reserve=DAILY_TOKEN_LIMIT - 100)
            assert not repository.update_usage(day, reserve=101)
            repository.update_usage(day, settle=(DAILY_TOKEN_LIMIT - 100, 20))
            usage = repository.usage(day)["event"]
            assert usage["usedTokens"] == 20
            assert usage["reservedTokens"] == 0
        finally:
            repository.close()


def test_history_examples_are_limited_to_matching_review_category():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            for index, category in enumerate(("competitor_first_ai", "competitor_first_ai", "multiple_competitors_no_odaily")):
                key = f"event:history-{index}:{category}"
                repository.insert_candidate(
                    key=key, category=category, title=category, evidence=[],
                )
                repository.decide(key, decision="accepted", actor="editor@example.com", note=category)
            examples = repository.history_examples("competitor_first_ai")
            assert len(examples) == 2
            assert all(example["category"] == "competitor_first_ai" for example in examples)
            assert all(repository.get_item(f"event:history-{index}:{category}") for index, category in enumerate(("competitor_first_ai", "competitor_first_ai", "multiple_competitors_no_odaily")))
        finally:
            repository.close()


def test_queue_shows_only_completed_priority_recommendations():
    with TemporaryDirectory() as tmp:
        repository = AIReviewRepository(Path(tmp) / "review.sqlite")
        try:
            for key in ("visible", "consider", "drop", "queued"):
                repository.insert_candidate(
                    key=f"event:{key}:multiple_competitors_no_odaily", category="multiple_competitors_no_odaily",
                    title=key, evidence=[],
                )
            visible = repository.db.execute("SELECT id FROM ai_review_items WHERE item_key='event:visible:multiple_competitors_no_odaily'").fetchone()[0]
            consider = repository.db.execute("SELECT id FROM ai_review_items WHERE item_key='event:consider:multiple_competitors_no_odaily'").fetchone()[0]
            dropped = repository.db.execute("SELECT id FROM ai_review_items WHERE item_key='event:drop:multiple_competitors_no_odaily'").fetchone()[0]
            repository.complete_analysis(visible, recommendation="priority", reason="可持续追踪", model="gpt-6-luna", input_tokens=10, output_tokens=2)
            repository.complete_analysis(consider, recommendation="consider", reason="证据有限", model="gpt-6-luna", input_tokens=10, output_tokens=2)
            repository.complete_analysis(dropped, recommendation="drop", reason="不跟进", model="gpt-6-luna", input_tokens=10, output_tokens=2)
            page = repository.list_items()
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
            for index in range(51):
                service.repository.insert_candidate(
                    key=f"event:batch{index}:multiple_competitors_no_odaily",
                    category="multiple_competitors_no_odaily", title=f"batch{index}", evidence=[],
                )
            before_six = datetime(2026, 11, 2, 21, 59, tzinfo=UTC)
            at_six = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)
            assert service.run_if_due(now=before_six)["status"] == "not_due"
            first = service.run_if_due(now=at_six)
            assert first["status"] == "in_progress"
            assert first["analyzed"] == 50
            second = service.run_if_due(now=at_six + timedelta(seconds=2))
            assert second["status"] == "completed"
            assert second["analyzed"] == 1
            assert service.run_if_due(now=at_six + timedelta(hours=1))["status"] == "already_ran"
        finally:
            service.close()
