from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings

from .hyperliquid_repository import _deserialize_window_entries, _serialize_window_entries
from .models import HyperliquidActivity, HyperliquidAddress, HyperliquidRuntimeSettings, HyperliquidState, HyperliquidWindowEntry
from .sqlite_repository import _dt, _iso, record_heartbeat


class SQLiteWhaleWatchHyperliquidRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_settings (
                    singleton_key text PRIMARY KEY CHECK(singleton_key='global'),single_fill_min_notional_usd text NOT NULL,
                    aggregate_min_notional_usd text NOT NULL,aggregate_window_seconds integer NOT NULL,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_addresses (
                    id integer PRIMARY KEY AUTOINCREMENT,address text NOT NULL,address_lower text NOT NULL UNIQUE,label text NOT NULL,
                    enabled integer NOT NULL DEFAULT 1,created_by text,updated_by text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(length(address_lower)=42 AND substr(address_lower,1,2)='0x' AND address_lower NOT GLOB '*[^0-9a-fx]*'));
                CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_states (
                    address_id integer PRIMARY KEY REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
                    seeded_at text,last_polled_at text,last_success_at text,last_error text,last_seen_time integer,
                    aggregate_window_entries text NOT NULL DEFAULT '[]',aggregate_alert_active integer NOT NULL DEFAULT 0,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_activities (
                    id integer PRIMARY KEY AUTOINCREMENT,address_id integer NOT NULL REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
                    fill_key text NOT NULL,coin text NOT NULL,direction text NOT NULL,side text NOT NULL,price text NOT NULL,size text NOT NULL,
                    notional_usd text NOT NULL,closed_pnl text NOT NULL,tx_hash text,fill_time text NOT NULL,fill_time_ms integer NOT NULL,
                    summary text NOT NULL,telegram_text text NOT NULL,alert_kind text NOT NULL DEFAULT 'single',aggregate_fill_count integer,
                    telegram_result text NOT NULL DEFAULT '{}',telegram_sent_at text,tx_url text NOT NULL,raw_payload text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(address_id,fill_key));
                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL,worker_id text NOT NULL,status text NOT NULL,last_seen_at text NOT NULL,last_success_at text,last_error text,
                    metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component,worker_id));
            """)
            conn.commit()

    def get_runtime_settings(self, *, default_single_fill_min_notional_usd: Decimal, default_aggregate_min_notional_usd: Decimal,
                             default_aggregate_window_seconds: int) -> HyperliquidRuntimeSettings:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            conn.execute("INSERT OR IGNORE INTO whale_watch_hyperliquid_settings(singleton_key,single_fill_min_notional_usd,aggregate_min_notional_usd,aggregate_window_seconds) VALUES('global',?,?,?)",
                         (str(default_single_fill_min_notional_usd),str(default_aggregate_min_notional_usd),default_aggregate_window_seconds))
            row = conn.execute("SELECT * FROM whale_watch_hyperliquid_settings WHERE singleton_key='global'").fetchone(); conn.commit()
        return HyperliquidRuntimeSettings(single_fill_min_notional_usd=Decimal(row["single_fill_min_notional_usd"]),
            aggregate_min_notional_usd=Decimal(row["aggregate_min_notional_usd"]),aggregate_window_seconds=int(row["aggregate_window_seconds"]),updated_at=_dt(row["updated_at"]))

    def list_addresses(self, *, include_disabled: bool = False) -> list[HyperliquidAddress]:
        self.init_schema(); where="" if include_disabled else "WHERE enabled=1"
        with connect_sqlite(self.path) as conn: rows=conn.execute(f"SELECT * FROM whale_watch_hyperliquid_addresses {where} ORDER BY enabled DESC,label,address_lower").fetchall()
        return [HyperliquidAddress(id=int(r["id"]),address=r["address"],address_lower=r["address_lower"],label=r["label"],enabled=bool(r["enabled"]),
                 created_by=r["created_by"],updated_by=r["updated_by"],created_at=_dt(r["created_at"]),updated_at=_dt(r["updated_at"])) for r in rows]

    def get_state(self, *, address_id: int) -> HyperliquidState | None:
        self.init_schema()
        with connect_sqlite(self.path) as conn: row=conn.execute("SELECT * FROM whale_watch_hyperliquid_states WHERE address_id=?",(address_id,)).fetchone()
        if not row:return None
        return HyperliquidState(address_id=int(row["address_id"]),seeded_at=_dt(row["seeded_at"]),last_polled_at=_dt(row["last_polled_at"]),
            last_success_at=_dt(row["last_success_at"]),last_error=row["last_error"],last_seen_time=row["last_seen_time"],
            aggregate_window_entries=tuple(_deserialize_window_entries(json.loads(row["aggregate_window_entries"] or "[]"))),aggregate_alert_active=bool(row["aggregate_alert_active"]))

    def _upsert_state(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                      aggregate_window_entries: list[HyperliquidWindowEntry] | None, aggregate_alert_active: bool | None, seeded: bool) -> None:
        self.init_schema(); stamp=_iso(polled_at); entries=json.dumps(_serialize_window_entries(aggregate_window_entries or []),ensure_ascii=False) if aggregate_window_entries is not None else None
        with connect_sqlite(self.path) as conn:
            conn.execute("""INSERT INTO whale_watch_hyperliquid_states(address_id,seeded_at,last_polled_at,last_success_at,last_seen_time,aggregate_window_entries,aggregate_alert_active)
                VALUES(?,?,?,?,?,COALESCE(?,'[]'),COALESCE(?,0)) ON CONFLICT(address_id) DO UPDATE SET
                seeded_at=CASE WHEN ? THEN COALESCE(whale_watch_hyperliquid_states.seeded_at,excluded.seeded_at) ELSE whale_watch_hyperliquid_states.seeded_at END,
                last_polled_at=excluded.last_polled_at,last_success_at=excluded.last_success_at,last_error=NULL,
                last_seen_time=MAX(COALESCE(whale_watch_hyperliquid_states.last_seen_time,0),COALESCE(excluded.last_seen_time,0)),
                aggregate_window_entries=COALESCE(?,whale_watch_hyperliquid_states.aggregate_window_entries),
                aggregate_alert_active=COALESCE(?,whale_watch_hyperliquid_states.aggregate_alert_active),updated_at=CURRENT_TIMESTAMP""",
                (address_id,stamp if seeded else None,stamp,stamp,last_seen_time,entries,aggregate_alert_active,seeded,entries,aggregate_alert_active)); conn.commit()

    def mark_seeded(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                    aggregate_window_entries: list[HyperliquidWindowEntry] | None = None, aggregate_alert_active: bool = False) -> None:
        self._upsert_state(address_id=address_id,last_seen_time=last_seen_time,polled_at=polled_at,aggregate_window_entries=aggregate_window_entries,aggregate_alert_active=aggregate_alert_active,seeded=True)

    def record_success(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                       aggregate_window_entries: list[HyperliquidWindowEntry] | None = None, aggregate_alert_active: bool | None = None) -> None:
        self._upsert_state(address_id=address_id,last_seen_time=last_seen_time,polled_at=polled_at,aggregate_window_entries=aggregate_window_entries,aggregate_alert_active=aggregate_alert_active,seeded=False)

    def record_error(self, *, address_id: int, error: str, polled_at: datetime) -> None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            conn.execute("""INSERT INTO whale_watch_hyperliquid_states(address_id,last_polled_at,last_error) VALUES(?,?,?)
                ON CONFLICT(address_id) DO UPDATE SET last_polled_at=excluded.last_polled_at,last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP""",
                (address_id,_iso(polled_at),error[:1000])); conn.commit()

    def save_activity(self, *, whale: HyperliquidAddress, activity: HyperliquidActivity) -> int | None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            cursor=conn.execute("""INSERT OR IGNORE INTO whale_watch_hyperliquid_activities(address_id,fill_key,coin,direction,side,price,size,notional_usd,closed_pnl,
                tx_hash,fill_time,fill_time_ms,summary,telegram_text,tx_url,raw_payload,alert_kind,aggregate_fill_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (whale.id,activity.fill_key,activity.coin,activity.direction,activity.side,str(activity.price),str(activity.size),str(activity.notional_usd),
                 str(activity.closed_pnl),str(activity.raw_payload.get("hash") or ""),_iso(activity.fill_time),activity.fill_time_ms,activity.summary,
                 activity.telegram_text,f"https://hyperbot.network/trader/{whale.address}",json.dumps(activity.raw_payload,ensure_ascii=False),activity.alert_kind,activity.aggregate_fill_count)); conn.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def update_activity_telegram_result(self, *, fill_key: str, telegram_result: dict[str, Any]) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE whale_watch_hyperliquid_activities SET telegram_result=?,telegram_sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE telegram_sent_at END,updated_at=CURRENT_TIMESTAMP WHERE fill_key=?",
                (json.dumps(telegram_result,ensure_ascii=False),bool(telegram_result.get("ok")),fill_key)); conn.commit()

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.init_schema(); record_heartbeat(self.path,component=component,worker_id=worker_id,status=status,success=success,error=error,metadata=metadata)


def create_whale_watch_hyperliquid_repository(database_url: str | None = None) -> SQLiteWhaleWatchHyperliquidRepository:
    del database_url
    return SQLiteWhaleWatchHyperliquidRepository(load_storage_settings().sqlite_path)
