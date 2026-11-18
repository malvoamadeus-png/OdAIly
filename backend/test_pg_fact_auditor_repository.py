from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from packages.common.source_exclusions import create_source_exclusion_repository, is_source_excluded
from packages.editor_plugin_postgres_feed import PostgresFactAuditorFeedWriter, PostgresEditorPluginFeedStore
from packages.fact_auditor.postgres_repository import PostgresFactReviewRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_fact_auditor_claim_save_retry_and_exclusions(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_fact_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresFactReviewRepository()
        repository.init_schema()
        now = datetime.now(UTC)
        with repository._connect() as connection:
            connection.execute(
                """INSERT INTO odaily_reference_items(source_item_id,source_url,title,content,published_at,raw_payload,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s)""",
                ("fact-source-1", "https://example.test/story", "Synthetic headline", "Synthetic article body",
                 now, repository._Jsonb({"sourceUrl": "https://example.test/original"}), repository._Jsonb({"fixture": True})),
            )
        task = repository.claim(worker_id="fact-test", lookback_minutes=30, max_attempts=3)
        assert task is not None
        assert task["source_item_id"] == "fact-source-1"
        assert task["attempts"] == 1
        repository.save(task, {"status": "flagged", "summary": "Synthetic discrepancy", "issues": [{"kind": "added_fact"}]}, worker_id="fact-test")
        detail = repository.detail("fact-source-1")
        assert detail["is_current"]
        assert detail["check"]["result"]["issues"][0]["kind"] == "added_fact"
        assert len(detail["runs"]) == 1
        assert repository.retry(task["id"])["status"] == "pending"

        PostgresFactAuditorFeedWriter().upsert_fact_auditor_alert(
            check_id=task["id"], source_item_id="fact-source-1", source_url="https://example.test/story",
            title="Synthetic headline", summary="Synthetic fact alert", audit_result={"status": "flagged"},
        )
        with PostgresEditorPluginFeedStore()._connect(autocommit=True) as connection:
            feed = connection.execute(
                "SELECT feed_item_id,feed_kind,meta_json FROM editor_plugin_local_feed_items WHERE feed_item_id=%s",
                (f"fact_auditor:{task['id']}",),
            ).fetchone()
        assert feed["feed_kind"] == "auditor_alert"
        assert feed["meta_json"]["fact_status"] == "flagged"

        exclusions = create_source_exclusion_repository()
        groups = exclusions.list_enabled_rule_groups()
        assert groups
        assert any("competitor" in group.scopes for group in groups)
        assert not is_source_excluded(groups, scopes=["fact_auditor"], title_texts=["unmatched synthetic text"])
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
