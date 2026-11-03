from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from packages.common.paths import get_paths


DEXSCREENER_URL = "https://api.dexscreener.com"
GECKOTERMINAL_URL = "https://api.geckoterminal.com/api/v2"
JUPITER_URL = "https://lite-api.jup.ag/price/v3"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
SAMPLE_BATCH_SIZE = 50
JUPITER_BATCH_SIZE = 5
DEXSCREENER_MIN_INTERVAL_SECONDS = 1.0
GECKO_MIN_INTERVAL_SECONDS = 3.0
JUPITER_MIN_INTERVAL_SECONDS = 2.25
SOLANA_RPC_MIN_INTERVAL_SECONDS = 1.0
SUPPLY_CACHE_SECONDS = 24 * 60 * 60
SAMPLE_RETENTION_DAYS = 30
CONSOLE_JOB_LIMIT = 100
PRIORITY_INTERVAL_SECONDS = 15 * 60
BASE_INTERVAL_SECONDS = 4 * 60 * 60
MILESTONE_NEAR_RATIO = 0.8


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return result if isinstance(result, dict) else {}


@dataclass(frozen=True)
class Settings:
    scanner_db: Path
    audit_db: Path
    http_timeout: float = 12.0
    loop_seconds: float = 10.0
    max_batch_size: int = SAMPLE_BATCH_SIZE
    base_interval_seconds: int = BASE_INTERVAL_SECONDS
    priority_interval_seconds: int = PRIORITY_INTERVAL_SECONDS
    dexscreener_min_interval_seconds: float = DEXSCREENER_MIN_INTERVAL_SECONDS
    gecko_min_interval_seconds: float = GECKO_MIN_INTERVAL_SECONDS
    jupiter_min_interval_seconds: float = JUPITER_MIN_INTERVAL_SECONDS
    solana_rpc_min_interval_seconds: float = SOLANA_RPC_MIN_INTERVAL_SECONDS
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        paths = get_paths()
        return cls(
            scanner_db=Path(os.getenv("MEME_SCANNER_DB_PATH") or paths.processed_dir / "meme_scanner.sqlite3"),
            audit_db=Path(os.getenv("MEME_QUOTE_AUDIT_DB_PATH") or paths.processed_dir / "meme_quote_audit.sqlite3"),
            http_timeout=max(1.0, float(os.getenv("MEME_QUOTE_AUDIT_TIMEOUT_SECONDS") or 12)),
            loop_seconds=max(1.0, float(os.getenv("MEME_QUOTE_AUDIT_LOOP_SECONDS") or 10)),
            max_batch_size=max(1, min(500, int(os.getenv("MEME_QUOTE_AUDIT_BATCH_SIZE") or SAMPLE_BATCH_SIZE))),
            base_interval_seconds=max(60, int(os.getenv("MEME_QUOTE_AUDIT_BASE_INTERVAL_SECONDS") or BASE_INTERVAL_SECONDS)),
            priority_interval_seconds=max(60, int(os.getenv("MEME_QUOTE_AUDIT_PRIORITY_INTERVAL_SECONDS") or PRIORITY_INTERVAL_SECONDS)),
            dexscreener_min_interval_seconds=max(0.1, float(os.getenv("MEME_QUOTE_AUDIT_DEXSCREENER_MIN_INTERVAL_SECONDS") or DEXSCREENER_MIN_INTERVAL_SECONDS)),
            gecko_min_interval_seconds=max(0.1, float(os.getenv("MEME_QUOTE_AUDIT_GECKO_MIN_INTERVAL_SECONDS") or GECKO_MIN_INTERVAL_SECONDS)),
            jupiter_min_interval_seconds=max(0.1, float(os.getenv("MEME_QUOTE_AUDIT_JUPITER_MIN_INTERVAL_SECONDS") or JUPITER_MIN_INTERVAL_SECONDS)),
            solana_rpc_min_interval_seconds=max(0.1, float(os.getenv("MEME_QUOTE_AUDIT_SOLANA_RPC_MIN_INTERVAL_SECONDS") or SOLANA_RPC_MIN_INTERVAL_SECONDS)),
            enabled=(os.getenv("MEME_QUOTE_AUDIT_ENABLED") or "true").strip().lower() not in {"0", "false", "no", "off"},
        )


class AuditStore:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS quote_samples (
              id INTEGER PRIMARY KEY,
              address TEXT NOT NULL,
              symbol TEXT NOT NULL,
              priority INTEGER NOT NULL,
              priority_reason TEXT NOT NULL,
              sampled_at TEXT NOT NULL,
              retry_after TEXT,
              dex_sampled_at TEXT,
              dex_status TEXT NOT NULL,
              dex_price REAL,
              dex_market_cap REAL,
              dex_pair_address TEXT,
              dex_liquidity REAL,
              gecko_sampled_at TEXT,
              gecko_status TEXT NOT NULL,
              gecko_price REAL,
              gecko_market_cap REAL,
              gecko_pool_address TEXT,
              gecko_pool_reserve REAL,
              gecko_pool_matched INTEGER NOT NULL,
              gecko_volume_m5 REAL,
              jupiter_sampled_at TEXT,
              jupiter_status TEXT NOT NULL,
              jupiter_price REAL,
              jupiter_market_cap REAL,
              jupiter_liquidity REAL,
              jupiter_block_id INTEGER,
              supply REAL,
              supply_status TEXT NOT NULL,
              divergence_ratio REAL,
              comparison_status TEXT NOT NULL,
              errors_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_quote_samples_address_time
              ON quote_samples(address, sampled_at DESC);
            CREATE INDEX IF NOT EXISTS idx_quote_samples_time
              ON quote_samples(sampled_at DESC);
            CREATE TABLE IF NOT EXISTS token_supplies (
              address TEXT PRIMARY KEY,
              supply REAL,
              decimals INTEGER,
              fetched_at TEXT NOT NULL,
              status TEXT NOT NULL,
              error TEXT
            );
            CREATE TABLE IF NOT EXISTS provider_calls (
              id INTEGER PRIMARY KEY,
              provider TEXT NOT NULL,
              called_at TEXT NOT NULL,
              outcome TEXT NOT NULL,
              duration_ms INTEGER NOT NULL,
              item_count INTEGER NOT NULL DEFAULT 1,
              error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_provider_calls_time
              ON provider_calls(called_at DESC);
            CREATE TABLE IF NOT EXISTS audit_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS provider_state (
              provider TEXT PRIMARY KEY,
              failures INTEGER NOT NULL DEFAULT 0,
              backoff_until TEXT,
              last_error TEXT
            );
            """
        )
        sample_columns = {str(row[1]) for row in self.conn.execute("PRAGMA table_info(quote_samples)")}
        for column in ("retry_after", "dex_sampled_at", "gecko_sampled_at", "jupiter_sampled_at"):
            if column not in sample_columns:
                self.conn.execute(f"ALTER TABLE quote_samples ADD COLUMN {column} TEXT")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def latest_sample_states(self) -> dict[str, dict[str, str | None]]:
        rows = self.conn.execute(
            """SELECT address,sampled_at,retry_after FROM quote_samples AS samples
               WHERE id=(SELECT MAX(id) FROM quote_samples WHERE address=samples.address)"""
        ).fetchall()
        return {
            str(row["address"]): {"sampled_at": str(row["sampled_at"]), "retry_after": row["retry_after"]}
            for row in rows
        }

    def get_supply(self, address: str, now: datetime) -> sqlite3.Row | None:
        row = self.conn.execute("SELECT * FROM token_supplies WHERE address=?", (address,)).fetchone()
        fetched = _parse_time(row["fetched_at"]) if row else None
        if fetched is None or str(row["status"]) != "success" or (now - fetched).total_seconds() >= SUPPLY_CACHE_SECONDS:
            return None
        return row

    def save_supply(self, address: str, supply: float | None, decimals: int | None, status: str, error: str | None) -> None:
        self.conn.execute(
            """INSERT INTO token_supplies(address,supply,decimals,fetched_at,status,error)
               VALUES(?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET supply=excluded.supply,
               decimals=excluded.decimals,fetched_at=excluded.fetched_at,status=excluded.status,error=excluded.error""",
            (address, supply, decimals, _iso(), status, error),
        )
        self.conn.commit()

    def record_provider_call(
        self, provider: str, outcome: str, duration_ms: int, *, item_count: int = 1, error_code: str | None = None
    ) -> None:
        self.conn.execute(
            "INSERT INTO provider_calls(provider,called_at,outcome,duration_ms,item_count,error_code) VALUES(?,?,?,?,?,?)",
            (provider, _iso(), outcome, max(0, duration_ms), item_count, error_code),
        )
        self.conn.commit()

    def provider_backoff(self, provider: str, now: datetime) -> int:
        row = self.conn.execute("SELECT backoff_until FROM provider_state WHERE provider=?", (provider,)).fetchone()
        until = _parse_time(row["backoff_until"]) if row else None
        return max(0, round((until - now).total_seconds())) if until and until > now else 0

    def provider_succeeded(self, provider: str) -> None:
        self.conn.execute(
            "INSERT INTO provider_state(provider,failures,backoff_until,last_error) VALUES(?,0,NULL,NULL) "
            "ON CONFLICT(provider) DO UPDATE SET failures=0,backoff_until=NULL,last_error=NULL",
            (provider,),
        )
        self.conn.commit()

    def provider_failed(self, provider: str, error: str, *, retry_after: int = 0) -> int:
        row = self.conn.execute("SELECT failures FROM provider_state WHERE provider=?", (provider,)).fetchone()
        failures = int(row["failures"] or 0) + 1 if row else 1
        delay = max(retry_after, min(900, 15 * (2 ** min(failures - 1, 6))))
        until = _iso(datetime.now(UTC) + timedelta(seconds=delay))
        self.conn.execute(
            "INSERT INTO provider_state(provider,failures,backoff_until,last_error) VALUES(?,?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET failures=excluded.failures,backoff_until=excluded.backoff_until,last_error=excluded.last_error",
            (provider, failures, until, error[:1000]),
        )
        self.conn.commit()
        return delay

    def save_sample(self, sample: dict[str, Any]) -> None:
        columns = tuple(sample)
        self.conn.execute(
            f"INSERT INTO quote_samples({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
            tuple(sample[column] for column in columns),
        )
        self.conn.commit()

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO audit_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def cleanup(self, now: datetime) -> None:
        cutoff = (now - timedelta(days=SAMPLE_RETENTION_DAYS)).isoformat()
        self.conn.execute("DELETE FROM quote_samples WHERE sampled_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM provider_calls WHERE called_at < ?", (cutoff,))
        self.conn.execute("DELETE FROM token_supplies WHERE fetched_at < ?", (cutoff,))
        self.conn.commit()


def active_candidates(scanner_db: Path) -> list[dict[str, Any]]:
    if not scanner_db.exists():
        return []
    connection = sqlite3.connect(f"{scanner_db.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    try:
        obs_columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(observations)")}
        if not {"address", "chain", "symbol", "last_market_cap", "tracking_status"}.issubset(obs_columns):
            return []
        rows = connection.execute(
            "SELECT address,symbol,last_market_cap,highest_market_cap FROM observations "
            "WHERE tracking_status='active' AND lower(chain)='solana'"
        ).fetchall()
        active = {str(row["address"]): dict(row) for row in rows}
        if not active:
            return []

        console_addresses: set[str] = set()
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
            jobs = connection.execute(
                "SELECT address,payload_json FROM jobs WHERE COALESCE(reason,'') NOT IN "
                "('volume_gate_failed','tg_market_cap_gate_failed','unsupported_chain','token_not_found') "
                "ORDER BY updated_at DESC,id DESC LIMIT ?", (CONSOLE_JOB_LIMIT,)
            ).fetchall()
            for job in jobs:
                address = str(job["address"] or "")
                payload = _json_object(job["payload_json"])
                if address in active and str(payload.get("chain") or "").lower() in {"solana", "sol"}:
                    console_addresses.add(address)

        from .scanner import market_cap_levels

        milestones_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_cap_milestones'"
        ).fetchone()
        consumed: dict[str, set[float]] = {}
        if milestones_table:
            for row in connection.execute("SELECT address,level FROM market_cap_milestones WHERE chain='solana'"):
                consumed.setdefault(str(row["address"]), set()).add(float(row["level"]))

        candidates: list[dict[str, Any]] = []
        for address, row in active.items():
            cap = _number(row.get("last_market_cap")) or 0.0
            remaining = [level for level in market_cap_levels("solana")
                         if level not in consumed.get(address, set()) and level > cap]
            near_milestone = bool(remaining and cap >= remaining[0] * MILESTONE_NEAR_RATIO)
            priority_reason = "console" if address in console_addresses else "near_milestone" if near_milestone else "baseline"
            candidates.append({
                "address": address,
                "symbol": str(row.get("symbol") or ""),
                "market_cap": cap,
                "highest_market_cap": _number(row.get("highest_market_cap")) or 0.0,
                "priority": 2 if priority_reason == "console" else 1 if priority_reason == "near_milestone" else 0,
                "priority_reason": priority_reason,
            })
        return candidates
    finally:
        connection.close()


class QuoteAuditClient:
    def __init__(self, timeout: float, store: AuditStore, session: requests.Session | None = None,
                 *, dexscreener_min_interval: float = DEXSCREENER_MIN_INTERVAL_SECONDS,
                 gecko_min_interval: float = GECKO_MIN_INTERVAL_SECONDS,
                 jupiter_min_interval: float = JUPITER_MIN_INTERVAL_SECONDS,
                 solana_rpc_min_interval: float = SOLANA_RPC_MIN_INTERVAL_SECONDS) -> None:
        self.timeout = timeout
        self.store = store
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "OdAIly-MemeQuoteAudit/1.0"})
        self.dexscreener_min_interval = dexscreener_min_interval
        self.gecko_min_interval = gecko_min_interval
        self.jupiter_min_interval = jupiter_min_interval
        self.solana_rpc_min_interval = solana_rpc_min_interval
        self._last_dexscreener_request = 0.0
        self._last_gecko_request = 0.0
        self._last_jupiter_request = 0.0
        self._last_solana_rpc_request = 0.0

    def _get_json(self, provider: str, url: str, *, params: dict[str, Any] | None = None,
                  item_count: int = 1) -> Any:
        backoff = self.store.provider_backoff(provider, datetime.now(UTC))
        if backoff:
            raise RuntimeError(f"{provider}:backoff:{backoff}s")
        started = time.monotonic()
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            body = response.json()
            self.store.record_provider_call(provider, "success", round((time.monotonic() - started) * 1000),
                                            item_count=item_count)
            self.store.provider_succeeded(provider)
            return body
        except (requests.RequestException, ValueError) as exc:
            error_response = getattr(exc, "response", None)
            code = str(getattr(error_response, "status_code", "request_error"))
            retry_after = 0
            if error_response is not None:
                try:
                    retry_after = int(error_response.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    retry_after = 0
            error = f"{provider}:{code}:{exc}"
            self.store.record_provider_call(provider, "failed", round((time.monotonic() - started) * 1000),
                                            item_count=item_count, error_code=code)
            self.store.provider_failed(provider, error, retry_after=retry_after)
            raise RuntimeError(error) from exc

    def dexscreener(self, address: str) -> dict[str, Any] | None:
        remaining = self.dexscreener_min_interval - (time.monotonic() - self._last_dexscreener_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_dexscreener_request = time.monotonic()
        body = self._get_json("dexscreener", f"{DEXSCREENER_URL}/tokens/v1/solana/{quote(address, safe='')}")
        if not isinstance(body, list):
            raise RuntimeError("dexscreener:invalid_response")
        pairs = [pair for pair in body if isinstance(pair, dict)
                 and str(pair.get("chainId") or "").lower() == "solana"
                 and str((pair.get("baseToken") or {}).get("address") or "") == address]
        complete = [pair for pair in pairs if pair.get("marketCap") is not None
                    and isinstance(pair.get("volume"), dict) and pair["volume"].get("h24") is not None]
        return max(complete or pairs, key=lambda pair: _number((pair.get("liquidity") or {}).get("usd")) or 0, default=None)

    def gecko_pools(self, address: str) -> list[dict[str, Any]]:
        remaining = self.gecko_min_interval - (time.monotonic() - self._last_gecko_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_gecko_request = time.monotonic()
        body = self._get_json("geckoterminal", f"{GECKOTERMINAL_URL}/networks/solana/tokens/{quote(address, safe='')}/pools",
                              params={"page": 1})
        rows = body.get("data") if isinstance(body, dict) else None
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def jupiter_prices(self, addresses: list[str]) -> dict[str, dict[str, Any]]:
        if not addresses:
            return {}
        remaining = self.jupiter_min_interval - (time.monotonic() - self._last_jupiter_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_jupiter_request = time.monotonic()
        body = self._get_json("jupiter", JUPITER_URL, params={"ids": ",".join(addresses)},
                              item_count=len(addresses))
        return {str(address): value for address, value in body.items() if isinstance(value, dict)} if isinstance(body, dict) else {}

    def token_supply(self, address: str) -> tuple[float | None, int | None, str, str | None]:
        backoff = self.store.provider_backoff("solana_rpc", datetime.now(UTC))
        if backoff:
            return None, None, "backoff", f"solana_rpc:backoff:{backoff}s"
        remaining = self.solana_rpc_min_interval - (time.monotonic() - self._last_solana_rpc_request)
        if remaining > 0:
            time.sleep(remaining)
        self._last_solana_rpc_request = time.monotonic()
        started = time.monotonic()
        try:
            response = self.session.post(
                SOLANA_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [address]},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise ValueError(str(payload["error"]))
            value = payload.get("result", {}).get("value", {}) if isinstance(payload, dict) else {}
            amount = int(str(value.get("amount") or ""))
            decimals = int(value.get("decimals"))
            supply = amount / (10 ** decimals)
            self.store.record_provider_call("solana_rpc", "success", round((time.monotonic() - started) * 1000))
            self.store.provider_succeeded("solana_rpc")
            return supply, decimals, "success", None
        except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
            code = str(getattr(getattr(exc, "response", None), "status_code", "request_error"))
            self.store.record_provider_call("solana_rpc", "failed", round((time.monotonic() - started) * 1000), error_code=code)
            self.store.provider_failed("solana_rpc", f"solana_rpc:{code}:{exc}")
            return None, None, "failed", f"solana_rpc:{code}:{exc}"


def _selected_gecko_pool(pools: list[dict[str, Any]], dex_pair: str | None) -> tuple[dict[str, Any] | None, bool]:
    attrs = [(pool, pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}) for pool in pools]
    if dex_pair:
        matched = [(pool, data) for pool, data in attrs
                   if str(data.get("address") or pool.get("id") or "").split("_", 1)[-1].lower() == dex_pair.lower()]
        if matched:
            return matched[0][0], True
    selected = max(attrs, key=lambda entry: _number(entry[1].get("reserve_in_usd")) or 0, default=None)
    return (selected[0], False) if selected else (None, False)


def _comparison_status(caps: list[float | None]) -> tuple[float | None, str]:
    values = [value for value in caps if value is not None and value > 0]
    if len(values) < 2:
        return None, "incomplete"
    ratio = max(values) / min(values)
    return ratio, "divergent" if ratio > 1.3 else "aligned"


def _gecko_token_price(pool: dict[str, Any], address: str) -> float | None:
    attrs = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
    relationships = pool.get("relationships") if isinstance(pool.get("relationships"), dict) else {}
    base = relationships.get("base_token") if isinstance(relationships.get("base_token"), dict) else {}
    quote_token = relationships.get("quote_token") if isinstance(relationships.get("quote_token"), dict) else {}
    base_data = base.get("data") if isinstance(base.get("data"), dict) else {}
    quote_data = quote_token.get("data") if isinstance(quote_token.get("data"), dict) else {}
    base_id = str(base_data.get("id") or "").split("_", 1)[-1]
    quote_id = str(quote_data.get("id") or "").split("_", 1)[-1]
    if base_id.lower() == address.lower():
        return _number(attrs.get("base_token_price_usd")) or _number(attrs.get("token_price_usd"))
    if quote_id.lower() == address.lower():
        return _number(attrs.get("quote_token_price_usd")) or _number(attrs.get("token_price_usd"))
    return _number(attrs.get("token_price_usd"))


def _record_sample(candidate: dict[str, Any], client: QuoteAuditClient, store: AuditStore,
                   jupiter: dict[str, Any], now: datetime) -> None:
    address = str(candidate["address"])
    errors: dict[str, str] = {}
    dex_pair = None
    dex_price = dex_cap = dex_liquidity = None
    dex_status = "missing"
    try:
        pair = client.dexscreener(address)
        dex_sampled_at = _iso()
        if pair:
            dex_pair = str(pair.get("pairAddress") or "") or None
            dex_price = _number(pair.get("priceUsd"))
            dex_cap = _number(pair.get("marketCap") or pair.get("fdv"))
            dex_liquidity = _number((pair.get("liquidity") or {}).get("usd"))
            dex_status = "success" if dex_price is not None and dex_cap is not None else (
                "incomplete" if dex_price is not None or dex_cap is not None else "missing"
            )
    except Exception as exc:
        dex_sampled_at = _iso()
        dex_status = "failed"
        errors["dexscreener"] = str(exc)

    gecko_price = gecko_cap = gecko_reserve = gecko_volume_m5 = None
    gecko_pool_address = None
    gecko_pool_matched = False
    gecko_status = "missing"
    try:
        pool, gecko_pool_matched = _selected_gecko_pool(client.gecko_pools(address), dex_pair)
        gecko_sampled_at = _iso()
        if pool:
            attrs = pool.get("attributes") if isinstance(pool.get("attributes"), dict) else {}
            gecko_pool_address = str(attrs.get("address") or str(pool.get("id") or "").split("_", 1)[-1]) or None
            gecko_price = _gecko_token_price(pool, address)
            gecko_cap = _number(attrs.get("fdv_usd"))
            gecko_reserve = _number(attrs.get("reserve_in_usd"))
            gecko_volume_m5 = _number((attrs.get("volume_usd") or {}).get("m5"))
            gecko_status = "success" if gecko_price is not None and gecko_cap is not None else (
                "incomplete" if gecko_price is not None or gecko_cap is not None else "missing"
            )
    except Exception as exc:
        gecko_sampled_at = _iso()
        gecko_status = "failed"
        errors["geckoterminal"] = str(exc)

    jupiter_price = _number(jupiter.get("usdPrice"))
    jupiter_liquidity = _number(jupiter.get("liquidity"))
    jupiter_block_value = _number(jupiter.get("blockId"))
    jupiter_block = int(jupiter_block_value) if jupiter_block_value is not None else None
    jupiter_status = "success" if jupiter_price is not None else "failed" if jupiter.get("_error") else "missing"
    jupiter_sampled_at = str(jupiter.get("_sampled_at") or _iso())
    if jupiter.get("_error"):
        errors["jupiter"] = str(jupiter["_error"])

    supply_row = store.get_supply(address, now)
    if supply_row:
        supply = _number(supply_row["supply"])
        supply_status, supply_error = str(supply_row["status"]), supply_row["error"]
    else:
        supply, decimals, supply_status, supply_error = client.token_supply(address)
        store.save_supply(address, supply, decimals, supply_status, supply_error)
    if supply_error:
        errors["solana_rpc"] = str(supply_error)
    jupiter_cap = jupiter_price * supply if jupiter_price is not None and supply is not None else None
    values = [value for value in (dex_cap, gecko_cap, jupiter_cap) if value is not None and value > 0]
    ratio = max(values) / min(values) if len(values) >= 2 else None
    comparison = "incomplete" if ratio is None else "divergent" if ratio > 1.3 else "aligned"
    retry_delay = max(
        [store.provider_backoff(provider, datetime.now(UTC)) for provider in errors]
        or [0]
    )
    retry_after = _iso(datetime.now(UTC) + timedelta(seconds=max(60, retry_delay))) if errors else None
    store.save_sample({
        "address": address, "symbol": candidate["symbol"], "priority": int(candidate["priority"]),
        "priority_reason": candidate["priority_reason"], "sampled_at": _iso(), "retry_after": retry_after,
        "dex_sampled_at": dex_sampled_at, "gecko_sampled_at": gecko_sampled_at,
        "jupiter_sampled_at": jupiter_sampled_at,
        "dex_status": dex_status, "dex_price": dex_price, "dex_market_cap": dex_cap,
        "dex_pair_address": dex_pair, "dex_liquidity": dex_liquidity,
        "gecko_status": gecko_status, "gecko_price": gecko_price, "gecko_market_cap": gecko_cap,
        "gecko_pool_address": gecko_pool_address, "gecko_pool_reserve": gecko_reserve,
        "gecko_pool_matched": int(gecko_pool_matched), "gecko_volume_m5": gecko_volume_m5,
        "jupiter_status": jupiter_status, "jupiter_price": jupiter_price, "jupiter_market_cap": jupiter_cap,
        "jupiter_liquidity": jupiter_liquidity, "jupiter_block_id": jupiter_block,
        "supply": supply, "supply_status": supply_status, "divergence_ratio": ratio,
        "comparison_status": comparison, "errors_json": json.dumps(errors, ensure_ascii=False),
    })


def sample_batch(settings: Settings, store: AuditStore, client: QuoteAuditClient, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    candidates = active_candidates(settings.scanner_db)
    latest = store.latest_sample_states()
    due: list[dict[str, Any]] = []
    for candidate in candidates:
        interval = settings.priority_interval_seconds if candidate["priority"] else settings.base_interval_seconds
        state = latest.get(candidate["address"])
        last = _parse_time(state.get("sampled_at")) if state else None
        retry_at = _parse_time(state.get("retry_after")) if state else None
        candidate["due"] = (
            last is None
            or (retry_at is not None and retry_at <= now)
            or (retry_at is None and (now - last).total_seconds() >= interval)
        )
        if candidate["due"]:
            due.append(candidate)
    due.sort(key=lambda row: (
        -int(row["priority"]),
        latest.get(row["address"], {}).get("sampled_at") or "",
        row["address"],
    ))
    batch = due[:settings.max_batch_size]
    store.set_meta("backlog", max(0, len(due) - len(batch)))
    if not batch:
        store.set_meta("last_scheduler_at", _iso(now))
        return {"sampled": 0, "backlog": 0, "active": len(candidates)}

    sampled = 0
    for start in range(0, len(batch), JUPITER_BATCH_SIZE):
        group = batch[start:start + JUPITER_BATCH_SIZE]
        prices: dict[str, dict[str, Any]] = {}
        try:
            prices.update(client.jupiter_prices([str(row["address"]) for row in group]))
        except Exception as exc:
            prices.update({str(row["address"]): {"_error": str(exc)} for row in group})
        jupiter_sampled_at = _iso()
        for candidate in group:
            price = dict(prices.get(str(candidate["address"]), {}))
            price["_sampled_at"] = jupiter_sampled_at
            _record_sample(candidate, client, store, price, now)
            sampled += 1

    backlog = max(0, len(due) - sampled)
    store.set_meta("backlog", backlog)
    store.set_meta("last_scheduler_at", _iso(now))
    store.cleanup(now)
    print(f"[meme-quote-audit] sampled={sampled} backlog={backlog} active={len(candidates)}")
    return {"sampled": sampled, "backlog": backlog, "active": len(candidates)}


def run(args: Any | None = None) -> int:
    settings = Settings.from_env()
    if not settings.enabled:
        print("[meme-quote-audit] disabled by MEME_QUOTE_AUDIT_ENABLED", file=sys.stderr)
        return 0
    if args is not None:
        if getattr(args, "scanner_db", None):
            settings = Settings(**{**settings.__dict__, "scanner_db": Path(args.scanner_db)})
        if getattr(args, "audit_db", None):
            settings = Settings(**{**settings.__dict__, "audit_db": Path(args.audit_db)})
        if getattr(args, "once", False):
            store = AuditStore(settings.audit_db)
            try:
                result = sample_batch(settings, store, QuoteAuditClient(
                    settings.http_timeout, store,
                    dexscreener_min_interval=settings.dexscreener_min_interval_seconds,
                    gecko_min_interval=settings.gecko_min_interval_seconds,
                    jupiter_min_interval=settings.jupiter_min_interval_seconds,
                    solana_rpc_min_interval=settings.solana_rpc_min_interval_seconds,
                ))
                print(json.dumps(result, ensure_ascii=False))
            finally:
                store.close()
            return 0
    store = AuditStore(settings.audit_db)
    client = QuoteAuditClient(
        settings.http_timeout, store,
        dexscreener_min_interval=settings.dexscreener_min_interval_seconds,
        gecko_min_interval=settings.gecko_min_interval_seconds,
        jupiter_min_interval=settings.jupiter_min_interval_seconds,
        solana_rpc_min_interval=settings.solana_rpc_min_interval_seconds,
    )
    try:
        while True:
            started = time.monotonic()
            try:
                sample_batch(settings, store, client)
            except Exception as exc:
                print(f"[meme-quote-audit] scheduler error: {exc}", file=sys.stderr)
            time.sleep(max(0.1, settings.loop_seconds - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0
    finally:
        store.close()
