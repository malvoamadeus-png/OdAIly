from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from packages.common.pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url

from .detector import normalize_evm_address
from .models import HyperliquidActivity, HyperliquidAddress, HyperliquidState


class WhaleWatchHyperliquidRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_addresses(self, *, include_disabled: bool = False) -> list[HyperliquidAddress]: ...
    def get_state(self, *, address_id: int) -> HyperliquidState | None: ...
    def mark_seeded(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime) -> None: ...
    def record_success(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime) -> None: ...
    def record_error(self, *, address_id: int, error: str, polled_at: datetime) -> None: ...
    def save_activity(self, *, whale: HyperliquidAddress, activity: HyperliquidActivity) -> bool: ...
    def update_activity_telegram_result(self, *, fill_key: str, telegram_result: dict[str, Any]) -> None: ...
    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None: ...


class PostgresWhaleWatchHyperliquidRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()

    def _connect(self):
        return self._psycopg.connect(self.database_url, row_factory=self._dict_row)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def list_addresses(self, *, include_disabled: bool = False) -> list[HyperliquidAddress]:
        where_sql = "" if include_disabled else "WHERE enabled = true"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, address, address_lower, label, enabled, created_at, updated_at
                FROM whale_watch_hyperliquid_addresses
                {where_sql}
                ORDER BY enabled DESC, label ASC, address_lower ASC
                """
            ).fetchall()
        return [_row_to_address(row) for row in rows]

    def get_state(self, *, address_id: int) -> HyperliquidState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT address_id, seeded_at, last_polled_at, last_success_at, last_error, last_seen_time
                FROM whale_watch_hyperliquid_states
                WHERE address_id = %s
                """,
                (address_id,),
            ).fetchone()
        return _row_to_state(row) if row else None

    def mark_seeded(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_hyperliquid_states (
                    address_id, seeded_at, last_polled_at, last_success_at, last_error, last_seen_time
                )
                VALUES (%s, %s, %s, %s, NULL, %s)
                ON CONFLICT (address_id) DO UPDATE SET
                    seeded_at = COALESCE(whale_watch_hyperliquid_states.seeded_at, EXCLUDED.seeded_at),
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error = NULL,
                    last_seen_time = GREATEST(
                        COALESCE(whale_watch_hyperliquid_states.last_seen_time, 0),
                        COALESCE(EXCLUDED.last_seen_time, 0)
                    ),
                    updated_at = now()
                """,
                (address_id, polled_at, polled_at, polled_at, last_seen_time),
            )
            conn.commit()

    def record_success(self, *, address_id: int, last_seen_time: int | None, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_hyperliquid_states (
                    address_id, last_polled_at, last_success_at, last_error, last_seen_time
                )
                VALUES (%s, %s, %s, NULL, %s)
                ON CONFLICT (address_id) DO UPDATE SET
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error = NULL,
                    last_seen_time = GREATEST(
                        COALESCE(whale_watch_hyperliquid_states.last_seen_time, 0),
                        COALESCE(EXCLUDED.last_seen_time, 0)
                    ),
                    updated_at = now()
                """,
                (address_id, polled_at, polled_at, last_seen_time),
            )
            conn.commit()

    def record_error(self, *, address_id: int, error: str, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_hyperliquid_states (address_id, last_polled_at, last_error)
                VALUES (%s, %s, %s)
                ON CONFLICT (address_id) DO UPDATE SET
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                """,
                (address_id, polled_at, error[:1000]),
            )
            conn.commit()

    def save_activity(self, *, whale: HyperliquidAddress, activity: HyperliquidActivity) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO whale_watch_hyperliquid_activities (
                    address_id, fill_key, coin, direction, side, price, size,
                    notional_usd, closed_pnl, tx_hash, fill_time, fill_time_ms,
                    summary, telegram_text, tx_url, raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address_id, fill_key) DO NOTHING
                RETURNING id
                """,
                (
                    whale.id,
                    activity.fill_key,
                    activity.coin,
                    activity.direction,
                    activity.side,
                    str(activity.price),
                    str(activity.size),
                    str(activity.notional_usd),
                    str(activity.closed_pnl),
                    str(activity.raw_payload.get("hash") or ""),
                    activity.fill_time,
                    activity.fill_time_ms,
                    activity.summary,
                    activity.telegram_text,
                    f"https://app.hyperliquid.xyz/explorer/address/{whale.address}",
                    self._Jsonb(activity.raw_payload),
                ),
            ).fetchone()
            conn.commit()
        return row is not None

    def update_activity_telegram_result(self, *, fill_key: str, telegram_result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE whale_watch_hyperliquid_activities
                SET telegram_result = %s,
                    telegram_sent_at = CASE WHEN (%s)::jsonb ->> 'ok' = 'true' THEN now() ELSE telegram_sent_at END,
                    updated_at = now()
                WHERE fill_key = %s
                """,
                (self._Jsonb(telegram_result), self._Jsonb(telegram_result), fill_key),
            )
            conn.commit()

    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pipeline_worker_heartbeats (
                    component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
                )
                VALUES (%s, %s, %s, now(), CASE WHEN %s THEN now() ELSE NULL END, %s, %s)
                ON CONFLICT (component, worker_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    last_seen_at = EXCLUDED.last_seen_at,
                    last_success_at = CASE
                        WHEN %s THEN EXCLUDED.last_success_at
                        ELSE pipeline_worker_heartbeats.last_success_at
                    END,
                    last_error = EXCLUDED.last_error,
                    metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (component, worker_id, status, success, error, self._Jsonb(metadata or {}), success),
            )
            conn.commit()


def _row_to_address(row: dict[str, Any]) -> HyperliquidAddress:
    return HyperliquidAddress(
        id=int(row["id"]),
        address=str(row["address"]),
        address_lower=str(row["address_lower"]),
        label=str(row["label"]),
        enabled=bool(row["enabled"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_state(row: dict[str, Any]) -> HyperliquidState:
    return HyperliquidState(
        address_id=int(row["address_id"]),
        seeded_at=row.get("seeded_at"),
        last_polled_at=row.get("last_polled_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        last_seen_time=row.get("last_seen_time"),
    )


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + CONSOLE_AUTH_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_addresses (
    id bigserial PRIMARY KEY,
    address text NOT NULL,
    address_lower text NOT NULL UNIQUE,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT whale_watch_hyperliquid_address_format CHECK (address_lower ~ '^0x[0-9a-f]{40}$')
);

CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_states (
    address_id bigint PRIMARY KEY REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    last_seen_time bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS whale_watch_hyperliquid_activities (
    id bigserial PRIMARY KEY,
    address_id bigint NOT NULL REFERENCES whale_watch_hyperliquid_addresses(id) ON DELETE CASCADE,
    fill_key text NOT NULL,
    coin text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('Open Long', 'Open Short', 'Close Long', 'Close Short')),
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
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_sent_at timestamptz,
    tx_url text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (address_id, fill_key)
);

CREATE INDEX IF NOT EXISTS idx_whale_watch_hl_addresses_enabled
ON whale_watch_hyperliquid_addresses(enabled, address_lower);

CREATE INDEX IF NOT EXISTS idx_whale_watch_hl_activities_created
ON whale_watch_hyperliquid_activities(created_at DESC);

CREATE OR REPLACE FUNCTION notify_whale_watch_config_changed()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'whale_watch_config_changed',
        json_build_object('table', TG_TABLE_NAME, 'op', TG_OP)::text
    );
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_whale_watch_hyperliquid_addresses_notify ON whale_watch_hyperliquid_addresses;
CREATE TRIGGER trg_whale_watch_hyperliquid_addresses_notify
AFTER INSERT OR UPDATE OR DELETE ON whale_watch_hyperliquid_addresses
FOR EACH ROW EXECUTE FUNCTION notify_whale_watch_config_changed();

ALTER TABLE whale_watch_hyperliquid_addresses ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_hyperliquid_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_hyperliquid_activities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS whale_watch_hyperliquid_addresses_console_admin_all ON whale_watch_hyperliquid_addresses;
DROP POLICY IF EXISTS whale_watch_hyperliquid_states_console_admin_select ON whale_watch_hyperliquid_states;
DROP POLICY IF EXISTS whale_watch_hyperliquid_activities_console_admin_select ON whale_watch_hyperliquid_activities;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON whale_watch_hyperliquid_addresses, whale_watch_hyperliquid_states, whale_watch_hyperliquid_activities FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE whale_watch_hyperliquid_addresses_id_seq, whale_watch_hyperliquid_activities_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON whale_watch_hyperliquid_addresses TO authenticated;
        GRANT SELECT ON whale_watch_hyperliquid_states, whale_watch_hyperliquid_activities TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE whale_watch_hyperliquid_addresses_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY whale_watch_hyperliquid_addresses_console_admin_all ON whale_watch_hyperliquid_addresses
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_hyperliquid_states_console_admin_select ON whale_watch_hyperliquid_states
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_hyperliquid_activities_console_admin_select ON whale_watch_hyperliquid_activities
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
"""


def normalize_hyperliquid_address_for_storage(value: str) -> tuple[str, str]:
    address = normalize_evm_address(value)
    return address, address.lower()
