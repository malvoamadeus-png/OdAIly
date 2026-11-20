from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg import sql

from packages.maintenance.repository import PostgresMaintenanceRepository


def test_postgres_maintenance_dry_run_and_cleanup(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_maintenance_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        repository = PostgresMaintenanceRepository()
        repository.init_schema()
        with repository._connect() as connection:
            connection.execute(
                "INSERT INTO pipeline_alerts(alert_key,last_sent_at,last_message,created_at,updated_at) "
                "VALUES ('old-alert',now(),'old',now()-interval '10 days',now()-interval '10 days')"
            )
            connection.execute("CREATE TABLE main.x_task_pipeline(task_id bigint primary key,judge_output jsonb,search_result jsonb,writer_output jsonb,draft_title text,draft_content text,publisher_output jsonb,push_result jsonb,telegram_result jsonb,final_title text,final_content text,updated_at timestamptz)")
            connection.execute("CREATE TABLE main.tasks(id bigint primary key,status text,updated_at timestamptz,raw_payload jsonb,metadata jsonb,title text,content text)")
            connection.execute("CREATE TABLE main.newsflash_production_metadata(delivery_key text,odaily_id text)")
            connection.execute("CREATE TABLE main.auditor_checks(id bigint,source_item_id text,status text)")
            connection.execute("CREATE TABLE main.fact_checks(source_item_id text,status text)")
            connection.execute("CREATE TABLE main.search_event_candidates(primary_task_id bigint,status text,expires_at timestamptz)")
            connection.execute("CREATE TABLE main.editor_plugin_local_feedbacks(feed_item_id text,feed_kind text,feedback text,manual_rejection_status text)")
            connection.execute("INSERT INTO main.tasks VALUES(1,'duplicate',now()-interval '10 days','{\"raw\":1}'::jsonb,'{\"site_key\":\"x\",\"unused\":\"drop\"}'::jsonb,'Title','Body')")
            connection.execute("INSERT INTO main.tasks VALUES(2,'published',now()-interval '10 days','{\"raw\":2}'::jsonb,'{}','Protected title','Protected body')")
            connection.execute("INSERT INTO main.tasks VALUES(3,'ready_review',now()-interval '10 days','{\"raw\":3}'::jsonb,'{}','Old flagged title','Old flagged body')")
            connection.execute("INSERT INTO main.tasks VALUES(4,'published',now()-interval '10 days','{\"raw\":4}'::jsonb,'{}','Rejected title','Rejected body')")
            connection.execute("INSERT INTO main.newsflash_production_metadata VALUES('odaily-task-2','source-2'),('odaily-task-3','source-3'),('odaily-task-4','source-4')")
            connection.execute("INSERT INTO main.auditor_checks VALUES(40,'source-2','pending'),(42,'source-3','flagged'),(41,'source-4','flagged')")
            connection.execute("INSERT INTO main.editor_plugin_local_feedbacks VALUES('auditor:41','auditor_alert','reject','pending')")
            connection.execute("INSERT INTO main.x_task_pipeline VALUES(1,'{\"reason\":\"why\"}'::jsonb,'{\"hit\":\"source\"}'::jsonb,'{\"draft\":1}'::jsonb,'Draft','Draft body','{\"published\":1}'::jsonb,'{\"push\":1}'::jsonb,'{}','Final title','Final body',now()-interval '10 days')")
            connection.execute("INSERT INTO main.x_task_pipeline VALUES(2,'{\"reason\":\"protect\"}'::jsonb,'{}','{}','Protected draft','Body','{}','{}','{}','Protected final','Protected final body',now()-interval '10 days')")
            connection.execute("INSERT INTO main.x_task_pipeline VALUES(3,'{\"reason\":\"flagged-old\"}'::jsonb,'{}','{}','Old flagged draft','Body','{}','{}','{}','Old flagged final','Old flagged final body',now()-interval '10 days')")
            connection.execute("INSERT INTO main.x_task_pipeline VALUES(4,'{\"reason\":\"rejected\"}'::jsonb,'{}','{}','Rejected draft','Body','{}','{}','{}','Rejected final','Rejected final body',now()-interval '10 days')")
        dry = repository.cleanup(dry_run=True, retention_days=7)
        assert dry.deleted["pipeline_alerts"] == 1
        assert dry.cleared["tasks_payloads"] == 1
        assert dry.cleared["x_task_pipeline_outputs"] == 1
        with repository._connect(autocommit=True) as connection:
            assert connection.execute("SELECT count(*) AS n FROM pipeline_alerts").fetchone()["n"] == 1
        applied = repository.cleanup(dry_run=False, retention_days=7)
        assert applied.deleted["pipeline_alerts"] == 1
        with repository._connect(autocommit=True) as connection:
            assert connection.execute("SELECT count(*) AS n FROM pipeline_alerts").fetchone()["n"] == 0
            row = connection.execute("SELECT judge_output,search_result,draft_title,draft_content,final_title,final_content,publisher_output,push_result FROM x_task_pipeline WHERE task_id=1").fetchone()
            assert row["judge_output"] == {}
            assert row["search_result"] == {}
            assert row["draft_title"] is None and row["draft_content"] is None
            assert row["final_title"] == "Final title" and row["final_content"] == "Final body"
            assert row["publisher_output"] == {"published": 1} and row["push_result"] == {"push": 1}
            protected = connection.execute("SELECT raw_payload FROM tasks WHERE id=2").fetchone()
            assert protected["raw_payload"] == {"raw": 2}
            outbox_protected = connection.execute("SELECT raw_payload FROM tasks WHERE id=4").fetchone()
            assert outbox_protected["raw_payload"] == {"raw": 4}
            flagged_old = connection.execute("SELECT raw_payload FROM tasks WHERE id=3").fetchone()
            assert flagged_old["raw_payload"] == {"raw": 3}
            flagged_pipeline = connection.execute("SELECT judge_output,draft_title,draft_content FROM x_task_pipeline WHERE task_id=3").fetchone()
            assert flagged_pipeline["judge_output"] == {"reason": "flagged-old"}
            assert flagged_pipeline["draft_title"] == "Old flagged draft"
            assert flagged_pipeline["draft_content"] == "Body"
        assert applied.cleared["tasks_payloads"] == 1
        assert applied.cleared["x_task_pipeline_outputs"] == 1
        assert applied.has_more is False
        repeated = repository.cleanup(dry_run=False, retention_days=7)
        assert repeated.cleared["tasks_payloads"] == 0
        assert repeated.cleared["x_task_pipeline_outputs"] == 0
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_main_pipeline_retention_does_not_delete_other_module_data(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_main_retention_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    try:
        repository = PostgresMaintenanceRepository()
        repository.init_schema()
        with repository._connect() as connection:
            connection.execute("CREATE TABLE main.tasks(id bigint primary key,status text,updated_at timestamptz,raw_payload jsonb,metadata jsonb,title text,content text)")
            connection.execute("CREATE TABLE main.x_task_pipeline(task_id bigint primary key,judge_output jsonb,search_result jsonb,writer_output jsonb,draft_title text,draft_content text)")
            connection.execute("CREATE TABLE main.whale_watch_activities(id bigint primary key,created_at timestamptz)")
            connection.execute("CREATE TABLE main.whale_watch_hyperliquid_activities(id bigint primary key,created_at timestamptz)")
            connection.execute("CREATE TABLE main.editor_plugin_feedbacks(id bigint primary key,created_at timestamptz)")
            connection.execute("INSERT INTO main.tasks VALUES(1,'published',now()-interval '100 days','{\"payload\":1}','{\"site_key\":\"x\"}','Title','Body')")
            connection.execute("INSERT INTO main.x_task_pipeline VALUES(1,'{\"judge\":1}','{\"search\":1}','{\"writer\":1}','Draft','Draft body')")
            connection.execute("INSERT INTO main.whale_watch_activities VALUES(1,now()-interval '100 days')")
            connection.execute("INSERT INTO main.whale_watch_hyperliquid_activities VALUES(1,now()-interval '100 days')")
            connection.execute("INSERT INTO main.editor_plugin_feedbacks VALUES(1,now()-interval '100 days')")
            connection.execute("INSERT INTO main.pipeline_worker_heartbeats(component,worker_id,updated_at) VALUES('fixture','worker-1',now()-interval '100 days')")
            connection.execute("INSERT INTO main.pipeline_alerts(alert_key,last_sent_at,last_message,created_at,updated_at) VALUES('alert-1',now(),'old',now()-interval '100 days',now()-interval '100 days')")

        result = repository.cleanup_main_pipeline_fields()

        assert result.deleted == {}
        assert result.cleared["tasks_payloads"] == 1
        assert result.cleared["x_task_pipeline_outputs"] == 1
        with repository._connect(autocommit=True) as connection:
            task = connection.execute("SELECT raw_payload,title,content FROM main.tasks WHERE id=1").fetchone()
            assert task["raw_payload"] == {}
            assert task["title"] == "Title" and task["content"] == "Body"
            for table in (
                "whale_watch_activities", "whale_watch_hyperliquid_activities",
                "editor_plugin_feedbacks", "pipeline_worker_heartbeats", "pipeline_alerts",
            ):
                assert connection.execute(f"SELECT count(*) AS n FROM main.{table}").fetchone()["n"] == 1, table
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
