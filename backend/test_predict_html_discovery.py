import json
from datetime import UTC, datetime, timedelta

from packages.predict_market_discovery.html_feed import parse_detail, parse_feed
from packages.predict_market_discovery.html_scan import HtmlDiscoveryStore, daily_filter_reason, scan_once


def test_parse_html_feed_extracts_server_rendered_categories():
    page = {"pages": [{"totalCount": 19, "pageInfo": {"hasNextPage": True},
                       "edges": [{"node": {"id": "abc", "slug": "abc", "title": "题目"}}]}]}
    state = ('{"dehydratedAt":123,"state":{"data":' + json.dumps(page) +
             '},"queryKey":["categories","infinite",{},"MOST_POPULAR"]}')
    html = '<script>self.__next_f.push(' + json.dumps([1, state]) + ')</script>'
    result = parse_feed(html)
    assert result["nodes"][0]["title"] == "题目"
    assert result["total_count"] == 19
    assert result["has_next_page"] is True


def test_baselines_are_per_feed_and_new_topics_are_deduplicated(tmp_path):
    now = datetime(2026, 11, 17, 8, tzinfo=UTC)
    store = HtmlDiscoveryStore(tmp_path / "discovery.sqlite")
    node = lambda key, time: {"id": key, "slug": key, "title": key, "createdAt": time.isoformat()}
    old = node("old", now - timedelta(days=3))
    first = node("first", now - timedelta(hours=1))
    result = lambda nodes: {"nodes": nodes, "total_count": len(nodes), "has_next_page": False}
    scan_once(store, now=now, fetch=lambda feed: result([first]))
    fresh = node("fresh", now - timedelta(minutes=5))
    scan_once(store, now=now, fetch=lambda feed: result([first, fresh, old]))
    scan_once(store, now=now, fetch=lambda feed: result([first, fresh, old]))
    with store.connect() as con:
        rows = {row["category_id"]: row["status"] for row in con.execute("SELECT * FROM html_topics")}
    assert rows == {"first": "baseline", "fresh": "pending", "old": "too_old"}


def test_failed_feed_does_not_establish_baseline(tmp_path):
    now = datetime(2026, 11, 17, 8, tzinfo=UTC)
    store = HtmlDiscoveryStore(tmp_path / "discovery.sqlite")
    def fetch(feed):
        if feed == "tech":
            raise ValueError("failed")
        return {"nodes": [], "has_next_page": False}
    scan_once(store, now=now, fetch=fetch)
    with store.connect() as con:
        assert con.execute("SELECT 1 FROM feed_baselines WHERE feed='tech'").fetchone() is None
        assert con.execute("SELECT status FROM html_scan_runs WHERE feed='tech'").fetchone()[0] == "failed"


def test_daily_filter_requires_date_for_non_variant_templates():
    assert daily_filter_reason({"slug": "renaiss-pokemon-index-up-or-down-nov-17", "title": "Renaiss 宝可梦指数涨跌 11月17日"})
    assert daily_filter_reason({"slug": "highest-temperature-in-tokyo-on-november-17-2026", "title": "11月17日东京最高气温"})
    assert daily_filter_reason({"title": "Bitcoin", "marketVariant": "CRYPTO_UP_DOWN"})
    assert daily_filter_reason({"slug": "will-ai-bubble-burst-by", "title": "AI泡沫会在2027年底前破裂吗"}) is None


def test_detail_follows_utf8_sized_flight_reference():
    rules = "结算规则：Yes。"
    category = json.dumps({"slug": "sample", "description": "$aa"})
    state = '{"dehydratedAt":123,"state":{"data":' + category + '}}\naa:T' + format(len(rules.encode()), 'x') + ',' + rules + '\nignored'
    html = '<script>self.__next_f.push(' + json.dumps([1, state]) + ')</script>'
    assert parse_detail(html, "sample")["description"] == rules


def test_writer_and_delivery_are_safe_on_uncertain_response(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from packages.predict_market_discovery import html_worker
    now = datetime.now(UTC)
    store = HtmlDiscoveryStore(tmp_path / 'discovery.sqlite')
    store.initialize()
    empty = {"nodes": [], "total_count": 0, "has_next_page": False}
    store.record_feed('tech', empty, now)
    node = {"id": "fresh", "slug": "fresh", "title": "Will A join B?", "createdAt": now.isoformat()}
    store.record_feed('tech', {"nodes": [node], "total_count": 1, "has_next_page": False}, now)
    monkeypatch.setattr(html_worker, 'fetch_detail', lambda slug: {'description': 'Official rules', 'category': {}})
    monkeypatch.setattr(html_worker, '_market_rules', lambda slug: [{'id': '1', 'question': 'Will A join B?', 'description': 'Official rules'}])
    settings = SimpleNamespace(writer_model='model', writer_reasoning_effort='low', push_endpoint='https://example.test', request_timeout_seconds=1)
    monkeypatch.setattr(html_worker, 'load_x_processing_settings', lambda: settings)
    writer = SimpleNamespace(generate_text=lambda **kwargs: json.dumps({'daily': False, 'reason': '', 'subject': 'A加入B', 'rules_summary': 'A加入B时结算为是', 'rules_complete': True}))
    monkeypatch.setattr(html_worker, '_writer_client', lambda _: writer)
    def fail_post(*args, **kwargs):
        raise html_worker.requests.Timeout('timeout')
    monkeypatch.setattr(html_worker.requests, 'post', fail_post)
    assert html_worker.process_pending(store)[0]['status'] == 'delivery_uncertain'
    assert html_worker.process_pending(store) == []
    with store.connect() as con:
        row = con.execute("SELECT * FROM html_topics WHERE category_id='fresh'").fetchone()
        assert 'predict.fun上新' in row['final_title']
        assert row['status'] == 'delivery_uncertain'
