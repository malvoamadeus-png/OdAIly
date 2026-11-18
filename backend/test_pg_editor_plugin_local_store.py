from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.editor_plugin_postgres_local_store import PostgresEditorPluginLocalStore


def test_editor_plugin_local_store_roundtrip_without_seed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_editor_local_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        PostgresEditorPluginLocalStore.initialize_schema()
        store = PostgresEditorPluginLocalStore(initialize=False)
        now = datetime.now(UTC)
        store.upsert_feed_items([{
            "feed_item_id": "feed-1", "feed_kind": "newsflash", "lane": "high", "title": "Title",
            "summary": "Summary", "occurred_at": now, "meta_json": {"odaily_delivery_key": "delivery-1"},
        }])
        store.upsert_session(token_hash="session-1", user_id="user-1", email="USER@example.test",
                             display_name="User", expires_at=now + timedelta(hours=1))
        session = store.get_session("session-1")
        assert session is not None and session.email == "user@example.test"
        assert store.get_session("missing") is None
        store.mark_seen(feed_item_id="feed-1", feed_kind="newsflash", read_method="alt_lens",
                        actor_user_id="user-1", actor_email="user@example.test", actor_display_name="User")
        store.record_feedback(feed_item_id="feed-1", feed_kind="newsflash", feedback="accept",
                              actor_user_id="user-1", actor_email="user@example.test", actor_display_name="User",
                              session_id="session-1", extra_json={"source": "fixture"})
        state = store.feed_state(actor_email="user@example.test", feed_item_ids=["feed-1"])
        assert state[0]["latest_feedback"] == "accept"
        assert state[0]["is_read"] is True
        assert store.readers_for_delivery_keys(["delivery-1"])["delivery-1"][0]["seen_count"] == 1
        pending = store.list_pending_feedbacks()
        assert len(pending) == 1 and pending[0].extra_json == {"source": "fixture"}
        store.mark_feedback_failed(pending[0].id, error="retry")
        failed = store.list_pending_feedbacks()
        assert failed[0].id == pending[0].id
        store.mark_feedback_synced(failed[0].id)
        assert store.stats()["feedbacks"]["pending"] == 0
        assert store.list_feed_items(limit=10, max_age_hours=2)[0]["badges"] == []
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
