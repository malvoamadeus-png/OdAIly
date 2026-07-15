from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from packages.common.postgres import build_psycopg_connect_kwargs
from packages.common.pipeline_schema import CONSOLE_AUTH_SCHEMA_SQL, PIPELINE_MONITORING_SCHEMA_SQL
from packages.x_processing.repository import _import_psycopg, get_database_url

from .detector import normalize_evm_address
from .models import Activity, ChainState, WhaleAddress


CONFIG_NOTIFY_CHANNEL = "whale_watch_config_changed"


class WhaleWatchRepository(Protocol):
    def init_schema(self) -> None: ...
    def list_addresses(self, *, include_disabled: bool = False) -> list[WhaleAddress]: ...
    def list_addresses_created_since(self, *, since: datetime) -> list[WhaleAddress]: ...
    def delete_addresses(self, *, ids: list[int]) -> int: ...
    def get_chain_state(self, *, address_id: int, chain_key: str) -> ChainState | None: ...
    def mark_chain_seeded(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None: ...
    def record_chain_success(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None: ...
    def record_chain_error(self, *, address_id: int, chain_key: str, error: str, polled_at: datetime) -> None: ...
    def save_activity(self, *, whale: WhaleAddress, chain_key: str, activity: Activity) -> bool: ...
    def update_activity_telegram_result(self, *, tx_hash: str, fingerprint: str, telegram_result: dict[str, Any]) -> None: ...
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


class PostgresWhaleWatchRepository:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or get_database_url()
        self._psycopg, self._dict_row, self._Jsonb = _import_psycopg()
        self.application_name = "odaily-whale-watch"

    def _connect(self, *, autocommit: bool = False):
        return self._psycopg.connect(
            self.database_url,
            **build_psycopg_connect_kwargs(
                row_factory=self._dict_row,
                autocommit=autocommit,
                application_name=self.application_name,
            ),
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(SCHEMA_SQL)
            conn.commit()

    def list_addresses(self, *, include_disabled: bool = False) -> list[WhaleAddress]:
        where_sql = "" if include_disabled else "WHERE enabled = true"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, address, address_lower, label, enabled, created_by, updated_by, created_at, updated_at
                FROM whale_watch_addresses
                {where_sql}
                ORDER BY enabled DESC, label ASC, address_lower ASC
                """
            ).fetchall()
        return [_row_to_address(row) for row in rows]

    def list_addresses_created_since(self, *, since: datetime) -> list[WhaleAddress]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, address, address_lower, label, enabled, created_by, updated_by, created_at, updated_at
                FROM whale_watch_addresses
                WHERE created_at >= %s
                ORDER BY created_at DESC, id DESC
                """,
                (since,),
            ).fetchall()
        return [_row_to_address(row) for row in rows]

    def delete_addresses(self, *, ids: list[int]) -> int:
        if not ids:
            return 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                DELETE FROM whale_watch_addresses
                WHERE id = ANY(%s)
                RETURNING id
                """,
                (ids,),
            ).fetchall()
            conn.commit()
        return len(rows)

    def get_chain_state(self, *, address_id: int, chain_key: str) -> ChainState | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT address_id, chain_key, seeded_at, last_polled_at, last_success_at, last_error, last_seen_block
                FROM whale_watch_chain_states
                WHERE address_id = %s AND chain_key = %s
                """,
                (address_id, chain_key),
            ).fetchone()
        return _row_to_state(row) if row else None

    def mark_chain_seeded(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_chain_states (
                    address_id, chain_key, seeded_at, last_polled_at, last_success_at, last_error, last_seen_block
                )
                VALUES (%s, %s, %s, %s, %s, NULL, %s)
                ON CONFLICT (address_id, chain_key) DO UPDATE SET
                    seeded_at = COALESCE(whale_watch_chain_states.seeded_at, EXCLUDED.seeded_at),
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error = NULL,
                    last_seen_block = GREATEST(
                        COALESCE(whale_watch_chain_states.last_seen_block, 0),
                        COALESCE(EXCLUDED.last_seen_block, 0)
                    ),
                    updated_at = now()
                """,
                (address_id, chain_key, polled_at, polled_at, polled_at, block_number),
            )
            conn.commit()

    def record_chain_success(self, *, address_id: int, chain_key: str, block_number: int | None, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_chain_states (
                    address_id, chain_key, last_polled_at, last_success_at, last_error, last_seen_block
                )
                VALUES (%s, %s, %s, %s, NULL, %s)
                ON CONFLICT (address_id, chain_key) DO UPDATE SET
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_success_at = EXCLUDED.last_success_at,
                    last_error = NULL,
                    last_seen_block = GREATEST(
                        COALESCE(whale_watch_chain_states.last_seen_block, 0),
                        COALESCE(EXCLUDED.last_seen_block, 0)
                    ),
                    updated_at = now()
                """,
                (address_id, chain_key, polled_at, polled_at, block_number),
            )
            conn.commit()

    def record_chain_error(self, *, address_id: int, chain_key: str, error: str, polled_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO whale_watch_chain_states (address_id, chain_key, last_polled_at, last_error)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (address_id, chain_key) DO UPDATE SET
                    last_polled_at = EXCLUDED.last_polled_at,
                    last_error = EXCLUDED.last_error,
                    updated_at = now()
                """,
                (address_id, chain_key, polled_at, error[:1000]),
            )
            conn.commit()

    def save_activity(self, *, whale: WhaleAddress, chain_key: str, activity: Activity) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO whale_watch_activities (
                    address_id, chain_key, tx_hash, activity_fingerprint, activity_type,
                    direction, counterparty, block_number, tx_timestamp, summary,
                    telegram_text, tx_url, raw_payload
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address_id, chain_key, tx_hash, activity_fingerprint) DO NOTHING
                RETURNING id
                """,
                (
                    whale.id,
                    chain_key,
                    activity.tx_hash,
                    activity.fingerprint,
                    activity.kind,
                    activity.direction,
                    activity.counterparty,
                    activity.block_number,
                    activity.timestamp,
                    activity.summary,
                    activity.telegram_text,
                    activity.tx_url,
                    self._Jsonb(activity.raw_payload),
                ),
            ).fetchone()
            conn.commit()
        return row is not None

    def update_activity_telegram_result(self, *, tx_hash: str, fingerprint: str, telegram_result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE whale_watch_activities
                SET telegram_result = %s,
                    telegram_sent_at = CASE WHEN (%s)::jsonb ->> 'ok' = 'true' THEN now() ELSE telegram_sent_at END,
                    updated_at = now()
                WHERE tx_hash = %s AND activity_fingerprint = %s
                """,
                (self._Jsonb(telegram_result), self._Jsonb(telegram_result), tx_hash, fingerprint),
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


def _row_to_address(row: dict[str, Any]) -> WhaleAddress:
    return WhaleAddress(
        id=int(row["id"]),
        address=str(row["address"]),
        address_lower=str(row["address_lower"]),
        label=str(row["label"]),
        enabled=bool(row["enabled"]),
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_state(row: dict[str, Any]) -> ChainState:
    return ChainState(
        address_id=int(row["address_id"]),
        chain_key=str(row["chain_key"]),
        seeded_at=row.get("seeded_at"),
        last_polled_at=row.get("last_polled_at"),
        last_success_at=row.get("last_success_at"),
        last_error=row.get("last_error"),
        last_seen_block=row.get("last_seen_block"),
    )


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + CONSOLE_AUTH_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS whale_watch_addresses (
    id bigserial PRIMARY KEY,
    address text NOT NULL,
    address_lower text NOT NULL UNIQUE,
    label text NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    created_by text,
    updated_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT whale_watch_address_format CHECK (address_lower ~ '^0x[0-9a-f]{40}$')
);

ALTER TABLE whale_watch_addresses
    ADD COLUMN IF NOT EXISTS created_by text;

ALTER TABLE whale_watch_addresses
    ADD COLUMN IF NOT EXISTS updated_by text;

CREATE TABLE IF NOT EXISTS whale_watch_chain_states (
    address_id bigint NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,
    chain_key text NOT NULL,
    seeded_at timestamptz,
    last_polled_at timestamptz,
    last_success_at timestamptz,
    last_error text,
    last_seen_block bigint,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (address_id, chain_key)
);

CREATE TABLE IF NOT EXISTS whale_watch_activities (
    id bigserial PRIMARY KEY,
    address_id bigint NOT NULL REFERENCES whale_watch_addresses(id) ON DELETE CASCADE,
    chain_key text NOT NULL,
    tx_hash text NOT NULL,
    activity_fingerprint text NOT NULL,
    activity_type text NOT NULL CHECK (activity_type IN ('transfer', 'swap')),
    direction text CHECK (direction IS NULL OR direction IN ('in', 'out')),
    counterparty text,
    block_number bigint NOT NULL,
    tx_timestamp timestamptz,
    summary text NOT NULL,
    telegram_text text NOT NULL,
    telegram_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    telegram_sent_at timestamptz,
    tx_url text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (address_id, chain_key, tx_hash, activity_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_whale_watch_addresses_enabled
ON whale_watch_addresses(enabled, address_lower);

CREATE INDEX IF NOT EXISTS idx_whale_watch_activities_created
ON whale_watch_activities(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_whale_watch_addresses_created_at
ON whale_watch_addresses(created_at DESC);

CREATE OR REPLACE FUNCTION whale_watch_actor_email()
RETURNS text
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(lower(COALESCE(auth.jwt() ->> 'email', '')), '');
$$;

CREATE OR REPLACE FUNCTION whale_watch_addresses_set_audit_fields()
RETURNS trigger AS $$
DECLARE
    v_actor text := whale_watch_actor_email();
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.created_by := COALESCE(NULLIF(NEW.created_by, ''), v_actor, NEW.created_by);
        NEW.updated_by := COALESCE(NULLIF(NEW.updated_by, ''), v_actor, NEW.updated_by, NEW.created_by);
    ELSE
        NEW.created_by := COALESCE(OLD.created_by, NEW.created_by);
        NEW.updated_by := COALESCE(v_actor, NEW.updated_by, OLD.updated_by, OLD.created_by);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION whale_watch_addresses_guard_insert()
RETURNS trigger AS $$
DECLARE
    v_existing_count bigint;
    v_threshold bigint := 500;
BEGIN
    IF EXISTS (
        SELECT 1
        FROM whale_watch_addresses
        WHERE address_lower = NEW.address_lower
    ) THEN
        RETURN NEW;
    END IF;

    SELECT COUNT(*) INTO v_existing_count FROM whale_watch_addresses;
    IF v_existing_count >= v_threshold THEN
        RAISE EXCEPTION '链上巨鲸地址数量已达 % 条，已阻止继续新增，请先排查异常地址批量写入。', v_threshold;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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

DROP TRIGGER IF EXISTS trg_whale_watch_addresses_notify ON whale_watch_addresses;
CREATE TRIGGER trg_whale_watch_addresses_notify
AFTER INSERT OR UPDATE OR DELETE ON whale_watch_addresses
FOR EACH ROW EXECUTE FUNCTION notify_whale_watch_config_changed();

DROP TRIGGER IF EXISTS trg_whale_watch_addresses_set_audit_fields ON whale_watch_addresses;
CREATE TRIGGER trg_whale_watch_addresses_set_audit_fields
BEFORE INSERT OR UPDATE ON whale_watch_addresses
FOR EACH ROW EXECUTE FUNCTION whale_watch_addresses_set_audit_fields();

DROP TRIGGER IF EXISTS trg_whale_watch_addresses_guard_insert ON whale_watch_addresses;
CREATE TRIGGER trg_whale_watch_addresses_guard_insert
BEFORE INSERT ON whale_watch_addresses
FOR EACH ROW EXECUTE FUNCTION whale_watch_addresses_guard_insert();

ALTER TABLE whale_watch_addresses ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_chain_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE whale_watch_activities ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS whale_watch_addresses_anon_all ON whale_watch_addresses;
DROP POLICY IF EXISTS whale_watch_chain_states_anon_all ON whale_watch_chain_states;
DROP POLICY IF EXISTS whale_watch_activities_anon_all ON whale_watch_activities;
DROP POLICY IF EXISTS whale_watch_addresses_console_admin_all ON whale_watch_addresses;
DROP POLICY IF EXISTS whale_watch_chain_states_console_admin_select ON whale_watch_chain_states;
DROP POLICY IF EXISTS whale_watch_activities_console_admin_select ON whale_watch_activities;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL PRIVILEGES ON whale_watch_addresses, whale_watch_chain_states, whale_watch_activities FROM anon;
        REVOKE ALL PRIVILEGES ON SEQUENCE whale_watch_addresses_id_seq, whale_watch_activities_id_seq FROM anon;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        GRANT USAGE ON SCHEMA public TO authenticated;
        GRANT SELECT, INSERT, UPDATE, DELETE ON whale_watch_addresses TO authenticated;
        GRANT SELECT ON whale_watch_chain_states, whale_watch_activities TO authenticated;
        GRANT USAGE, SELECT ON SEQUENCE whale_watch_addresses_id_seq TO authenticated;

        EXECUTE 'CREATE POLICY whale_watch_addresses_console_admin_all ON whale_watch_addresses
            FOR ALL TO authenticated USING (is_console_admin()) WITH CHECK (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_chain_states_console_admin_select ON whale_watch_chain_states
            FOR SELECT TO authenticated USING (is_console_admin())';
        EXECUTE 'CREATE POLICY whale_watch_activities_console_admin_select ON whale_watch_activities
            FOR SELECT TO authenticated USING (is_console_admin())';
    END IF;
END
$$;
"""


def normalize_address_for_storage(value: str) -> tuple[str, str]:
    address = normalize_evm_address(value)
    return address, address.lower()
