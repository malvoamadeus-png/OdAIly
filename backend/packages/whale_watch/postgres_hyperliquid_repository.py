from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any

from packages.common.pipeline_schema import PIPELINE_MONITORING_SCHEMA_SQL
from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction
from packages.system_observability.postgres_schema import SourceStateTriggerSpec, ensure_source_state_schema, install_source_state_trigger

from .hyperliquid_sqlite_repository import _serialize_window_entries, _deserialize_window_entries
from .models import HyperliquidActivity, HyperliquidAddress, HyperliquidRuntimeSettings, HyperliquidState, HyperliquidWindowEntry


HYPERLIQUID_SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_settings (
    singleton_key text PRIMARY KEY CHECK (singleton_key = 'global'),
    single_fill_min_notional_usd text NOT NULL,
    aggregate_min_notional_usd text NOT NULL,
    aggregate_window_seconds integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_addresses (
    id bigserial PRIMARY KEY,
    address text NOT NULL,
    address_lower text NOT NULL UNIQUE,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_by text,
    updated_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (address_lower ~ '^0x[0-9a-f]{40}$')
);

CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_states (
    address_id bigint PRIMARY KEY REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    last_seen_time bigint,
    aggregate_window_entries jsonb NOT NULL DEFAULT '[]'::jsonb,
    aggregate_alert_active boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_activities (
    id bigserial PRIMARY KEY,
    address_id bigint NOT NULL REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
    fill_key text NOT NULL,
    coin text NOT NULL,
    direction text NOT NULL,
    side text NOT NULL,
    price text NOT NULL,
    size text NOT NULL,
    notional_usd text NOT NULL,
    closed_pnl text NOT NULL,
    tx_hash text,
    fill_time timestamptz NOT NULL,
    fill_time_ms bigint NOT NULL,
    summary text NOT NULL,
    telegram_text text NOT NULL,
    alert_kind text NOT NULL DEFAULT 'single',
    aggregate_fill_count integer,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_sent_at timestamptz,
    tx_url text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (address_id, fill_key)
);
"""


def _iso(value: datetime | None) -> datetime | None:
    return value.astimezone(UTC) if value else None


def _address(row: dict[str, Any]) -> HyperliquidAddress:
    return HyperliquidAddress(
        id=int(row["id"]), address=str(row["address"]), address_lower=str(row["address_lower"]),
        label=str(row["label"]), enabled=bool(row["enabled"]), created_by=row.get("created_by"),
        updated_by=row.get("updated_by"), created_at=row.get("created_at"), updated_at=row.get("updated_at"),
    )


class PostgresWhaleWatchHyperliquidRepository:
    def __init__(self, database_url: str | None = None) -> None:
        if database_url is not None:
            raise ValueError("Configure ODAILY_POSTGRES_DSN instead of passing a database URL")
        from psycopg.types.json import Jsonb

        self._Jsonb = Jsonb
        self.application_name = "odaily-whale-watch-hyperliquid"

    @contextmanager
    def _connect(self, *, autocommit: bool = False):
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            if autocommit:
                yield connection
            else:
                with postgres_transaction(connection):
                    yield connection

    def init_schema(self) -> None:
        self.initialize_schema()

    @staticmethod
    def initialize_schema() -> None:
        with connect_postgres(schema="main", application_name="odaily-whale-watch-hyperliquid-migrate") as connection:
            trigger_spec = ("whale_watch_hyperliquid_addresses", ("id",), ("address", "address_lower", "label", "enabled"))

            def apply(conn):
                conn.execute(HYPERLIQUID_SCHEMA_SQL)
                ensure_source_state_schema(conn)
                install_source_state_trigger(conn, SourceStateTriggerSpec(
                    schema="main", table=trigger_spec[0], row_id_columns=trigger_spec[1], payload_columns=trigger_spec[2],
                ))

            apply_schema_migrations(
                connection,
                schema="main",
                component="whale_watch_hyperliquid",
                migrations=(PostgresMigration(
                    1, "whale_watch_hyperliquid_initial",
                    sha256((HYPERLIQUID_SCHEMA_SQL + repr(trigger_spec)).encode()).hexdigest(), apply,
                ),),
            )

    def get_runtime_settings(self, *, default_single_fill_min_notional_usd: Decimal,
                             default_aggregate_min_notional_usd: Decimal,
                             default_aggregate_window_seconds: int) -> HyperliquidRuntimeSettings:
        self.init_schema()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO whale_watch_hyperliquid_settings(
                    singleton_key,single_fill_min_notional_usd,aggregate_min_notional_usd,aggregate_window_seconds
                ) VALUES('global',%s,%s,%s) ON CONFLICT(singleton_key) DO NOTHING""",
                (str(default_single_fill_min_notional_usd), str(default_aggregate_min_notional_usd), default_aggregate_window_seconds),
            )
            row = connection.execute("SELECT * FROM whale_watch_hyperliquid_settings WHERE singleton_key='global'").fetchone()
        return HyperliquidRuntimeSettings(
            single_fill_min_notional_usd=Decimal(row["single_fill_min_notional_usd"]),
            aggregate_min_notional_usd=Decimal(row["aggregate_min_notional_usd"]),
            aggregate_window_seconds=int(row["aggregate_window_seconds"]), updated_at=row["updated_at"],
        )

    def list_addresses(self, *, include_disabled: bool = False) -> list[HyperliquidAddress]:
        self.init_schema()
        where = "" if include_disabled else "WHERE enabled=true"
        with self._connect(autocommit=True) as connection:
            rows = connection.execute(f"SELECT * FROM whale_watch_hyperliquid_addresses {where} ORDER BY enabled DESC,label,address_lower").fetchall()
        return [_address(row) for row in rows]

    def get_state(self, *, address_id: int) -> HyperliquidState | None:
        self.init_schema()
        with self._connect(autocommit=True) as connection:
            row = connection.execute("SELECT * FROM whale_watch_hyperliquid_states WHERE address_id=%s", (address_id,)).fetchone()
        if not row:
            return None
        return HyperliquidState(
            address_id=int(row["address_id"]), seeded_at=row["seeded_at"], last_polled_at=row["last_polled_at"],
            last_success_at=row["last_success_at"], last_error=row["last_error"], last_seen_time=row["last_seen_time"],
            aggregate_window_entries=tuple(_deserialize_window_entries(row["aggregate_window_entries"])),
            aggregate_alert_active=bool(row["aggregate_alert_active"]),
        )

    def _upsert_state(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                      aggregate_window_entries: list[HyperliquidWindowEntry] | None,
                      aggregate_alert_active: bool | None, seeded: bool) -> None:
        self.init_schema()
        entries = self._Jsonb(_serialize_window_entries(aggregate_window_entries)) if aggregate_window_entries is not None else None
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO whale_watch_hyperliquid_states(
                    address_id,seeded_at,last_polled_at,last_success_at,last_seen_time,aggregate_window_entries,aggregate_alert_active
                ) VALUES(%s,%s,%s,%s,%s,COALESCE(%s,'[]'::jsonb),COALESCE(%s,false))
                ON CONFLICT(address_id) DO UPDATE SET
                    seeded_at=CASE WHEN %s THEN COALESCE(whale_watch_hyperliquid_states.seeded_at,EXCLUDED.seeded_at) ELSE whale_watch_hyperliquid_states.seeded_at END,
                    last_polled_at=EXCLUDED.last_polled_at,last_success_at=EXCLUDED.last_success_at,last_error=NULL,
                    last_seen_time=GREATEST(COALESCE(whale_watch_hyperliquid_states.last_seen_time,0),COALESCE(EXCLUDED.last_seen_time,0)),
                    aggregate_window_entries=COALESCE(%s,whale_watch_hyperliquid_states.aggregate_window_entries),
                    aggregate_alert_active=COALESCE(%s,whale_watch_hyperliquid_states.aggregate_alert_active),updated_at=now()""",
                (address_id, _iso(polled_at) if seeded else None, _iso(polled_at), _iso(polled_at), last_seen_time,
                 entries, aggregate_alert_active, seeded, entries, aggregate_alert_active),
            )

    def mark_seeded(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                    aggregate_window_entries: list[HyperliquidWindowEntry] | None = None,
                    aggregate_alert_active: bool = False) -> None:
        self._upsert_state(address_id=address_id, last_seen_time=last_seen_time, polled_at=polled_at,
                            aggregate_window_entries=aggregate_window_entries, aggregate_alert_active=aggregate_alert_active, seeded=True)

    def record_success(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime,
                       aggregate_window_entries: list[HyperliquidWindowEntry] | None = None,
                       aggregate_alert_active: bool | None = None) -> None:
        self._upsert_state(address_id=address_id, last_seen_time=last_seen_time, polled_at=polled_at,
                            aggregate_window_entries=aggregate_window_entries, aggregate_alert_active=aggregate_alert_active, seeded=False)

    def record_error(self, *, address_id: int, error: str, polled_at: datetime) -> None:
        self.init_schema()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO whale_watch_hyperliquid_states(address_id,last_polled_at,last_error) VALUES(%s,%s,%s)
                ON CONFLICT(address_id) DO UPDATE SET last_polled_at=EXCLUDED.last_polled_at,last_error=EXCLUDED.last_error,updated_at=now()""",
                (address_id, _iso(polled_at), error[:1000]),
            )

    def save_activity(self, *, whale: HyperliquidAddress, activity: HyperliquidActivity) -> int | None:
        self.init_schema()
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO whale_watch_hyperliquid_activities(
                    address_id,fill_key,coin,direction,side,price,size,notional_usd,closed_pnl,tx_hash,fill_time,fill_time_ms,
                    summary,telegram_text,tx_url,raw_payload,alert_kind,aggregate_fill_count
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(address_id,fill_key) DO NOTHING RETURNING id""",
                (whale.id, activity.fill_key, activity.coin, activity.direction, activity.side, str(activity.price), str(activity.size),
                 str(activity.notional_usd), str(activity.closed_pnl), str(activity.raw_payload.get("hash") or ""),
                 _iso(activity.fill_time), activity.fill_time_ms, activity.summary, activity.telegram_text,
                 f"https://hyperbot.network/trader/{whale.address}", self._Jsonb(activity.raw_payload), activity.alert_kind,
                 activity.aggregate_fill_count),
            ).fetchone()
        return int(row["id"]) if row else None

    def update_activity_telegram_result(self, *, fill_key: str, telegram_result: dict[str, Any]) -> None:
        self.init_schema()
        with self._connect() as connection:
            connection.execute(
                """UPDATE whale_watch_hyperliquid_activities SET telegram_result=%s,
                telegram_sent_at=CASE WHEN (%s)::jsonb->>'ok'='true' THEN now() ELSE telegram_sent_at END,
                updated_at=now() WHERE fill_key=%s""",
                (self._Jsonb(telegram_result), self._Jsonb(telegram_result), fill_key),
            )

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool,
                                error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        self.init_schema()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata)
                VALUES(%s,%s,%s,now(),CASE WHEN %s THEN now() ELSE NULL END,%s,%s)
                ON CONFLICT(component,worker_id) DO UPDATE SET status=EXCLUDED.status,last_seen_at=EXCLUDED.last_seen_at,
                last_success_at=CASE WHEN %s THEN EXCLUDED.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END,
                last_error=EXCLUDED.last_error,metadata=EXCLUDED.metadata,updated_at=now()""",
                (component, worker_id, status, success, error[:2000] if error else None, self._Jsonb(metadata or {}), success),
            )


__all__ = ["PostgresWhaleWatchHyperliquidRepository"]
