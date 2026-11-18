from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import psycopg
from psycopg import sql
import pytest

from packages.non_mainstream_media.models import ParsedArticle, SiteDefinition
from packages.non_mainstream_media.repository import PostgresNonMainstreamMediaRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_non_mainstream_media_source_and_task_round_trip(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_nms_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    try:
        PostgresXProcessingRepository().init_schema()
        repository = PostgresNonMainstreamMediaRepository()
        repository.init_schema()
        repository.sync_sources([SiteDefinition(
            site_key="synthetic-source",
            display_name="Synthetic Source",
            homepage_url="https://example.test",
            list_url="https://example.test/news",
            capture_method="html_request",
            pipeline_mode="write_flow",
        )])
        source = repository.list_sources()[0]
        assert repository.get_settings().global_interval_seconds == 60
        assert repository.mark_seen(source, "seed-1", seeded=True)
        assert not repository.mark_seen(source, "seed-1", seeded=False)
        assert repository.unseen_source_item_ids("synthetic-source", ["seed-1", "new-1"]) == {"new-1"}

        article = ParsedArticle(
            source_item_id="article-1",
            canonical_url="https://example.test/article-1",
            title="Synthetic article",
            content="Synthetic article body",
            published_at=datetime.now(UTC),
            raw_payload={"marker": "nms-test"},
            metadata={"fixture": True},
        )
        task_id = repository.save_task(source, article)
        assert task_id is not None
        assert repository.save_task(source, article) == task_id
        with repository._connect(autocommit=True) as connection:
            task = connection.execute("SELECT source,source_item_id,metadata FROM tasks WHERE id=%s", (task_id,)).fetchone()
            outbox_count = connection.execute(
                "SELECT count(*) AS n FROM odaily_source_state_outbox "
                "WHERE source_table='non_mainstream_media_sources'"
            ).fetchone()["n"]
        assert task["source"] == "non_mainstream_media"
        assert task["source_item_id"] == "https://example.test/article-1"
        assert task["metadata"]["site_key"] == "synthetic-source"
        assert outbox_count >= 1
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
