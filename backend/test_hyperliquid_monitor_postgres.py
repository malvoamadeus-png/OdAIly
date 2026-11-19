import json

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
