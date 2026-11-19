from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql

from packages.business_news.postgres_repository import PostgresBusinessNewsRepository


def _update(update_id: int, message_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1793275200,
            "chat": {"id": -100777, "type": "supergroup", "title": "Duplicate Test"},
            "from": {"id": 77, "first_name": "Tester"},
            "text": "这是一条长度足够的合成商务消息用于测试重投幂等处理",
        },
    }


def test_postgres_business_news_ignores_message_unique_redelivery(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_business_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    try:
        repository = PostgresBusinessNewsRepository()
        first = repository.ingest_update(
            _update(100, 55), source_urls=[], own_bot_id="999", bot_id="business-test"
        )
        assert first is not None
        repository.save_group(chat_id=first["chat_id"], customer_name="Test", enabled=True)
        repository.ingest_update(
            _update(101, 56), source_urls=[], own_bot_id="999", bot_id="business-test"
        )
        before = repository.list_dashboard({"chat_id": first["chat_id"]})["total"]
        # Telegram can replay a message under a different update_id. The cursor
        # must advance, while the (chat_id, message_id) unique key stays singular.
        repository.ingest_update(
            _update(102, 56), source_urls=[], own_bot_id="999", bot_id="business-test"
        )
        after = repository.list_dashboard({"chat_id": first["chat_id"]})["total"]
        assert before == after == 1
        assert repository.last_update_id("business-test") == 102
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_business_news_binds_scalar_plugin_notice_as_jsonb(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_business_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    try:
        repository = PostgresBusinessNewsRepository()
        item = repository.ingest_update(_update(201, 88), source_urls=[], own_bot_id="999", bot_id="business-test")
        assert item is not None
        repository.save_group(chat_id=item["chat_id"], customer_name="Test", enabled=True)
        repository.ingest_update(_update(202, 89), source_urls=[], own_bot_id="999", bot_id="business-test")
        message = repository.claim_pending(limit=1)[0]
        repository.update_message(int(message["id"]), plugin_notice_result="sent")
        with repository._connect() as connection:
            result = connection.execute(
                "SELECT plugin_notice_result, jsonb_typeof(plugin_notice_result) AS value_type "
                "FROM business_message_inbox WHERE id=%s", (message["id"],),
            ).fetchone()
        assert result["plugin_notice_result"] == "sent"
        assert result["value_type"] == "string"
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
