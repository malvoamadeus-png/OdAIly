from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql

from packages.console_data_api import ConsoleDataApi
from packages.binance_square.postgres_repository import PostgresBinanceSquareRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def test_postgres_console_data_filters_mutates_and_hydrates_pipeline(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_console_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    try:
        processing = PostgresXProcessingRepository()
        processing.init_schema()
        PostgresBinanceSquareRepository()
        api = ConsoleDataApi()
        suffix = uuid.uuid4().hex
        with processing._connect() as connection:
            task = connection.execute(
                """INSERT INTO tasks(source,source_item_id,title,content,status,metadata)
                   VALUES('x',%s,'console title','console body','pending',%s) RETURNING id""",
                (suffix, '{"console":true}'),
            ).fetchone()
            task_id = int(task["id"])
            candidate = connection.execute(
                """INSERT INTO search_event_candidates(primary_task_id,status,title,content,content_hash,metadata,expires_at)
                   VALUES(%s,'active','candidate title','body','hash-'||%s,'{}',now()+interval '1 hour') RETURNING id""",
                (task_id, suffix),
            ).fetchone()
            connection.execute(
                """INSERT INTO search_event_sources(candidate_id,task_id,source,source_item_id,title,content,role,metadata)
                   VALUES(%s,%s,'x',%s,'console title','console body','primary','{}')""",
                (int(candidate["id"]), task_id, suffix),
            )
            connection.execute(
                "INSERT INTO x_task_pipeline(task_id,search_result) VALUES(%s,%s)",
                (task_id, '{"is_duplicate":true,"duplicate_target_type":"inflight_candidate","duplicate_target_id":"' + str(candidate["id"]) + '"}'),
            )
        rows = api.execute({
            "table": "tasks", "operation": "select",
            "select": "id,source,source_item_id,metadata,x_task_pipeline(search_result)",
            "filters": [{"column": "id", "op": "eq", "value": task_id}],
            "orders": [{"column": "created_at", "ascending": False}], "limit": 5,
        })
        assert rows[0]["metadata"] == {"console": True}
        assert rows[0]["x_task_pipeline"]["search_result"]["duplicate_target"]["title"] == "candidate title"
        assert api.execute({"table": "tasks", "filters": [{"column": "id", "op": "in", "value": []}]}) == []
        saved = api.execute({
            "table": "binance_square_accounts", "operation": "upsert", "on_conflict": "slug_lower",
            "data": {"profile_url": "https://www.binance.com/en/square/profile/pgconsole", "enabled": True},
        })
        assert saved[0]["slug"] == "pgconsole"
        changed = api.execute({
            "table": "binance_square_accounts", "operation": "update",
            "filters": [{"column": "slug_lower", "op": "eq", "value": "pgconsole"}],
            "data": {"write_name": "Console Test"},
        })
        assert changed[0]["write_name"] == "Console Test"
        deleted = api.execute({
            "table": "binance_square_accounts", "operation": "delete",
            "filters": [{"column": "slug_lower", "op": "eq", "value": "pgconsole"}],
        })
        assert deleted[0]["slug"] == "pgconsole"
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
