from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from packages.auditor.repository import PostgresAuditorRepository


def test_postgres_auditor_initializes_without_x_processing(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")

    database_name = f"odaily_audit_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    try:
        repository = PostgresAuditorRepository()
        repository.init_schema()
        with repository._connect() as connection:
            connection.execute(
                """
                INSERT INTO odaily_reference_items (
                    source_item_id, source_url, title, content, published_at, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    "audit-test-item",
                    "https://www.odaily.news/newsflash/audit-test-item",
                    "Synthetic title",
                    "Synthetic content",
                    datetime.now(UTC),
                    repository._Jsonb({"fixture": "test"}),
                ),
            )

        task = repository.claim_task(
            worker_id="auditor-test",
            prompt_version="test-v1",
            lookback_minutes=30,
        )
        assert task is not None
        assert task.source_item_id == "audit-test-item"
        assert task.metadata == {"fixture": "test"}
        assert task.content_hash

        stale_task = task
        with repository._connect() as connection:
            connection.execute(
                "UPDATE auditor_checks SET locked_until=now()-interval '1 second' WHERE id=%s",
                (task.id,),
            )
        task = repository.claim_task(
            worker_id="auditor-test-reclaimed",
            prompt_version="test-v1",
            lookback_minutes=30,
        )
        assert task is not None and task.claim_token != stale_task.claim_token
        repository.complete_failed(stale_task, error="stale worker")
        with repository._connect() as connection:
            row = connection.execute("SELECT status, claim_token FROM auditor_checks WHERE id=%s", (task.id,)).fetchone()
        assert row == {"status": "processing", "claim_token": task.claim_token}

        repository.complete_flagged(
            task,
            model="test-model",
            prompt_version="test-v1",
            raw_output='{"has_issue": true}',
            result={"has_issue": True, "issues": [{"type": "typo"}]},
            telegram_text="Synthetic alert",
            telegram_result={"ok": False, "error": "test delivery failure"},
        )
        with repository._connect() as connection:
            row = connection.execute(
                "SELECT status, audit_result, telegram_result, last_error, locked_by "
                "FROM auditor_checks WHERE id=%s",
                (task.id,),
            ).fetchone()
        assert row == {
            "status": "flagged",
            "audit_result": {"has_issue": True, "issues": [{"type": "typo"}]},
            "telegram_result": {"ok": False, "error": "test delivery failure"},
            "last_error": "test delivery failure",
            "locked_by": None,
        }

        repository.record_worker_heartbeat(
            component="auditor",
            worker_id="auditor-test",
            status="running",
            success=True,
            metadata={"fixture": "test"},
        )
        with repository._connect() as connection:
            heartbeat = connection.execute(
                "SELECT status, metadata FROM pipeline_worker_heartbeats "
                "WHERE component='auditor' AND worker_id='auditor-test'"
            ).fetchone()
        assert heartbeat == {"status": "running", "metadata": {"fixture": "test"}}
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",
                (database_name,),
            )
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
