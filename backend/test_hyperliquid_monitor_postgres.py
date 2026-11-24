import json
from contextlib import contextmanager

from packages.hyperliquid_news.postgres_naming import PostgresNamingStore
from packages.hyperliquid_news.postgres_store import PostgresNewsStore, address_not_blocked_sql
from packages.hyperliquid_news.service import NewsMonitor
from packages.hyperliquid_news.store import NewsStore
from test_hyperliquid_news import FakeClient


def test_normal_monitor_accepts_decoded_postgres_address_sources(tmp_path, monkeypatch):
    store = NewsStore(tmp_path / "test.sqlite")
    store.initialize()
    addresses = store.addresses

    def postgres_shaped_addresses(*args, **kwargs):
        return [{**row, "sources": json.loads(row["sources"])} for row in addresses(*args, **kwargs)]

    monkeypatch.setattr(store, "addresses", postgres_shaped_addresses)
    monitor = NewsMonitor(store=store, client=FakeClient(), leaderboard_seeds=0, addresses_per_pass=1)
    result = monitor.run_once(now_ms=100000)
    assert result["processed"] == 1
    assert result["failed"] == {}


def test_postgres_address_block_filter_guards_payload_casts_by_key_prefix():
    clause = address_not_blocked_sql("n.address")
    assert "hl_news_metadata b" in clause
    assert "hl_news_metadata_large b" in clause
    assert "CASE WHEN starts_with(b.key,'address_block:') THEN b.payload->>'blocked'='true' ELSE false END" in clause
    assert "CASE WHEN starts_with(b.key,'address_block:') THEN (b.payload::jsonb)->>'blocked'='true' ELSE false END" in clause


def test_postgres_naming_claim_due_and_store_queries_use_guarded_block_filter():
    executed: list[str] = []

    class FakeResult:
        def fetchall(self):
            return []

        def fetchone(self):
            return {"count": 0}

    class FakeConn:
        def execute(self, sql, params=()):
            executed.append(sql)
            return FakeResult()

    store = PostgresNewsStore.__new__(PostgresNewsStore)

    @contextmanager
    def fake_connect(*, autocommit: bool = False):
        yield FakeConn()

    store._connect = fake_connect
    naming = PostgresNamingStore(store)

    assert naming.claim_due(now_ms=1000, limit=4) == []
    assert store.addresses(5, normal_schedule=False) == []
    assert store.addresses(5, normal_schedule=True) == []
    assert store.recent_states(0) == {}
    assert store.account_due(1000, 3) == []

    block_queries = [sql for sql in executed if "address_block:" in sql]
    assert len(block_queries) == 7
    for sql in block_queries:
        assert "CASE WHEN starts_with(b.key,'address_block:') THEN (b.payload::jsonb)->>'blocked'='true' ELSE false END" in sql
        assert "AND (b.payload::jsonb)->>'blocked'='true'" not in sql
