#!/usr/bin/env python3
"""Read-only verification of PostgreSQL observability evidence references."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import zlib
import sqlite3
from pathlib import Path

from psycopg import sql

from packages.common.pg_native import connect_postgres
from packages.common.storage import load_postgres_settings
from packages.common.native_sqlite_import import _configure_sqlite_temp_directory


REFERENCE_SOURCES = (
    ("system_information_items", "evidence_ref"),
    ("system_information_events", "evidence_ref"),
    ("system_information_decision_samples", "evidence_ref"),
    ("system_information_evidence_pins", "evidence_ref"),
    ("system_review_analysis_attempts", "raw_response_ref"),
)


def verify(schema: str = "system_observability") -> dict:
    missing_relations: list[str] = []
    with connect_postgres(schema=schema, application_name="odaily-evidence-closure-check") as conn:
        source_queries = []
        for table, column in REFERENCE_SOURCES:
            exists = conn.execute("SELECT to_regclass(%s) AS relation", (f"{schema}.{table}",)).fetchone()["relation"]
            if not exists:
                missing_relations.append(table)
                continue
            source_queries.append(sql.SQL("SELECT {} AS evidence_ref FROM {} WHERE {} IS NOT NULL")
                                  .format(sql.Identifier(column), sql.Identifier(schema, table), sql.Identifier(column)))
        if missing_relations:
            return {"schema": schema, "complete": False, "missing_relations": missing_relations,
                    "failure_count": 0, "failures": []}
        references = sql.SQL(" UNION ").join(source_queries)
        failures = []
        source_kinds = {"small_evidence": 0, "external_gzip": 0}
        reference_count = 0
        verified = 0
        settings = load_postgres_settings()
        with conn.transaction():
            conn.execute("SELECT pg_catalog.set_config('statement_timeout', %s, true), pg_catalog.set_config('lock_timeout', %s, true)",
                         (f"{settings.migration_statement_timeout_ms}ms", f"{settings.migration_lock_timeout_ms}ms"))
            with conn.cursor(name="evidence_reference_scan") as cursor:
                cursor.execute(references)
                while rows := cursor.fetchmany(500):
                    for reference in rows:
                        ref = str(reference["evidence_ref"])
                        reference_count += 1
                        row = conn.execute(sql.SQL("SELECT path,byte_size FROM {} WHERE evidence_ref=%s")
                                           .format(sql.Identifier(schema, "system_information_evidence")), (ref,)).fetchone()
                        if row is None:
                            failures.append({"evidence_ref": ref, "reason": "missing metadata row"})
                            continue
                        path = str(row["path"] or "")
                        try:
                            if path.startswith("postgres://") or "/_sqlite/" in path.replace("\\", "/"):
                                blob = conn.execute(sql.SQL("SELECT compressed_payload,byte_size FROM {} WHERE evidence_ref=%s")
                                                    .format(sql.Identifier(schema, "small_evidence")), (ref,)).fetchone()
                                if blob is None:
                                    failures.append({"evidence_ref": ref, "reason": "missing small_evidence blob"})
                                    continue
                                compressed = bytes(blob["compressed_payload"])
                                content = gzip.decompress(compressed)
                                if len(content) != int(blob["byte_size"]) or hashlib.sha256(content).hexdigest() != ref:
                                    failures.append({"evidence_ref": ref, "reason": "small_evidence hash/size mismatch"})
                                    continue
                                source_kinds["small_evidence"] += 1
                            else:
                                compressed = Path(path).read_bytes()
                                content = gzip.decompress(compressed)
                                if len(content) != int(row["byte_size"]) or hashlib.sha256(content).hexdigest() != ref:
                                    failures.append({"evidence_ref": ref, "reason": "external gzip hash/size mismatch", "path": path})
                                    continue
                                source_kinds["external_gzip"] += 1
                            verified += 1
                        except (OSError, EOFError, gzip.BadGzipFile, zlib.error) as exc:
                            failures.append({"evidence_ref": ref, "reason": f"{type(exc).__name__}: {exc}", "path": path})

    return {
        "schema": schema,
        "reference_count": reference_count,
        "verified": verified,
        "source_kinds": source_kinds,
        "missing_relations": missing_relations,
        "failure_count": len(failures),
        "failures": failures[:200],
        "failures_truncated": len(failures) > 200,
        "complete": not missing_relations and not failures,
    }


def compare_frozen_sqlite_refs(source_path: Path, schema: str = "system_observability") -> dict:
    """Compare the complete unique evidence-ref sets using ordered streaming scans."""
    _configure_sqlite_temp_directory()
    source = sqlite3.connect(f"file:{source_path.expanduser().resolve()}?mode=ro", uri=True)
    source_queries = (
        ("system_information_items", "evidence_ref"),
        ("system_information_events", "evidence_ref"),
        ("system_information_decision_samples", "evidence_ref"),
        ("system_information_evidence_pins", "evidence_ref"),
        ("system_review_analysis_attempts", "raw_response_ref"),
    )
    try:
        existing = {str(row[0]) for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        clauses = [f'SELECT "{column}" AS ref FROM "{table}" WHERE "{column}" IS NOT NULL'
                   for table, column in source_queries if table in existing]
        missing_tables = [table for table, _ in source_queries if table not in existing]
        source_cursor = source.execute("SELECT DISTINCT ref FROM (" + " UNION ALL ".join(clauses) + ") ORDER BY ref")
        with connect_postgres(schema=schema, application_name="odaily-evidence-ref-set-compare") as conn:
            pg_queries = [sql.SQL("SELECT {} AS ref FROM {} WHERE {} IS NOT NULL").format(
                sql.Identifier(column), sql.Identifier(schema, table), sql.Identifier(column))
                for table, column in REFERENCE_SOURCES]
            settings = load_postgres_settings()
            with conn.transaction():
                conn.execute("SELECT pg_catalog.set_config('statement_timeout', %s, true), pg_catalog.set_config('lock_timeout', %s, true)",
                             (f"{settings.migration_statement_timeout_ms}ms", f"{settings.migration_lock_timeout_ms}ms"))
                with conn.cursor(name="evidence_ref_set_compare") as cursor:
                    cursor.execute(sql.SQL("SELECT DISTINCT ref FROM ({}) refs ORDER BY ref")
                                   .format(sql.SQL(" UNION ALL ").join(pg_queries)))
                    source_value = source_cursor.fetchone()
                    pg_value = cursor.fetchone()
                    missing_in_pg = []
                    missing_in_source = []
                    source_count = pg_count = 0
                    while source_value is not None or pg_value is not None:
                        left = str(source_value[0]) if source_value is not None else None
                        right = str(pg_value["ref"]) if pg_value is not None else None
                        if right is None or (left is not None and left < right):
                            source_count += 1
                            missing_in_pg.append(left)
                            source_value = source_cursor.fetchone()
                        elif left is None or right < left:
                            pg_count += 1
                            missing_in_source.append(right)
                            pg_value = cursor.fetchone()
                        else:
                            source_count += 1
                            pg_count += 1
                            source_value = source_cursor.fetchone()
                            pg_value = cursor.fetchone()
        return {"source_path": str(source_path.expanduser().resolve()), "schema": schema,
                "source_ref_count": source_count, "postgres_ref_count": pg_count,
                "missing_in_postgres_count": len(missing_in_pg), "missing_in_source_count": len(missing_in_source),
                "missing_tables": missing_tables, "missing_in_postgres": missing_in_pg[:200],
                "missing_in_postgres_truncated": len(missing_in_pg) > 200,
                "missing_in_source": missing_in_source[:200], "missing_in_source_truncated": len(missing_in_source) > 200,
                "complete": not missing_tables and not missing_in_pg and not missing_in_source}
    finally:
        source.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="system_observability")
    parser.add_argument("--sqlite-source", type=Path,
                        help="also compare every unique ref against the frozen system_information SQLite source")
    args = parser.parse_args(argv)
    try:
        report = verify(args.schema)
        if args.sqlite_source:
            report["source_set_comparison"] = compare_frozen_sqlite_refs(args.sqlite_source, args.schema)
            report["complete"] = report["complete"] and report["source_set_comparison"]["complete"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["complete"] else 1
    except Exception as exc:
        print(json.dumps({"complete": False, "error_type": type(exc).__name__, "error": str(exc)},
                         ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
