from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction

from .client import address_key
from .history import fill_key
from .models import Candidate
from .scenarios import SCENARIOS, event_scenarios
from .store import encode

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hl_news_addresses (
    address text PRIMARY KEY, first_seen_ms bigint NOT NULL, last_seen_ms bigint NOT NULL,
    sources jsonb NOT NULL, last_polled_ms bigint NOT NULL DEFAULT 0, error text
);
CREATE TABLE IF NOT EXISTS hl_news_fills (
    address text NOT NULL, fill_key text NOT NULL, time_ms bigint NOT NULL, payload jsonb NOT NULL,
    PRIMARY KEY(address,fill_key)
);
CREATE INDEX IF NOT EXISTS hl_news_fills_time ON hl_news_fills(address,time_ms);
CREATE TABLE IF NOT EXISTS hl_news_monitor_fill_keys (
    monitor text NOT NULL,address text NOT NULL,fill_key text NOT NULL,time_ms bigint NOT NULL,
    PRIMARY KEY(monitor,address,fill_key)
);
CREATE INDEX IF NOT EXISTS hl_news_monitor_fill_keys_time ON hl_news_monitor_fill_keys(monitor,address,time_ms);
CREATE TABLE IF NOT EXISTS hl_news_compact_history (
    address text PRIMARY KEY, through_ms bigint NOT NULL, payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_compact_work (
    address text PRIMARY KEY, through_ms bigint NOT NULL, payload jsonb NOT NULL, source_count bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_compact_segments (
    address text NOT NULL, start_ms bigint NOT NULL, end_ms bigint NOT NULL,
    payload jsonb NOT NULL, summary jsonb NOT NULL, PRIMARY KEY(address,start_ms)
);
CREATE INDEX IF NOT EXISTS hl_news_compact_segments_end ON hl_news_compact_segments(address,end_ms);
CREATE TABLE IF NOT EXISTS hl_news_state (
    address text PRIMARY KEY, observed_ms bigint NOT NULL,state jsonb NOT NULL,history jsonb NOT NULL,coverage jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_normal_state (
    address text PRIMARY KEY, observed_ms bigint NOT NULL,state jsonb NOT NULL,history jsonb NOT NULL,coverage jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_normal_schedule (
    address text PRIMARY KEY,last_polled_ms bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_candidates (
    event_key text PRIMARY KEY,observed_ms bigint NOT NULL,coin text NOT NULL,payload jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS hl_news_candidates_observed ON hl_news_candidates(observed_ms DESC,event_key ASC);
CREATE TABLE IF NOT EXISTS hl_news_candidate_timeline (
    event_key text PRIMARY KEY,observed_ms bigint NOT NULL,coin text NOT NULL,kinds jsonb NOT NULL,addresses jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS hl_news_candidate_timeline_observed ON hl_news_candidate_timeline(observed_ms DESC,event_key ASC);
CREATE TABLE IF NOT EXISTS hl_news_candidate_addresses (
    event_key text NOT NULL,address_lower text NOT NULL,observed_ms bigint NOT NULL,PRIMARY KEY(event_key,address_lower)
);
CREATE INDEX IF NOT EXISTS hl_news_candidate_addresses_address_time ON hl_news_candidate_addresses(address_lower,observed_ms DESC,event_key ASC);
CREATE TABLE IF NOT EXISTS hl_news_candidate_scenarios (
    event_key text NOT NULL,scenario_key text NOT NULL,PRIMARY KEY(event_key,scenario_key)
);
CREATE INDEX IF NOT EXISTS hl_news_candidate_scenarios_key ON hl_news_candidate_scenarios(scenario_key,event_key);
CREATE TABLE IF NOT EXISTS hl_news_dispatch (
    event_key text PRIMARY KEY,signature text NOT NULL,task_id bigint,status text NOT NULL,error text,
    created_at timestamptz NOT NULL,updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS hl_news_dispatch_signature ON hl_news_dispatch(signature);
CREATE TABLE IF NOT EXISTS hl_news_account_pool (
    address text PRIMARY KEY REFERENCES hl_news_addresses(address),tier text NOT NULL DEFAULT 'pending',
    qualified boolean NOT NULL DEFAULT false,next_poll_ms bigint NOT NULL DEFAULT 0,last_action_ms bigint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS hl_news_account_due ON hl_news_account_pool(qualified,next_poll_ms);
CREATE TABLE IF NOT EXISTS hl_news_metadata (
    key text PRIMARY KEY,payload jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS hl_news_test_cases (
    event_key text PRIMARY KEY,observed_ms bigint NOT NULL,payload jsonb NOT NULL,preview jsonb,created_ms bigint NOT NULL
);
"""
SCENARIO_VERSION = [[item[0], item[3], item[4]] for item in SCENARIOS]
SCHEMA_CHECKSUM = sha256((SCHEMA_SQL + encode(SCENARIO_VERSION)).encode("utf-8")).hexdigest()
STATE_TABLES = {"hl_news_state", "hl_news_normal_state"}
TERMINAL_TASK_STATUSES = ("ready_review", "auto_published", "published", "discarded", "duplicate", "expired", "legacy_skipped")


def address_not_blocked_sql(address_expr: str) -> str:
    """Exclude blocked addresses without detoasting or casting non-block metadata rows."""
    return (
        f"NOT EXISTS (SELECT 1 FROM hl_news_metadata b "
        f"WHERE b.key='address_block:' || {address_expr} "
        f"AND starts_with(b.key,'address_block:') "
        f"AND CASE WHEN starts_with(b.key,'address_block:') THEN b.payload->>'blocked'='true' ELSE false END) "
        f"AND NOT EXISTS (SELECT 1 FROM hl_news_metadata_large b "
        f"WHERE b.key='address_block:' || {address_expr} "
        f"AND starts_with(b.key,'address_block:') "
        f"AND CASE WHEN starts_with(b.key,'address_block:') THEN (b.payload::jsonb)->>'blocked'='true' ELSE false END)"
    )


class PostgresNewsStore:
    def __init__(self, path: Path) -> None:
        self.backend = "postgres"
        self.path = path.resolve()
        self.init_schema()

    @contextmanager
    def _connect(self, *, autocommit: bool = False):
        with connect_postgres(schema="hyperliquid_news", application_name="odaily-hyperliquid-news") as conn:
            if autocommit:
                yield conn
            else:
                with postgres_transaction(conn):
                    yield conn

    def init_schema(self) -> None:
        self.initialize_schema()
        from .postgres_naming import PostgresNamingStore
        PostgresNamingStore(self).initialize()

    @staticmethod
    def initialize_schema() -> None:
        with connect_postgres(schema="hyperliquid_news", application_name="odaily-hyperliquid-news-migrate") as conn:
            apply_schema_migrations(conn, schema="hyperliquid_news", component="hyperliquid_news",
                migrations=(PostgresMigration(1, "hyperliquid_news_initial", SCHEMA_CHECKSUM,
                    lambda connection: connection.execute(SCHEMA_SQL)),
                    PostgresMigration(2, "hyperliquid_news_large_metadata", sha256(b"CREATE TABLE IF NOT EXISTS hl_news_metadata_large(key text PRIMARY KEY,payload text NOT NULL)").hexdigest(),
                    lambda connection: connection.execute("CREATE TABLE IF NOT EXISTS hl_news_metadata_large(key text PRIMARY KEY,payload text NOT NULL)")),))
    def initialize(self) -> None:
        """Keep the SQLite store's public startup contract without rebuilding indexes."""
        self.init_schema()

    @staticmethod
    def _state_table(table: str) -> str:
        if table not in STATE_TABLES:
            raise ValueError("Unknown Hyperliquid state table")
        return table

    @staticmethod
    def _index_candidate(conn, event_key: str, payload: dict[str, Any]) -> None:
        addresses = [address_key(item) for item in payload.get("addresses", [])]
        conn.execute("INSERT INTO hl_news_candidate_timeline(event_key,observed_ms,coin,kinds,addresses) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(event_key) DO UPDATE SET observed_ms=excluded.observed_ms,coin=excluded.coin,kinds=excluded.kinds,addresses=excluded.addresses",
                     (event_key, int(payload.get("observed_ms") or 0), str(payload.get("coin") or ""), json.dumps(payload.get("kinds") or []), json.dumps(addresses)))
        conn.execute("DELETE FROM hl_news_candidate_addresses WHERE event_key=%s", (event_key,))
        for address in addresses:
            conn.execute("INSERT INTO hl_news_candidate_addresses(event_key,address_lower,observed_ms) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                         (event_key, address.lower(), int(payload.get("observed_ms") or 0)))
        for scenario in event_scenarios(payload):
            conn.execute("INSERT INTO hl_news_candidate_scenarios(event_key,scenario_key) VALUES(%s,%s) ON CONFLICT DO NOTHING", (event_key, scenario))

    def _insert_candidate(self, conn, event: Candidate | dict[str, Any]) -> bool:
        payload = asdict(event) if not isinstance(event, dict) else event
        if self.event_blocked(payload, conn=conn):
            return False
        result = conn.execute("INSERT INTO hl_news_candidates(event_key,observed_ms,coin,payload) VALUES(%s,%s,%s,%s) ON CONFLICT(event_key) DO NOTHING",
                              (str(payload["key"]), int(payload["observed_ms"]), str(payload["coin"]), encode(payload)))
        if result.rowcount:
            self._index_candidate(conn, str(payload["key"]), payload)
        return bool(result.rowcount)

    def discover(self, address: str, *, source: dict[str, Any], now_ms: int, limit: int | None = 500) -> bool:
        address = address_key(address)
        with self._connect() as conn:
            if self.blocked(address, conn=conn):
                return False
            row = conn.execute("SELECT sources FROM hl_news_addresses WHERE address=%s", (address,)).fetchone()
            if row is None and limit is not None and conn.execute(f"SELECT count(*) AS count FROM hl_news_addresses WHERE {address_not_blocked_sql('hl_news_addresses.address')}").fetchone()["count"] >= limit:
                return False
            sources = row["sources"] if row else []
            if isinstance(sources, str):
                sources = json.loads(sources)
            if source.get("kind") == "leaderboard":
                sources = [s for s in sources if not (s.get("kind") == "leaderboard" and s.get("window") == source.get("window") and s.get("side") == source.get("side"))]
            elif source.get("kind") == "large_observed_trade":
                sources = [s for s in sources if not (s.get("kind") == source["kind"] and s.get("coin") == source.get("coin"))]
            if source not in sources:
                sources.append(source)
            conn.execute("INSERT INTO hl_news_addresses(address,first_seen_ms,last_seen_ms,sources) VALUES(%s,%s,%s,%s) ON CONFLICT(address) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,sources=excluded.sources",
                         (address, now_ms, now_ms, json.dumps(sources, ensure_ascii=False)))
        return True

    def add_manual_address(self, address: str, *, now_ms: int, updated_by: str) -> dict[str, Any]:
        from datetime import datetime
        address = address_key(address)
        source = {"kind": "manual", "added_by": updated_by, "added_at": datetime.fromtimestamp(now_ms / 1000, UTC).isoformat()}
        with self._connect() as conn:
            if self.blocked(address, conn=conn):
                raise ValueError("Address is blocked; unblock it before adding it")
            row = conn.execute("SELECT first_seen_ms,last_seen_ms,sources FROM hl_news_addresses WHERE address=%s FOR UPDATE", (address,)).fetchone()
            sources = row["sources"] if row else []
            if isinstance(sources, str):
                sources = json.loads(sources)
            already = any(item.get("kind") == "manual" for item in sources)
            if not already:
                sources.append(source)
            conn.execute("INSERT INTO hl_news_addresses(address,first_seen_ms,last_seen_ms,sources) VALUES(%s,%s,%s,%s) ON CONFLICT(address) DO UPDATE SET last_seen_ms=GREATEST(hl_news_addresses.last_seen_ms,excluded.last_seen_ms),sources=excluded.sources",
                         (address, now_ms, now_ms, json.dumps(sources, ensure_ascii=False)))
        self.schedule_account(address, now_ms=now_ms)
        return {"address": address, "created": row is None, "already_manual": already}

    def addresses(self, limit: int, *, normal_schedule: bool = False) -> list[dict[str, Any]]:
        blocked = address_not_blocked_sql("a.address")
        with self._connect() as conn:
            if normal_schedule:
                rows = conn.execute(f"SELECT a.* FROM hl_news_addresses a LEFT JOIN hl_news_normal_schedule s USING(address) WHERE {blocked} ORDER BY COALESCE(s.last_polled_ms,0),a.address LIMIT %s", (limit,)).fetchall()
            else:
                rows = conn.execute(f"SELECT a.* FROM hl_news_addresses a WHERE {blocked} ORDER BY a.last_polled_ms,a.address LIMIT %s", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def load(self, address: str, *, state_table: str = "hl_news_state") -> dict[str, Any] | None:
        table = self._state_table(state_table)
        with self._connect() as conn:
            row = conn.execute(f"SELECT state,history,coverage FROM {table} WHERE address=%s", (address,)).fetchone()
        return {key: row[key] for key in ("state", "history", "coverage")} if row else None

    def fills(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM hl_news_fills WHERE address=%s AND time_ms BETWEEN %s AND %s ORDER BY time_ms,fill_key", (address, start_ms, end_ms)).fetchall()
        return [row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) for row in rows]

    def monitor_fill_keys(self, address: str, monitor: str, start_ms: int, end_ms: int) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT fill_key FROM hl_news_monitor_fill_keys WHERE monitor=%s AND address=%s AND time_ms BETWEEN %s AND %s", (monitor, address, start_ms, end_ms)).fetchall()
        return {row["fill_key"] for row in rows}

    def compact_boundary(self, address: str) -> int | None:
        with self._connect() as conn:
            row = conn.execute("SELECT through_ms FROM hl_news_compact_history WHERE address=%s", (address,)).fetchone()
        return int(row["through_ms"]) if row else None

    def compact_history(self, address: str, start_ms: int = 0, *, cuts: list[int] | None = None, aggregate_rounds: bool = False) -> tuple[int, dict[str, Any]] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT through_ms,payload FROM hl_news_compact_history WHERE address=%s", (address,)).fetchone()
            if not row:
                return None
            segments = conn.execute("SELECT start_ms,end_ms,payload,summary FROM hl_news_compact_segments WHERE address=%s AND end_ms>=%s ORDER BY start_ms", (address, start_ms)).fetchall()
        payloads = []
        for segment in segments:
            split = (not aggregate_rounds or cuts is None or any(segment["start_ms"] < cut <= segment["end_ms"] for cut in cuts))
            payload = segment["payload"] if split else segment["summary"]
            payloads.append(payload if isinstance(payload, dict) else json.loads(payload))
        carry = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
        data = {"version": 1, "assets": {}}
        for payload in payloads:
            for coin, asset in payload["assets"].items():
                target = data["assets"].setdefault(coin, {"moments": [], "episodes": [], "gaps": []})
                for field in ("moments", "episodes", "gaps"):
                    target[field].extend(asset[field])
                target.setdefault("round_summaries", []).extend(asset.get("round_summaries", []))
        for coin, asset in carry["assets"].items():
            target = data["assets"].setdefault(coin, {"moments": [], "episodes": [], "gaps": []})
            if not target["moments"] or target["moments"][-1]["time_ms"] != asset["moments"][-1]["time_ms"]:
                target["moments"].extend(asset["moments"])
            target["episodes"].extend(asset["episodes"])
            target["expected"] = asset["expected"]
            target["ordering_expected"] = asset["ordering_expected"]
        return int(row["through_ms"]), data

    def _metadata_in_transaction(self, conn, key: str, default: Any) -> Any:
        row = conn.execute("SELECT payload FROM hl_news_metadata WHERE key=%s", (key,)).fetchone()
        if row is None:
            row = conn.execute("SELECT payload FROM hl_news_metadata_large WHERE key=%s", (key,)).fetchone()
        if not row:
            return default
        value = row["payload"]
        return json.loads(value) if isinstance(value, str) else value

    @staticmethod
    def _has_non_empty_metadata_in_transaction(conn, key: str) -> bool:
        row = conn.execute(
            "SELECT (pg_column_size(payload) > 64 OR payload NOT IN ('{}'::jsonb, '[]'::jsonb, 'null'::jsonb, '\"\"'::jsonb, 'false'::jsonb, '0'::jsonb)) AS has_value "
            "FROM hl_news_metadata WHERE key=%s",
            (key,),
        ).fetchone()
        if row is not None:
            return bool(row["has_value"])
        row = conn.execute(
            "SELECT (pg_column_size(payload) > 64 OR btrim(payload) NOT IN ('', '{}', '[]', 'null', '\"\"', 'false', '0')) AS has_value "
            "FROM hl_news_metadata_large WHERE key=%s",
            (key,),
        ).fetchone()
        return bool(row and row["has_value"])

    @staticmethod
    def _save_metadata_in_transaction(conn, key: str, payload: str) -> None:
        if len(payload.encode("utf-8")) > 200 * 1024 * 1024:
            conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", (key,))
            conn.execute("INSERT INTO hl_news_metadata_large(key,payload) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, payload))
        else:
            conn.execute("DELETE FROM hl_news_metadata_large WHERE key=%s", (key,))
            conn.execute("INSERT INTO hl_news_metadata(key,payload) VALUES(%s,%s::jsonb) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, payload))

    def compact_fills(self, address: str, *, cutoff_ms: int, batch_size: int = 5000) -> dict[str, Any]:
        """Build bounded compact batches outside the address lock and fence commit."""
        from .compact_history import extend_compact, summarize_segment, verify_compact_history
        if batch_size < 1:
            raise ValueError("Invalid compact batch size")
        def source_state(conn):
            normal=conn.execute("SELECT state,coverage FROM hl_news_normal_state WHERE address=%s",(address,)).fetchone()
            cursors=[]
            if normal:
                state,coverage=normal["state"],normal["coverage"]
                cursor=state.get("observed_ms") if coverage.get("coverage")=="not_queried_no_eligibility" else state.get("history_last_queried_ms",state.get("observed_ms"))
                if not coverage.get("pagination_complete",True):return None,"normal_history_incomplete"
                if cursor is not None:cursors.append(int(cursor))
            account=conn.execute("SELECT state,coverage FROM hl_news_state WHERE address=%s",(address,)).fetchone()
            pool=conn.execute("SELECT qualified FROM hl_news_account_pool WHERE address=%s",(address,)).fetchone()
            account_active=not pool or bool(pool["qualified"])
            if account and account_active:
                state,coverage=account["state"],account["coverage"]
                cursor=state.get("account_history_checked_ms")
                if cursor is None or not coverage.get("pagination_complete",True):return None,"account_history_incomplete"
                cursors.append(int(cursor))
            if not cursors:return None,"history_cursor_missing"
            for prefix in ("normal_pending_fills:","account_pending_fills:"):
                if prefix=="account_pending_fills:" and not account_active:continue
                if self._has_non_empty_metadata_in_transaction(conn,prefix+address):return None,"pending_history"
            return (tuple(cursors),account_active),None

        # Read and fence a bounded source batch while serialized with ingestion.
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (address,))
            state,error=source_state(conn)
            if error:return {"fills":0,"reason":error}
            cursors,account_active=state
            cutoff_ms=min(cutoff_ms,min(cursors)-60000)
            saved=conn.execute("SELECT through_ms,payload FROM hl_news_compact_history WHERE address=%s FOR UPDATE",(address,)).fetchone()
            work=None if saved else conn.execute("SELECT * FROM hl_news_compact_work WHERE address=%s FOR UPDATE",(address,)).fetchone()
            checkpoint=saved or work
            through_ms=int(checkpoint["through_ms"]) if checkpoint else -1
            data=checkpoint["payload"] if checkpoint else {"version":1,"assets":{}}
            if isinstance(data,str): data=json.loads(data)
            if isinstance(data,str):data=json.loads(data)
            if saved:
                old=conn.execute("SELECT fill_key FROM hl_news_fills WHERE address=%s AND time_ms<=%s ORDER BY time_ms,fill_key LIMIT %s",(address,through_ms,batch_size)).fetchall()
                if old:
                    with conn.cursor() as cursor:
                        cursor.executemany("DELETE FROM hl_news_fills WHERE address=%s AND fill_key=%s",[(address,row["fill_key"]) for row in old])
                    return {"fills":len(old),"phase":"cleanup","through_ms":through_ms}
            rows=conn.execute("SELECT fill_key,time_ms,payload FROM hl_news_fills WHERE address=%s AND time_ms>%s AND time_ms<%s ORDER BY time_ms,fill_key LIMIT %s",(address,through_ms,cutoff_ms,batch_size+1)).fetchall()
            if len(rows)>batch_size:
                boundary=rows[-1]["time_ms"]
                rows=[row for row in rows if row["time_ms"]<boundary]
                if not rows: raise ValueError("Compaction batch is saturated at one timestamp")
            if not rows:
                if work:
                    if through_ms>=cutoff_ms: return {"fills":0,"reason":"cursor_behind_work"}
                    source_count=conn.execute("SELECT count(*) FROM hl_news_fills WHERE address=%s AND time_ms<=%s",(address,through_ms)).fetchone()["count"]
                    if int(source_count)!=int(work["source_count"]):
                        conn.execute("DELETE FROM hl_news_compact_segments WHERE address=%s",(address,))
                        conn.execute("DELETE FROM hl_news_compact_work WHERE address=%s",(address,))
                        return {"fills":0,"phase":"rebuild","reason":"source_fills_changed"}
                    conn.execute("INSERT INTO hl_news_compact_history(address,through_ms,payload) VALUES(%s,%s,%s) ON CONFLICT(address) DO UPDATE SET through_ms=excluded.through_ms,payload=excluded.payload",(address,through_ms,json.dumps(work["payload"])))
                    conn.execute("DELETE FROM hl_news_compact_work WHERE address=%s",(address,))
                    return {"fills":0,"phase":"activated","through_ms":through_ms}
                return {"fills":0,"through_ms":through_ms}
            source_fingerprint=conn.execute("SELECT count(*) AS n,COALESCE(max(time_ms),-1) AS max_time FROM hl_news_fills WHERE address=%s AND time_ms<=%s",(address,int(rows[-1]["time_ms"]))).fetchone()
            snapshot={"state":state,"through_ms":through_ms,"saved":bool(saved),"work_count":int(work["source_count"]) if work else 0,
                      "data":data,"rows":[dict(row) for row in rows],"fingerprint":(int(source_fingerprint["n"]),int(source_fingerprint["max_time"]))}

        # Expensive parsing, extension, summarization, and verification run after checkout release.
        rows=snapshot["rows"]
        fills=[row["payload"] if isinstance(row["payload"],dict) else json.loads(row["payload"]) for row in rows]
        data=snapshot["data"]
        updated=extend_compact(data,fills)
        new_through=int(rows[-1]["time_ms"])
        batch_only=extend_compact({"version":1,"assets":{}},fills)
        verify_compact_history(fills,batch_only,[(int(rows[0]["time_ms"]),new_through)])
        segment={"version":1,"assets":{}}
        carry={"version":1,"assets":{}}
        for coin,asset in updated["assets"].items():
            old_asset=data["assets"].get(coin)
            old_moments=len(old_asset["moments"]) if old_asset else 0
            old_gaps=len(old_asset["gaps"]) if old_asset else 0
            episodes=asset["episodes"]
            active=episodes[-1:] if episodes and episodes[-1]["end_ms"] is None and episodes[-1]["continuous"] else []
            finished=episodes[:-1] if active else episodes
            new_moments=asset["moments"][old_moments:]
            if new_moments or finished:segment["assets"][coin]={"moments":new_moments,"episodes":finished,"gaps":asset["gaps"][old_gaps:]}
            carry["assets"][coin]={"moments":asset["moments"][-1:],"episodes":active,"gaps":[],"expected":asset["expected"],"ordering_expected":asset["ordering_expected"]}
        segment_json=json.dumps(segment)
        summary_json=json.dumps(summarize_segment(segment))
        carry_json=json.dumps(carry)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",(address,))
            state,error=source_state(conn)
            if error or state!=snapshot["state"]:return {"fills":0,"phase":"retry","reason":"source_cursor_changed"}
            saved=conn.execute("SELECT through_ms,payload FROM hl_news_compact_history WHERE address=%s FOR UPDATE",(address,)).fetchone()
            work=None if saved else conn.execute("SELECT through_ms,payload,source_count FROM hl_news_compact_work WHERE address=%s FOR UPDATE",(address,)).fetchone()
            current=saved or work
            if (int(current["through_ms"]) if current else -1)!=snapshot["through_ms"] or bool(saved)!=snapshot["saved"]:
                return {"fills":0,"phase":"retry","reason":"checkpoint_changed"}
            fingerprint=conn.execute("SELECT count(*) AS n,COALESCE(max(time_ms),-1) AS max_time FROM hl_news_fills WHERE address=%s AND time_ms<=%s",(address,new_through)).fetchone()
            if (int(fingerprint["n"]),int(fingerprint["max_time"]))!=snapshot["fingerprint"]:
                return {"fills":0,"phase":"retry","reason":"source_fills_changed"}
            for prefix in ("normal_pending_fills:","account_pending_fills:"):
                if prefix=="account_pending_fills:" and not state[1]:continue
                if self._has_non_empty_metadata_in_transaction(conn,prefix+address):return {"fills":0,"reason":"pending_history"}
            conn.execute("INSERT INTO hl_news_compact_segments(address,start_ms,end_ms,payload,summary) VALUES(%s,%s,%s,%s::jsonb,%s::jsonb)",(address,int(rows[0]["time_ms"]),new_through,segment_json,summary_json))
            if snapshot["saved"]:
                conn.execute("UPDATE hl_news_compact_history SET through_ms=%s,payload=%s::jsonb WHERE address=%s",(new_through,carry_json,address))
                conn.execute("DELETE FROM hl_news_fills WHERE address=%s AND fill_key=ANY(%s)",(address,[row["fill_key"] for row in rows]))
            else:
                previous_count=int(work["source_count"]) if work else 0
                conn.execute("INSERT INTO hl_news_compact_work(address,through_ms,payload,source_count) VALUES(%s,%s,%s::jsonb,%s) ON CONFLICT(address) DO UPDATE SET through_ms=excluded.through_ms,payload=excluded.payload,source_count=excluded.source_count",(address,new_through,carry_json,previous_count+len(rows)))
            return {"fills":len(rows),"phase":"incremental" if snapshot["saved"] else "building","through_ms":new_through}

    def metadata(self, key: str, default: Any) -> Any:
        with self._connect(autocommit=True) as conn:
            row = conn.execute("SELECT payload FROM hl_news_metadata WHERE key=%s", (key,)).fetchone()
            if row is None:
                row = conn.execute("SELECT payload FROM hl_news_metadata_large WHERE key=%s", (key,)).fetchone()
        if not row:
            return default
        value = row["payload"]
        return json.loads(value) if isinstance(value, str) else value

    def metadata_keys(self, prefix: str) -> list[str]:
        with self._connect() as conn:
            return [row["key"] for row in conn.execute("SELECT key FROM (SELECT key FROM hl_news_metadata UNION SELECT key FROM hl_news_metadata_large) m WHERE key LIKE %s ORDER BY key", (prefix + "%",)).fetchall()]

    def hyperliquid_news_task_sources(self) -> list[dict[str, Any]]:
        with connect_postgres(schema="main", application_name="odaily-hyperliquid-test-seeds") as conn:
            return conn.execute("SELECT title,content FROM main.tasks WHERE source<>'hyperliquid_news' AND (lower(content) LIKE '%hyperliquid%' OR lower(title) LIKE '%hyperliquid%') ORDER BY id DESC LIMIT 1500").fetchall()

    def save_test_preview_progress(self, key: str, value: dict[str, Any]) -> None:
        self.save_global(key, value, [])

    def save_global(self, key: str, value: Any, candidates: list[Candidate]) -> None:
        with self._connect() as conn:
            for event in candidates:
                self._insert_candidate(conn, event)
            self._save_metadata_in_transaction(conn, key, json.dumps(value, ensure_ascii=False))

    def recent_states(self, since_ms: int, *, state_table: str = "hl_news_state") -> dict[str, dict[str, Any]]:
        table = self._state_table(state_table)
        blocked = address_not_blocked_sql(f"{table}.address")
        with self._connect() as conn:
            rows = conn.execute(f"SELECT address,state,history FROM {table} WHERE observed_ms>=%s AND {blocked}", (since_ms,)).fetchall()
        return {row["address"]: {"state": row["state"], "history": row["history"]} for row in rows}

    def candidates(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT payload FROM hl_news_candidates ORDER BY observed_ms,event_key").fetchall()
        return [row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) for row in rows]

    def candidates_for_address(self, address: str, *, limit: int = 8) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT c.payload FROM hl_news_candidate_addresses a
                JOIN hl_news_candidates c USING(event_key) WHERE a.address_lower=%s
                ORDER BY a.observed_ms DESC LIMIT %s""", (address_key(address).lower(), max(1, min(limit, 50)))).fetchall()
        return [row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"]) for row in rows]

    def test_case_rows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT event_key,observed_ms,payload,preview,created_ms FROM hl_news_test_cases ORDER BY observed_ms DESC,event_key").fetchall()
        return [{**dict(row), "payload": row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])} for row in rows]

    def upsert_test_case(self, event: dict[str, Any]) -> None:
        now = int(time.time() * 1000)
        with self._connect() as conn:
            previous = conn.execute("SELECT payload FROM hl_news_test_cases WHERE event_key=%s FOR UPDATE", (event["key"],)).fetchone()
            payload = previous["payload"] if previous else None
            if isinstance(payload, str): payload = json.loads(payload)
            if payload:
                performance = payload.get("evidence", {}).get("history", {}).get("official_account_performance")
                if performance:
                    event["evidence"].setdefault("history", {})["official_account_performance"] = performance
                total_pnl = payload.get("evidence", {}).get("history", {}).get("official_account_total_pnl")
                if total_pnl:
                    event["evidence"].setdefault("history", {})["official_account_total_pnl"] = total_pnl
                if encode(payload) == encode(event):
                    return
            conn.execute("""INSERT INTO hl_news_test_cases(event_key,observed_ms,payload,preview,created_ms) VALUES(%s,%s,%s::jsonb,NULL,%s)
                ON CONFLICT(event_key) DO UPDATE SET observed_ms=excluded.observed_ms,payload=excluded.payload,preview=NULL""",
                (event["key"], int(event["observed_ms"]), encode(event), now))

    def delete_test_case(self, event_key: str) -> bool:
        with self._connect() as conn:
            return bool(conn.execute("DELETE FROM hl_news_test_cases WHERE event_key=%s", (event_key,)).rowcount)

    def test_case_count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT count(*) AS n FROM hl_news_test_cases").fetchone()["n"])

    def clear_test_cases(self) -> dict[str, int]:
        with self._connect() as conn:
            with conn.transaction():
                keys = [row["event_key"] for row in conn.execute("SELECT event_key FROM hl_news_test_cases").fetchall()]
                previews = conn.execute(
                    "DELETE FROM hl_news_metadata WHERE left(key,18)='editorial_preview:' "
                    "AND (substring(key from 19)=ANY(%s) OR position('editorial_preview:test:' in key)=1 "
                    "OR position('editorial_preview:test-price:' in key)=1 "
                    "OR position('editorial_preview:test-startup:' in key)=1)",
                    (keys,),
                ).rowcount
                removed = conn.execute("DELETE FROM hl_news_test_cases").rowcount
        return {"cases_removed": int(removed), "previews_removed": int(previews)}

    def prune_replayed_test_cases(self, address: str, keep: set[str]) -> int:
        rows = self.test_case_rows()
        remove = []
        for row in rows:
            event = row["payload"]
            replay = event.get("evidence", {}).get("test_replay", {})
            addresses = {str(value).lower() for value in event.get("addresses", [])}
            if address in addresses and replay.get("real_address") and row["event_key"] not in keep:
                remove.append(row["event_key"])
        with self._connect() as conn:
            with conn.transaction():
                for event_key in remove:
                    conn.execute("DELETE FROM hl_news_test_cases WHERE event_key=%s", (event_key,))
                    conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", ("editorial_preview:" + event_key,))
        return len(remove)

    def prune_test_cases(self, scenarios, production_scenarios: set[str], experimental_scenarios: set[str]) -> dict[str, int]:
        from .store import encode

        rows = self.test_case_rows()
        removed = retained = previews_removed = 0
        with self._connect() as conn:
            with conn.transaction():
                for row in rows:
                    event = row["payload"]
                    experimental = event.get("evidence", {}).get("test_replay", {}).get("thresholds_are_experimental")
                    unresolved = [key for key in scenarios(event)
                                  if key not in production_scenarios or experimental and key in experimental_scenarios]
                    preview_key = "editorial_preview:" + row["event_key"]
                    if not unresolved:
                        conn.execute("DELETE FROM hl_news_test_cases WHERE event_key=%s", (row["event_key"],))
                        previews_removed += conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", (preview_key,)).rowcount
                        removed += 1
                        continue
                    event["test_scenarios"] = unresolved
                    conn.execute("UPDATE hl_news_test_cases SET payload=%s::jsonb,preview=NULL WHERE event_key=%s",
                                 (encode(event), row["event_key"]))
                    previews_removed += conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", (preview_key,)).rowcount
                    retained += 1
        return {"retained": retained, "removed": removed, "previews_removed": previews_removed}

    def replace_test_case_payloads(self, items: list[tuple[str, dict[str, Any]]]) -> None:
        with self._connect() as conn:
            with conn.transaction():
                for event_key, event in items:
                    conn.execute("UPDATE hl_news_test_cases SET payload=%s::jsonb,preview=NULL WHERE event_key=%s",
                                 (encode(event), event_key))
                    conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", ("editorial_preview:" + event_key,))

    def test_position_snapshots(self, *, coins: list[str] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        with self._connect() as conn:
            result: dict[tuple[str, str], dict[str, Any]] = {}
            for table in ("hl_news_normal_state", "hl_news_state"):
                rows = conn.execute(f"SELECT address,observed_ms,state,history FROM {table}").fetchall()
                for row in rows:
                    state = row["state"] if isinstance(row["state"], dict) else json.loads(row["state"])
                    history = row["history"] if isinstance(row["history"], dict) else json.loads(row["history"])
                    for coin, position in state.get("positions", {}).items():
                        if coins and not any(coin.upper() == value.upper() or coin.upper().endswith(":" + value.upper()) for value in coins):
                            continue
                        key = (str(row["address"]).lower(), coin)
                        item = {"address": str(row["address"]).lower(), "observed_ms": int(row["observed_ms"]),
                                "state": state, "history": history, "coin": coin, "position": position}
                        if key not in result or item["observed_ms"] > result[key]["observed_ms"]:
                            result[key] = item
        return result

    def schedule_account(self, address: str, *, now_ms: int, leaderboard_qualified: bool = False) -> None:
        address = address_key(address)
        with self._connect() as conn:
            if self.blocked(address, conn=conn):
                return
            conn.execute("INSERT INTO hl_news_account_pool(address,next_poll_ms,qualified) VALUES(%s,%s,%s) ON CONFLICT(address) DO UPDATE SET next_poll_ms=LEAST(hl_news_account_pool.next_poll_ms,excluded.next_poll_ms)", (address, now_ms, leaderboard_qualified))

    def account_due(self, now_ms: int, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        blocked = address_not_blocked_sql("hl_news_account_pool.address")
        with self._connect() as conn:
            probe = conn.execute(f"SELECT * FROM hl_news_account_pool WHERE {blocked} AND qualified=false AND next_poll_ms<=%s ORDER BY next_poll_ms,address LIMIT 1", (now_ms,)).fetchall()
            active = conn.execute(f"SELECT * FROM hl_news_account_pool WHERE {blocked} AND qualified=true AND next_poll_ms<=%s ORDER BY next_poll_ms,address LIMIT %s", (now_ms, limit-len(probe))).fetchall()
            remaining = limit-len(probe)-len(active)
            extra = conn.execute(f"SELECT * FROM hl_news_account_pool WHERE {blocked} AND qualified=false AND next_poll_ms<=%s ORDER BY next_poll_ms,address LIMIT %s OFFSET %s", (now_ms, max(0, remaining), len(probe))).fetchall()
        return [dict(row) for row in [*active, *probe, *extra]]

    def update_account_schedule(self, address: str, *, now_ms: int, tier: str, interval_ms: int, qualified: bool, last_action_ms: int = 0) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE hl_news_account_pool SET tier=%s,qualified=%s,next_poll_ms=%s,last_action_ms=GREATEST(last_action_ms,%s) WHERE address=%s", (tier, qualified, now_ms+interval_ms, last_action_ms, address))

    def account_pool_stats(self, now_ms: int) -> dict[str, Any]:
        with self._connect() as conn:
            tiers = conn.execute("SELECT tier,count(*) AS n FROM hl_news_account_pool GROUP BY tier").fetchall()
            due = conn.execute("SELECT count(*) AS n,min(next_poll_ms) AS oldest FROM hl_news_account_pool WHERE qualified=true AND next_poll_ms<=%s", (now_ms,)).fetchone()
        return {"tiers": {row["tier"]: int(row["n"]) for row in tiers}, "qualified_due": int(due["n"]), "max_qualified_delay_ms": max(0, now_ms-int(due["oldest"])) if due["oldest"] is not None else 0}

    def blocked(self, address: str, *, conn=None) -> bool:
        if conn is None:
            with self._connect() as connection:
                return self.blocked(address, conn=connection)
        return bool(self._metadata_in_transaction(conn, "address_block:" + address_key(address), {}).get("blocked"))

    def is_manual_protected(self, address: str) -> bool:
        address = address_key(address)
        with self._connect(autocommit=True) as conn:
            row = conn.execute(
                "SELECT manual_locked FROM hl_news_address_names WHERE address=%s",
                (address,),
            ).fetchone()
            if row and bool(row["manual_locked"]):
                return True
        note = self.metadata("address_note:" + address, {})
        return isinstance(note, dict) and (note.get("origin") == "manual" or (bool(note.get("note")) and "origin" not in note))

    def event_blocked(self, event: dict[str, Any], *, conn=None) -> bool:
        if conn is None:
            with self._connect() as connection:
                return self.event_blocked(event, conn=connection)
        for address in event.get("addresses", []):
            rule = self._metadata_in_transaction(conn, "address_block:" + address_key(address), {})
            if rule and (rule.get("blocked") or int(event.get("observed_ms", 0)) <= int(rule.get("last_blocked_ms", -1))):
                return True
        return False

    def record_error(self, address: str, now_ms: int, error: str, *, normal_schedule: bool = False) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE hl_news_addresses SET last_polled_ms=%s,error=%s WHERE address=%s", (now_ms, error[:2000], address))
            if normal_schedule:
                conn.execute("INSERT INTO hl_news_normal_schedule(address,last_polled_ms) VALUES(%s,%s) ON CONFLICT(address) DO UPDATE SET last_polled_ms=excluded.last_polled_ms", (address, now_ms))

    def save_address_note(self, address: str, note: str, *, updated_by: str) -> dict[str, Any]:
        from .postgres_naming import PostgresNamingStore
        return PostgresNamingStore(self).set_manual(address, note, updated_by=updated_by)

    def with_address_notes(self, event: dict[str, Any]) -> dict[str, Any]:
        from .naming import resolve_address_attributions
        from .postgres_naming import PostgresNamingStore
        naming = PostgresNamingStore(self)
        return resolve_address_attributions(
            event,
            get_note=lambda addr: self.metadata("address_note:" + addr, {}),
            get_naming_row=naming.get,
        )

    def save_address_block(self, address: str, blocked: bool, *, updated_by: str) -> dict[str, Any]:
        from .naming import _now
        address = address_key(address)
        if not isinstance(blocked, bool):
            raise ValueError("blocked must be a boolean")
        now_ms = _now()
        value = {"address": address, "blocked": blocked, "updated_by": updated_by, "updated_at": datetime.now(UTC).isoformat()}
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM hl_news_addresses WHERE address=%s FOR UPDATE", (address,)).fetchone():
                raise ValueError("Unknown monitored address")
            previous = self._metadata_in_transaction(conn, "address_block:" + address, {})
            was_blocked = bool(previous.get("blocked"))
            value["last_blocked_ms"] = now_ms if blocked or was_blocked else int(previous.get("last_blocked_ms", -1))
            self._save_metadata_in_transaction(conn, "address_block:"+address, json.dumps(value))
            conn.execute("DELETE FROM hl_news_metadata_large WHERE key=%s", ("address_block:"+address,))
            if blocked:
                conn.execute("DELETE FROM hl_news_account_pool WHERE address=%s", (address,))
                for prefix in ("account_pending_fills:", "normal_pending_fills:"):
                    conn.execute("DELETE FROM hl_news_metadata WHERE key=%s", (prefix + address,))
                    conn.execute("DELETE FROM hl_news_metadata_large WHERE key=%s", (prefix + address,))
                rows = conn.execute("SELECT event_key FROM hl_news_candidates WHERE EXISTS (SELECT 1 FROM jsonb_array_elements_text(payload->'addresses') a(value) WHERE lower(a.value)=%s)", (address,)).fetchall()
                for row in rows:
                    conn.execute("INSERT INTO hl_news_dispatch(event_key,signature,task_id,status,error,created_at,updated_at) VALUES(%s,%s,NULL,'address_blocked',NULL,%s,%s) ON CONFLICT(event_key) DO NOTHING", (row["event_key"], "blocked:"+row["event_key"], datetime.now(UTC), datetime.now(UTC)))
            elif was_blocked:
                conn.execute("DELETE FROM hl_news_state WHERE address=%s", (address,))
                conn.execute("DELETE FROM hl_news_normal_state WHERE address=%s", (address,))
            if blocked:
                conn.execute("UPDATE hl_news_address_names SET status='blocked',current_name=NULL,inflight_token=NULL,inflight_started_ms=NULL WHERE address=%s AND manual_locked=false", (address,))
            elif was_blocked:
                conn.execute("UPDATE hl_news_address_names SET status='expired',next_check_ms=%s,current_name=NULL WHERE address=%s AND manual_locked=false", (now_ms, address))
        return value

    def commit_observation(self, *, address: str, now_ms: int, state: dict[str, Any], history: dict[str, Any], coverage: dict[str, Any], fills: list[dict[str, Any]], candidates: list[Candidate], state_table: str = "hl_news_state", check_compact_boundary: bool = False, expected_compact_through: int | None = None) -> None:
        table = self._state_table(state_table)
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (address,))
            if self.event_blocked({"addresses": [address], "observed_ms": now_ms}, conn=conn):
                return
            old = conn.execute(f"SELECT observed_ms FROM {table} WHERE address=%s FOR UPDATE", (address,)).fetchone()
            if old and int(old["observed_ms"]) >= now_ms:
                raise ValueError("Observation must advance monotonically")
            archive = conn.execute("SELECT through_ms FROM hl_news_compact_history WHERE address=%s", (address,)).fetchone()
            actual_through = int(archive["through_ms"]) if archive else None
            if check_compact_boundary and actual_through != expected_compact_through:
                raise ValueError("Compacted history changed during observation")
            if archive and any(int(fill["time"]) <= int(archive["through_ms"]) for fill in fills):
                raise ValueError("Late fill at or before compacted history boundary")
            for fill in fills:
                conn.execute("INSERT INTO hl_news_fills(address,fill_key,time_ms,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", (address, fill_key(fill), int(fill["time"]), json.dumps(fill, ensure_ascii=False)))
            if state.get("separate_fill_dedup"):
                monitor = "normal" if table == "hl_news_normal_state" else "account"
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO hl_news_monitor_fill_keys(monitor,address,fill_key,time_ms) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING", [(monitor,address,fill_key(fill),int(fill["time"])) for fill in fills])
                cursor = state.get("history_last_queried_ms" if monitor == "normal" else "account_history_checked_ms")
                if cursor is not None:
                    conn.execute("DELETE FROM hl_news_monitor_fill_keys WHERE monitor=%s AND address=%s AND time_ms<%s", (monitor,address,int(cursor)-60000))
            for event in candidates:
                self._insert_candidate(conn, event)
            conn.execute(f"INSERT INTO {table}(address,observed_ms,state,history,coverage) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(address) DO UPDATE SET observed_ms=excluded.observed_ms,state=excluded.state,history=excluded.history,coverage=excluded.coverage", (address,now_ms,json.dumps(state,ensure_ascii=False),json.dumps(history,ensure_ascii=False),json.dumps(coverage,ensure_ascii=False)))
            conn.execute("UPDATE hl_news_addresses SET last_polled_ms=%s,error=NULL WHERE address=%s", (now_ms,address))
            if table == "hl_news_normal_state":
                conn.execute("INSERT INTO hl_news_normal_schedule(address,last_polled_ms) VALUES(%s,%s) ON CONFLICT(address) DO UPDATE SET last_polled_ms=excluded.last_polled_ms", (address,now_ms))
            self._refresh_auto_position_name(conn, address, now_ms)

    @staticmethod
    def _refresh_auto_position_name(conn, address: str, now_ms: int) -> None:
        name = conn.execute("SELECT kind,coin,side,manual_locked,current_name FROM hl_news_address_names WHERE address=%s", (address,)).fetchone()
        if name and name["kind"] == "position" and not name["manual_locked"]:
            rows = conn.execute("SELECT observed_ms,state FROM hl_news_normal_state WHERE address=%s UNION ALL SELECT observed_ms,state FROM hl_news_state WHERE address=%s", (address,address)).fetchall()
            snapshots = [(int(row["observed_ms"]), row["state"] if isinstance(row["state"],dict) else json.loads(row["state"])) for row in rows]
            positions = max(snapshots,key=lambda item:item[0])[1].get("positions") or {} if snapshots else {}
            position = positions.get(name["coin"])
            size = float(position.get("size",0)) if isinstance(position,dict) else 0.0
            expected = 1 if name["side"] == "long" else -1
            if not position or size == 0 or (1 if size > 0 else -1) != expected:
                conn.execute("UPDATE hl_news_address_names SET status='expired',current_name=NULL,reason='position_changed_or_closed',next_check_ms=%s,retry_after_ms=0,inflight_token=NULL,inflight_started_ms=NULL WHERE address=%s AND manual_locked=false AND kind='position'", (now_ms,address))
                conn.execute("DELETE FROM hl_news_metadata WHERE key=%s AND (payload::jsonb)->>'origin'='auto'", ("address_note:"+address,))

    def wake_unqualified_accounts(self, now_ms: int) -> int:
        with self._connect() as conn:
            result = conn.execute("UPDATE hl_news_account_pool SET next_poll_ms=LEAST(next_poll_ms,%s) WHERE qualified=false", (now_ms,))
            return result.rowcount or 0

    def reset_normal_states(self) -> int:
        with self._connect() as conn:
            return conn.execute("DELETE FROM hl_news_normal_state").rowcount or 0
