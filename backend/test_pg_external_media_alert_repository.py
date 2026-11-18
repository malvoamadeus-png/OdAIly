from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg import sql

from packages.external_media_alert.models import MediaNewsflashItem
from packages.external_media_alert.repository import PostgresExternalMediaAlertRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_external_media_alert_repository_round_trip(monkeypatch):
    admin_dsn = os.environ["ODAILY_TEST_POSTGRES_DSN"]
    database_name = f"odaily_media_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresExternalMediaAlertRepository()
        repository.init_schema()
        item = MediaNewsflashItem(
            source="synthetic-media",
            title="Synthetic media headline",
            content="Synthetic media content",
            source_url="https://example.test/news/1",
            published_at=datetime.now(UTC),
            raw_payload={"marker": "external-media-test"},
            metadata={"fixture": True},
        )
        assert repository.save_media_newsflash_items([item]) == (1, 0)
        assert repository.save_media_newsflash_items([item]) == (0, 1)
        task = repository.claim_task("domain_judge", worker_id="media-test")
        assert task is not None
        assert task.source == "mainstream_media"
        assert task.metadata["fixture"] is True
        repository.complete_domain(
            task.id,
            route="crypto",
            prompt=None,
            model="test-model",
            raw_output='{"route":"crypto"}',
            categories=("exchange",),
        )
        assert repository.get_task(task.id).status == "classified"
        pipeline = repository.get_pipeline(task.id)
        assert pipeline.domain_route == "crypto"
        assert pipeline.domain_output == {"route": "crypto", "raw_output": '{"route":"crypto"}', "categories": ["exchange"]}

        stale = repository.claim_task("search", worker_id="stale-search", lock_seconds=1)
        assert stale is not None
        with repository._connect() as connection:
            connection.execute("UPDATE tasks SET locked_until=now()-interval '1 second' WHERE id=%s", (stale.id,))
        current = repository.claim_task("search", worker_id="current-search")
        assert current is not None and current.id == stale.id
        assert current.claim_token and current.claim_token != stale.claim_token
        repository.set_claim_context(stale)
        with pytest.raises(RuntimeError, match="claim is stale"):
            repository.complete_search_ready(stale.id, result={"stale": True})
        assert repository.get_task(current.id).status == "deduping"
        repository.set_claim_context(current)
        repository.complete_search_ready(current.id, result={"is_duplicate": False})
        assert repository.get_task(current.id).status == "deduped"
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
