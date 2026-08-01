from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings

from .models import Activity, ChainState, WhaleAddress


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def record_heartbeat(path: Path, *, component: str, worker_id: str, status: str, success: bool,
                     error: str | None, metadata: dict[str, Any] | None) -> None:
    now = _iso(datetime.now(UTC))
    with connect_sqlite(path) as conn:
        conn.execute("""INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,
            last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END,
            last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP""",
            (component, worker_id, status, now, now if success else None, error, json.dumps(metadata or {}, ensure_ascii=False), success))
        conn.commit()


class SQLiteWhaleWatchRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS whale_watch_addresses (
                    id integer PRIMARY KEY AUTOINCREMENT,address text NOT NULL,address_lower text NOT NULL UNIQUE,label text NOT NULL,
                    enabled integer NOT NULL DEFAULT 1,created_by text,updated_by text,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK(length(address_lower)=42 AND substr(address_lower,1,2)='0x' AND address_lower NOT GLOB '*[^0-9a-fx]*'));
                CREATE TRIGGER IF NOT EXISTS whale_watch_addresses_limit
                BEFORE INSERT ON whale_watch_addresses
                WHEN (SELECT count(*) FROM whale_watch_addresses) >= 500
                     AND NOT EXISTS (SELECT 1 FROM whale_watch_addresses WHERE address_lower=NEW.address_lower)
                BEGIN SELECT RAISE(ABORT, 'whale address limit reached'); END;
                CREATE TABLE IF NOT EXISTS whale_watch_chain_states (
                    address_id integer NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,chain_key text NOT NULL,
                    seeded_at text,last_polled_at text,last_success_at text,last_error text,last_seen_block integer,
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(address_id,chain_key));
                CREATE TABLE IF NOT EXISTS whale_watch_activities (
                    id integer PRIMARY KEY AUTOINCREMENT,address_id integer NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,
                    chain_key text NOT NULL,tx_hash text NOT NULL,activity_fingerprint text NOT NULL,activity_type text NOT NULL,
                    direction text,counterparty text,block_number integer NOT NULL,tx_timestamp text,summary text NOT NULL,
                    telegram_text text NOT NULL,telegram_result text NOT NULL DEFAULT '{}',telegram_sent_at text,tx_url text NOT NULL,
                    raw_payload text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(address_id,chain_key,tx_hash,activity_fingerprint));
                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL,worker_id text NOT NULL,status text NOT NULL,last_seen_at text NOT NULL,last_success_at text,last_error text,
                    metadata text NOT NULL DEFAULT '{}',created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component,worker_id));
            """)
            conn.commit()

    def list_addresses(self, *, include_disabled: bool = False) -> list[WhaleAddress]:
        self.init_schema()
        where = "" if include_disabled else "WHERE enabled=1"
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f"SELECT * FROM whale_watch_addresses {where} ORDER BY enabled DESC,label,address_lower").fetchall()
        return [self._address(row) for row in rows]

    def list_addresses_created_since(self, *, since: datetime) -> list[WhaleAddress]:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            rows = conn.execute("SELECT * FROM whale_watch_addresses WHERE created_at>=? ORDER BY created_at DESC,id DESC", (_iso(since),)).fetchall()
        return [self._address(row) for row in rows]

    def delete_addresses(self, *, ids: list[int]) -> int:
        if not ids: return 0
        self.init_schema()
        placeholders = ",".join("?" for _ in ids)
        with connect_sqlite(self.path) as conn:
            cursor = conn.execute(f"DELETE FROM whale_watch_addresses WHERE id IN ({placeholders})", ids)
            conn.commit()
        return cursor.rowcount

    def get_chain_state(self, *, address_id: int, chain_key: str) -> ChainState | None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            row = conn.execute("SELECT * FROM whale_watch_chain_states WHERE address_id=? AND chain_key=?", (address_id, chain_key)).fetchone()
        if not row: return None
        return ChainState(address_id=int(row["address_id"]),chain_key=str(row["chain_key"]),seeded_at=_dt(row["seeded_at"]),
                          last_polled_at=_dt(row["last_polled_at"]),last_success_at=_dt(row["last_success_at"]),
                          last_error=row["last_error"],last_seen_block=row["last_seen_block"])

    def _upsert_state(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime, seeded: bool) -> None:
        self.init_schema(); stamp = _iso(polled_at)
        with connect_sqlite(self.path) as conn:
            conn.execute("""INSERT INTO whale_watch_chain_states(address_id,chain_key,seeded_at,last_polled_at,last_success_at,last_seen_block)
                VALUES(?,?,?,?,?,?) ON CONFLICT(address_id,chain_key) DO UPDATE SET seeded_at=CASE WHEN ? THEN COALESCE(whale_watch_chain_states.seeded_at,excluded.seeded_at) ELSE whale_watch_chain_states.seeded_at END,
                last_polled_at=excluded.last_polled_at,last_success_at=excluded.last_success_at,last_error=NULL,
                last_seen_block=MAX(COALESCE(whale_watch_chain_states.last_seen_block,0),COALESCE(excluded.last_seen_block,0)),updated_at=CURRENT_TIMESTAMP""",
                (address_id,chain_key,stamp if seeded else None,stamp,stamp,block_number,seeded))
            conn.commit()

    def mark_chain_seeded(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        self._upsert_state(address_id=address_id,chain_key=chain_key,block_number=block_number,polled_at=polled_at,seeded=True)

    def record_chain_success(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        self._upsert_state(address_id=address_id,chain_key=chain_key,block_number=block_number,polled_at=polled_at,seeded=False)

    def record_chain_error(self, *, address_id: int, chain_key: str, error: str, polled_at: datetime) -> None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            conn.execute("""INSERT INTO whale_watch_chain_states(address_id,chain_key,last_polled_at,last_error) VALUES(?,?,?,?)
                ON CONFLICT(address_id,chain_key) DO UPDATE SET last_polled_at=excluded.last_polled_at,last_error=excluded.last_error,updated_at=CURRENT_TIMESTAMP""",
                (address_id,chain_key,_iso(polled_at),error[:1000]))
            conn.commit()

    def save_activity(self, *, whale: WhaleAddress, chain_key: str, activity: Activity) -> int | None:
        self.init_schema()
        with connect_sqlite(self.path) as conn:
            cursor = conn.execute("""INSERT OR IGNORE INTO whale_watch_activities(address_id,chain_key,tx_hash,activity_fingerprint,activity_type,
                direction,counterparty,block_number,tx_timestamp,summary,telegram_text,tx_url,raw_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (whale.id,chain_key,activity.tx_hash,activity.fingerprint,activity.kind,activity.direction,activity.counterparty,
                 activity.block_number,_iso(activity.timestamp),activity.summary,activity.telegram_text,activity.tx_url,json.dumps(activity.raw_payload,ensure_ascii=False)))
            conn.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None

    def update_activity_telegram_result(self, *, tx_hash: str, fingerprint: str, telegram_result: dict[str, Any]) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE whale_watch_activities SET telegram_result=?,telegram_sent_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE telegram_sent_at END,updated_at=CURRENT_TIMESTAMP WHERE tx_hash=? AND activity_fingerprint=?",
                         (json.dumps(telegram_result,ensure_ascii=False),bool(telegram_result.get("ok")),tx_hash,fingerprint)); conn.commit()

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.init_schema(); record_heartbeat(self.path,component=component,worker_id=worker_id,status=status,success=success,error=error,metadata=metadata)

    @staticmethod
    def _address(row) -> WhaleAddress:
        return WhaleAddress(id=int(row["id"]),address=str(row["address"]),address_lower=str(row["address_lower"]),label=str(row["label"]),
                            enabled=bool(row["enabled"]),created_by=row["created_by"],updated_by=row["updated_by"],
                            created_at=_dt(row["created_at"]),updated_at=_dt(row["updated_at"]))


def create_whale_watch_repository(database_url: str | None = None) -> SQLiteWhaleWatchRepository:
    del database_url
    return SQLiteWhaleWatchRepository(load_storage_settings().sqlite_path)
