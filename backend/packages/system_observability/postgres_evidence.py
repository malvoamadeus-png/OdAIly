from __future__ import annotations

import gzip
import hashlib
import zlib
from dataclasses import dataclass

from psycopg.sql import Identifier

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.common.storage import PostgresSettings, load_postgres_settings


def _require_transaction(connection, operation: str) -> None:
    if connection.info.transaction_status.name != "INTRANS":
        raise RuntimeError(f"{operation} requires an active caller-owned PostgreSQL transaction")


class EvidenceIntegrityError(RuntimeError):
    pass


SMALL_EVIDENCE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS small_evidence (
    evidence_ref text PRIMARY KEY,
    compressed_payload bytea NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_small_evidence_created ON small_evidence(created_at, evidence_ref);
"""
SMALL_EVIDENCE_CHECKSUM = hashlib.sha256(SMALL_EVIDENCE_SCHEMA_SQL.encode("utf-8")).hexdigest()


def _install_small_evidence_schema(connection) -> None:
    for statement in SMALL_EVIDENCE_SCHEMA_SQL.split(";"):
        if statement.strip():
            connection.execute(statement)


@dataclass(frozen=True, slots=True)
class PostgresSmallEvidenceStore:
    schema: str = "system_observability"

    def initialize(self, *, settings: PostgresSettings | None = None) -> None:
        current = settings or load_postgres_settings()
        with connect_postgres(schema=self.schema, application_name="odaily-observability-evidence-migrate", settings=current) as connection:
            apply_schema_migrations(
                connection,
                schema=self.schema,
                component="system_observability_evidence",
                migrations=(PostgresMigration(1, "small evidence bytes", SMALL_EVIDENCE_CHECKSUM, _install_small_evidence_schema),),
                settings=current,
            )

    def put(self, evidence_ref: str, payload: bytes, *, uncompressed_size: int) -> bool:
        if len(evidence_ref) != 64 or any(character not in "0123456789abcdef" for character in evidence_ref):
            raise ValueError("evidence_ref must be a lowercase SHA-256 digest")
        try:
            uncompressed = gzip.decompress(payload)
        except (OSError, EOFError, zlib.error) as exc:
            raise EvidenceIntegrityError("evidence payload is not valid gzip data") from exc
        if len(uncompressed) != uncompressed_size or hashlib.sha256(uncompressed).hexdigest() != evidence_ref:
            raise EvidenceIntegrityError("evidence payload does not match its reference or declared size")
        with connect_postgres(schema=self.schema, application_name="odaily-observability-evidence-write") as connection:
            with connection.transaction():
                return self.put_in_transaction(connection, evidence_ref, payload, uncompressed_size=uncompressed_size)

    def put_in_transaction(self, connection, evidence_ref: str, payload: bytes, *, uncompressed_size: int) -> bool:
        """Store and lock a blob in the caller's transaction before adding references."""
        _require_transaction(connection, "put_in_transaction")
        uncompressed = self._validate_payload(evidence_ref, payload, uncompressed_size)
        inserted = connection.execute(
            "INSERT INTO small_evidence(evidence_ref,compressed_payload,byte_size) VALUES(%s,%s,%s) "
            "ON CONFLICT(evidence_ref) DO NOTHING RETURNING evidence_ref",
            (evidence_ref, payload, uncompressed_size),
        ).fetchone()
        if inserted:
            connection.execute("SELECT evidence_ref FROM small_evidence WHERE evidence_ref=%s FOR KEY SHARE", (evidence_ref,))
            return True
        existing = connection.execute(
            "SELECT compressed_payload,byte_size FROM small_evidence WHERE evidence_ref=%s FOR KEY SHARE",
            (evidence_ref,),
        ).fetchone()
        if existing is None or int(existing["byte_size"]) != uncompressed_size:
            raise EvidenceIntegrityError("stored evidence metadata conflicts with its reference")
        stored_payload = bytes(existing["compressed_payload"])
        try:
            stored_content = gzip.decompress(stored_payload)
        except (OSError, EOFError, zlib.error) as exc:
            raise EvidenceIntegrityError("stored evidence is not valid gzip data") from exc
        if hashlib.sha256(stored_content).hexdigest() != evidence_ref or stored_content != uncompressed:
            raise EvidenceIntegrityError("stored evidence content conflicts with its reference")
        return False

    @staticmethod
    def _validate_payload(evidence_ref: str, payload: bytes, uncompressed_size: int) -> bytes:
        if len(evidence_ref) != 64 or any(character not in "0123456789abcdef" for character in evidence_ref):
            raise ValueError("evidence_ref must be a lowercase SHA-256 digest")
        try:
            uncompressed = gzip.decompress(payload)
        except (OSError, EOFError, zlib.error) as exc:
            raise EvidenceIntegrityError("evidence payload is not valid gzip data") from exc
        if len(uncompressed) != uncompressed_size or hashlib.sha256(uncompressed).hexdigest() != evidence_ref:
            raise EvidenceIntegrityError("evidence payload does not match its reference or declared size")
        return uncompressed

    def lock_for_reference(self, connection, evidence_ref: str) -> None:
        """Serialize a new reference against evidence garbage collection."""
        _require_transaction(connection, "lock_for_reference")
        row = connection.execute(
            "SELECT evidence_ref FROM small_evidence WHERE evidence_ref=%s FOR KEY SHARE",
            (evidence_ref,),
        ).fetchone()
        if row is None:
            raise EvidenceIntegrityError("cannot reference missing small evidence")

    def get(self, evidence_ref: str) -> bytes | None:
        with connect_postgres(schema=self.schema, application_name="odaily-observability-evidence-read") as connection:
            row = connection.execute(
                "SELECT compressed_payload,byte_size FROM small_evidence WHERE evidence_ref=%s",
                (evidence_ref,),
            ).fetchone()
        if row is None:
            return None
        payload = bytes(row["compressed_payload"])
        try:
            content = gzip.decompress(payload)
        except (OSError, EOFError, zlib.error) as exc:
            raise EvidenceIntegrityError("stored evidence is not valid gzip data") from exc
        if len(content) != int(row["byte_size"]) or hashlib.sha256(content).hexdigest() != evidence_ref:
            raise EvidenceIntegrityError("stored evidence failed integrity verification")
        return payload

    def delete_if_unreferenced(self, evidence_ref: str) -> bool:
        with connect_postgres(schema=self.schema, application_name="odaily-observability-evidence-delete") as connection:
            with connection.transaction():
                locked = connection.execute(
                    "SELECT evidence_ref FROM small_evidence WHERE evidence_ref=%s FOR UPDATE",
                    (evidence_ref,),
                ).fetchone()
                if locked is None:
                    return False
                reference_tables = (
                    ("system_information_items", "evidence_ref"),
                    ("system_information_events", "evidence_ref"),
                    ("system_information_decision_samples", "evidence_ref"),
                    ("system_information_evidence_pins", "evidence_ref"),
                    ("system_review_analysis_attempts", "raw_response_ref"),
                    ("main.x_processing_attempts", "response_ref"),
                    ("main.x_processing_attempt_events", "response_ref"),
                )
                existing_tables = [
                    (table, column) for table, column in reference_tables
                    if connection.execute("SELECT to_regclass(%s) AS relation", (
                        f"{table}" if table.startswith("main.") else f"{self.schema}.{table}",
                    )).fetchone()["relation"]
                ]
                references = None
                if existing_tables:
                    union = " UNION ALL ".join(
                        f"SELECT 1 FROM {Identifier(*(table.split('.'))).as_string(connection)} WHERE {Identifier(column).as_string(connection)}=%s"
                        for table, column in existing_tables
                    )
                    references = connection.execute(
                        union + " LIMIT 1",
                        tuple(evidence_ref for _table, _column in existing_tables),
                    ).fetchone()
                if references:
                    return False
                return bool(connection.execute("DELETE FROM small_evidence WHERE evidence_ref=%s", (evidence_ref,)).rowcount)

    def delete_many_if_unreferenced(self, evidence_refs: list[str]) -> int:
        refs = list(dict.fromkeys(evidence_refs))
        if not refs:
            return 0
        with connect_postgres(schema=self.schema, application_name="odaily-observability-evidence-delete") as connection:
            with connection.transaction():
                return self.delete_many_in_transaction(connection, refs)

    def delete_many_in_transaction(self, connection, evidence_refs: list[str]) -> int:
        _require_transaction(connection, "delete_many_in_transaction")
        refs = list(dict.fromkeys(evidence_refs))
        if not refs:
            return 0
        reference_tables = (
            (f"{self.schema}.system_information_items", "evidence_ref"),
            (f"{self.schema}.system_information_events", "evidence_ref"),
            (f"{self.schema}.system_information_decision_samples", "evidence_ref"),
            (f"{self.schema}.system_information_evidence_pins", "evidence_ref"),
            (f"{self.schema}.system_review_analysis_attempts", "raw_response_ref"),
            ("main.x_processing_attempts", "response_ref"),
            ("main.x_processing_attempt_events", "response_ref"),
        )
        connection.execute("SELECT evidence_ref FROM small_evidence WHERE evidence_ref=ANY(%s) ORDER BY evidence_ref FOR UPDATE", (refs,)).fetchall()
        predicates = []
        for relation, column in reference_tables:
            if connection.execute("SELECT to_regclass(%s) AS relation", (relation,)).fetchone()["relation"]:
                schema, table = relation.split(".", 1)
                predicates.append(f"NOT EXISTS (SELECT 1 FROM {Identifier(schema, table).as_string(connection)} r WHERE r.{Identifier(column).as_string(connection)}=e.evidence_ref)")
        if not predicates:
            return 0
        result = connection.execute("DELETE FROM small_evidence e WHERE e.evidence_ref=ANY(%s) AND " + " AND ".join(predicates), (refs,))
        return result.rowcount or 0
