from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen
import json
from psycopg.types.json import Jsonb

import pytest


def test_worker_heartbeat_jsonb_serializes_datetime_metadata() -> None:
    from packages.x_processing.repository import PostgresXProcessingRepository

    repository = object.__new__(PostgresXProcessingRepository)
    from psycopg.types.json import Jsonb as PsycopgJsonb
    repository._Jsonb = PsycopgJsonb
    timestamp = datetime.now(UTC)
    encoded = repository._Jsonb(
        {"queue_exhausted_jobs": [{"updated_at": timestamp}]},
        dumps=lambda value: json.dumps(
            value, ensure_ascii=False,
            default=lambda item: item.isoformat() if isinstance(item, datetime) else str(item),
        ),
    )
    assert json.loads(encoded.dumps(encoded.obj)) == {
        "queue_exhausted_jobs": [{"updated_at": timestamp.isoformat()}],
    }


def test_postgres_worker_heartbeat_accepts_datetime_metadata(queue_settings, monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.x_processing.repository import PostgresXProcessingRepository
    from psycopg.sql import Identifier

    schema = "test_lp_heartbeat_" + uuid.uuid4().hex[:10]
    original_connect = connect_postgres
    monkeypatch.setattr(
        "packages.x_processing.repository.connect_postgres",
        lambda *, schema, application_name=None, settings=None: original_connect(
            schema=os.getenv("ODAILY_TEST_HEARTBEAT_SCHEMA", "main"),
            application_name=application_name, settings=settings or queue_settings,
        ),
    )
    try:
        with original_connect(schema="main", settings=queue_settings) as conn:
            conn.execute("CREATE SCHEMA " + Identifier(schema).as_string(conn))
        monkeypatch.setenv("ODAILY_TEST_HEARTBEAT_SCHEMA", schema)
        with original_connect(schema=schema, settings=queue_settings) as conn:
            conn.execute(
                "CREATE TABLE pipeline_worker_heartbeats(component text NOT NULL,worker_id text NOT NULL,"
                "status text NOT NULL,last_seen_at timestamptz NOT NULL,last_success_at timestamptz,last_error text,"
                "metadata jsonb NOT NULL DEFAULT '{}'::jsonb,updated_at timestamptz NOT NULL DEFAULT now(),"
                "PRIMARY KEY(component,worker_id))"
            )
        repository = PostgresXProcessingRepository()
        timestamp = datetime.now(UTC)
        repository.record_worker_heartbeat(
            component="local_pipeline", worker_id="datetime-heartbeat", status="running", success=True,
            metadata={"queue_exhausted_jobs": [{"updated_at": timestamp}]},
        )
        with original_connect(schema=schema, settings=queue_settings) as conn:
            metadata = conn.execute(
                "SELECT metadata FROM pipeline_worker_heartbeats WHERE component='local_pipeline' AND worker_id='datetime-heartbeat'"
            ).fetchone()["metadata"]
        assert metadata["queue_exhausted_jobs"][0]["updated_at"] == timestamp.isoformat()
    finally:
        with original_connect(schema="main", settings=queue_settings) as conn:
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")


@pytest.fixture
def queue_settings():
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL queue checks")
    from packages.common.storage import PostgresSettings

    return PostgresSettings(
        dsn=dsn,
        connect_timeout_seconds=5,
        statement_timeout_ms=10_000,
        lock_timeout_ms=2_000,
        idle_transaction_timeout_ms=10_000,
        migration_statement_timeout_ms=60_000,
        migration_lock_timeout_ms=5_000,
        migration_idle_transaction_timeout_ms=30_000,
        pool_max_size=4,
        pool_checkout_timeout_seconds=5,
        pool_max_waiting=16,
        application_name="odaily-queue-test",
    )


def test_postgres_queue_concurrent_claim_and_lease_fencing(queue_settings, monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.local_pipeline.postgres_queue import PostgresLocalPipelineQueue
    from psycopg.sql import Identifier

    schema = "test_lp_" + uuid.uuid4().hex[:12]
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_storage_settings", lambda: type("Storage", (), {"epoch": "queue-test"})())
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_postgres_settings", lambda: queue_settings)
    original_connect = connect_postgres
    monkeypatch.setattr(
        "packages.local_pipeline.postgres_queue.connect_postgres",
        lambda *, schema, application_name=None, settings=None: original_connect(
            schema=schema, application_name=application_name, settings=settings or queue_settings
        ),
    )

    try:
        queue = PostgresLocalPipelineQueue(max_attempts=3, schema=schema)
        for index in range(12):
            queue.enqueue(job_type="write_flow", task_id=index + 1, source="test", source_item_id=str(index))

        def claim(worker_id: str):
            return queue.claim_next(worker_id=worker_id)

        with ThreadPoolExecutor(max_workers=4) as executor:
            jobs = [job for job in executor.map(claim, [f"worker-{i}" for i in range(12)]) if job is not None]
        assert len(jobs) == 12
        assert len({job.id for job in jobs}) == 12

        expired = jobs[0]
        with connect_postgres(schema=schema, settings=queue_settings) as connection:
            connection.execute(
                "UPDATE local_pipeline_jobs SET locked_at=%s WHERE id=%s",
                (datetime.now(UTC) - timedelta(hours=1), expired.id),
            )
        assert queue.requeue_stale_running_jobs(stale_before=datetime.now(UTC) - timedelta(minutes=30)) == 1
        reclaimed = queue.claim_next(worker_id="replacement")
        assert reclaimed is not None and reclaimed.id == expired.id
        assert reclaimed.lease_token != expired.lease_token
        assert queue.mark_succeeded(expired.id, lease_token=expired.lease_token) is False
        assert queue.mark_succeeded(reclaimed.id, lease_token=reclaimed.lease_token) is True

        queued = queue.enqueue(job_type="write_flow", task_id=20, source="test", source_item_id="live")
        running = queue.claim_next(worker_id="active")
        assert running is not None and running.id == queued.id
        repeated = queue.enqueue(job_type="alert_only", task_id=21, source="test", source_item_id="live", payload={"replacement": True})
        assert repeated.status == "running"
        assert repeated.task_id == running.task_id
        assert repeated.payload == running.payload
        assert repeated.lease_token == running.lease_token
        assert queue.mark_succeeded(running.id, lease_token=running.lease_token) is True
        for job in jobs:
            if job.id != expired.id:
                assert queue.mark_succeeded(job.id, lease_token=job.lease_token) is True

        final_attempt = queue.enqueue(job_type="write_flow", task_id=22, source="test", source_item_id="final-live")
        with connect_postgres(schema=schema, settings=queue_settings) as connection:
            connection.execute("UPDATE local_pipeline_jobs SET attempt_count=3 WHERE id=%s", (final_attempt.id,))
        assert queue.exhaust_over_limit_jobs() == 0
        with connect_postgres(schema=schema, settings=queue_settings) as connection:
            row = connection.execute("SELECT status FROM local_pipeline_jobs WHERE id=%s", (final_attempt.id,)).fetchone()
            assert row["status"] == "pending"
            connection.execute("UPDATE local_pipeline_jobs SET status='running',locked_at=%s,locked_by='live' WHERE id=%s", (datetime.now(UTC), final_attempt.id))
        assert queue.exhaust_over_limit_jobs() == 0
        assert queue.requeue_stale_running_jobs(stale_before=datetime.now(UTC) + timedelta(seconds=1)) == 1
        assert queue.stats()["exhausted"] == 1

        retry = queue.enqueue(job_type="write_flow", task_id=23, source="test", source_item_id="retry")
        retry_claim = queue.claim_next(worker_id="retry-worker")
        assert retry_claim is not None and retry_claim.id == retry.id
        assert queue.mark_failed(retry.id, error="first failure", attempt_count=1, lease_token=retry_claim.lease_token) is True
        with connect_postgres(schema=schema, settings=queue_settings) as connection:
            connection.execute("UPDATE local_pipeline_jobs SET next_attempt_at=%s WHERE id=%s", (datetime.now(UTC) - timedelta(seconds=1), retry.id))
        second_claim = queue.claim_next(worker_id="retry-worker")
        assert second_claim is not None and second_claim.id == retry.id
        assert queue.mark_failed(retry.id, error="final failure", attempt_count=3, lease_token=second_claim.lease_token) is True
        assert queue.stats()["exhausted"] == 2
        assert queue.stats_by_epoch()["queue-test"]["exhausted"] == 2
    finally:
        with connect_postgres(schema="main", application_name="odaily-local-pipeline-test-cleanup", settings=queue_settings) as cleanup:
            cleanup.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component='local_pipeline'", (schema,))
            cleanup.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(cleanup) + " CASCADE")


def test_postgres_queue_adopts_explicit_frozen_sqlite_epoch(queue_settings, monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.local_pipeline.postgres_queue import PostgresLocalPipelineQueue
    from psycopg.sql import Identifier

    schema = "test_lp_adopt_" + uuid.uuid4().hex[:10]
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_storage_settings",
                        lambda: type("Storage", (), {"epoch": "postgres-cutover-test"})())
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_postgres_settings", lambda: queue_settings)
    original_connect = connect_postgres
    monkeypatch.setattr(
        "packages.local_pipeline.postgres_queue.connect_postgres",
        lambda *, schema, application_name=None, settings=None: original_connect(
            schema=schema, application_name=application_name, settings=settings or queue_settings
        ),
    )
    try:
        queue = PostgresLocalPipelineQueue(schema=schema)
        with connect_postgres(schema=schema, settings=queue_settings) as conn:
            with conn.cursor() as cursor:
                cursor.executemany(
                    "INSERT INTO local_pipeline_jobs(job_type,task_id,source,source_item_id,payload,storage_epoch,status,next_attempt_at,updated_at) "
                    "VALUES('write_flow',%s,'fixture',%s,'{}'::jsonb,%s,%s,now(),now())",
                    [(1, "pending", "sqlite-20260802", "pending"),
                     (2, "exhausted", "sqlite-20260802", "exhausted"),
                     (3, "done", "sqlite-20260802", "succeeded"),
                     (4, "legacy", "postgres-legacy", "pending")],
                )

        report = queue.adopt_frozen_sqlite_epoch(
            source_epoch="sqlite-20260802", source_writes_stopped=True,
        )
        assert report["adopted"] == 3
        assert report["source_status_counts"] == {"exhausted": 1, "pending": 1, "succeeded": 1}
        assert report["destination_epoch"] == "postgres-cutover-test"
        with connect_postgres(schema=schema, settings=queue_settings) as conn:
            counts = conn.execute(
                "SELECT storage_epoch,status,count(*) AS n FROM local_pipeline_jobs GROUP BY storage_epoch,status ORDER BY storage_epoch,status"
            ).fetchall()
        assert [(row["storage_epoch"], row["status"], row["n"]) for row in counts] == [
            ("postgres-cutover-test", "exhausted", 1),
            ("postgres-cutover-test", "pending", 1),
            ("postgres-cutover-test", "succeeded", 1),
            ("postgres-legacy", "pending", 1),
        ]
    finally:
        with connect_postgres(schema="main", application_name="odaily-local-pipeline-test-cleanup", settings=queue_settings) as cleanup:
            cleanup.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component='local_pipeline'", (schema,))
            cleanup.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(cleanup) + " CASCADE")


def test_postgres_local_pipeline_http_job_is_processed(queue_settings, monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.local_pipeline.postgres_queue import PostgresLocalPipelineQueue
    from packages.local_pipeline.server import LocalPipelineHandler, LocalPipelineHTTPServer, LocalPipelineService
    from psycopg import sql

    schema = "test_lp_http_" + uuid.uuid4().hex[:10]
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_storage_settings", lambda: type("Storage", (), {"epoch": "http-test"})())
    monkeypatch.setattr("packages.local_pipeline.postgres_queue.load_postgres_settings", lambda: queue_settings)
    original_connect = connect_postgres
    monkeypatch.setattr(
        "packages.local_pipeline.postgres_queue.connect_postgres",
        lambda *, schema, application_name=None, settings=None: original_connect(
            schema=schema, application_name=application_name, settings=settings or queue_settings
        ),
    )

    class Processor:
        worker_id = "http-test-worker"

        def process(self, job):
            assert job.source_item_id == "mock-ai-job"
            return type("Result", (), {"task_id": job.task_id, "status": "completed", "message": "mocked"})()

        def record_heartbeat(self, **kwargs):
            pass

    try:
        queue = PostgresLocalPipelineQueue(max_attempts=3, schema=schema)
        service = LocalPipelineService(queue=queue, processor=Processor())  # type: ignore[arg-type]
        service.start()
        server = LocalPipelineHTTPServer(("127.0.0.1", 0), LocalPipelineHandler)
        server.service = service
        server_thread = Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            body = json.dumps({
                "job_type": "write_flow", "task_id": 801,
                "source": "mock", "source_item_id": "mock-ai-job",
            }).encode()
            request = Request(f"http://127.0.0.1:{server.server_port}/pipeline/jobs", data=body, headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                assert response.status == 201
                accepted = json.load(response)
            assert accepted["status"] == "pending"
            deadline = datetime.now(UTC) + timedelta(seconds=5)
            while datetime.now(UTC) < deadline and queue.stats().get("succeeded", 0) != 1:
                time.sleep(0.02)
            assert queue.stats() == {"succeeded": 1}
        finally:
            service.stop()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
    finally:
        with connect_postgres(schema="main", application_name="odaily-local-pipeline-test-cleanup", settings=queue_settings) as cleanup:
            cleanup.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component='local_pipeline'", (schema,))
            cleanup.execute("DROP SCHEMA IF EXISTS " + sql.Identifier(schema).as_string(cleanup) + " CASCADE")
