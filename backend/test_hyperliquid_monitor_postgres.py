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


def test_postgres_metadata_decodes_outside_connection_in_autocommit_mode(monkeypatch):
    events: list[str] = []

    class FakeResult:
        def fetchone(self):
            return {"payload": '{"ok": true}'}

    class FakeConn:
        def execute(self, sql, params=()):
            events.append("execute")
            return FakeResult()

    store = PostgresNewsStore.__new__(PostgresNewsStore)

    @contextmanager
    def fake_connect(*, autocommit: bool = False):
        events.append(f"enter:autocommit={autocommit}")
        yield FakeConn()
        events.append("exit")

    real_loads = json.loads

    def tracking_loads(raw):
        events.append("json.loads")
        return real_loads(raw)

    store._connect = fake_connect
    monkeypatch.setattr("packages.hyperliquid_news.postgres_store.json.loads", tracking_loads)

    assert store.metadata("account_pending_fills:0xabc", {}) == {"ok": True}
    assert events == ["enter:autocommit=True", "execute", "exit", "json.loads"]


def test_save_address_block_clears_pending_fills_in_sqlite_and_postgres(tmp_path):
    addr = "0x1111111111111111111111111111111111111111"
    sqlite_store = NewsStore(tmp_path / "block.sqlite")
    sqlite_store.initialize()
    sqlite_store.discover(addr, source={"kind": "test"}, now_ms=1_000, limit=None)
    sqlite_store.save_global("account_pending_fills:" + addr, {"fills": [{"tid": 1}]}, [])
    sqlite_store.save_global("normal_pending_fills:" + addr, {"fills": [{"tid": 2}]}, [])
    sqlite_store.save_address_block(addr, True, updated_by="tester")
    assert sqlite_store.metadata("account_pending_fills:" + addr, {}) == {}
    assert sqlite_store.metadata("normal_pending_fills:" + addr, {}) == {}

    executed: list[tuple[str, tuple]] = []

    class FakeResult:
        def __init__(self, row=None):
            self._row = row

        def fetchone(self):
            return self._row

        def fetchall(self):
            return []

    class FakeConn:
        def execute(self, sql, params=()):
            executed.append((sql, tuple(params) if isinstance(params, (list, tuple)) else (params,)))
            if "FROM hl_news_addresses WHERE address" in sql:
                return FakeResult({"exists": 1})
            return FakeResult(None)

    pg_store = PostgresNewsStore.__new__(PostgresNewsStore)

    @contextmanager
    def fake_connect(*, autocommit: bool = False):
        yield FakeConn()

    pg_store._connect = fake_connect
    pg_store.save_address_block(addr, True, updated_by="tester")
    deleted = [(sql, params) for sql, params in executed if sql.startswith("DELETE FROM hl_news_metadata")]
    assert ("DELETE FROM hl_news_metadata WHERE key=%s", ("account_pending_fills:" + addr,)) in deleted
    assert ("DELETE FROM hl_news_metadata_large WHERE key=%s", ("account_pending_fills:" + addr,)) in deleted
    assert ("DELETE FROM hl_news_metadata WHERE key=%s", ("normal_pending_fills:" + addr,)) in deleted
    assert ("DELETE FROM hl_news_metadata_large WHERE key=%s", ("normal_pending_fills:" + addr,)) in deleted


def test_normal_monitor_prunes_expired_pending_fills_outside_window(tmp_path):
    addr = "0x2222222222222222222222222222222222222222"
    now_ms = 200 * 86_400_000
    window_ms = 7 * 86_400_000
    old_fill = {"coin": "BTC", "time": now_ms - window_ms - 10_000, "tid": 1, "hash": "0x1", "side": "B", "sz": "1", "px": "100", "dir": "Open Long", "startPosition": "0", "closedPnl": "0", "fee": "0"}
    fresh_fill = {"coin": "BTC", "time": now_ms - 60_000, "tid": 2, "hash": "0x2", "side": "B", "sz": "1", "px": "100", "dir": "Open Long", "startPosition": "1", "closedPnl": "0", "fee": "0"}
    new_fill = {"coin": "BTC", "time": now_ms - 10_000, "tid": 3, "hash": "0x3", "side": "B", "sz": "1", "px": "100", "dir": "Open Long", "startPosition": "2", "closedPnl": "0", "fee": "0"}

    store = NewsStore(tmp_path / "prune.sqlite")
    store.initialize()
    store.discover(addr, source={"kind": "test"}, now_ms=now_ms - 1_000, limit=None)
    store.save_global(
        "normal_pending_fills:" + addr,
        {"coverage": {"pagination_complete": False, "next_start_ms": now_ms - 30_000}, "fills": [old_fill, fresh_fill]},
        [],
    )

    class PagingClient(FakeClient):
        def recent_trades(self, coin):
            return []

        def history(self, address, start_ms, end_ms, *, max_pages=6):
            return [new_fill], {"pagination_complete": False, "next_start_ms": end_ms - 5_000, "reason": "page_budget"}

    monitor = NewsMonitor(
        store=store,
        client=PagingClient(),
        leaderboard_seeds=0,
        addresses_per_pass=1,
        history_window_ms=window_ms,
    )
    report = monitor.run_once(now_ms=now_ms)
    assert report["incomplete_history"] == [addr]
    saved = store.metadata("normal_pending_fills:" + addr, {})
    assert [f["tid"] for f in saved["fills"]] == [2, 3]


def test_account_monitor_prunes_expired_pending_fills_outside_day_window(tmp_path):
    from packages.hyperliquid_news.account_monitor import AccountProfitMonitor, DAY

    addr = "0x3333333333333333333333333333333333333333"
    started = 10 * DAY
    now_ms = 20 * DAY
    expired_fill = {"coin": "ETH", "time": now_ms - DAY - 60_000, "tid": 10, "hash": "0x10", "side": "A", "sz": "1", "px": "100", "dir": "Close Long", "startPosition": "10", "closedPnl": "0", "fee": "0"}
    fresh_fill = {"coin": "ETH", "time": now_ms - 60_000, "tid": 11, "hash": "0x11", "side": "A", "sz": "1", "px": "100", "dir": "Close Long", "startPosition": "9", "closedPnl": "0", "fee": "0"}
    new_fill = {"coin": "ETH", "time": now_ms - 10_000, "tid": 12, "hash": "0x12", "side": "A", "sz": "1", "px": "100", "dir": "Close Long", "startPosition": "8", "closedPnl": "0", "fee": "0"}

    store = NewsStore(tmp_path / "acct_prune.sqlite")
    store.initialize()
    store.save_global("production_account_profit", {"enabled": True, "started_ms": started, "enabled_scenarios": ["account_profit"]}, [])
    store.discover(addr, source={"kind": "manual"}, now_ms=started, limit=None)
    store.schedule_account(addr, now_ms=started, leaderboard_qualified=True)
    store.save_global("account_performance:" + addr, {"observed_ms": now_ms - 1_000, "windows": {"allTime": {"pnl": "25000000", "observed_ms": now_ms - 1_000}}}, [])
    store.commit_observation(
        address=addr,
        now_ms=started + 1_000,
        state={"observed_ms": started + 1_000, "positions": {"ETH": {"size": "10"}}, "account_production_started_ms": started, "account_monitoring_since_ms": started + 1_000, "account_history_checked_ms": started + 1_000},
        history={},
        coverage={"pagination_complete": True, "next_start_ms": started + 1_000},
        fills=[],
        candidates=[],
    )
    store.save_global(
        "account_pending_fills:" + addr,
        {"coverage": {"pagination_complete": False, "next_start_ms": now_ms - 30_000}, "fills": [expired_fill, fresh_fill]},
        [],
    )

    class PagingAccountClient(FakeClient):
        def leaderboard(self):
            return []

        def recent_trades(self, coin):
            return []

        def dex_names(self):
            return [""]

        def markets(self, dex):
            return {"ETH": {"dex": "", "mark": "100"}}

        def portfolio(self, address):
            return [["perpAllTime", {"pnlHistory": [[now_ms, "25000000"]]}]]

        def positions(self, address, dex):
            return {"assetPositions": [{"position": {"coin": "ETH", "szi": "8", "positionValue": "800", "unrealizedPnl": "0", "liquidationPx": None}}]}

        def history(self, address, start_ms, end_ms, **kwargs):
            return [new_fill], {"pagination_complete": False, "next_start_ms": end_ms - 5_000, "reason": "page_budget"}

    monitor = AccountProfitMonitor(store, PagingAccountClient())
    report = monitor.run_once(now_ms=now_ms)
    assert report["incomplete_history"] == [addr]
    saved = store.metadata("account_pending_fills:" + addr, {})
    assert [f["tid"] for f in saved["fills"]] == [11, 12]
