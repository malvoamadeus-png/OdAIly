from __future__ import annotations

import uuid
import os

import pytest


@pytest.fixture
def postgres_runtime():
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL integration checks")
    from packages.common.pg_runtime import PostgresSettings

    return PostgresSettings(
        dsn=dsn,
        connect_timeout_seconds=5,
        statement_timeout_ms=10_000,
        lock_timeout_ms=2_000,
        idle_transaction_timeout_ms=10_000,
        migration_statement_timeout_ms=60_000,
        migration_lock_timeout_ms=5_000,
        migration_idle_transaction_timeout_ms=30_000,
        pool_max_size=2,
        pool_checkout_timeout_seconds=5,
        pool_max_waiting=16,
        application_name="odaily-investment-schema-test",
    )


@pytest.fixture
def investment_schema(postgres_runtime):
    from packages.common.pg_native import connect_postgres
    from psycopg.sql import Identifier

    schema = "test_" + uuid.uuid4().hex[:16]
    with connect_postgres(schema="main", application_name="odaily-investment-schema-test", settings=postgres_runtime) as conn:
        conn.execute("CREATE SCHEMA " + Identifier(schema).as_string(conn))
    with connect_postgres(schema=schema, application_name="odaily-investment-schema-test", settings=postgres_runtime) as conn:
        conn.execute(
            "CREATE TABLE hottopic_accounts(screen_name_lower text PRIMARY KEY,status text NOT NULL,"
            "investment_leads_enabled boolean NOT NULL DEFAULT true)"
        )
    try:
        yield schema
    finally:
        with connect_postgres(schema="main", application_name="odaily-investment-schema-cleanup", settings=postgres_runtime) as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component='investment_leads'",
                    (schema,),
                )
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")


def test_investment_lead_worker_selects_postgres_backend(tmp_path, monkeypatch) -> None:
    from packages.common.storage import PostgresSettings, StorageSettings
    from packages.investment_leads import service as service_module
    import packages.common.storage as storage_module

    settings = PostgresSettings(
        dsn="postgresql://localhost/test", connect_timeout_seconds=5, statement_timeout_ms=1000,
        lock_timeout_ms=1000, idle_transaction_timeout_ms=1000, migration_statement_timeout_ms=1000,
        migration_lock_timeout_ms=1000, migration_idle_transaction_timeout_ms=1000, pool_max_size=1,
        pool_checkout_timeout_seconds=1, pool_max_waiting=1, application_name="worker-test",
    )
    monkeypatch.setattr(storage_module, "load_storage_settings", lambda: StorageSettings(
        backend="postgres", epoch="test", sqlite_path=tmp_path / "main.sqlite",
    ))
    monkeypatch.setattr(storage_module, "load_postgres_settings", lambda: settings)
    captured = {}

    class FakeService:
        def __init__(self, path, *, postgres_repository=None):
            captured["path"] = path
            captured["repository"] = postgres_repository

        def process_once(self):
            return {"scanned": 0, "derived": 0, "playbooks": 0, "supplemented": 0, "identitySearched": 0}

        def close(self):
            pass

    monkeypatch.setattr(service_module, "InvestmentLeadService", FakeService)
    sqlite_path = tmp_path / "hottopic.sqlite"
    assert service_module.run_worker(sqlite_path, once=True) == 0
    assert captured["path"] == sqlite_path
    assert isinstance(captured["repository"], service_module.InvestmentLeadPostgresRepository)
    assert captured["repository"].settings is settings
    assert not sqlite_path.exists()


def test_investment_lead_schema_uses_native_versioned_migration(postgres_runtime, investment_schema) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    assert initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime) == (1, 2, 3)
    assert initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime) == ()

    with connect_postgres(
        schema=investment_schema,
        application_name="odaily-investment-schema-test",
        settings=postgres_runtime,
    ) as conn:
        with conn.transaction():
            conn.execute(
                "INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',now())"
            )
            conn.execute("INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                         "VALUES(%s,%s,%s,%s,now(),now())",
                         ("material:1", "hash:1", "original", Jsonb({"text": "evidence"})))
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES(%s,%s,'scan','v1','pending',now(),now())",
                ("material:1", "hash:1"),
            )
        assert conn.execute(
            "SELECT payload_json FROM investment_lead_materials WHERE material_id=%s", ("material:1",)
        ).fetchone()["payload_json"] == {"text": "evidence"}
        with conn.transaction():
            conn.execute("SET LOCAL enable_seqscan=off")
            plan = conn.execute(
                "EXPLAIN SELECT material_id,content_hash,last_observed_at FROM investment_lead_materials "
                "WHERE (last_observed_at,material_id,content_hash)>(%s,%s,%s) AND last_observed_at<%s "
                "ORDER BY last_observed_at,material_id,content_hash LIMIT 100",
                ("2020-01-01T00:00:00Z", "", "", "2030-01-01T00:00:00Z"),
            ).fetchall()
            plan_text = " ".join(row["QUERY PLAN"] for row in plan)
            assert "idx_investment_material_expiry" in plan_text
            assert "Index Cond" in plan_text


def test_scan_claim_uses_lease_and_skip_locked(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-claim-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            for index in range(2):
                material_id, content_hash = f"material:{index}", f"hash:{index}"
                conn.execute(
                    "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                    "VALUES(%s,%s,'original',%s,%s,%s)",
                    (material_id, content_hash, Jsonb({"body": str(index)}), now, now),
                )
                conn.execute(
                    "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                    "VALUES(%s,%s,'alice','original',%s,%s)",
                    (material_id, content_hash, now, now),
                )
                conn.execute(
                    "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                    "VALUES(%s,%s,'scan','v1','pending',%s,%s)",
                    (material_id, content_hash, now, now),
                )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    first = repository.claim_scan_batch(now=now, lease=timedelta(minutes=60), limit=1, max_attempts=3)
    second = repository.claim_scan_batch(now=now, lease=timedelta(minutes=60), limit=1, max_attempts=3)
    assert len(first) == len(second) == 1
    assert first[0]["id"] != second[0]["id"]
    assert first[0]["payload_json"] in ({"body": "0"}, {"body": "1"})

    job = first[0]
    job["derive_prompt_version"] = "derive-v1"
    assert repository.save_scan_result(
        job=job,
        result={"classification": "driver_candidate", "subject": "A", "impact_relations": []},
        actual_model="test-model",
        fallback_reason=None,
        input_tokens=10,
        output_tokens=2,
        now=now,
        max_attempts=3,
    ) is True
    assert repository.fail_scan_task(
        task_id=int(job["id"]), now=now, message="late worker", attempts=int(job["attempts"]),
        lease_until=job["lease_until"], max_attempts=3,
    ) is False
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-claim-test", settings=postgres_runtime) as conn:
        state = conn.execute("SELECT status FROM investment_lead_tasks WHERE id=%s", (job["id"],)).fetchone()
        assert state["status"] == "succeeded"
        assert conn.execute("SELECT COUNT(*) AS count FROM investment_lead_tasks WHERE stage='derive'").fetchone()["count"] == 1


def test_postgres_service_processes_scan_and_records_usage(postgres_runtime, investment_schema, tmp_path) -> None:
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from packages.common.pg_native import connect_postgres
    from packages.common.pg_runtime import postgres_pool_stats
    from packages.investment_leads.analysis import ModelOutput
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from packages.investment_leads.service import InvestmentLeadService
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    task_created_at = now - timedelta(seconds=60)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-service-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',%s)", (now,))
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:service','hash:service','original',%s,%s,%s)",
                (Jsonb({"material_id": "material:service", "text": "A company announced a concrete supply agreement."}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:service','hash:service','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:service','hash:service','scan','v1','pending',%s,%s)",
                (task_created_at, task_created_at),
            )

    class Analyzer:
        def scan(self, materials):
            assert postgres_pool_stats()["borrowed"] == 0
            return ModelOutput(
                [{"material_id": materials[0]["material_id"], "content_hash": materials[0]["content_hash"],
                  "classification": "no_candidate", "subject": "Supply agreement"}],
                "test-model", None, 12, 3, 1,
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    service = InvestmentLeadService(Path(tmp_path) / "unused.sqlite", analyzer=Analyzer(), postgres_repository=repository)
    assert service.process_once() == {"scanned": 1, "derived": 0, "playbooks": 0, "supplemented": 0, "identitySearched": 0}
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-service-test", settings=postgres_runtime) as conn:
        task = conn.execute("SELECT status,actual_model,input_tokens,output_tokens FROM investment_lead_tasks WHERE stage='scan'").fetchone()
        assert task == {"status": "no_candidate", "actual_model": "test-model", "input_tokens": 12, "output_tokens": 3}
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_events").fetchone()["n"] == 1
        usage = conn.execute("SELECT calls,input_tokens,output_tokens FROM investment_lead_usage WHERE kind='scan'").fetchone()
        assert usage == {"calls": 1, "input_tokens": 12, "output_tokens": 3}


def test_postgres_supplement_save_checks_lease_and_requeues_scan(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-supplement-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:supplement','hash:supplement','original',%s,%s,%s)",
                (Jsonb({"text": "Post body", "links": ["https://example.com"]}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:supplement','hash:supplement','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:supplement','hash:supplement','supplement','v1','pending',%s,%s),"
                "('material:supplement','hash:supplement','scan','v1','needs_material',%s,%s)", (now, now, now, now),
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    job = repository.claim_supplement(now=now, lease=timedelta(minutes=60), max_attempts=3)
    assert job is not None
    material = {**job["payload_json"], "supplement_attempted": True, "supplement_materials": [{"url": "https://example.com", "text": "Evidence"}]}
    expired_at = now + timedelta(minutes=61)
    assert not repository.save_supplement_result(job=job, material=material, now=expired_at, usage_calls=1)
    repository.reclaim_stale(now=expired_at, lease=timedelta(minutes=60), max_attempts=3)
    reclaimed = repository.claim_supplement(now=expired_at, lease=timedelta(minutes=60), max_attempts=3)
    assert reclaimed is not None and reclaimed["attempts"] == 2
    material = {**reclaimed["payload_json"], "supplement_attempted": True, "supplement_materials": [{"url": "https://example.com", "text": "Evidence"}]}
    assert repository.save_supplement_result(job=reclaimed, material=material, now=expired_at, usage_calls=1)
    assert not repository.save_supplement_result(job=reclaimed, material=material, now=expired_at, usage_calls=1)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-supplement-test", settings=postgres_runtime) as conn:
        stored = conn.execute("SELECT payload_json FROM investment_lead_materials WHERE material_id='material:supplement'").fetchone()["payload_json"]
        assert stored["supplement_attempted"] is True
        assert conn.execute("SELECT status FROM investment_lead_tasks WHERE stage='scan'").fetchone()["status"] == "pending"
        assert conn.execute("SELECT calls FROM investment_lead_usage WHERE kind='supplement'").fetchone()["calls"] == 1


def test_expired_unreclaimed_investment_leases_reject_all_finish_and_fail_paths(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    lease_at = datetime.now(UTC)
    expired_at = lease_at + timedelta(seconds=1)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-expired-fence-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute("INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) VALUES('material:expired','hash:expired','original',%s,%s,%s)", (Jsonb({"text":"evidence"}),lease_at,lease_at))
            conn.execute("INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) VALUES('material:expired','hash:expired','alice','original',%s,%s)",(lease_at,lease_at))
            conn.execute("INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,attempts,lease_until,created_at,updated_at) VALUES('material:expired','hash:expired','scan','scan-v1','processing',1,%s,%s,%s),('material:expired','hash:expired','derive','derive-v1','processing',1,%s,%s,%s)",(lease_at,lease_at,lease_at,lease_at,lease_at,lease_at))
            conn.execute("INSERT INTO investment_leads(id,subject,driver_logic,first_seen_at,last_substantive_at,created_at,updated_at) VALUES('lead:expired','Subject','Logic',%s,%s,%s,%s)",(lease_at,lease_at,lease_at,lease_at))
            conn.execute("INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) VALUES('lead:expired','material:expired','hash:expired','evidence',%s)",(lease_at,))
            conn.execute("INSERT INTO investment_lead_identity_searches(lead_id,asset_key,asset_name,context_json,status,attempts,lease_until,created_at,updated_at) VALUES('lead:expired','asset-key','Asset',%s,'processing',1,%s,%s,%s)",(Jsonb({}),lease_at,lease_at,lease_at))
            scan_id=conn.execute("SELECT id FROM investment_lead_tasks WHERE stage='scan'").fetchone()["id"]
            derive_id=conn.execute("SELECT id FROM investment_lead_tasks WHERE stage='derive'").fetchone()["id"]
            identity_id=conn.execute("SELECT id FROM investment_lead_identity_searches").fetchone()["id"]

    repository=InvestmentLeadPostgresRepository(schema=investment_schema,settings=postgres_runtime)
    scan_job={"id":scan_id,"material_id":"material:expired","content_hash":"hash:expired","attempts":1,"lease_until":lease_at,"prompt_version":"scan-v1","derive_prompt_version":"derive-v1"}
    derive_job={"id":derive_id,"material_id":"material:expired","content_hash":"hash:expired","attempts":1,"lease_until":lease_at}
    identity_job={"id":identity_id,"lead_id":"lead:expired","asset_key":"asset-key","asset_name":"Asset","attempts":1,"lease_until":lease_at}
    assert repository.save_scan_result(job=scan_job,result={"classification":"driver_candidate"},actual_model="stale",fallback_reason=None,input_tokens=1,output_tokens=1,now=expired_at,max_attempts=3) is False
    assert repository.fail_scan_task(task_id=scan_id,now=expired_at,message="stale",attempts=1,lease_until=lease_at,max_attempts=3) is False
    assert repository.save_derive_result(job=derive_job,leads=[{"subject":"Stale","driver_logic":"Stale","assets":[]}],related_events=[],actual_model="stale",fallback_reason=None,input_tokens=1,output_tokens=1,now=expired_at,derive_prompt_version="derive-v1",identity_prompt_version="identity-v1",max_attempts=3) is False
    assert repository.set_task_status(job=derive_job,status="ignored",now=expired_at) is False
    assert repository.finish_identity_search(job=identity_job,result={"identity":{"ticker":"T"}},now=expired_at,actual_model="stale",input_tokens=1,output_tokens=1,identity_prompt_version="identity-v1") is False
    assert repository.fail_identity_search(job=identity_job,now=expired_at,message="stale",max_attempts=3) is False
    with connect_postgres(schema=investment_schema,application_name="odaily-investment-expired-fence-test",settings=postgres_runtime) as conn:
        assert conn.execute("SELECT status FROM investment_lead_tasks WHERE id=%s",(scan_id,)).fetchone()["status"]=="processing"
        assert conn.execute("SELECT status FROM investment_lead_tasks WHERE id=%s",(derive_id,)).fetchone()["status"]=="processing"
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_events").fetchone()["n"]==0
        assert conn.execute("SELECT status FROM investment_lead_identity_searches WHERE id=%s",(identity_id,)).fetchone()["status"]=="processing"
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_usage").fetchone()["n"]==0


def test_scan_claim_waits_for_young_partial_batch(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-batch-wait-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:young','hash:young','original',%s,%s,%s)", (Jsonb({"text": "body"}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:young','hash:young','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:young','hash:young','scan','v1','pending',%s,%s)", (now, now),
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    assert repository.claim_scan_batch(
        now=now, lease=timedelta(minutes=60), limit=10, max_attempts=3, flush_seconds=30,
    ) == []
    assert repository.claim_scan_batch(
        now=now + timedelta(seconds=31), lease=timedelta(minutes=60), limit=10, max_attempts=3, flush_seconds=30,
    )


def test_stale_scan_owner_cannot_overwrite_reclaimed_lease(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-stale-lease-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:stale','hash:stale','original',%s,%s,%s)", (Jsonb({"text": "Evidence"}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:stale','hash:stale','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:stale','hash:stale','scan','v1','pending',%s,%s)", (now, now),
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    old_owner = repository.claim_scan_batch(now=now, lease=timedelta(minutes=1), limit=1, max_attempts=3)[0]
    reclaimed_at = now + timedelta(minutes=2)
    repository.reclaim_stale(now=reclaimed_at, lease=timedelta(minutes=1), max_attempts=3)
    new_owner = repository.claim_scan_batch(now=reclaimed_at, lease=timedelta(minutes=60), limit=1, max_attempts=3)[0]
    job = {**new_owner, "derive_prompt_version": "derive-v1"}
    assert int(new_owner["attempts"]) == int(old_owner["attempts"]) + 1
    assert repository.save_scan_result(
        job={**old_owner, "derive_prompt_version": "derive-v1"},
        result={"classification": "no_candidate"}, actual_model="stale", fallback_reason=None,
        input_tokens=1, output_tokens=1, now=reclaimed_at, max_attempts=3,
    ) is False
    assert repository.fail_scan_task(
        task_id=int(old_owner["id"]), now=reclaimed_at, message="stale", attempts=int(old_owner["attempts"]),
        lease_until=old_owner["lease_until"], max_attempts=3,
    ) is False
    assert repository.save_scan_result(
        job=job, result={"classification": "no_candidate"}, actual_model="current", fallback_reason=None,
        input_tokens=0, output_tokens=0, now=reclaimed_at, max_attempts=3,
    ) is True


def test_scan_result_is_ignored_if_source_account_is_disabled(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-source-race-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:disabled','hash:disabled','original',%s,%s,%s)", (Jsonb({"text": "Evidence"}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:disabled','hash:disabled','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:disabled','hash:disabled','scan','v1','pending',%s,%s)", (now, now),
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    job = repository.claim_scan_batch(now=now, lease=timedelta(minutes=60), limit=1, max_attempts=3)[0]
    job["derive_prompt_version"] = "derive-v1"
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-source-race-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("UPDATE hottopic_accounts SET investment_leads_enabled=false WHERE screen_name_lower='alice'")
    assert repository.save_scan_result(
        job=job, result={"classification": "driver_candidate"}, actual_model="test", fallback_reason=None,
        input_tokens=1, output_tokens=1, now=now + timedelta(seconds=1), max_attempts=3,
    ) is False
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-source-race-test", settings=postgres_runtime) as conn:
        assert conn.execute("SELECT status FROM investment_lead_tasks WHERE stage='scan'").fetchone()["status"] == "ignored"
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_events").fetchone()["n"] == 0


def test_postgres_service_derives_lead_and_enriches_asset_identity(postgres_runtime, investment_schema, tmp_path) -> None:
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from packages.common.pg_native import connect_postgres
    from packages.common.pg_runtime import postgres_pool_stats
    from packages.investment_leads.analysis import IdentitySearchOutput, ModelOutput
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from packages.investment_leads.service import InvestmentLeadService
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    created = now - timedelta(minutes=1)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-derive-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',%s)", (now,))
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:derive','hash:derive','original',%s,%s,%s)",
                (Jsonb({"material_id": "material:derive", "text": "Acme announced a binding supply agreement."}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:derive','hash:derive','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_events(material_id,content_hash,classification,subject,summary,actual_model,prompt_version,created_at) "
                "VALUES('material:derive','hash:derive','driver_candidate','Acme supply','Agreement expands capacity','test-model','scan-v1',%s)",
                (created,),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:derive','hash:derive','derive','derive-v1','pending',%s,%s)", (created, created),
            )

    class Analyzer:
        def derive(self, _candidate, _related_events, _existing_leads):
            assert postgres_pool_stats()["borrowed"] == 0
            return ModelOutput([{
                "matched_lead_id": "", "subject": "Acme supply expansion", "driver_logic": "Capacity supports a supply driver.",
                "conditions": "Agreement remains in force", "assets": [{"name": "Acme Corp", "ticker": "", "market": ""}],
                "source_claims": [{"material_id": "material:derive", "kind": "post", "text": "Binding supply agreement."}],
                "source_material_ids": ["material:derive"],
            }], "derive-model", None, 20, 4, 1)

        def search_identity(self, _asset_name, _context):
            assert postgres_pool_stats()["borrowed"] == 0
            return IdentitySearchOutput(
                {"name": "Acme Corporation", "ticker": "ACME", "market": "NASDAQ", "summary": "Listed issuer"},
                [{"url": "https://example.com/filing", "text": "Issuer filing"}],
                [{"url": "https://example.com/filing", "title": "Issuer filing"}], "search-model", 8, 2,
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    service = InvestmentLeadService(Path(tmp_path) / "unused.sqlite", analyzer=Analyzer(), postgres_repository=repository)
    result = service.process_once()
    assert result["derived"] == 1 and result["identitySearched"] == 1
    dashboard = service.dashboard()
    assert dashboard["results"]["active"] == 1
    listed = service.list_leads(account="alice")
    assert listed["total"] == 1 and listed["items"][0]["subject"] == "Acme supply expansion"
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-derive-test", settings=postgres_runtime) as conn:
        lead = conn.execute("SELECT id,revision,assets_json FROM investment_leads").fetchone()
        assert lead["revision"] == 2
        assert lead["assets_json"][0]["ticker"] == "ACME"
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_revisions WHERE lead_id=%s", (lead["id"],)).fetchone()["n"] == 2
        assert conn.execute("SELECT status FROM investment_lead_identity_searches").fetchone()["status"] == "succeeded"
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_sources WHERE lead_id=%s", (lead["id"],)).fetchone()["n"] == 1
    detail = service.lead_detail(lead["id"])
    assert detail is not None and detail["identitySearches"][0]["identity"]["ticker"] == "ACME"


def test_postgres_service_releases_checkout_during_supplement_fetch(postgres_runtime, investment_schema, tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from packages.common.pg_native import connect_postgres
    from packages.common.pg_runtime import postgres_pool_stats
    from packages.investment_leads.analysis import ModelOutput
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from packages.investment_leads.service import InvestmentLeadService
    from psycopg.types.json import Jsonb
    import packages.investment_leads.service as service_module

    now = datetime.now(UTC)
    created = now - timedelta(minutes=1)
    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-supplement-fetch-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',%s)", (now,))
            conn.execute("INSERT INTO hottopic_accounts(screen_name_lower,status,investment_leads_enabled) VALUES('alice','followed',true)")
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:fetch','hash:fetch','original',%s,%s,%s)",
                (Jsonb({"material_id": "material:fetch", "text": "A post with a source link.", "links": ["https://example.com/source"]}), now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES('material:fetch','hash:fetch','alice','original',%s,%s)", (now, now),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:fetch','hash:fetch','scan','v1','pending',%s,%s)", (created, created),
            )

    class Analyzer:
        def scan(self, materials):
            assert postgres_pool_stats()["borrowed"] == 0
            return ModelOutput([{"material_id": materials[0]["material_id"], "content_hash": materials[0]["content_hash"],
                                 "classification": "incomplete", "reason": "Missing context"}], "test-model", None, 1, 1, 1)

    def read_page(url):
        assert url == "https://example.com/source"
        assert postgres_pool_stats()["borrowed"] == 0
        return {"url": url, "text": "Fetched source evidence"}

    monkeypatch.setattr(service_module, "_read_public_page", read_page)
    service = InvestmentLeadService(
        Path(tmp_path) / "unused.sqlite", analyzer=Analyzer(),
        postgres_repository=InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime),
    )
    result = service.process_once()
    assert result["scanned"] == 1 and result["supplemented"] == 1


def test_postgres_maintenance_resumes_keysets_and_preserves_linked_evidence(postgres_runtime, investment_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.investment_leads.postgres_repository import InvestmentLeadPostgresRepository
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema
    from psycopg.types.json import Jsonb

    initialize_investment_leads_schema(schema=investment_schema, settings=postgres_runtime)
    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-maintenance-test", settings=postgres_runtime) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',%s)", (old,))
            conn.execute(
                "INSERT INTO investment_leads(id,subject,driver_logic,first_seen_at,last_substantive_at,created_at,updated_at) "
                "VALUES('lead:protected','Protected','Retain evidence',%s,%s,%s,%s)", (old, old, old, old),
            )
            for index in range(105):
                material_id, content_hash = f"material:{index:03d}", f"hash:{index:03d}"
                conn.execute(
                    "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                    "VALUES(%s,%s,'original',%s,%s,%s)",
                    (material_id, content_hash, Jsonb({}), old, old),
                )
                conn.execute(
                    "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                    "VALUES(%s,%s,'scan','v1','pending',%s,%s)", (material_id, content_hash, old, old),
                )
                conn.execute(
                    "INSERT INTO investment_lead_events(material_id,content_hash,classification,actual_model,prompt_version,created_at) "
                    "VALUES(%s,%s,'driver_candidate','test','v1',%s)", (material_id, content_hash, old),
                )
            conn.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,relation,payload_json,first_observed_at,last_observed_at) "
                "VALUES('material:zzz','hash:zzz','original',%s,%s,%s)", (Jsonb({}), old, old),
            )
            conn.execute(
                "INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                "VALUES('lead:protected','material:zzz','hash:zzz','source',%s)", (old,),
            )
            conn.execute(
                "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                "VALUES('material:zzz','hash:zzz','scan','v1','pending',%s,%s)", (old, old),
            )
            conn.execute(
                "INSERT INTO investment_lead_events(material_id,content_hash,classification,actual_model,prompt_version,created_at) "
                "VALUES('material:zzz','hash:zzz','driver_candidate','test','v1',%s)", (old,),
            )

    repository = InvestmentLeadPostgresRepository(schema=investment_schema, settings=postgres_runtime)
    first = repository.maintain(now=now, cutoff=now - timedelta(days=30), budget_seconds=10)
    second = repository.maintain(now=now + timedelta(seconds=10), cutoff=now - timedelta(days=30), budget_seconds=10)
    assert first == {"tasks": 100, "identity_searches": 0, "events": 100, "materials": 100, "continued": 1}
    assert second == {"tasks": 6, "identity_searches": 0, "events": 5, "materials": 5, "continued": 0}
    with connect_postgres(schema=investment_schema, application_name="odaily-investment-maintenance-test", settings=postgres_runtime) as conn:
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_materials").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_events").fetchone()["n"] == 1
        task_statuses = conn.execute("SELECT status,count(*) AS n FROM investment_lead_tasks GROUP BY status").fetchall()
        assert task_statuses == [{"status": "expired", "n": 1}]
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_maintenance_progress").fetchone()["n"] == 0
