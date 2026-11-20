from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Sequence

from psycopg import Connection
from psycopg.sql import Identifier, Literal

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.common.storage import PostgresSettings


IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

SOURCE_STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS odaily_source_state_outbox (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_schema text NOT NULL,
    source_table text NOT NULL,
    source_row_id text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('INSERT','UPDATE','DELETE')),
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    projected_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_odaily_source_state_outbox_source_event
    ON odaily_source_state_outbox(source_schema,source_table,event_id);
CREATE INDEX IF NOT EXISTS idx_odaily_source_state_outbox_pending
    ON odaily_source_state_outbox(event_id) WHERE projected_at IS NULL;
"""

SOURCE_STATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION main.odaily_capture_source_state() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_old jsonb;
    v_new jsonb;
    v_row jsonb;
    v_payload jsonb := '{}'::jsonb;
    v_row_id text;
    v_row_keys jsonb := '[]'::jsonb;
    v_key text;
    v_count integer;
    v_operation text := TG_OP;
    v_index integer;
BEGIN
    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD); END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW); END IF;
    v_row := CASE WHEN TG_OP = 'DELETE' THEN v_old ELSE v_new END;
    v_count := TG_ARGV[0]::integer;
    FOR v_index IN 1..v_count LOOP
        v_key := TG_ARGV[v_index];
        v_row_keys := v_row_keys || jsonb_build_array(v_row->>v_key);
    END LOOP;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements_text(v_row_keys) AS value WHERE value = '') THEN
        RAISE EXCEPTION 'source state trigger % has no row identity', TG_NAME;
    END IF;
    v_row_id := CASE WHEN v_count = 1 THEN v_row_keys->>0 ELSE v_row_keys::text END;
    IF TG_OP = 'UPDATE' THEN
        FOR v_index IN 1..v_count LOOP
            v_key := TG_ARGV[v_index];
            IF (v_old->v_key) IS DISTINCT FROM (v_new->v_key) THEN
                RAISE EXCEPTION 'source state trigger % does not allow identity updates', TG_NAME;
            END IF;
        END LOOP;
        FOR v_index IN v_count + 1..TG_NARGS - 1 LOOP
            v_key := TG_ARGV[v_index];
            IF (v_old->v_key) IS DISTINCT FROM (v_new->v_key) THEN
                v_payload := v_payload || jsonb_build_object(v_key, v_new->v_key);
            END IF;
        END LOOP;
        IF v_payload = '{}'::jsonb THEN RETURN NEW; END IF;
    ELSE
        FOR v_index IN v_count + 1..TG_NARGS - 1 LOOP
            v_key := TG_ARGV[v_index];
            IF v_row ? v_key THEN v_payload := v_payload || jsonb_build_object(v_key, v_row->v_key); END IF;
        END LOOP;
    END IF;
    INSERT INTO main.odaily_source_state_outbox(
        source_schema,source_table,source_row_id,operation,payload
    ) VALUES (TG_TABLE_SCHEMA,TG_TABLE_NAME,v_row_id,v_operation,v_payload);
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$$;
"""
SOURCE_STATE_CHECKSUM = hashlib.sha256(
    (SOURCE_STATE_SCHEMA_SQL + SOURCE_STATE_FUNCTION_SQL).encode("utf-8")
).hexdigest()


def _install_source_state_schema(connection: Connection) -> None:
    for statement in SOURCE_STATE_SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(SOURCE_STATE_FUNCTION_SQL)


def _require_transaction(connection: Connection, operation: str) -> None:
    if connection.info.transaction_status.name != "INTRANS":
        raise RuntimeError(f"{operation} requires an active caller-owned PostgreSQL transaction")


def ensure_source_state_schema(connection: Connection, *, settings: PostgresSettings | None = None) -> tuple[int, ...]:
    """Create the shared outbox/function from within a source module migration transaction."""
    return apply_schema_migrations(
        connection,
        schema="main",
        component="system_observability_source_outbox",
        migrations=(PostgresMigration(1, "source state outbox", SOURCE_STATE_CHECKSUM, _install_source_state_schema),),
        settings=settings,
    )


def initialize_source_state_schema() -> None:
    with connect_postgres(schema="main", application_name="odaily-observability-outbox-migrate") as connection:
        ensure_source_state_schema(connection)


@dataclass(frozen=True, slots=True)
class SourceStateTriggerSpec:
    schema: str
    table: str
    row_id_columns: tuple[str, ...]
    payload_columns: tuple[str, ...]
    trigger_name: str | None = None


def install_source_state_trigger(connection: Connection, spec: SourceStateTriggerSpec, *, operations: tuple[str, ...] = ("INSERT", "UPDATE", "DELETE")) -> None:
    """Install a same-transaction outbox trigger with an explicit payload allowlist."""
    for value in (spec.schema, spec.table, *spec.row_id_columns, *spec.payload_columns):
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError(f"Unsupported source audit identifier: {value!r}")
    if not spec.row_id_columns:
        raise ValueError("source state triggers require at least one row identity column")
    if not operations or len(set(operations)) != len(operations) or any(op not in {"INSERT", "UPDATE", "DELETE"} for op in operations):
        raise ValueError("unsupported source audit operations")
    if len(set(spec.payload_columns)) != len(spec.payload_columns):
        raise ValueError("source state payload columns must be unique")
    name = spec.trigger_name or f"trg_odaily_source_state_{spec.table}"
    if not IDENTIFIER_PATTERN.fullmatch(name):
        raise ValueError(f"Unsupported source audit trigger name: {name!r}")
    arguments = [Literal(str(len(spec.row_id_columns)))]
    arguments.extend(Literal(value) for value in (*spec.row_id_columns, *spec.payload_columns))
    argument_sql = ",".join(argument.as_string(connection) for argument in arguments)
    trigger_sql = (
        f"CREATE TRIGGER {Identifier(name).as_string(connection)} "
        f"AFTER {' OR '.join(operations)} ON {Identifier(spec.schema, spec.table).as_string(connection)} "
        "FOR EACH ROW EXECUTE FUNCTION main.odaily_capture_source_state(" + argument_sql + ")"
    )
    connection.execute(f"DROP TRIGGER IF EXISTS {Identifier(name).as_string(connection)} ON {Identifier(spec.schema, spec.table).as_string(connection)}")
    connection.execute(trigger_sql)


def claim_source_state_events(connection: Connection, *, limit: int = 500, outbox_schema: str = "main") -> list[dict]:
    """Claim unprojected committed events inside the caller's projection transaction."""
    _require_transaction(connection, "claim_source_state_events")
    if limit < 1 or limit > 5000:
        raise ValueError("limit must be between 1 and 5000")
    if not IDENTIFIER_PATTERN.fullmatch(outbox_schema):
        raise ValueError("unsupported source outbox schema")
    return connection.execute(
        "SELECT event_id,source_schema,source_table,source_row_id,operation,payload,occurred_at "
        f"FROM {Identifier(outbox_schema, 'odaily_source_state_outbox').as_string(connection)} "
        "WHERE projected_at IS NULL AND event_id > COALESCE((SELECT state_value::bigint "
        "FROM system_information_state WHERE state_key='feed_history_reset_event_id'), 0) "
        "ORDER BY event_id FOR UPDATE SKIP LOCKED LIMIT %s",
        (limit,),
    ).fetchall()


def acknowledge_source_state_events(connection: Connection, event_ids: Sequence[int], *, outbox_schema: str = "main") -> int:
    _require_transaction(connection, "acknowledge_source_state_events")
    if not event_ids:
        return 0
    if not IDENTIFIER_PATTERN.fullmatch(outbox_schema):
        raise ValueError("unsupported source outbox schema")
    result = connection.execute(
        f"UPDATE {Identifier(outbox_schema, 'odaily_source_state_outbox').as_string(connection)} SET projected_at=clock_timestamp() "
        "WHERE event_id=ANY(%s) AND projected_at IS NULL",
        (list(event_ids),),
    )
    return result.rowcount or 0


def prune_projected_source_events(connection: Connection, *, outbox_schema: str = "main", limit: int = 5000) -> int:
    if not IDENTIFIER_PATTERN.fullmatch(outbox_schema) or not 1 <= limit <= 5000:
        raise ValueError("unsupported source outbox cleanup parameters")
    relation = Identifier(outbox_schema, "odaily_source_state_outbox").as_string(connection)
    result = connection.execute(
        f"DELETE FROM {relation} WHERE event_id IN (SELECT event_id FROM {relation} "
        "WHERE projected_at < clock_timestamp() - interval '24 hours' "
        "OR (projected_at IS NULL AND event_id <= COALESCE((SELECT state_value::bigint "
        "FROM system_information_state WHERE state_key='feed_history_reset_event_id'),0) "
        "AND occurred_at < clock_timestamp() - interval '24 hours') "
        "ORDER BY event_id LIMIT %s FOR UPDATE SKIP LOCKED)", (limit,),
    )
    return result.rowcount or 0
