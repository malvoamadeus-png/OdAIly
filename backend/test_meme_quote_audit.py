from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from packages.meme_dashboard import MemeDashboardStore
from packages.meme_scanner import quote_audit


def _scanner_db(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE observations (
              address TEXT, chain TEXT, symbol TEXT, last_market_cap REAL,
              highest_market_cap REAL, tracking_status TEXT
            );
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY, address TEXT, payload_json TEXT,
              reason TEXT, updated_at TEXT
            );
            CREATE TABLE market_cap_milestones (
              address TEXT, chain TEXT, level REAL
            );
            """
        )
        connection.executemany(
            "INSERT INTO observations VALUES(?,?,?,?,?,'active')",
            [
                ("SoNear", "solana", "NEAR", 900_000, 900_000),
                ("SoConsole", "solana", "CONSOLE", 100_000, 100_000),
                ("SoBase", "solana", "BASE", 100_000, 100_000),
            ],
        )
        connection.execute(
            "INSERT INTO jobs VALUES(1,'SoConsole',?,'', '2026-11-03T00:00:00+00:00')",
            (json.dumps({"chain": "solana"}),),
        )


class FakeQuoteClient:
    def __init__(self) -> None:
        self.sampled_addresses: list[str] = []

    def jupiter_prices(self, addresses: list[str]) -> dict[str, dict[str, float]]:
        return {address: {"usdPrice": 0.001, "liquidity": 5000, "blockId": 123} for address in addresses}

    def dexscreener(self, address: str) -> dict[str, object]:
        self.sampled_addresses.append(address)
        return {
            "pairAddress": "PoolA",
            "priceUsd": "0.001",
            "marketCap": 1_000_000,
            "liquidity": {"usd": 10_000},
        }

    def gecko_pools(self, address: str) -> list[dict[str, object]]:
        return [{
            "id": "solana_PoolA",
            "attributes": {
                "address": "PoolA",
                "token_price_usd": "0.00101",
                "fdv_usd": "1010000",
                "reserve_in_usd": "9000",
                "volume_usd": {"m5": "100"},
            },
        }]

    def token_supply(self, address: str) -> tuple[float, int, str, None]:
        return 1_000_000_000, 6, "success", None


def test_candidate_priority_and_milestone_window(tmp_path: Path) -> None:
    scanner_db = tmp_path / "scanner.sqlite3"
    _scanner_db(scanner_db)

    candidates = {row["address"]: row for row in quote_audit.active_candidates(scanner_db)}

    assert candidates["SoConsole"]["priority"] == 2
    assert candidates["SoConsole"]["priority_reason"] == "console"
    assert candidates["SoNear"]["priority"] == 1
    assert candidates["SoNear"]["priority_reason"] == "near_milestone"
    assert candidates["SoBase"]["priority"] == 0


def test_sample_batch_persists_three_source_market_caps_and_prioritizes_console(tmp_path: Path) -> None:
    scanner_db = tmp_path / "scanner.sqlite3"
    _scanner_db(scanner_db)
    audit_db = tmp_path / "audit.sqlite3"
    settings = quote_audit.Settings(scanner_db, audit_db, max_batch_size=1)
    store = quote_audit.AuditStore(audit_db)
    client = FakeQuoteClient()
    try:
        summary = quote_audit.sample_batch(
            settings,
            store,
            client,  # type: ignore[arg-type]
            now=quote_audit.datetime(2026, 11, 3, tzinfo=quote_audit.UTC),
        )
        row = store.conn.execute("SELECT * FROM quote_samples").fetchone()
    finally:
        store.close()

    assert summary["sampled"] == 1
    assert summary["backlog"] == 2
    assert client.sampled_addresses == ["SoConsole"]
    assert row["dex_market_cap"] == 1_000_000
    assert row["gecko_market_cap"] == 1_010_000
    assert row["jupiter_market_cap"] == 1_000_000
    assert row["gecko_pool_matched"] == 1
    assert row["comparison_status"] == "aligned"


def test_gecko_pool_match_falls_back_to_highest_reserve() -> None:
    pools = [
        {"attributes": {"address": "small", "reserve_in_usd": "100"}},
        {"attributes": {"address": "large", "reserve_in_usd": "500"}},
    ]

    selected, matched = quote_audit._selected_gecko_pool(pools, "missing")

    assert selected["attributes"]["address"] == "large"
    assert matched is False
    selected, matched = quote_audit._selected_gecko_pool(pools, "SMALL")
    assert selected["attributes"]["address"] == "small"
    assert matched is True


def test_gecko_price_uses_the_pool_side_matching_the_mint() -> None:
    pool = {
        "attributes": {"base_token_price_usd": "2", "quote_token_price_usd": "0.001"},
        "relationships": {
            "base_token": {"data": {"id": "solana_OtherMint"}},
            "quote_token": {"data": {"id": "solana_TargetMint"}},
        },
    }

    assert quote_audit._gecko_token_price(pool, "TargetMint") == 0.001
    assert quote_audit._gecko_token_price(pool, "OtherMint") == 2


def test_token_supply_uses_raw_amount_and_decimals(tmp_path: Path) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"result": {"value": {"amount": "1234500", "decimals": 6}}}

    class Session:
        headers: dict[str, str] = {}

        def post(self, *args: object, **kwargs: object) -> Response:
            return Response()

    store = quote_audit.AuditStore(tmp_path / "audit.sqlite3")
    try:
        client = quote_audit.QuoteAuditClient(2, store, Session())  # type: ignore[arg-type]
        supply, decimals, status, error = client.token_supply("SoMint")
    finally:
        store.close()

    assert (supply, decimals, status, error) == (1.2345, 6, "success", None)


def test_audit_store_adds_retry_column_to_older_database(tmp_path: Path) -> None:
    path = tmp_path / "old-audit.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE quote_samples (
            id INTEGER PRIMARY KEY,address TEXT NOT NULL,symbol TEXT NOT NULL,priority INTEGER NOT NULL,
            priority_reason TEXT NOT NULL,sampled_at TEXT NOT NULL,dex_status TEXT NOT NULL,dex_price REAL,
            dex_market_cap REAL,dex_pair_address TEXT,dex_liquidity REAL,gecko_status TEXT NOT NULL,
            gecko_price REAL,gecko_market_cap REAL,gecko_pool_address TEXT,gecko_pool_reserve REAL,
            gecko_pool_matched INTEGER NOT NULL,gecko_volume_m5 REAL,jupiter_status TEXT NOT NULL,
            jupiter_price REAL,jupiter_market_cap REAL,jupiter_liquidity REAL,jupiter_block_id INTEGER,
            supply REAL,supply_status TEXT NOT NULL,divergence_ratio REAL,comparison_status TEXT NOT NULL,
            errors_json TEXT NOT NULL)"""
        )

    store = quote_audit.AuditStore(path)
    try:
        columns = {row[1] for row in store.conn.execute("PRAGMA table_info(quote_samples)")}
    finally:
        store.close()

    assert "retry_after" in columns


def test_legacy_quote_audit_database_is_not_read_by_console(tmp_path: Path, monkeypatch) -> None:
    scanner_db = tmp_path / "scanner.sqlite3"
    audit_db = tmp_path / "audit.sqlite3"
    address = "SoConsole"
    with sqlite3.connect(scanner_db) as connection:
        connection.executescript(
            """
            CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,
              payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,
              title TEXT,content TEXT,updated_at TEXT
            );
            CREATE TABLE tg_candidates (id INTEGER PRIMARY KEY,mention_count INTEGER,chat_count INTEGER,sender_count INTEGER);
            """
        )
        connection.execute(
            "INSERT INTO jobs VALUES(1,?,'market_cap:solana:SoConsole:1000000',1000000,?,'market_cap_milestone',"
            "'2026-11-03T00:00:00+00:00','publisher_pending',NULL,'title','content','2026-11-03T00:01:00+00:00')",
            (address, json.dumps({"chain": "solana", "symbol": "TEST", "market_cap": 1_000_000})),
        )
    store = quote_audit.AuditStore(audit_db)
    try:
        store.save_sample({
            "address": address, "symbol": "TEST", "priority": 2, "priority_reason": "console",
            "sampled_at": "2026-11-03T00:02:00+00:00",
            "dex_status": "success", "dex_price": 0.001, "dex_market_cap": 1_000_000,
            "dex_pair_address": "PoolA", "dex_liquidity": 10_000,
            "gecko_status": "success", "gecko_price": 0.00101, "gecko_market_cap": 1_010_000,
            "gecko_pool_address": "PoolA", "gecko_pool_reserve": 9_000, "gecko_pool_matched": 1,
            "gecko_volume_m5": 100, "jupiter_status": "success", "jupiter_price": 0.001,
            "jupiter_market_cap": 1_000_000, "jupiter_liquidity": 5_000, "jupiter_block_id": 123,
            "supply": 1_000_000_000, "supply_status": "success", "divergence_ratio": 1.01,
            "comparison_status": "aligned", "errors_json": "{}",
        })
    finally:
        store.close()
    monkeypatch.setenv("MEME_SCANNER_DB_PATH", str(scanner_db))
    monkeypatch.setenv("MEME_QUOTE_AUDIT_DB_PATH", str(audit_db))

    dashboard = MemeDashboardStore(scanner_db).dashboard()

    item = dashboard["items"][0]
    assert "quote_audit" not in item
    assert item["market_verification"] is None
    assert "quote_audit_summary" not in dashboard


def test_dashboard_remains_available_without_quote_audit_database(tmp_path: Path, monkeypatch) -> None:
    scanner_db = tmp_path / "scanner.sqlite3"
    with sqlite3.connect(scanner_db) as connection:
        connection.executescript(
            """CREATE TABLE jobs (
              id INTEGER PRIMARY KEY,address TEXT,trigger_key TEXT,trigger_level REAL,
              payload_json TEXT,trigger_kind TEXT,queued_at TEXT,status TEXT,reason TEXT,
              title TEXT,content TEXT,updated_at TEXT
            );
            CREATE TABLE tg_candidates (id INTEGER PRIMARY KEY,mention_count INTEGER,chat_count INTEGER,sender_count INTEGER);"""
        )
    monkeypatch.setenv("MEME_QUOTE_AUDIT_DB_PATH", str(tmp_path / "not-created.sqlite3"))

    dashboard = MemeDashboardStore(scanner_db).dashboard()

    assert dashboard["available"] is True
    assert "quote_audit_summary" not in dashboard
