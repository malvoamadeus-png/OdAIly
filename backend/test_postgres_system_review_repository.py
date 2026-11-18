from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def postgres_review_repository(monkeypatch, tmp_path):
    admin_dsn=os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn: pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL system review checks")
    database_name="odaily_sr_"+uuid.uuid4().hex[:12]
    with psycopg.connect(admin_dsn,autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    dsn=psycopg.conninfo.make_conninfo(admin_dsn,dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN",dsn)
    schema="test_sr_"+uuid.uuid4().hex[:12]
    from packages.system_observability.postgres_reviews import PostgresSystemReviewRepository
    repo=PostgresSystemReviewRepository(tmp_path/"info.sqlite",schema=schema)
    repo.initialize()
    yield repo
    from packages.common.pg_runtime import close_postgres_pools
    close_postgres_pools()
    with psycopg.connect(admin_dsn,autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s",(database_name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_system_review_issue_decision_and_attempt_evidence(postgres_review_repository):
    repo=postgres_review_repository
    key=repo.upsert_issue({"task_kind":"fixture","category":"quality","title":"Fixture review","identity":{"id":"fixture"},"scope":{"source":"test"},"evidence":[{"summary":"source sample"}]})
    attempt=repo.start_analysis_attempt(key)
    raw="raw model response "+("x"*5000)
    reference=repo.record_analysis_response(attempt,raw,"req-1",120)
    with repo.connect() as conn:
        conn.execute("UPDATE system_information_evidence SET created_at_ms=0 WHERE evidence_ref=%s",(reference,))
    gc=repo.information_store.cleanup_expired_evidence(now_ms=2_000_000_000_000,retain_days=1,limit=20,dry_run=False)
    assert gc["protected"]==1
    repo.finish_analysis_attempt(attempt,status="completed")
    detail=repo.detail(key)
    assert detail["analysis_history"][0]["raw_response"]==raw
    assert repo.decide(key,"accepted","editor","verified")["decisions"][0]["decision"]=="accepted"
    assert reference


def test_native_observability_schema_initialization_does_not_seed_runtime_state(postgres_review_repository):
    import uuid

    from packages.common.pg_native import connect_postgres
    from packages.system_observability.postgres_store import PostgresSystemInformationStore

    schema = "test_native_" + uuid.uuid4().hex[:12]
    PostgresSystemInformationStore.initialize_native_schema(schema=schema)
    with connect_postgres(schema=schema, application_name="test-native-observability-schema") as conn:
        relations = {
            row["tablename"]
            for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=%s",
                (postgres_review_repository.schema,),
            ).fetchall()
        }
        state = conn.execute("SELECT count(*) AS n FROM system_information_state").fetchone()["n"]
    assert {"system_information_items", "system_review_items", "system_information_external_error_daily"} <= relations
    assert state == 0


def test_postgres_system_review_usage_reservation_and_channel_buckets(postgres_review_repository):
    repo=postgres_review_repository
    assert repo.reserve("2026-11-18",1000)
    repo.settle("2026-11-18",1000,600)
    repo.release("2026-11-18",500)
    bucket="2026-11-18T00:00:00+00:00"
    assert repo.record_channel_observation("worker","one",failed=True,bucket=bucket)==1
    assert repo.record_channel_observation("worker","one",failed=False,bucket=bucket)==1
    assert repo.record_channel_observation("worker","one",failed=True,bucket="2026-11-18T00:05:00+00:00")==2


def test_postgres_system_review_service_uses_native_repository(postgres_review_repository, monkeypatch, tmp_path):
    from packages.system_observability import create_system_observability_runtime
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.postgres_reviews import PostgresSystemReviewService

    repo = postgres_review_repository
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    runtime = create_system_observability_runtime(
        primary_database_path=tmp_path / "unused.sqlite",
        system_observability_dir=tmp_path / "observability",
        postgres_schema=repo.schema,
    )
    assert isinstance(runtime.projector, PostgresSourceStateProjector)
    assert isinstance(runtime.review_service, PostgresSystemReviewService)
    assert runtime.information_store.schema == repo.schema
    service = runtime.review_service
    assert service.information_store is runtime.information_store
    assert runtime.review_repository.information_store is runtime.information_store
    with runtime.information_store.connect() as conn:
        conn.execute("CREATE TABLE main.pipeline_worker_heartbeats(component text,worker_id text,status text,last_seen_at timestamptz,last_success_at timestamptz,last_error text,metadata jsonb)")
        conn.execute("CREATE TABLE main.x_capture_accounts(id bigint PRIMARY KEY,enabled boolean NOT NULL,username text)")
        conn.execute("CREATE TABLE main.x_capture_settings(global_interval_seconds integer NOT NULL)")
        conn.execute("INSERT INTO main.x_capture_accounts VALUES(1,true,'fixture')")
        conn.execute("INSERT INTO main.x_capture_settings VALUES(60)")
        conn.execute("INSERT INTO main.pipeline_worker_heartbeats VALUES('x_capture','fixture-worker','failed',now(),now()-interval '1 hour','fixture error','{}'::jsonb)")
        conn.execute("CREATE TABLE main.x_processing_attempts(id bigint,task_id bigint,stage text,attempt_no integer,executor text,status text,error_code text,error_message text,started_at timestamptz,finished_at timestamptz,rule_json jsonb)")
        conn.execute("CREATE TABLE main.tasks(id bigint PRIMARY KEY,source text,source_item_id text,status text,updated_at timestamptz)")
        conn.execute("CREATE TABLE main.x_task_pipeline(task_id bigint PRIMARY KEY,last_error text,prompt_template_key text,prompt_version_id integer)")
        days = ["2026-11-14 12:00:00+00", "2026-11-15 12:00:00+00", "2026-11-16 12:00:00+00"]
        for index, day in enumerate(days, start=1):
            conn.execute("INSERT INTO main.tasks VALUES(%s,%s,%s,%s,%s)",(index,"fixture","item-"+str(index),"write_failed",day))
            conn.execute("INSERT INTO main.x_task_pipeline VALUES(%s,%s,%s,%s)",(index,"writer output feature mode fields are invalid","writer",2))
            conn.execute("INSERT INTO main.x_processing_attempts VALUES(%s,%s,'write',1,'writer','failed','validation','writer output feature mode fields are invalid',%s,%s,'{}'::jsonb)",(index,index,day,day))
    result = service.run_deterministic_checks()
    assert set(result) == {"channel_outage", "missed_coverage", "output_audit", "rule_conflict", "failure_recovery"}
    failures = [item for item in runtime.review_repository.list_items({"status": "all"})["items"] if item["category"] == "inspection_failed"]
    assert not failures, [(item["task_kind"], item["summary"]) for item in failures]
    assert result["channel_outage"] == 1
    assert result["failure_recovery"] == 1
    persistent = [item for item in runtime.review_repository.list_items({"status": "all"})["items"] if item["category"] == "persistent_internal_failure"]
    assert len(persistent) == 1
    assert persistent[0]["affected_count"] == 3
    assert not failures
