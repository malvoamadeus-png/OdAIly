from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime

import pytest


@pytest.fixture
def postgres_settings(monkeypatch):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL integration checks")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", dsn)
    from packages.common.pg_runtime import PostgresSettings

    return PostgresSettings(
        dsn=dsn, connect_timeout_seconds=5, statement_timeout_ms=10_000, lock_timeout_ms=2_000,
        idle_transaction_timeout_ms=10_000, migration_statement_timeout_ms=60_000,
        migration_lock_timeout_ms=5_000, migration_idle_transaction_timeout_ms=30_000,
        pool_max_size=2, pool_checkout_timeout_seconds=5, pool_max_waiting=16,
        application_name="odaily-hottopic-import-test",
    )


@pytest.fixture
def hottopic_schema(postgres_settings):
    from psycopg.sql import Identifier
    from packages.common.pg_native import connect_postgres

    schema = "test_" + uuid.uuid4().hex[:16]
    with connect_postgres(schema="main", application_name="odaily-hottopic-import-test", settings=postgres_settings) as conn:
        conn.execute("CREATE SCHEMA " + Identifier(schema).as_string(conn))
    try:
        yield schema
    finally:
        with connect_postgres(schema="main", application_name="odaily-hottopic-import-cleanup", settings=postgres_settings) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component='hottopic'", (schema,))
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")


def test_nonempty_hottopic_import_preserves_json_timestamps_and_foreign_keys(postgres_settings, hottopic_schema, tmp_path):
    from packages.common.native_sqlite_import import SourceMapping, TableMapping, import_native_sqlite_source
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_schema import initialize_hottopic_schema

    initialize_hottopic_schema(schema=hottopic_schema, settings=postgres_settings)
    source_path = tmp_path / "hottopic.sqlite"
    published = datetime(2026, 10, 5, 12, 34, 56, tzinfo=UTC)
    with sqlite3.connect(source_path) as source:
        source.execute("PRAGMA foreign_keys=ON")
        source.executescript("""
            CREATE TABLE topics (
                topic_id TEXT PRIMARY KEY, working_title TEXT NOT NULL, canonical_subject TEXT NOT NULL,
                core_entities_json TEXT NOT NULL, event_or_issue TEXT NOT NULL,
                started_at TEXT NOT NULL, first_seen_at TEXT NOT NULL, seed_expires_at TEXT NOT NULL,
                matching_status TEXT NOT NULL, visibility TEXT NOT NULL
            );
            CREATE TABLE threads (
                thread_id TEXT PRIMARY KEY, topic_id TEXT NOT NULL REFERENCES topics(topic_id),
                working_label TEXT NOT NULL, current_thesis TEXT NOT NULL, relation_to_topic TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
            );
        """)
        source.execute(
            "INSERT INTO topics VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("topic-import-1", "Rates outlook", "central bank policy", json.dumps(["rates", "inflation"]),
             "policy decision", published.isoformat(), published.isoformat(), published.isoformat(), "active", "visible"),
        )
        source.execute(
            "INSERT INTO threads VALUES(?,?,?,?,?,?,?)",
            ("thread-import-1", "topic-import-1", "Rates", "Policy path", "support", published.isoformat(), published.isoformat()),
        )
    mapping = SourceMapping("hottopic", hottopic_schema, (
        TableMapping("topics", hottopic_schema, "topics"),
        TableMapping("threads", hottopic_schema, "threads"),
    ))

    result = import_native_sqlite_source(source_key="hottopic", source_path=source_path, batch_size=1, source_mapping=mapping)

    assert result["table_count"] == 2
    assert {audit["child_table"] for audit in result["foreign_key_audits"]} == {"threads"}
    assert result["foreign_key_audits"][0]["orphan_rows"] == 0
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-import-verify", settings=postgres_settings) as conn:
        topic = conn.execute("SELECT core_entities_json,started_at FROM topics WHERE topic_id=%s", ("topic-import-1",)).fetchone()
        thread = conn.execute("SELECT topic_id,first_seen_at FROM threads WHERE thread_id=%s", ("thread-import-1",)).fetchone()
        assert topic["core_entities_json"] == ["rates", "inflation"]
        assert topic["started_at"] == published
        assert thread["topic_id"] == "topic-import-1"
        assert thread["first_seen_at"] == published

    rerun = import_native_sqlite_source(source_key="hottopic", source_path=source_path, batch_size=1, source_mapping=mapping)
    assert [table["imported_rows"] for table in rerun["tables"]] == [1, 1]
