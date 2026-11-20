from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from psycopg.sql import Identifier

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from .error_policy import EXTERNAL_CATEGORIES, day_start_ms, error_messages, expire_external_errors, external_error, failure_category, scrub_external_errors
from .postgres_evidence import PostgresSmallEvidenceStore
from .store import _fingerprint, _safe_json, timestamp_ms


INFORMATION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_information_items (
 item_id text PRIMARY KEY, business_key text NOT NULL, source text NOT NULL, source_id text,
 task_id bigint, title text NOT NULL DEFAULT '', summary text NOT NULL DEFAULT '', source_url text,
 stage text NOT NULL DEFAULT 'unknown', status text NOT NULL DEFAULT 'unknown', result text NOT NULL DEFAULT 'unknown',
 failure_category text, created_at_ms bigint, updated_at_ms bigint, observed_at_ms bigint NOT NULL,
 source_database text NOT NULL, source_table text NOT NULL, source_row_id text NOT NULL,
 evidence_ref text, fingerprint text NOT NULL, legacy_history boolean NOT NULL DEFAULT true,
 news_visible boolean NOT NULL DEFAULT false, admission text NOT NULL DEFAULT 'unassessed',
 parent_item_id text, duplicate_json jsonb, source_name text NOT NULL DEFAULT '', source_updated_at_ms bigint,
 search_text text NOT NULL DEFAULT '', search_terms text[] NOT NULL DEFAULT '{}',
 source_revision bigint NOT NULL DEFAULT 0, search_key bigint GENERATED ALWAYS AS IDENTITY UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_system_information_created ON system_information_items(created_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_activity ON system_information_items(updated_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_business ON system_information_items(business_key,created_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_business_source ON system_information_items(business_key,source);
CREATE INDEX IF NOT EXISTS idx_system_information_business_activity ON system_information_items(business_key,updated_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_status ON system_information_items(status,created_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_source ON system_information_items(source,created_at_ms DESC,item_id DESC);
CREATE INDEX IF NOT EXISTS idx_system_information_task ON system_information_items(task_id);
CREATE INDEX IF NOT EXISTS idx_system_information_news_activity ON system_information_items(updated_at_ms DESC,item_id DESC) WHERE news_visible AND parent_item_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_system_information_parent ON system_information_items(parent_item_id);
CREATE INDEX IF NOT EXISTS idx_system_information_origin ON system_information_items(source_database,source_table,source_row_id);
CREATE TABLE IF NOT EXISTS system_information_events (
 event_id text PRIMARY KEY, item_id text NOT NULL REFERENCES system_information_items(item_id) ON DELETE CASCADE,
 event_type text NOT NULL, stage text NOT NULL, status text NOT NULL, result text NOT NULL,
 happened_at_ms bigint, observed_at_ms bigint NOT NULL, summary text NOT NULL DEFAULT '', reason text,
 error_code text, evidence_ref text, details_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_system_information_events_item ON system_information_events(item_id,happened_at_ms,event_id);
CREATE TABLE IF NOT EXISTS system_information_state(state_key text PRIMARY KEY,state_value text NOT NULL,updated_at_ms bigint NOT NULL);
CREATE TABLE IF NOT EXISTS system_information_source_identity(source_schema text NOT NULL,source_table text NOT NULL,source_row_id text NOT NULL,item_id text NOT NULL REFERENCES system_information_items(item_id) ON DELETE CASCADE,updated_at_ms bigint NOT NULL,PRIMARY KEY(source_schema,source_table,source_row_id),UNIQUE(item_id));
CREATE INDEX IF NOT EXISTS idx_system_information_search_terms ON system_information_items USING gin(search_terms);
CREATE TABLE IF NOT EXISTS system_information_evidence(evidence_ref text PRIMARY KEY,path text NOT NULL,byte_size bigint NOT NULL,created_at_ms bigint NOT NULL);
CREATE TABLE IF NOT EXISTS system_information_expired_evidence_candidates(evidence_ref text PRIMARY KEY,eligible_at_ms bigint NOT NULL);
CREATE TABLE IF NOT EXISTS system_information_evidence_pins(pin_key text PRIMARY KEY,evidence_ref text NOT NULL REFERENCES system_information_evidence(evidence_ref),pinned_at_ms bigint NOT NULL);
CREATE TABLE IF NOT EXISTS system_information_storage_samples(sample_date text PRIMARY KEY,database_bytes bigint NOT NULL,evidence_bytes bigint NOT NULL,sampled_at_ms bigint NOT NULL);
CREATE TABLE IF NOT EXISTS system_information_decision_samples(
 sample_key text PRIMARY KEY,sample_date text NOT NULL,sampled_at_ms bigint NOT NULL,raw_expires_at_ms bigint NOT NULL,
 item_id text NOT NULL,business_key text NOT NULL,source text NOT NULL,source_id text,stage text NOT NULL,
 status text NOT NULL,result text NOT NULL,reason_summary text NOT NULL DEFAULT '',rule_version text,
 evidence_ref text,evidence_bytes bigint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_system_information_decision_samples_date ON system_information_decision_samples(sampled_at_ms DESC,business_key,source,result);
CREATE INDEX IF NOT EXISTS idx_system_information_items_evidence ON system_information_items(evidence_ref);
CREATE INDEX IF NOT EXISTS idx_system_information_events_evidence ON system_information_events(evidence_ref);
CREATE INDEX IF NOT EXISTS idx_system_information_decision_samples_evidence ON system_information_decision_samples(evidence_ref);
CREATE INDEX IF NOT EXISTS idx_system_information_evidence_pins_evidence ON system_information_evidence_pins(evidence_ref);
CREATE TABLE IF NOT EXISTS system_information_external_error_daily(
 database_key text NOT NULL,error_date text NOT NULL,source text NOT NULL,stage text NOT NULL,category text NOT NULL,
 occurrences bigint NOT NULL,PRIMARY KEY(database_key,error_date,source,stage,category));
"""
SCHEMA_CHECKSUM = hashlib.sha256(INFORMATION_SCHEMA_SQL.encode()).hexdigest()
SYSTEM_INFORMATION_IMPORT_MANIFEST = {
    "items": "system_information_items columns are imported without dropping historical statuses or identity fields",
    "events": "system_information_events event_id and item_id relationships are preserved",
    "evidence": "system_information_evidence references retain content-addressed gzip files; small evidence bytes migrate to small_evidence",
    "search": "historical SQLite token postings are rebuildable and are not imported; keyword search is disabled in the runtime list",
    "source_identity": "source_database paths map to native schemas only through an explicit source_schema_map; item_id is retained",
}


def _install_schema(connection) -> None:
    for statement in INFORMATION_SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


EXTERNAL_ERROR_LEDGER_SQL = "CREATE TABLE IF NOT EXISTS system_information_external_error_ledger(row_key text PRIMARY KEY)"
EXTERNAL_ERROR_LEDGER_CHECKSUM = hashlib.sha256(EXTERNAL_ERROR_LEDGER_SQL.encode()).hexdigest()


def _install_external_error_ledger(connection) -> None:
    connection.execute(EXTERNAL_ERROR_LEDGER_SQL)


SEARCH_POSTINGS_MIGRATION_SQL = "ALTER TABLE system_information_items ADD COLUMN IF NOT EXISTS search_key bigint GENERATED ALWAYS AS IDENTITY; CREATE UNIQUE INDEX IF NOT EXISTS idx_system_information_search_key ON system_information_items(search_key); CREATE TABLE IF NOT EXISTS system_information_search_legacy(term text NOT NULL,item_key bigint NOT NULL REFERENCES system_information_items(search_key) ON DELETE CASCADE,PRIMARY KEY(term,item_key)); CREATE INDEX IF NOT EXISTS idx_system_information_search_legacy_item ON system_information_search_legacy(item_key);"
SEARCH_POSTINGS_MIGRATION_CHECKSUM = hashlib.sha256(SEARCH_POSTINGS_MIGRATION_SQL.encode()).hexdigest()

SEARCH_POSTINGS_RETIREMENT_SQL = "DROP TABLE IF EXISTS system_information_search_legacy; ALTER TABLE system_information_items DROP COLUMN IF EXISTS search_key CASCADE;"
SEARCH_POSTINGS_RETIREMENT_CHECKSUM = hashlib.sha256(SEARCH_POSTINGS_RETIREMENT_SQL.encode()).hexdigest()

SOURCE_IDENTITY_ITEM_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_system_information_identity_item ON system_information_source_identity(item_id)"
SOURCE_IDENTITY_ITEM_INDEX_CHECKSUM = hashlib.sha256(SOURCE_IDENTITY_ITEM_INDEX_SQL.encode()).hexdigest()
EVIDENCE_FILE_GC_SQL = "CREATE TABLE IF NOT EXISTS system_information_evidence_file_gc(evidence_ref text PRIMARY KEY,path text NOT NULL,queued_at_ms bigint NOT NULL)"
EVIDENCE_FILE_GC_CHECKSUM = hashlib.sha256(EVIDENCE_FILE_GC_SQL.encode()).hexdigest()


def _allow_shared_source_identity_items(connection) -> None:
    constraint = connection.execute(
        "SELECT conname FROM pg_constraint WHERE conrelid='system_information_source_identity'::regclass "
        "AND contype='u' AND pg_get_constraintdef(oid)='UNIQUE (item_id)'"
    ).fetchone()
    if constraint:
        from psycopg import sql

        connection.execute(sql.SQL("ALTER TABLE system_information_source_identity DROP CONSTRAINT {}")
                           .format(sql.Identifier(str(constraint["conname"]))))


SOURCE_IDENTITY_FANIN_SQL = "allow multiple source row identities to map to one information item"
SOURCE_IDENTITY_FANIN_CHECKSUM = hashlib.sha256(SOURCE_IDENTITY_FANIN_SQL.encode()).hexdigest()


def _install_search_postings_schema(connection) -> None:
    for statement in SEARCH_POSTINGS_MIGRATION_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


def _retire_search_postings_schema(connection) -> None:
    for statement in SEARCH_POSTINGS_RETIREMENT_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


COUNTED_ERROR_TABLES = ("auditor_checks", "fact_checks", "x_capture_attempts", "x_processing_attempts", "editor_plugin_generation_logs")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class PostgresSystemInformationStore:
    """PostgreSQL implementation of the system information read/write contract."""

    def __init__(self, database_path: Path | None = None, evidence_dir: Path | None = None, *, schema: str = "system_observability") -> None:
        self.database_path = database_path
        self.evidence_dir = evidence_dir or ((database_path.parent / "system_information" / "evidence") if database_path else Path(os.getenv("ODAILY_SYSTEM_OBSERVABILITY_DIR") or "data/runtime/system_observability") / "evidence")
        self.schema = schema
        self.evidence = PostgresSmallEvidenceStore(schema=schema)

    def connect(self):
        return connect_postgres(schema=self.schema, application_name="odaily-system-information")

    @staticmethod
    def initialize_schema(*, schema: str = "system_observability") -> None:
        with connect_postgres(schema=schema, application_name="odaily-system-information-migrate") as conn:
            apply_schema_migrations(
                conn, schema=schema, component="system_information",
                migrations=(PostgresMigration(1, "system information tables", SCHEMA_CHECKSUM, _install_schema),
                            PostgresMigration(2, "external error retention ledger", EXTERNAL_ERROR_LEDGER_CHECKSUM, _install_external_error_ledger),
                            PostgresMigration(3, "preserve imported search postings", SEARCH_POSTINGS_MIGRATION_CHECKSUM, _install_search_postings_schema),
                            PostgresMigration(4, "allow shared source identity items", SOURCE_IDENTITY_FANIN_CHECKSUM, _allow_shared_source_identity_items),
                            PostgresMigration(5, "retire legacy search postings", SEARCH_POSTINGS_RETIREMENT_CHECKSUM, _retire_search_postings_schema),
                            PostgresMigration(6, "index source identity item references", SOURCE_IDENTITY_ITEM_INDEX_CHECKSUM,
                                              lambda connection: connection.execute(SOURCE_IDENTITY_ITEM_INDEX_SQL)),
                            PostgresMigration(7, "recoverable external evidence file GC", EVIDENCE_FILE_GC_CHECKSUM,
                                              lambda connection: connection.execute(EVIDENCE_FILE_GC_SQL))),
            )
        PostgresSmallEvidenceStore(schema=schema).initialize()

    @staticmethod
    def initialize_native_schema(*, schema: str = "system_observability") -> None:
        """Initialize every native observability relation without runtime state seeding."""
        PostgresSystemInformationStore.initialize_schema(schema=schema)
        from .postgres_reviews import PostgresSystemReviewRepository
        PostgresSystemReviewRepository.initialize_schema(schema=schema)

    def initialize(self) -> None:
        self.initialize_schema(schema=self.schema)
        with self.connect() as conn:
            now = _now_ms()
            conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('search_bigrams_rebuild_started','1',%s) ON CONFLICT(state_key) DO NOTHING", (now,))
            conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('search_bigrams_rebuild_done','1',%s) ON CONFLICT(state_key) DO NOTHING", (now,))
            conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('news_projection_v1','1',%s) ON CONFLICT(state_key) DO NOTHING", (now,))
            conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('observability_started_at',%s,%s) ON CONFLICT(state_key) DO NOTHING", (str(now), now))

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(%s,%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (key, str(value), _now_ms()))

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT state_value FROM system_information_state WHERE state_key=%s", (key,)).fetchone()
        return str(row["state_value"]) if row else default

    def has_item(self, item_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM system_information_items WHERE item_id=%s", (item_id,)).fetchone() is not None

    def resolve_source_item_id(self, source_schema: str, source_table: str, source_row_id: str, fallback: str) -> str:
        with self.connect() as conn:
            row=conn.execute("SELECT item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s",(source_schema,source_table,str(source_row_id))).fetchone()
        return str(row["item_id"]) if row else fallback

    def register_imported_source_identities(
        self,
        items: Iterable[dict[str, Any]],
        *,
        source_schema_map: dict[str, str],
        max_batch: int = 2000,
        skip_identities: set[tuple[str, str, str]] | None = None,
    ) -> int:
        """Register legacy item identities during import using an explicit DB-to-schema map."""
        normalized_map = {str(key): str(value) for key, value in source_schema_map.items()}
        if not normalized_map:
            raise ValueError("source_schema_map must explicitly map legacy database names")
        for schema in normalized_map.values():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
                raise ValueError(f"unsupported PostgreSQL source schema: {schema!r}")
        prepared: dict[tuple[str, str, str], str] = {}
        conflicts = skip_identities if skip_identities is not None else set()
        new_conflicts: set[tuple[str, str, str]] = set()
        basename_map: dict[str, str] = {}
        for legacy_path, schema in normalized_map.items():
            basename = legacy_path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
            previous = basename_map.get(basename)
            if previous is not None and previous != schema:
                raise ValueError(f"ambiguous legacy source database basename: {basename!r}")
            basename_map[basename] = schema
        for item in items:
            if len(prepared) >= max(1, min(5000, max_batch)):
                raise ValueError("source identity import batch exceeds its configured bound")
            legacy_database = str(item.get("source_database") or "")
            source_schema = normalized_map.get(legacy_database)
            if source_schema is None:
                basename = legacy_database.replace("\\", "/").rsplit("/", 1)[-1].casefold()
                source_schema = basename_map.get(basename)
            if source_schema is None:
                raise ValueError(f"legacy source database has no native schema mapping: {legacy_database!r}")
            identity = (source_schema, str(item.get("source_table") or ""), str(item.get("source_row_id") or ""))
            item_id = str(item.get("item_id") or "")
            if not all(identity) or not item_id:
                raise ValueError("imported source identity requires item_id, source_table and source_row_id")
            existing = prepared.get(identity)
            if existing is not None and existing != item_id:
                conflicts.add(identity)
                new_conflicts.add(identity)
                prepared.pop(identity, None)
                continue
            if identity in conflicts:
                continue
            prepared[identity] = item_id
        inserted = 0
        with self.connect() as conn:
            with conn.transaction():
                identities = list(prepared)
                if new_conflicts:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "DELETE FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s",
                            list(new_conflicts),
                        )
                existing_rows = conn.execute(
                    "SELECT source_schema,source_table,source_row_id,item_id FROM system_information_source_identity "
                    "WHERE (source_schema,source_table,source_row_id) IN "
                    "(SELECT * FROM unnest(%s::text[],%s::text[],%s::text[])) FOR UPDATE",
                    ([key[0] for key in identities], [key[1] for key in identities], [key[2] for key in identities]),
                ).fetchall() if identities else []
                for row in existing_rows:
                    key = (str(row["source_schema"]), str(row["source_table"]), str(row["source_row_id"]))
                    if str(row["item_id"]) != prepared[key]:
                        conflicts.add(key)
                        new_conflicts.add(key)
                if new_conflicts:
                    prepared = {key: value for key, value in prepared.items() if key not in new_conflicts}
                if new_conflicts:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "DELETE FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s",
                            list(new_conflicts),
                        )
                rows = [(*identity, item_id, _now_ms()) for identity, item_id in prepared.items()]
                if rows:
                    with conn.cursor() as cursor:
                        cursor.executemany(
                            "INSERT INTO system_information_source_identity(source_schema,source_table,source_row_id,item_id,updated_at_ms) "
                            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(source_schema,source_table,source_row_id) DO NOTHING",
                            rows,
                        )
                        inserted = max(0, cursor.rowcount or 0)
        return inserted

    def import_external_error_totals(self, rows: Iterable[dict[str, Any]], *, max_batch: int = 5000) -> int:
        values=[]
        for row in rows:
            values.append((str(row["database_key"]),str(row["error_date"]),str(row["source"]),str(row["stage"]),str(row["category"]),max(0,int(row["occurrences"]))))
            if len(values)>max(1,min(10_000,max_batch)):
                raise ValueError("external error total import batch exceeds its configured bound")
        if not values:return 0
        with self.connect() as conn:
            with conn.transaction():
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO system_information_external_error_daily(database_key,error_date,source,stage,category,occurrences) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(database_key,error_date,source,stage,category) DO UPDATE SET occurrences=excluded.occurrences",values)
        return len(values)

    def save_evidence(self, evidence: Any) -> str:
        with self.connect() as conn:
            with conn.transaction():
                ref = self._put_evidence(conn, evidence)
        return str(ref)

    def load_evidence(self, evidence_ref: str | None) -> Any:
        if not evidence_ref:
            return None
        with self.connect() as conn:
            return self._load_evidence_in_connection(conn, evidence_ref)

    def _load_evidence_in_connection(self, conn, evidence_ref: str) -> Any:
        row = conn.execute("SELECT path FROM system_information_evidence WHERE evidence_ref=%s", (evidence_ref,)).fetchone()
        payload = None
        path = str(row["path"] or "") if row else ""
        if path.startswith("postgres://") or "/_sqlite/" in path.replace("\\", "/"):
            stored = conn.execute("SELECT compressed_payload,byte_size FROM small_evidence WHERE evidence_ref=%s", (evidence_ref,)).fetchone()
            if stored:
                try:
                    compressed = bytes(stored["compressed_payload"])
                    decoded = gzip.decompress(compressed)
                    if len(decoded) == int(stored["byte_size"]):
                        payload = compressed
                except (OSError, EOFError, zlib.error):
                    payload = None
        elif row and row["path"]:
            try:
                payload = Path(row["path"]).read_bytes()
            except OSError:
                pass
        if payload is None:
            return {"unavailable": True, "evidence_ref": evidence_ref}
        try:
            decoded = gzip.decompress(payload)
            if hashlib.sha256(decoded).hexdigest() != evidence_ref:
                return {"unavailable": True, "evidence_ref": evidence_ref}
            return json.loads(decoded)
        except (OSError, EOFError, zlib.error, json.JSONDecodeError, ValueError):
            return {"unavailable": True, "evidence_ref": evidence_ref}

    def _put_evidence(self, conn, evidence: Any) -> str | None:
        if evidence is None:
            return None
        safe = _safe_json(evidence)
        payload = _json(safe).encode()
        ref = hashlib.sha256(payload).hexdigest()
        self._lock_external_evidence_key(conn, ref)
        conn.execute("DELETE FROM system_information_evidence_file_gc WHERE evidence_ref=%s", (ref,))
        compressed = gzip.compress(payload, compresslevel=6, mtime=0)
        from .store import SystemInformationStore
        if SystemInformationStore._is_small_capture(safe, len(payload)):
            self.evidence.put_in_transaction(conn, ref, compressed, uncompressed_size=len(payload))
            path = f"postgres://small_evidence/{ref}"
            conn.execute("INSERT INTO system_information_evidence(evidence_ref,path,byte_size,created_at_ms) VALUES(%s,%s,%s,%s) ON CONFLICT(evidence_ref) DO UPDATE SET created_at_ms=GREATEST(system_information_evidence.created_at_ms,excluded.created_at_ms)", (ref, path, len(payload), _now_ms()))
            return ref
        directory = self.evidence_dir / ref[:2]
        file_path = directory / f"{ref}.json.gz"
        conn.execute("INSERT INTO system_information_evidence(evidence_ref,path,byte_size,created_at_ms) VALUES(%s,%s,%s,%s) ON CONFLICT(evidence_ref) DO UPDATE SET path=excluded.path,byte_size=excluded.byte_size,created_at_ms=GREATEST(system_information_evidence.created_at_ms,excluded.created_at_ms)", (ref, str(file_path), len(payload), _now_ms()))
        if not file_path.exists():
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=".evidence-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(compressed)
                os.replace(temp_name, file_path)
            finally:
                Path(temp_name).unlink(missing_ok=True)
        return ref

    @staticmethod
    def _lock_external_evidence_key(conn, evidence_ref: str) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("system-evidence:" + evidence_ref,))

    @staticmethod
    def _lock_reference(conn, evidence_ref: str) -> None:
        if conn.execute("SELECT evidence_ref FROM system_information_evidence WHERE evidence_ref=%s FOR KEY SHARE", (evidence_ref,)).fetchone() is None:
            raise RuntimeError("cannot reference missing system evidence")

    def pin_evidence(self, evidence_ref: str, pin_key: str) -> None:
        with self.connect() as conn:
            with conn.transaction():
                self._lock_reference(conn, evidence_ref)
                conn.execute("INSERT INTO system_information_evidence(evidence_ref,path,byte_size,created_at_ms) SELECT evidence_ref,%s,byte_size,%s FROM small_evidence WHERE evidence_ref=%s ON CONFLICT(evidence_ref) DO NOTHING", (f"postgres://small_evidence/{evidence_ref}", _now_ms(), evidence_ref))
                conn.execute("INSERT INTO system_information_evidence_pins(pin_key,evidence_ref,pinned_at_ms) VALUES(%s,%s,%s) ON CONFLICT(pin_key) DO UPDATE SET evidence_ref=excluded.evidence_ref,pinned_at_ms=excluded.pinned_at_ms", (pin_key, evidence_ref, _now_ms()))

    def unpin_evidence(self, pin_key: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM system_information_evidence_pins WHERE pin_key=%s", (pin_key,))

    def _upsert_snapshot(self, conn, snapshot: dict[str, Any], evidence: Any, observed: int) -> bool:
        source_schema=str(snapshot.get("source_schema") or "")
        source_identity=None
        identity_row_id = str(snapshot.get("source_identity_row_id") or snapshot["source_row_id"])
        if source_schema:
            identity_lock=f"source-identity:{source_schema}:{snapshot['source_table']}:{identity_row_id}"
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",(identity_lock,))
            source_identity=conn.execute("SELECT item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s FOR UPDATE",(source_schema,snapshot["source_table"],identity_row_id)).fetchone()
            if source_identity:
                snapshot["item_id"] = str(source_identity["item_id"])
        item_id = str(snapshot["item_id"])
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("system-information:" + item_id,))
        operation_time = snapshot.get("updated_at_ms") or snapshot.get("created_at_ms")
        material = expire_external_errors({"snapshot": snapshot, "evidence": evidence}, operation_time)
        snapshot, evidence = material["snapshot"], material["evidence"]
        messages = error_messages(evidence)
        if messages and operation_time is not None and operation_time >= day_start_ms():
            snapshot = {**snapshot, "failure_category": failure_category(messages[0])}
        fingerprint = _fingerprint({key: value for key, value in snapshot.items() if key not in {"updated_at_ms", "source_revision", "source_identity_row_id"}})
        revision = int(snapshot.get("source_revision") or 0)
        previous = conn.execute("SELECT fingerprint,title,summary,source_url,stage,status,result,updated_at_ms,source_revision,source_name,legacy_history FROM system_information_items WHERE item_id=%s FOR UPDATE", (item_id,)).fetchone()
        if previous and ((revision and revision < int(previous["source_revision"])) or ((not revision or not previous["source_revision"]) and snapshot.get("updated_at_ms") is not None and previous["updated_at_ms"] is not None and int(snapshot["updated_at_ms"]) < int(previous["updated_at_ms"]))):
            return False
        if previous and previous["fingerprint"] == fingerprint:
            self._record_source_identity(conn, snapshot, item_id, observed)
            incoming_time = snapshot.get("updated_at_ms") or 0
            if incoming_time > (previous["updated_at_ms"] or 0) or revision > int(previous["source_revision"]):
                conn.execute("UPDATE system_information_items SET updated_at_ms=GREATEST(COALESCE(updated_at_ms,0),%s),source_revision=GREATEST(source_revision,%s) WHERE item_id=%s", (incoming_time,revision,item_id))
            if snapshot.get("parent_item_id"):
                conn.execute("UPDATE system_information_items SET updated_at_ms=GREATEST(COALESCE(updated_at_ms,0),%s) WHERE item_id=%s", (incoming_time,snapshot["parent_item_id"]))
            return False
        evidence_ref = self._put_evidence(conn, evidence)
        source_name = snapshot.get("source_name") or (previous["source_name"] if previous and snapshot.get("source_table") in {"x_capture_attempts", "binance_square_attempts"} else "")
        legacy_history = bool(snapshot.get("legacy_history", True)) and (bool(previous["legacy_history"]) if previous else True)
        # Runtime projection no longer builds keyword indexes. Keep imported
        # historical postings intact, but leave new native rows unindexed.
        values = (item_id, snapshot["business_key"], snapshot["source"], snapshot.get("source_id"), snapshot.get("task_id"), snapshot.get("title") or "", snapshot.get("summary") or "", snapshot.get("source_url"), snapshot.get("stage") or "unknown", snapshot.get("status") or "unknown", snapshot.get("result") or "unknown", snapshot.get("failure_category"), snapshot.get("created_at_ms"), snapshot.get("updated_at_ms"), observed, snapshot["source_database"], snapshot["source_table"], str(snapshot["source_row_id"]), evidence_ref, fingerprint, legacy_history, bool(snapshot.get("news_visible", True)), snapshot.get("admission", "unassessed"), snapshot.get("parent_item_id"), _json(snapshot.get("duplicate_target")), source_name, snapshot.get("source_updated_at_ms"), revision, "", [])
        conn.execute("""INSERT INTO system_information_items(item_id,business_key,source,source_id,task_id,title,summary,source_url,stage,status,result,failure_category,created_at_ms,updated_at_ms,observed_at_ms,source_database,source_table,source_row_id,evidence_ref,fingerprint,legacy_history,news_visible,admission,parent_item_id,duplicate_json,source_name,source_updated_at_ms,source_revision,search_text,search_terms)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
            ON CONFLICT(item_id) DO UPDATE SET business_key=excluded.business_key,source=excluded.source,source_id=excluded.source_id,task_id=excluded.task_id,title=excluded.title,summary=excluded.summary,source_url=excluded.source_url,stage=excluded.stage,status=excluded.status,result=excluded.result,failure_category=excluded.failure_category,created_at_ms=excluded.created_at_ms,updated_at_ms=GREATEST(COALESCE(system_information_items.updated_at_ms,0),COALESCE(excluded.updated_at_ms,0)),observed_at_ms=excluded.observed_at_ms,source_database=excluded.source_database,source_table=excluded.source_table,source_row_id=excluded.source_row_id,evidence_ref=excluded.evidence_ref,fingerprint=excluded.fingerprint,legacy_history=system_information_items.legacy_history AND excluded.legacy_history,news_visible=excluded.news_visible,admission=excluded.admission,parent_item_id=excluded.parent_item_id,duplicate_json=excluded.duplicate_json,source_name=excluded.source_name,source_updated_at_ms=excluded.source_updated_at_ms,source_revision=GREATEST(system_information_items.source_revision,excluded.source_revision)""", values)
        self._record_source_identity(conn, snapshot, item_id, observed)
        if snapshot.get("parent_item_id"):
            conn.execute("UPDATE system_information_items SET updated_at_ms=GREATEST(COALESCE(updated_at_ms,0),%s) WHERE item_id=%s", (snapshot.get("updated_at_ms") or 0,snapshot["parent_item_id"]))
        event_id = hashlib.sha256(f"{item_id}:{fingerprint}".encode()).hexdigest()
        state_changed = previous is not None and any(previous[key] != (snapshot.get(key) or ("" if key in {"title", "summary", "source_url"} else None)) for key in ("stage", "status", "result", "title", "summary", "source_url"))
        details = {"history_complete": previous is not None and not bool(snapshot.get("legacy_history")), "source_row_id": str(snapshot["source_row_id"])}
        self._append_event(conn, observed=observed, item_id=item_id, event_id=event_id, event_type="legacy_snapshot" if previous is None else "observed_change" if state_changed else "artifact_update", stage=snapshot.get("stage") or "unknown", status=snapshot.get("status") or "unknown", result=snapshot.get("result") or "unknown", happened_at_ms=snapshot.get("updated_at_ms") or snapshot.get("created_at_ms"), summary=snapshot.get("summary") or "", reason=snapshot.get("reason"), error_code=snapshot.get("error_code"), evidence_ref=evidence_ref, details=details)
        return True

    def upsert_snapshot(self, snapshot: dict[str, Any], evidence: Any) -> bool:
        with self.connect() as conn:
            with conn.transaction():
                return self._upsert_snapshot(conn, snapshot, evidence, _now_ms())

    def _append_event(self, conn, *, observed: int, item_id: str, event_id: str, event_type: str, stage: str, status: str, result: str, happened_at_ms: int | None, summary: str, reason: str | None = None, error_code: str | None = None, evidence: Any = None, evidence_ref: str | None = None, details: dict[str, Any] | None = None) -> bool:
        material = expire_external_errors({"reason": reason, "error_code": error_code, "summary": summary, "status": status, "evidence": evidence, "details": details}, happened_at_ms)
        reason, error_code, summary = material["reason"], material["error_code"], material["summary"]
        evidence, details = material["evidence"], material["details"]
        if conn.execute("SELECT 1 FROM system_information_items WHERE item_id=%s", (item_id,)).fetchone() is None:
            return False
        if evidence is not None:
            evidence_ref = self._put_evidence(conn, evidence)
        elif evidence_ref:
            self._lock_reference(conn, evidence_ref)
        result = conn.execute("""INSERT INTO system_information_events(event_id,item_id,event_type,stage,status,result,happened_at_ms,observed_at_ms,summary,reason,error_code,evidence_ref,details_json)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT(event_id) DO NOTHING""", (event_id,item_id,event_type,stage,status,result,happened_at_ms,observed,summary[:1200],reason[:1200] if reason else None,error_code,evidence_ref,_json(details or {})))
        if result.rowcount and event_type == "stage_attempt" and happened_at_ms is not None:
            conn.execute("UPDATE system_information_items SET updated_at_ms=GREATEST(COALESCE(updated_at_ms,0),%s) WHERE item_id=%s", (happened_at_ms,item_id))
        return bool(result.rowcount)

    def append_event(self, **kwargs: Any) -> bool:
        with self.connect() as conn:
            with conn.transaction():
                return self._append_event(conn, observed=_now_ms(), **kwargs)

    def apply_source_audit_batch(self, entries: list[dict[str, Any]], *, watermark_key: str, watermark: int) -> tuple[int, int]:
        with self.connect() as conn:
            with conn.transaction():
                return self.apply_source_audit_batch_in_transaction(conn, entries, watermark_key=watermark_key, watermark=watermark)

    def apply_source_audit_batch_in_transaction(self, conn, entries: list[dict[str, Any]], *, watermark_key: str, watermark: int, now_ms: int | None = None) -> tuple[int, int]:
        if conn.info.transaction_status.name != "INTRANS":
            raise RuntimeError("apply_source_audit_batch_in_transaction requires a caller-owned transaction")
        changed = 0
        observed = now_ms or _now_ms()
        ordered_entries = sorted(entries, key=lambda entry: (str(entry["snapshot"]["item_id"]), int(entry.get("source_revision") or entry["snapshot"].get("source_revision") or 0)))
        for entry in ordered_entries:
            snapshot = dict(entry["snapshot"])
            if entry.get("source_revision") is not None:
                snapshot["source_revision"] = int(entry["source_revision"])
            item_id = str(snapshot["item_id"])
            if snapshot.get("source_table") == "x_capture_attempts":
                item_id = self.resolve_source_item_id_in_transaction(conn, snapshot)
                snapshot["item_id"] = item_id
                entry["snapshot"]["item_id"] = item_id
            prior = conn.execute("SELECT updated_at_ms FROM system_information_items WHERE item_id=%s", (item_id,)).fetchone()
            incoming = snapshot.get("updated_at_ms") or 0
            if snapshot.get("source_table") == "x_capture_attempts" and prior and incoming < (prior["updated_at_ms"] or 0):
                self._record_source_identity(conn, snapshot, item_id, observed)
                continue
            changed += int(self._upsert_snapshot(conn, snapshot, entry.get("evidence"), observed))
            entry["snapshot"]["item_id"] = snapshot["item_id"]
        for entry in entries:
            snap = entry["snapshot"]
            if not entry.get("skip_timeline"):
                self._append_event(conn, observed=observed, item_id=str(snap["item_id"]), event_id=str(entry["event_id"]), event_type="source_state_change", stage=str(entry["stage"]), status=str(entry["status"]), result=str(entry["result"]), happened_at_ms=int(entry["happened_at_ms"]), summary=str(entry["summary"]), reason=entry.get("reason"), error_code=entry.get("error_code"), evidence=entry.get("evidence"), details=entry.get("details"))
        conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(%s,%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (watermark_key,str(watermark),observed))
        return len(entries), changed

    @staticmethod
    def resolve_source_item_id_in_transaction(conn, snapshot: dict[str, Any]) -> str:
        source_schema = str(snapshot.get("source_schema") or "")
        if not source_schema:
            return str(snapshot["item_id"])
        row = conn.execute(
            "SELECT item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s",
            (source_schema, snapshot["source_table"], str(snapshot["source_row_id"])),
        ).fetchone()
        return str(row["item_id"]) if row else str(snapshot["item_id"])

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(payload.get("limit") or 50), 50))
        sort = "created_at_ms" if payload.get("sort") == "created" else "updated_at_ms"
        clauses: list[str] = []
        params: list[Any] = []
        if payload.get("view") == "news":
            clauses.extend(["i.news_visible", "i.parent_item_id IS NULL", "i.source_table <> 'newsflash_items'"])
        # Native X capture writes one stable account snapshot; older imported
        # per-attempt copies are excluded while retention removes them.
        clauses.append("(i.source_table <> 'x_capture_attempts' OR (i.source_id IS NOT NULL AND i.item_id='main:x_capture_attempts:' || i.source_id))")
        for key, column in (("business", "business_key"), ("source", "source"), ("stage", "stage"), ("status", "status"), ("result", "result"), ("failure_category", "failure_category")):
            value = str(payload.get(key) or "").strip()
            if not value or value == "all":
                continue
            groups = {
                "failed": "(position('failed' in i.status)>0 OR i.status IN ('failed','error','exhausted'))",
                "processing": "i.status IN ('pending','judging','judged','deduping','deduped','searched','writing','written','formatting','formatted','publisher_pending','publishing','queued','processing','running','retry')",
                "backoffice": "i.status IN ('ready_review','pending_review','backoffice')",
                "direct_publish": "i.status IN ('auto_published','published','sent','succeeded')",
                "normal_stop": "i.status IN ('discarded','duplicate','ignored','skipped','ended')",
            }
            clauses.append(groups[value] if key == "status" and value in groups else f"i.{column}=%s")
            if not (key == "status" and value in groups):
                params.append(value)
        server_now = _now_ms()
        requested_end = timestamp_ms(payload.get("until"))
        window_end = min(requested_end, server_now) if requested_end is not None else server_now
        requested_start = timestamp_ms(payload.get("since"))
        window_start = max(server_now - 6 * 60 * 60 * 1000, requested_start or server_now - 6 * 60 * 60 * 1000)
        clauses.extend([f"i.{sort}>=%s", f"i.{sort}<%s"])
        params.extend([window_start, window_end])
        # Old clients may still send q; silently ignore it while allowing
        # downstream wrappers to roll forward independently.
        base_where = " WHERE " + " AND ".join(clauses) if clauses else ""
        base_params = list(params)
        cursor = payload.get("cursor")
        if isinstance(cursor, dict):
            cursor_id, cursor_time = str(cursor.get("id") or ""), cursor.get("time")
            if cursor_time is None:
                clauses.append(f"(i.{sort} IS NULL AND i.item_id<%s)")
                params.append(cursor_id)
            else:
                clauses.append(f"(i.{sort}<%s OR (i.{sort}=%s AND i.item_id<%s) OR i.{sort} IS NULL)")
                params.extend([int(cursor_time), int(cursor_time), cursor_id])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) AS total FROM system_information_items i{base_where}", base_params).fetchone()["total"]
            rows = conn.execute(f"""SELECT i.item_id,i.business_key,i.source,i.source_id,i.task_id,i.title,i.summary,i.source_url,
                i.stage,i.status,i.result,i.failure_category,i.created_at_ms,i.updated_at_ms,i.observed_at_ms,
                i.evidence_ref,i.legacy_history,i.admission,i.duplicate_json,i.source_name,i.source_updated_at_ms,
                i.source_database,i.source_table,i.source_row_id
                FROM system_information_items i{where} ORDER BY i.{sort} DESC NULLS LAST,i.item_id DESC LIMIT %s""", [*params, limit + 1]).fetchall()
            items = [dict(row) for row in rows]
            more = len(items) > limit
            items = items[:limit]
            for item in items:
                item["duplicate_target"] = item.pop("duplicate_json")
                item.update(expire_external_errors(item, item.get("updated_at_ms")))
            next_cursor = {"time": items[-1].get(sort), "id": items[-1]["item_id"]} if more and items else None
            state = {row["state_key"]: row["state_value"] for row in conn.execute("SELECT state_key,state_value FROM system_information_state WHERE state_key=ANY(%s)", (["main_tasks_lag", "main_tasks_last_success"],))}
        return {"items": items, "total": int(total), "limit": limit, "next_cursor": next_cursor,
                "index": {"generated_at_ms": int(state["main_tasks_last_success"]) if state.get("main_tasks_last_success") else None, "lag": int(state["main_tasks_lag"]) if state.get("main_tasks_lag") else None},
                "window_since_ms": window_start, "window_until_ms": window_end}

    def detail(self, item_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            item = conn.execute("SELECT * FROM system_information_items WHERE item_id=%s", (item_id,)).fetchone()
            if item is None:
                return None
            child_ids = [str(row["item_id"]) for row in conn.execute(
                "SELECT item_id FROM system_information_items WHERE parent_item_id=%s", (item_id,)
            ).fetchall()]
            event_item_ids = [item_id, *child_ids]
            events = conn.execute(
                "SELECT event_id,event_type,stage,status,result,happened_at_ms,observed_at_ms,summary,reason,error_code,evidence_ref,details_json "
                "FROM system_information_events WHERE item_id=ANY(%s) "
                "ORDER BY COALESCE(happened_at_ms,observed_at_ms),observed_at_ms,event_id",
                (event_item_ids,),
            ).fetchall()
            evidence_refs = {str(row["evidence_ref"]) for row in [item, *events] if row["evidence_ref"]}
            evidence = {
                reference: self._load_evidence_in_connection(conn, reference)
                for reference in evidence_refs
            }

        result = dict(item)
        result["duplicate_target"] = result.pop("duplicate_json")
        result["evidence"] = evidence.get(str(result["evidence_ref"])) if result.get("evidence_ref") else None
        result["timeline"] = []
        for row in events:
            event = dict(row)
            event["details"] = event.pop("details_json") or {}
            if isinstance(event["details"], str):
                try:
                    event["details"] = json.loads(event["details"])
                except ValueError:
                    event["details"] = {}
            event["evidence"] = evidence.get(str(event["evidence_ref"])) if event.get("evidence_ref") else None
            if isinstance(event["details"], dict) and "_inline_evidence" in event["details"]:
                event["evidence"] = event["details"].pop("_inline_evidence")
            result["timeline"].append(event)
        expired_attempts = {event["details"].get("attempt_id") for event in result["timeline"] if event.get("happened_at_ms") is not None and event["happened_at_ms"] < day_start_ms() and external_error(event) and event["details"].get("attempt_id") is not None}
        result["timeline"] = [event for event in result["timeline"] if not (event.get("happened_at_ms") is not None and event["happened_at_ms"] < day_start_ms() and (external_error(event) or event["details"].get("attempt_id") in expired_attempts))]
        return expire_external_errors(result, result.get("updated_at_ms"))

    def coverage(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = conn.execute("SELECT business_key,source,COUNT(*) AS items FROM system_information_items GROUP BY business_key,source ORDER BY business_key,source").fetchall()
            states = {row["state_key"]: row["state_value"] for row in conn.execute("SELECT state_key,state_value FROM system_information_state")}
        adapters = []
        for key, value in states.items():
            if key.startswith("adapter_") and key.endswith("_status"):
                name = key.removeprefix("adapter_").removesuffix("_status")
                adapters.append({"name": name, "status": value, "audit_tables": int(states.get(f"adapter_{name}_audit_tables", "0") or 0), "audit_skipped": int(states.get(f"adapter_{name}_audit_skipped", "0") or 0), "audit_watermark": int(states.get(f"source_audit_{name}_watermark", "0") or 0)})
        return {"sources": [dict(row) for row in counts], "adapters": adapters, "projection": {"watermark": int(states.get("main_tasks_watermark", "0") or 0), "lag": int(states.get("main_tasks_lag", "0") or 0), "last_success_ms": int(states["main_tasks_last_success"]) if states.get("main_tasks_last_success") else None, "last_error": states.get("main_tasks_last_error") or None}}

    def record_external_error_totals(self, database_key: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.executemany("INSERT INTO system_information_external_error_daily(database_key,error_date,source,stage,category,occurrences) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(database_key,error_date,source,stage,category) DO UPDATE SET occurrences=excluded.occurrences", ((database_key,row["error_date"],row["source"],row["stage"],row["category"],row["occurrences"]) for row in rows))

    def external_error_totals(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT error_date,source,stage,category,SUM(occurrences) AS occurrences FROM system_information_external_error_daily GROUP BY error_date,source,stage,category ORDER BY error_date DESC,source,stage,category LIMIT 500").fetchall()]

    def cleanup_external_errors(self, *, now_ms: int | None = None, limit: int = 200) -> dict[str, int]:
        """Count and redact old upstream-error copies in the observation projection."""
        now = now_ms or _now_ms()
        cutoff = day_start_ms(now)
        counted = cleared = scanned = 0
        with self.connect() as conn:
            with conn.transaction():
                for table, key_column, time_column, event in (
                    ("system_information_items", "item_id", "updated_at_ms", False),
                    ("system_information_events", "event_id", "happened_at_ms", True),
                ):
                    cursor_key = f"external_retention:{table}"
                    state = conn.execute("SELECT state_value FROM system_information_state WHERE state_key=%s FOR UPDATE", (cursor_key,)).fetchone()
                    cursor = str(state["state_value"] if state else "")
                    query = (
                        f"SELECT r.* FROM {table} r WHERE r.{time_column}<%s AND r.{key_column}>%s "
                        f"ORDER BY r.{key_column} LIMIT %s FOR UPDATE OF r SKIP LOCKED"
                    )
                    rows = conn.execute(query, (cutoff, cursor, max(1, min(limit, 2000)))).fetchall()
                    for record in rows:
                        row = dict(record)
                        scanned += 1
                        evidence_ref = row.get("evidence_ref")
                        evidence = self._load_evidence_in_connection(conn, str(evidence_ref)) if evidence_ref else None
                        material = {**row, "evidence": evidence}
                        messages = error_messages(material)
                        category = next((kind for message in messages if (kind := failure_category(message)) in EXTERNAL_CATEGORIES), None)
                        if not category:
                            continue
                        happened = row.get(time_column) or row.get("observed_at_ms") or 0
                        error_date = (datetime.fromtimestamp(int(happened) / 1000, UTC) + timedelta(hours=8)).date().isoformat()
                        row_key = f"{table}:{row[key_column]}"
                        inserted = conn.execute("INSERT INTO system_information_external_error_ledger(row_key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING row_key", (row_key,)).fetchone()
                        if inserted:
                            parent = conn.execute("SELECT source FROM system_information_items WHERE item_id=%s", (row.get("item_id"),)).fetchone() if event else None
                            source = str((parent["source"] if parent else None) or row.get("source") or "unknown")
                            stage = str(row.get("stage") or "unknown")
                            conn.execute("INSERT INTO system_information_external_error_daily(database_key,error_date,source,stage,category,occurrences) VALUES(%s,%s,%s,%s,%s,1) ON CONFLICT(database_key,error_date,source,stage,category) DO UPDATE SET occurrences=system_information_external_error_daily.occurrences+1", ("system_observability", error_date, source, stage, category))
                            counted += 1
                        if evidence is not None:
                            cleaned = scrub_external_errors(evidence)
                            if cleaned != evidence:
                                new_ref = self._put_evidence(conn, cleaned)
                                self._replace_evidence_reference(conn, str(evidence_ref), str(new_ref))
                        if event:
                            conn.execute("UPDATE system_information_events SET summary='',reason=NULL,error_code=NULL,details_json=%s::jsonb WHERE event_id=%s", (_json(scrub_external_errors(row.get("details_json") or {})), row[key_column]))
                        else:
                            conn.execute("UPDATE system_information_items SET summary='',failure_category=NULL,fingerprint='' WHERE item_id=%s", (row[key_column],))
                            self._refresh_item_search(conn, str(row[key_column]), scrub_external_errors(evidence))
                        cleared += 1
                    next_cursor = str(rows[-1][key_column]) if rows else ""
                    conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES(%s,%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (cursor_key, next_cursor, now))
        return {"scanned": scanned, "counted": counted, "cleared": cleared}

    def _replace_evidence_reference(self, conn, old_ref: str, new_ref: str) -> None:
        item_ids = [str(row["item_id"]) for row in conn.execute("SELECT item_id FROM system_information_items WHERE evidence_ref=%s", (old_ref,)).fetchall()]
        for table, column in (("system_information_items", "evidence_ref"), ("system_information_events", "evidence_ref"),
                              ("system_information_decision_samples", "evidence_ref"), ("system_information_evidence_pins", "evidence_ref"),
                              ("system_review_analysis_attempts", "raw_response_ref")):
            if conn.execute("SELECT to_regclass(%s) AS relation", (f"{self.schema}.{table}",)).fetchone()["relation"]:
                conn.execute(f"UPDATE {table} SET {column}=%s WHERE {column}=%s", (new_ref, old_ref))
        for item_id in item_ids:
            evidence = self._load_evidence_in_connection(conn, new_ref)
            self._refresh_item_search(conn, item_id, evidence)

    def _sample_rule_version(self, evidence_ref: str, conn=None) -> str | None:
        evidence = self._load_evidence_in_connection(conn, evidence_ref) if conn is not None else self.load_evidence(evidence_ref)
        pending: list[Any] = [evidence]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                for key in ("prompt_version", "prompt_version_id", "policy_version", "template_version"):
                    if value.get(key) not in (None, ""):
                        return str(value[key])[:160]
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        return None

    def record_decision_samples(self, *, now_ms: int | None = None, limit: int = 50, max_bytes: int = 20 * 1024 * 1024) -> dict[str, int]:
        now = now_ms or _now_ms()
        sample_date = datetime.fromtimestamp(now / 1000, UTC).date().isoformat()
        with self.connect() as conn:
            with conn.transaction():
                state = conn.execute("SELECT state_value FROM system_information_state WHERE state_key='decision_sample_date' FOR UPDATE").fetchone()
                existing = int(conn.execute("SELECT COUNT(*) AS count FROM system_information_decision_samples WHERE sample_date=%s", (sample_date,)).fetchone()["count"])
                if state and state["state_value"] == sample_date:
                    return {"sampled": existing, "evidence_bytes": 0, "already_sampled": 1}
                conn.execute("DELETE FROM system_information_decision_samples WHERE sampled_at_ms<%s", (now - 30 * 86_400_000,))
                rows = conn.execute("""SELECT i.item_id,i.business_key,i.source,i.source_id,i.stage,i.status,i.result,i.summary,i.evidence_ref,e.byte_size
                    FROM system_information_items i JOIN system_information_evidence e ON e.evidence_ref=i.evidence_ref
                    WHERE i.updated_at_ms>=%s AND (i.result='stopped' OR i.status IN ('no_candidate','ignored','discarded','deduped','duplicate')) ORDER BY i.item_id""", (now - 86_400_000,)).fetchall()
                strata: dict[tuple[str, str, str, str], list[Any]] = {}
                for row in rows:
                    key = (row["business_key"], row["source"], row["status"], row["result"])
                    strata.setdefault(key, []).append(row)
                for key, values in strata.items():
                    values.sort(key=lambda row: hashlib.sha256(f"{sample_date}:{key}:{row['item_id']}".encode()).digest())
                selected: list[Any] = []
                total_bytes = 0
                remaining = sorted(strata)
                while remaining and len(selected) < max(1, min(50, int(limit))):
                    next_round = []
                    for key in remaining:
                        values = strata[key]
                        while values:
                            candidate = values.pop(0)
                            size = max(0, int(candidate["byte_size"] or 0))
                            if total_bytes + size <= max(1, min(20 * 1024 * 1024, int(max_bytes))):
                                selected.append(candidate)
                                total_bytes += size
                                break
                        if values:
                            next_round.append(key)
                    remaining = next_round
                for row in selected:
                    sample_key = hashlib.sha256(f"{sample_date}:{row['item_id']}".encode()).hexdigest()
                    ref = row["evidence_ref"]
                    self.evidence.lock_for_reference(conn, ref)
                    conn.execute("INSERT INTO system_information_decision_samples(sample_key,sample_date,sampled_at_ms,raw_expires_at_ms,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref,evidence_bytes) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(sample_key) DO NOTHING", (sample_key,sample_date,now,now+14*86_400_000,row["item_id"],row["business_key"],row["source"],row["source_id"],row["stage"],row["status"],row["result"],str(row["summary"] or "")[:1200],self._sample_rule_version(ref,conn),ref,int(row["byte_size"] or 0)))
                    conn.execute("INSERT INTO system_information_evidence_pins(pin_key,evidence_ref,pinned_at_ms) VALUES(%s,%s,%s) ON CONFLICT(pin_key) DO UPDATE SET evidence_ref=excluded.evidence_ref,pinned_at_ms=excluded.pinned_at_ms", (f"decision-sample:{sample_key}",ref,now))
                conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('decision_sample_date',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (sample_date,now))
        return {"sampled": len(selected), "evidence_bytes": total_bytes, "already_sampled": 0}

    def expire_decision_samples(self, *, now_ms: int | None = None) -> dict[str, int]:
        now = now_ms or _now_ms()
        with self.connect() as conn:
            with conn.transaction():
                expired = conn.execute("SELECT sample_key FROM system_information_decision_samples WHERE evidence_ref IS NOT NULL AND raw_expires_at_ms<=%s FOR UPDATE", (now,)).fetchall()
                for row in expired:
                    conn.execute("DELETE FROM system_information_evidence_pins WHERE pin_key=%s", (f"decision-sample:{row['sample_key']}",))
                    conn.execute("UPDATE system_information_decision_samples SET evidence_ref=NULL,evidence_bytes=0 WHERE sample_key=%s", (row["sample_key"],))
                removed = conn.execute("DELETE FROM system_information_decision_samples WHERE sampled_at_ms<%s", (now-30*86_400_000,)).rowcount
        return {"raw_expired": len(expired), "index_removed": int(removed)}

    def decision_samples(self, *, sample_date: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if sample_date:
                rows = conn.execute("SELECT * FROM system_information_decision_samples WHERE sample_date=%s ORDER BY sampled_at_ms DESC,business_key,source,item_id LIMIT %s", (sample_date,max(1,min(100,int(limit))))).fetchall()
            else:
                rows = conn.execute("SELECT * FROM system_information_decision_samples ORDER BY sampled_at_ms DESC,business_key,source,item_id LIMIT %s", (max(1,min(100,int(limit))),)).fetchall()
        return [dict(row) for row in rows]

    def decision_samples_for_sources(self, source_items: list[tuple[str,str]], *, through_ms: int, limit: int = 5) -> dict[tuple[str,str],list[dict[str,Any]]]:
        pairs = list(dict.fromkeys((str(source),str(source_id)) for source,source_id in source_items))[:12]
        if not pairs:
            return {}
        values_sql = ",".join("(%s,%s)" for _ in pairs)
        params = [value for pair in pairs for value in pair]
        params.append(through_ms)
        with self.connect() as conn:
            rows = conn.execute("SELECT sample_key,item_id,source,source_id,sampled_at_ms,sample_date,stage,status,result,reason_summary,rule_version,evidence_ref FROM system_information_decision_samples WHERE (source,source_id) IN ("+values_sql+") AND sampled_at_ms<=%s ORDER BY sampled_at_ms DESC,business_key,source,item_id",params).fetchall()
        grouped = {pair:[] for pair in pairs}
        for row in rows:
            key=(str(row["source"]),str(row["source_id"]))
            if key in grouped and len(grouped[key])<max(1,min(10,limit)):
                item=dict(row); item["raw_evidence_available"]=bool(item.pop("evidence_ref")); grouped[key].append(item)
        return {key:value for key,value in grouped.items() if value}

    def configuration_changes_for_sources(self, source_keys: list[str], *, through_ms: int, limit_per_source: int = 20) -> dict[str,list[dict[str,Any]]]:
        keys=list(dict.fromkeys(str(key) for key in source_keys if key))[:12]
        if not keys:
            return {}
        item_ids=[f"main:non_mainstream_media_sources:{key}" for key in keys]
        with self.connect() as conn:
            rows=conn.execute("SELECT i.source_id,e.event_id,e.event_type,e.status,e.happened_at_ms,e.summary,e.reason,e.evidence_ref,e.details_json FROM system_information_items i JOIN system_information_events e ON e.item_id=i.item_id WHERE i.item_id=ANY(%s) AND e.event_type='source_state_change' AND e.happened_at_ms<=%s ORDER BY e.happened_at_ms DESC,e.event_id",(item_ids,through_ms)).fetchall()
        grouped={key:[] for key in keys}
        for row in rows:
            key=str(row["source_id"] or "")
            if key not in grouped or len(grouped[key])>=max(1,min(20,limit_per_source)):
                continue
            item=dict(row); ref=item.pop("evidence_ref"); item["details"]=item.pop("details_json") or {}; item["evidence"]=self.load_evidence(ref) if ref else None; grouped[key].append(item)
        return {key:value for key,value in grouped.items() if value}

    def _evidence_excerpt(self, item: dict[str,Any], reference_key: str="evidence_ref", conn=None) -> dict[str,Any]:
        ref=item.pop(reference_key,None); evidence=(self._load_evidence_in_connection(conn,ref) if conn is not None else self.load_evidence(ref)) if ref else None
        encoded=_json(evidence) if evidence is not None else ""
        item["evidence_excerpt"]=encoded[:8000] if evidence is not None else None
        item["evidence_sha256"]=hashlib.sha256(encoded.encode()).hexdigest() if evidence is not None else None
        item["evidence_truncated"]=len(encoded)>8000 if evidence is not None else False
        item["evidence_available"]=evidence is not None
        return item

    def output_review_samples(self, *, now_ms: int | None = None, limit: int = 30) -> list[dict[str,Any]]:
        now=now_ms or _now_ms()
        with self.connect() as conn:
            rows=conn.execute("SELECT item_id,business_key,source,source_id,title,summary,source_url,stage,status,result,updated_at_ms,evidence_ref FROM system_information_items WHERE business_key IN ('x','crypto_media','meme','x_agent') AND result IN ('internal_complete','direct_publish','backoffice') AND evidence_ref IS NOT NULL AND updated_at_ms>=%s ORDER BY item_id",(now-86_400_000,)).fetchall()
        date=datetime.fromtimestamp(now/1000,UTC).date().isoformat(); strata={}
        for row in rows: strata.setdefault((row["business_key"],row["source"]),[]).append(row)
        for key,values in strata.items(): values.sort(key=lambda row:hashlib.sha256(f"{date}:{key}:{row['item_id']}".encode()).digest())
        selected=[]; remaining=sorted(strata)
        while remaining and len(selected)<max(1,min(30,int(limit))):
            next_round=[]
            for key in remaining:
                if strata[key]: selected.append(dict(strata[key].pop(0)))
                if strata[key]: next_round.append(key)
                if len(selected)>=max(1,min(30,int(limit))): break
            remaining=next_round
        return selected

    def stream_item_evidence_details(self, item_ids: list[str]) -> list[dict[str,Any]]:
        keys=sorted({str(value) for value in item_ids})[:30]
        if not keys: return []
        with self.connect() as conn:
            rows=conn.execute("SELECT item_id,business_key,source,source_id,stage,status,result,evidence_ref FROM system_information_items WHERE item_id=ANY(%s)",(keys,)).fetchall()
        result=[]
        for row in rows:
            item=dict(row); item["stream_item_id"]=item["item_id"]; result.append(self._evidence_excerpt(item))
        return result

    def decision_sample_details(self, sample_keys: list[str]) -> list[dict[str,Any]]:
        keys=sorted({str(value) for value in sample_keys})[:50]
        if not keys: return []
        with self.connect() as conn:
            rows=conn.execute("SELECT sample_key,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref FROM system_information_decision_samples WHERE sample_key=ANY(%s) ORDER BY sample_date DESC,business_key,source,item_id",(keys,)).fetchall()
        return [self._evidence_excerpt(dict(row)) for row in rows]

    @staticmethod
    def _compact_evidence(value: Any) -> Any:
        if isinstance(value, list):
            return [PostgresSystemInformationStore._compact_evidence(item) for item in value]
        if not isinstance(value, dict):
            return value
        if value.get("source_table") == "tasks":
            stamp = value.get("updated_at_ms") or value.get("created_at_ms")
            now = _now_ms()
            if stamp is None or int(stamp) >= now - 5 * 86_400_000:
                return value
        result = {}
        for key, item in value.items():
            normalized = str(key).casefold()
            if normalized == "content" and isinstance(item, str) and len(item) > 2000:
                result["content_excerpt"] = item[:2000]
                result["content_sha256"] = hashlib.sha256(item.encode()).hexdigest()
            elif normalized == "raw_model_response" and isinstance(item, str):
                result["raw_model_response_excerpt"] = item[:1000]
                result["raw_model_response_sha256"] = hashlib.sha256(item.encode()).hexdigest()
                result["raw_model_response_bytes"] = len(item.encode())
            elif normalized in {"writer_output", "model_response", "response_text", "raw_output"} and isinstance(item, str):
                result[f"{key}_excerpt"] = item[:1000]
                result[f"{key}_sha256"] = hashlib.sha256(item.encode()).hexdigest()
            else:
                result[str(key)] = PostgresSystemInformationStore._compact_evidence(item)
        return result

    def _evidence_references(self, conn, reference: str) -> list[dict[str, Any]]:
        specs = (("system_information_items", "evidence_ref", "status", "result"),
                 ("system_information_events", "evidence_ref", "status", "result"),
                 ("system_information_decision_samples", "evidence_ref", "status", "result"),
                 ("system_information_evidence_pins", "evidence_ref", "pin_key", "pin_key"),
                 ("system_review_analysis_attempts", "raw_response_ref", "status", "status"))
        refs = []
        for table, column, status_col, result_col in specs:
            if conn.execute("SELECT to_regclass(%s) AS relation", (f"{self.schema}.{table}",)).fetchone()["relation"]:
                refs.extend(dict(row) for row in conn.execute(f"SELECT %s AS table_name,{status_col} AS status,{result_col} AS result FROM {table} WHERE {column}=%s", (table,reference)).fetchall())
        # Source attempts live in main, outside this schema's FK graph. They
        # can still own the evidence after its information-feed item expires.
        for table in ("x_processing_attempts", "x_processing_attempt_events"):
            if conn.execute("SELECT to_regclass(%s) AS relation", (f"main.{table}",)).fetchone()["relation"]:
                refs.extend(dict(row) for row in conn.execute(
                    f"SELECT %s AS table_name,status,status AS result FROM main.{table} WHERE response_ref=%s",
                    (table, reference),
                ).fetchall())
        return refs

    def _has_business_response_reference(self, conn, reference: str) -> bool:
        for table in ("x_processing_attempts", "x_processing_attempt_events"):
            if conn.execute("SELECT to_regclass(%s) AS relation", (f"main.{table}",)).fetchone()["relation"]:
                if conn.execute(f"SELECT 1 FROM main.{table} WHERE response_ref=%s LIMIT 1", (reference,)).fetchone():
                    return True
        return False

    def _refresh_item_search(self, conn, item_id: str, evidence: Any) -> None:
        # Historical imported postings remain untouched. Runtime evidence
        # retention no longer creates or compacts keyword indexes.
        del conn, item_id, evidence

    def _record_source_identity(self, conn, snapshot: dict[str, Any], item_id: str, observed: int) -> None:
        schema = str(snapshot.get("source_schema") or "")
        if not schema:
            return
        source_table = str(snapshot["source_table"])
        row_id = str(snapshot.get("source_identity_row_id") or snapshot["source_row_id"])
        lock_key = f"source-identity:{schema}:{source_table}:{row_id}"
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (lock_key,))
        conn.execute(
            "INSERT INTO system_information_source_identity(source_schema,source_table,source_row_id,item_id,updated_at_ms) "
            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(source_schema,source_table,source_row_id) "
            "DO UPDATE SET item_id=excluded.item_id,updated_at_ms=excluded.updated_at_ms",
            (schema, source_table, row_id, item_id, observed),
        )

    def cleanup_expired_evidence(self, *, now_ms: int | None = None, retain_days: int = 90, limit: int = 200, dry_run: bool = True) -> dict[str,int]:
        now=now_ms or _now_ms()
        if not dry_run:
            self.expire_decision_samples(now_ms=now)
        cutoff=now-max(1,retain_days)*86_400_000
        compacted=compacted_bytes=protected=orphaned=orphaned_bytes=0
        old_files=[]
        gc_cursor=self.get_state("evidence_gc_cursor")
        compaction_cursor=self.get_state("evidence_compaction_cursor")
        with self.connect() as conn:
            with conn.transaction():
                rows=conn.execute("SELECT evidence_ref,path,byte_size FROM system_information_evidence WHERE created_at_ms<%s AND evidence_ref>%s ORDER BY evidence_ref FOR UPDATE SKIP LOCKED LIMIT %s",(cutoff,compaction_cursor,max(1,min(2000,limit)))).fetchall()
                for row in rows:
                    ref=str(row["evidence_ref"]); refs=self._evidence_references(conn,ref)
                    if any(str(r.get("status") or "") in {"pending","processing","running","queued","waiting","retry"} or str(r.get("result") or "") in {"processing","unknown","backoffice"} or any(word in str(r.get("status") or "").casefold() for word in ("fail","error")) or any(word in str(r.get("result") or "").casefold() for word in ("fail","error")) or r["table_name"]=="system_information_evidence_pins" for r in refs):
                        protected+=1; continue
                    payload=self._load_evidence_in_connection(conn,ref); compact=self._compact_evidence(payload)
                    if payload is None or compact==payload: continue
                    size=len(_json(compact).encode())
                    if dry_run:
                        compacted+=1; compacted_bytes+=max(0,int(row["byte_size"])-size); continue
                    new_ref=self._put_evidence(conn,compact)
                    item_ids=[str(r["item_id"]) for r in conn.execute("SELECT item_id FROM system_information_items WHERE evidence_ref=%s",(ref,)).fetchall()]
                    for table,column in (("system_information_items","evidence_ref"),("system_information_events","evidence_ref"),("system_information_decision_samples","evidence_ref"),("system_information_evidence_pins","evidence_ref"),("system_review_analysis_attempts","raw_response_ref")):
                        if conn.execute("SELECT to_regclass(%s) AS relation",(f"{self.schema}.{table}",)).fetchone()["relation"]:
                            conn.execute(f"UPDATE {table} SET {column}=%s WHERE {column}=%s",(new_ref,ref))
                    for item_id in item_ids:self._refresh_item_search(conn,item_id,compact)
                    if not self._evidence_references(conn,ref):
                        conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=%s",(ref,))
                        if not str(row["path"]).startswith("postgres://"): old_files.append(Path(row["path"]))
                    compacted+=1; compacted_bytes+=max(0,int(row["byte_size"])-size)
                if not dry_run:
                    next_compaction_cursor=str(rows[-1]["evidence_ref"]) if rows else ""
                    conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('evidence_compaction_cursor',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",(next_compaction_cursor,now))
                gc_rows=conn.execute("SELECT evidence_ref,path,byte_size FROM system_information_evidence WHERE evidence_ref>%s ORDER BY evidence_ref LIMIT %s FOR UPDATE SKIP LOCKED",(gc_cursor,max(1,min(2000,limit)))).fetchall()
                if not dry_run:
                    for row in gc_rows:
                        ref=str(row["evidence_ref"])
                        if not self._evidence_references(conn,ref):
                            conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=%s",(ref,))
                            if not str(row["path"]).startswith("postgres://"): old_files.append(Path(row["path"]))
                            orphaned+=1; orphaned_bytes+=int(row["byte_size"])
                    next_cursor=str(gc_rows[-1]["evidence_ref"]) if gc_rows else ""
                    conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('evidence_gc_cursor',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",(next_cursor,now))
        for path in old_files: self._unlink_if_unregistered(path)
        return {"compacted":compacted,"compacted_bytes":compacted_bytes,"protected":protected,"orphaned":orphaned,"orphaned_bytes":orphaned_bytes,"dry_run":int(dry_run)}

    def prune_feed_history_evidence(self, *, limit: int = 1000) -> dict[str, int]:
        """Remove feed-only evidence references in bounded batches after the feed reset."""
        batch = max(1, min(int(limit), 100000))
        small_refs: list[str] = []
        paths: list[tuple[str, Path]] = []
        with self.connect() as conn:
            with conn.transaction():
                optional_references = []
                for relation, column in ((f"{self.schema}.system_review_analysis_attempts", "raw_response_ref"),
                                         ("main.x_processing_attempts", "response_ref"),
                                         ("main.x_processing_attempt_events", "response_ref")):
                    if conn.execute("SELECT to_regclass(%s) AS relation", (relation,)).fetchone()["relation"]:
                        optional_references.append(f"AND NOT EXISTS (SELECT 1 FROM {relation} r WHERE r.{column}=e.evidence_ref)")
                rows = conn.execute(
                    f"""SELECT e.evidence_ref,e.path FROM system_information_evidence e
                       WHERE NOT EXISTS (SELECT 1 FROM system_information_items i WHERE i.evidence_ref=e.evidence_ref)
                         AND NOT EXISTS (SELECT 1 FROM system_information_events v WHERE v.evidence_ref=e.evidence_ref)
                         AND NOT EXISTS (SELECT 1 FROM system_information_decision_samples d WHERE d.evidence_ref=e.evidence_ref)
                         AND NOT EXISTS (SELECT 1 FROM system_information_evidence_pins p WHERE p.evidence_ref=e.evidence_ref)
                         {' '.join(optional_references)}
                       ORDER BY e.evidence_ref LIMIT %s""", (batch,),
                ).fetchall()
                candidate_refs = [str(row["evidence_ref"]) for row in rows]
                if candidate_refs:
                    for reference in sorted(candidate_refs):
                        self._lock_external_evidence_key(conn, reference)
                    rows = conn.execute(
                        f"""SELECT e.evidence_ref,e.path FROM system_information_evidence e
                           WHERE e.evidence_ref=ANY(%s)
                             AND NOT EXISTS (SELECT 1 FROM system_information_items i WHERE i.evidence_ref=e.evidence_ref)
                             AND NOT EXISTS (SELECT 1 FROM system_information_events v WHERE v.evidence_ref=e.evidence_ref)
                             AND NOT EXISTS (SELECT 1 FROM system_information_decision_samples d WHERE d.evidence_ref=e.evidence_ref)
                             AND NOT EXISTS (SELECT 1 FROM system_information_evidence_pins p WHERE p.evidence_ref=e.evidence_ref)
                             {' '.join(optional_references)}
                           ORDER BY e.evidence_ref FOR UPDATE OF e SKIP LOCKED""", (candidate_refs,),
                    ).fetchall()
                refs = [str(row["evidence_ref"]) for row in rows]
                if refs:
                    conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=ANY(%s)", (refs,))
                    for row in rows:
                        path = str(row["path"])
                        if path.startswith("postgres://small_evidence/"):
                            small_refs.append(str(row["evidence_ref"]))
                        else:
                            paths.append((str(row["evidence_ref"]), Path(path)))
                    if small_refs:
                        self.evidence.delete_many_in_transaction(conn, small_refs)
                    if paths:
                        with conn.cursor() as cursor:
                            cursor.executemany(
                                "INSERT INTO system_information_evidence_file_gc(evidence_ref,path,queued_at_ms) VALUES(%s,%s,%s) ON CONFLICT(evidence_ref) DO UPDATE SET path=excluded.path,queued_at_ms=excluded.queued_at_ms",
                                [(ref, str(path), _now_ms()) for ref, path in paths],
                            )
                conn.execute(
                    "INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('feed_evidence_cleanup_last_batch',%s,%s) "
                    "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",
                    (str(len(refs)), _now_ms()),
                )
        self._drain_evidence_file_gc(limit=batch)
        return {"scanned": len(rows), "removed": len(refs)}

    def _drain_evidence_file_gc(self, *, limit: int = 1000) -> int:
        removed = 0
        failure = ""
        with self.connect() as conn:
            queued = conn.execute("SELECT evidence_ref,path FROM system_information_evidence_file_gc ORDER BY queued_at_ms,evidence_ref LIMIT %s", (max(1, min(limit, 100000)),)).fetchall()
        for offset in range(0, len(queued), 500):
            chunk = [(str(row["evidence_ref"]), Path(str(row["path"]))) for row in queued[offset:offset + 500]]
            try:
                with self.connect() as conn:
                    with conn.transaction():
                        for ref, _path in chunk:
                            self._lock_external_evidence_key(conn, ref)
                        refs = [ref for ref, _path in chunk]
                        predicates = ["NOT EXISTS (SELECT 1 FROM system_information_evidence e WHERE e.evidence_ref=r.ref)"]
                        for relation, column in ((f"{self.schema}.system_information_items", "evidence_ref"),
                                                 (f"{self.schema}.system_information_events", "evidence_ref"),
                                                 (f"{self.schema}.system_information_decision_samples", "evidence_ref"),
                                                 (f"{self.schema}.system_information_evidence_pins", "evidence_ref"),
                                                 (f"{self.schema}.system_review_analysis_attempts", "raw_response_ref"),
                                                 ("main.x_processing_attempts", "response_ref"),
                                                 ("main.x_processing_attempt_events", "response_ref")):
                            if conn.execute("SELECT to_regclass(%s) AS relation", (relation,)).fetchone()["relation"]:
                                schema, table = relation.split(".", 1)
                                predicates.append(f"NOT EXISTS (SELECT 1 FROM {Identifier(schema, table).as_string(conn)} x WHERE x.{Identifier(column).as_string(conn)}=r.ref)")
                        safe_refs = {str(row["ref"]) for row in conn.execute(
                            "SELECT r.ref FROM unnest(%s::text[]) AS r(ref) WHERE " + " AND ".join(predicates), (refs,),
                        ).fetchall()}
                        for ref, path in chunk:
                            if ref in safe_refs and path == self.evidence_dir / ref[:2] / f"{ref}.json.gz":
                                path.unlink(missing_ok=True)
                                removed += 1
                            conn.execute("DELETE FROM system_information_evidence_file_gc WHERE evidence_ref=%s", (ref,))
            except Exception as exc:
                # Removed registry rows with leftover files are safe to retry later.
                failure = f"{type(exc).__name__}: {exc}"[:500]
                continue
        self.set_state("evidence_file_gc_error", failure)
        return removed

    def _unlink_if_unregistered(self,path:Path)->bool:
        ref=path.name.removesuffix(".json.gz")
        if len(ref)!=64 or path!=self.evidence_dir/ref[:2]/f"{ref}.json.gz": return False
        with self.connect() as conn:
            with conn.transaction():
                self._lock_external_evidence_key(conn, ref)
                if conn.execute("SELECT 1 FROM system_information_evidence WHERE evidence_ref=%s FOR UPDATE",(ref,)).fetchone() or self._evidence_references(conn,ref): return False
                existed=path.exists(); path.unlink(missing_ok=True); return existed

    def _remove_small_if_unregistered(self,ref:str)->bool:
        return self.evidence.delete_if_unreferenced(ref)

    def prune_unregistered_small_evidence(self,*,limit:int=200)->dict[str,int]:
        with self.connect() as conn:
            rows=conn.execute("SELECT e.evidence_ref FROM small_evidence e LEFT JOIN system_information_evidence r ON r.evidence_ref=e.evidence_ref WHERE r.evidence_ref IS NULL ORDER BY e.evidence_ref LIMIT %s",(max(1,min(1000,limit)),)).fetchall()
        removed=sum(int(self.evidence.delete_if_unreferenced(str(row["evidence_ref"]))) for row in rows)
        return {"scanned":len(rows),"removed":removed}

    def prune_orphan_evidence(self,*,limit:int=500,dry_run:bool=True)->dict[str,int]:
        old_files=[]; removed=[]
        cursor=self.get_state("orphan_evidence_cursor")
        with self.connect() as conn:
            with conn.transaction():
                rows=conn.execute("SELECT evidence_ref,path,byte_size FROM system_information_evidence WHERE evidence_ref>%s ORDER BY evidence_ref LIMIT %s FOR UPDATE SKIP LOCKED",(cursor,max(1,min(5000,limit)))).fetchall()
                for row in rows:
                    ref=str(row["evidence_ref"])
                    if self._evidence_references(conn,ref): continue
                    removed.append(int(row["byte_size"]))
                    if not dry_run:
                        conn.execute("DELETE FROM system_information_evidence WHERE evidence_ref=%s",(ref,))
                        if not str(row["path"]).startswith("postgres://"): old_files.append((Path(row["path"]),ref))
                if not dry_run:
                    next_cursor=str(rows[-1]["evidence_ref"]) if rows else ""
                    conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('orphan_evidence_cursor',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",(next_cursor,_now_ms()))
        if not dry_run:
            for path,ref in old_files:
                if not self._unlink_if_unregistered(path): self.evidence.delete_if_unreferenced(ref)
            with self.connect() as conn:
                refs=conn.execute("SELECT evidence_ref FROM small_evidence e LEFT JOIN system_information_evidence r USING(evidence_ref) WHERE r.evidence_ref IS NULL LIMIT %s",(max(1,min(5000,limit)),)).fetchall()
            for row in refs: self.evidence.delete_if_unreferenced(str(row["evidence_ref"]))
        return {"files":len(removed),"bytes":sum(removed),"dry_run":int(dry_run)}

    def storage_health(self,*,sample:bool=False,now_ms:int|None=None)->dict[str,Any]:
        now=now_ms or _now_ms(); usage=shutil.disk_usage(self.evidence_dir.parent); date=datetime.fromtimestamp(now/1000,UTC).date().isoformat()
        with self.connect() as conn:
            db=int(conn.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()["bytes"])
            ev=int(conn.execute("SELECT COALESCE(SUM(byte_size),0) AS bytes FROM system_information_evidence").fetchone()["bytes"])
            if sample: conn.execute("INSERT INTO system_information_storage_samples(sample_date,database_bytes,evidence_bytes,sampled_at_ms) VALUES(%s,%s,%s,%s) ON CONFLICT(sample_date) DO UPDATE SET database_bytes=excluded.database_bytes,evidence_bytes=excluded.evidence_bytes,sampled_at_ms=excluded.sampled_at_ms",(date,db,ev,now))
            samples=conn.execute("SELECT database_bytes,evidence_bytes,sampled_at_ms FROM system_information_storage_samples WHERE sampled_at_ms>=%s ORDER BY sampled_at_ms",(now-30*86_400_000,)).fetchall()
        growth=None
        if len(samples)>=2:
            days=max((int(samples[-1]["sampled_at_ms"])-int(samples[0]["sampled_at_ms"]))/86_400_000,1/24)
            growth=max(0,int(((int(samples[-1]["database_bytes"])+int(samples[-1]["evidence_bytes"]))-(int(samples[0]["database_bytes"])+int(samples[0]["evidence_bytes"])))/days))
        required=max(10*1024**3,int(usage.total*.15))
        return {"filesystem_total_bytes":usage.total,"filesystem_free_bytes":usage.free,"required_free_bytes":required,"space_ok":usage.free>=required,"database_bytes":db,"evidence_bytes":ev,"estimated_growth_bytes_per_day":growth,"estimated_growth_next_30d_bytes":growth*30 if growth is not None else None,"samples":len(samples),"sampled_at_ms":now}

    def _reindex_page(self, *, key: str, cursor_key: str, done_key: str, limit: int, now_ms: int) -> dict[str,int]:
        del key, cursor_key, done_key, limit, now_ms
        return {"scanned": 0, "done": 1}

    def rebuild_search_index(self, limit: int = 100) -> int:
        del limit
        return 0

    def compact_search_index(self, *, now_ms: int | None = None, limit: int = 25) -> dict[str,int]:
        del now_ms, limit
        return {"scanned": 0, "done": 1}

    def trim_information_items(self, *, now_ms: int | None = None, limit: int = 2000) -> dict[str, int]:
        """Delete expired feed copies in bounded batches; business rows own their evidence."""
        now = now_ms if now_ms is not None else _now_ms()
        cutoff = now - 6 * 60 * 60 * 1000
        batch = max(1, min(int(limit), 10000))
        with self.connect() as conn:
            with conn.transaction():
                rows = conn.execute(
                    "SELECT item_id FROM system_information_items WHERE COALESCE(updated_at_ms,observed_at_ms)<%s "
                    "ORDER BY COALESCE(updated_at_ms,observed_at_ms),item_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (cutoff, batch),
                ).fetchall()
                ids = [str(row["item_id"]) for row in rows]
                removed = conn.execute(
                    "DELETE FROM system_information_items WHERE item_id=ANY(%s)", (ids,),
                ).rowcount if ids else 0
        return {"scanned": len(rows), "removed": removed}

    def prune_task_status_events(self, *, source_database: Path | None = None, now_ms: int | None = None, limit: int = 2000, max_queued: int = 50) -> dict[str,int]:
        now=now_ms or _now_ms(); cutoff=now-5*86_400_000; cursor=self.get_state("status_retention_cursor")
        removed=scanned=0
        with self.connect() as conn:
            with conn.transaction():
                rows=conn.execute("SELECT e.event_id,e.event_type,e.happened_at_ms,e.observed_at_ms,e.item_id,e.status,e.result,e.error_code,e.evidence_ref,i.source_table,i.status AS item_status,i.result AS item_result FROM system_information_events e LEFT JOIN system_information_items i ON i.item_id=e.item_id WHERE e.event_id>%s ORDER BY e.event_id LIMIT %s",(cursor,max(1,min(limit,5000)))).fetchall()
                for row in rows:
                    scanned+=1
                    stamp=row["happened_at_ms"] or row["observed_at_ms"]
                    if stamp>=cutoff or row["source_table"]=="tasks" and row["item_status"] not in {"discarded","duplicate","auto_published","published","notified","ready_review","event_tracking_cancelled","expired","legacy_skipped","judge_failed","search_failed","write_failed","format_failed","publisher_failed"} or row["event_type"]=="stage_attempt" or row["error_code"] or any(s in str(row["status"] or "").casefold() for s in ("fail","error")) or any(s in str(row["result"] or "").casefold() for s in ("fail","error")):
                        continue
                    if row["item_status"] not in {"discarded","duplicate","auto_published","published","notified","succeeded","success","completed","finished","skipped","sent"}:
                        continue
                    if row["evidence_ref"] and conn.execute("SELECT 1 FROM system_information_evidence_pins WHERE evidence_ref=%s LIMIT 1",(row["evidence_ref"],)).fetchone():
                        continue
                    latest=conn.execute("SELECT event_id FROM system_information_events WHERE item_id=%s ORDER BY COALESCE(happened_at_ms,observed_at_ms) DESC,observed_at_ms DESC,event_id DESC LIMIT 1",(row["item_id"],)).fetchone()
                    if latest and latest["event_id"]==row["event_id"]: continue
                    conn.execute("DELETE FROM system_information_events WHERE event_id=%s",(row["event_id"],)); removed+=1
                next_cursor=str(rows[-1]["event_id"]) if rows else ""
                conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('status_retention_cursor',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",(next_cursor,now))
        return {"scanned":scanned,"removed":removed}

    def trim_task_evidence(self, *, source_database: Path | None = None, now_ms: int | None = None, limit: int = 100) -> dict[str,int]:
        now=now_ms or _now_ms();cursor=self.get_state("task_evidence_retention_cursor");changed=0
        terminal={"discarded","duplicate","auto_published","published","notified","ready_review","event_tracking_cancelled","expired","legacy_skipped","judge_failed","search_failed","write_failed","format_failed","publisher_failed"}
        with self.connect() as conn:
            with conn.transaction():
                rows=conn.execute("SELECT item_id,source_table,source_row_id,source,evidence_ref,updated_at_ms,created_at_ms,status FROM system_information_items WHERE item_id>%s AND source_table IN ('tasks','newsflash_items') AND evidence_ref IS NOT NULL ORDER BY item_id LIMIT %s FOR UPDATE SKIP LOCKED",(cursor,max(1,min(limit,500)))).fetchall()
                for row in rows:
                    stamp=row["updated_at_ms"] or row["created_at_ms"]
                    if row["source_table"]=="tasks" and (row["status"] not in terminal or stamp is None or stamp>=now-5*86_400_000):continue
                    if row["source_table"]=="newsflash_items" and (stamp is None or stamp>=now-30*86_400_000 or row["source"]=="odaily"):continue
                    if conn.execute("SELECT 1 FROM system_information_evidence_pins WHERE evidence_ref=%s LIMIT 1",(row["evidence_ref"],)).fetchone():continue
                    evidence=self._load_evidence_in_connection(conn,str(row["evidence_ref"]))
                    if not isinstance(evidence,dict):continue
                    source_row_id=str(row["source_row_id"])
                    if row["source_table"]=="tasks":
                        source=conn.execute("SELECT content,raw_payload FROM main.tasks WHERE id=%s",(int(source_row_id) if source_row_id.isdigit() else source_row_id,)).fetchone()
                        if source is None:continue
                        raw=source["raw_payload"]
                        if not (not source["content"] and raw in (None, "", "{}", {})):continue
                        task=dict(evidence.get("task") or {});pipeline=dict(evidence.get("pipeline") or {})
                        for key in ("pipeline_search_result","pipeline_judge_output","pipeline_publisher_output","pipeline_push_result"):task.pop(key,None)
                        for key in ("search_result","judge_output","publisher_output","push_result"):pipeline.pop(key,None)
                        if stamp<now-30*86_400_000:
                            for key in ("content","raw_payload","metadata"):task.pop(key,None)
                        evidence={**evidence,"task":task,"pipeline":pipeline}
                    else:
                        source=conn.execute("SELECT content,raw_payload FROM main.newsflash_items WHERE id=%s",(source_row_id,)).fetchone()
                        if source is None or not (not source["content"] and source["raw_payload"] in (None, "", "{}", {})):continue
                        row_payload=evidence.get("row")
                        if not isinstance(row_payload,dict):continue
                        row_payload=dict(row_payload)
                        for key in ("content","raw_payload","metadata"):row_payload.pop(key,None)
                        evidence={**evidence,"row":row_payload}
                    new_ref=self._put_evidence(conn,evidence)
                    conn.execute("UPDATE system_information_items SET evidence_ref=%s WHERE item_id=%s AND evidence_ref=%s",(new_ref,row["item_id"],row["evidence_ref"]))
                    conn.execute("UPDATE system_information_events SET evidence_ref=%s WHERE item_id=%s AND evidence_ref=%s AND event_type IN ('legacy_snapshot','observed_change','artifact_update')",(new_ref,row["item_id"],row["evidence_ref"]))
                    self._refresh_item_search(conn,str(row["item_id"]),evidence)
                    changed+=1
                next_cursor=str(rows[-1]["item_id"]) if rows else ""
                conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('task_evidence_retention_cursor',%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms",(next_cursor,now))
        return {"scanned":len(rows),"changed":changed}
