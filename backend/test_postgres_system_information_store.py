from __future__ import annotations

import os
import time
import uuid
from threading import Event
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from psycopg.sql import Identifier

import pytest


@pytest.fixture
def postgres_information_store(monkeypatch, tmp_path):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL system information checks")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", dsn)
    monkeypatch.setenv("ODAILY_POSTGRES_POOL_MAX_SIZE", "2")
    from packages.system_observability.postgres_store import PostgresSystemInformationStore

    schema = "test_si_" + uuid.uuid4().hex[:12]
    store = PostgresSystemInformationStore(evidence_dir=tmp_path / "evidence", schema=schema)
    store.initialize()
    yield store
    try:
        with store.connect() as conn:
            conn.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component IN ('system_information','system_observability_evidence')", (schema,))
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")
    finally:
        from packages.common.pg_runtime import close_postgres_pools
        close_postgres_pools()


def _snapshot(item_id: str) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "item_id": item_id,
        "business_key": "x",
        "source": "x_capture",
        "source_id": "fixture-1",
        "task_id": 17,
        "title": "Fixture title",
        "summary": "Fixture summary",
        "source_url": "https://example.invalid/post/1",
        "stage": "writing",
        "status": "write_failed",
        "result": "processing",
        "created_at_ms": now_ms - 60_000,
        "updated_at_ms": now_ms - 30_000,
        "source_database": "main",
        "source_table": "tasks",
        "source_row_id": "17",
        "news_visible": True,
        "admission": "accepted",
        "legacy_history": False,
    }


def test_postgres_information_snapshot_event_and_query(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    snapshot = _snapshot(item_id)

    assert store.upsert_snapshot(snapshot, {"content": "fixture evidence"}) is True
    assert store.upsert_snapshot(snapshot, {"content": "fixture evidence"}) is False
    assert store.append_event(
        item_id=item_id,
        event_id=f"{item_id}:attempt",
        event_type="stage_attempt",
        stage="writing",
        status="failed",
        result="failed",
        happened_at_ms=int(time.time() * 1000),
        summary="attempt failed",
        evidence={"error": "fixture"},
        details={"attempt_id": 42},
    ) is True

    result = store.query({"q": "Fixture title", "limit": 10})
    assert result["total"] == 1
    assert result["items"][0]["item_id"] == item_id
    failed = store.query({"status": "failed", "limit": 10})
    assert failed["total"] == 1
    assert failed["items"][0]["item_id"] == item_id
    assert store.query({"q": "summary title"})["total"] == 1
    detail = store.detail(item_id)
    assert detail is not None
    assert detail["evidence"] == {"content": "fixture evidence"}
    assert len(detail["timeline"]) == 2
    assert store.has_item(item_id)


def test_postgres_information_schema_has_no_legacy_search_postings(postgres_information_store) -> None:
    store = postgres_information_store
    with store.connect() as conn:
        assert conn.execute("SELECT to_regclass('system_observability.system_information_search_legacy') AS relation").fetchone()["relation"] is None
        assert conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='system_observability' AND table_name='system_information_items' AND column_name='search_key'").fetchone() is None

    assert store.query({"q": "历史关键词", "limit": 10})["total"] == 0


def test_postgres_runtime_does_not_generate_or_rebuild_search_index(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"no-search-index:{uuid.uuid4().hex}"
    assert store.upsert_snapshot(_snapshot(item_id), {"content": "not indexed"}) is True
    with store.connect() as conn:
        row = conn.execute("SELECT search_text,search_terms FROM system_information_items WHERE item_id=%s", (item_id,)).fetchone()
        assert row["search_text"] == ""
        assert row["search_terms"] == []
    assert store.rebuild_search_index() == 0
    assert store.compact_search_index() == {"scanned": 0, "done": 1}


def test_postgres_query_enforces_six_hour_window_and_stable_cursor(postgres_information_store) -> None:
    store = postgres_information_store
    now_ms = int(time.time() * 1000)
    for suffix, age in (("recent-a", 60_000), ("recent-b", 120_000), ("old", 7 * 60 * 60 * 1000)):
        snapshot = _snapshot(f"window:{suffix}")
        snapshot.update({"created_at_ms": now_ms - age, "updated_at_ms": now_ms - age, "source_id": suffix})
        store.upsert_snapshot(snapshot, None)
    first = store.query({"limit": 1, "since": "2000-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z"})
    assert first["total"] == 2
    assert first["window_until_ms"] <= int(time.time() * 1000)
    assert first["window_until_ms"] - first["window_since_ms"] == 6 * 60 * 60 * 1000
    assert first["next_cursor"]
    second = store.query({"limit": 1, "cursor": first["next_cursor"], "since": "2000-01-01T00:00:00Z", "until": "2099-01-01T00:00:00Z"})
    assert second["total"] == 2
    assert second["items"][0]["item_id"] != first["items"][0]["item_id"]


def test_editor_plugin_system_information_public_read_uses_postgres_store(postgres_information_store, monkeypatch) -> None:
    from packages.editor_plugin_api import EditorPluginNewsGenService

    item_id = f"plugin-read:{uuid.uuid4().hex}"
    snapshot = _snapshot(item_id)
    snapshot.update({"title": "插件读取", "summary": "历史摘要", "source_id": None, "task_id": None})
    postgres_information_store.upsert_snapshot(snapshot, {"content": "正文"})
    service = object.__new__(EditorPluginNewsGenService)
    service.system_information = postgres_information_store

    def sqlite_forbidden(*_args, **_kwargs):
        raise AssertionError("PostgreSQL system-information reads must not open SQLite")

    monkeypatch.setattr("packages.common.storage.connect_sqlite_readonly", sqlite_forbidden)
    result = EditorPluginNewsGenService.execute_system_information(
        service, None, {"action": "list", "q": "插件读取", "limit": 10},
    )
    assert result["total"] == 1
    assert result["items"][0]["item_id"] == item_id


def test_postgres_information_out_of_order_snapshot_does_not_regress(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    newer = _snapshot(item_id)
    newer.update({"status": "published", "result": "direct_publish", "updated_at_ms": 1_800_000_100_000, "source_revision": 200})
    older = {**newer, "status": "writing", "result": "processing", "updated_at_ms": 1_800_000_050_000, "source_revision": 100}
    entries = [
        {"snapshot": newer, "evidence": {"source": "newer"}, "source_revision": 200, "event_id": f"{item_id}:newer", "stage": "published", "status": "published", "result": "direct_publish", "happened_at_ms": newer["updated_at_ms"], "summary": "newer state"},
        {"snapshot": older, "evidence": {"source": "older"}, "source_revision": 100, "event_id": f"{item_id}:older", "stage": "writing", "status": "writing", "result": "processing", "happened_at_ms": older["updated_at_ms"], "summary": "older event"},
    ]
    store.apply_source_audit_batch(entries, watermark_key=f"pg-store-test:{item_id}", watermark=200)
    detail = store.detail(item_id)
    assert detail is not None
    assert detail["status"] == "published"
    assert {event["event_id"] for event in detail["timeline"]} >= {f"{item_id}:newer", f"{item_id}:older"}


def test_postgres_information_concurrent_first_insert_is_revision_fenced(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    old = _snapshot(item_id)
    old.update({"status": "writing", "result": "processing", "source_revision": 10})
    new = {**old, "status": "published", "result": "direct_publish", "source_revision": 20}
    with ThreadPoolExecutor(max_workers=2) as executor:
        writes = [executor.submit(store.upsert_snapshot, candidate, {"revision": candidate["source_revision"]}) for candidate in (old, new)]
        for write in writes:
            write.result()
    detail = store.detail(item_id)
    assert detail is not None
    assert detail["status"] == "published"


def test_postgres_information_evidence_supports_external_gzip_file(postgres_information_store) -> None:
    store = postgres_information_store
    evidence = {"content": "x" * 10_000}
    reference = store.save_evidence(evidence)
    assert store.load_evidence(reference) == evidence


def test_postgres_source_batch_commits_watermark_with_projection(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    snapshot = _snapshot(item_id)
    entry = {
        "snapshot": snapshot,
        "evidence": {"source": "trigger"},
        "event_id": f"source-audit:test:{uuid.uuid4().hex}",
        "stage": "writing",
        "status": "written",
        "result": "processing",
        "happened_at_ms": snapshot["updated_at_ms"],
        "summary": "source state changed",
    }
    imported, changed = store.apply_source_audit_batch([entry], watermark_key=f"pg-store-test:{item_id}", watermark=9)
    assert (imported, changed) == (1, 1)
    assert store.get_state(f"pg-store-test:{item_id}") == "9"
    assert store.detail(item_id) is not None


def test_postgres_information_retains_old_internal_event_and_classifies_local_storage_failures(postgres_information_store) -> None:
    from packages.system_observability.error_policy import external_error, failure_category

    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    assert store.upsert_snapshot(_snapshot(item_id), None)
    assert store.append_event(item_id=item_id, event_id=f"{item_id}:old-internal", event_type="stage_attempt", stage="writing", status="succeeded", result="internal_complete", happened_at_ms=1_600_000_000_000, summary="internal validation failed in 2020", reason="internal rule mismatch")
    detail = store.detail(item_id)
    assert detail is not None
    assert any(event["event_id"] == f"{item_id}:old-internal" for event in detail["timeline"])

    assert failure_category(SimpleNamespace(sqlstate="55P03")) == "local_storage"
    assert failure_category(SimpleNamespace(sqlstate="40P01")) == "local_storage"
    assert failure_category(SimpleNamespace(sqlstate="57014")) == "local_storage"
    assert failure_category("PoolTimeout: connection pool checkout timed out") == "local_storage"
    assert failure_category("HTTP 429 rate limit") == "upstream_auth"
    assert failure_category("provider statement timeout") == "upstream_timeout"
    assert external_error({"status": "failed", "error": "HTTP 429 rate limit"})


def test_postgres_evidence_compaction_preserves_evidence_without_search_rebuild(postgres_information_store) -> None:
    store=postgres_information_store
    item_id=f"pg-store-test:{uuid.uuid4().hex}"
    evidence={"content":"obsolete needle "+("x"*2500)}
    snapshot=_snapshot(item_id)
    snapshot.update({"status":"published","result":"direct_publish"})
    assert store.upsert_snapshot(snapshot,evidence)
    with store.connect() as conn:
        conn.execute("UPDATE system_information_evidence SET created_at_ms=0 WHERE evidence_ref=(SELECT evidence_ref FROM system_information_items WHERE item_id=%s)",(item_id,))
    assert store.query({"q":"obsolete needle"})["total"]==1
    result=store.cleanup_expired_evidence(now_ms=2_000_000_000_000,retain_days=1,limit=20,dry_run=False)
    assert result["compacted"]==1
    assert store.query({"q":"obsolete needle"})["total"]==1


def test_postgres_evidence_gc_dry_run_does_not_advance_cursors(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    assert store.upsert_snapshot(_snapshot(item_id), {"content": "large " + "x" * 2500})
    with store.connect() as conn:
        conn.execute("UPDATE system_information_evidence SET created_at_ms=0 WHERE evidence_ref=(SELECT evidence_ref FROM system_information_items WHERE item_id=%s)", (item_id,))
    before = {key: store.get_state(key) for key in ("evidence_gc_cursor", "evidence_compaction_cursor", "orphan_evidence_cursor")}
    store.cleanup_expired_evidence(now_ms=2_000_000_000_000, retain_days=1, limit=1, dry_run=True)
    store.prune_orphan_evidence(limit=1, dry_run=True)
    after = {key: store.get_state(key) for key in before}
    assert after == before


def test_postgres_evidence_compaction_preserves_inspection_response_reference(postgres_information_store) -> None:
    store = postgres_information_store
    from packages.system_observability.postgres_reviews import PostgresSystemReviewRepository

    reviews = PostgresSystemReviewRepository(schema=store.schema, information_store=store)
    reviews.initialize()
    item_key = f"inspection:{uuid.uuid4().hex}"
    review_key = reviews.upsert_issue({
        "task_kind": "D", "category": "decision_sample_review", "title": "巡检证据保留",
        "identity": {"id": item_key}, "actual": "检查模型响应引用", "evidence": [],
    })
    attempt_id = reviews.start_analysis_attempt(review_key)
    response = "response " + "x" * 2500
    ref = reviews.record_analysis_response(attempt_id, response, "fixture-request", 1)
    reviews.finish_analysis_attempt(attempt_id, status="completed")

    with store.connect() as conn:
        conn.execute("UPDATE system_information_evidence SET created_at_ms=0 WHERE evidence_ref=%s", (ref,))
    result = store.cleanup_expired_evidence(
        now_ms=2_000_000_000_000, retain_days=1, limit=20, dry_run=False,
    )
    assert result["compacted"] >= 1
    with reviews.connect() as conn:
        updated_ref = conn.execute(
            "SELECT raw_response_ref FROM system_review_analysis_attempts WHERE id=%s", (attempt_id,),
        ).fetchone()["raw_response_ref"]
    assert updated_ref and updated_ref != ref
    compacted = store.load_evidence(updated_ref)
    assert compacted["raw_model_response_sha256"]
    assert compacted["raw_model_response_bytes"] == len(response.encode())


def test_native_source_outbox_updates_imported_legacy_item_without_duplicate(postgres_information_store) -> None:
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.projector import TableSpec

    store = postgres_information_store
    schema = store.schema
    legacy_id = f"legacy-import:{uuid.uuid4().hex}"
    source_id = str(70_000 + int(uuid.uuid4().hex[:4], 16))
    with store.connect() as conn:
        qualified = Identifier(schema).as_string(conn)
        conn.execute(f"CREATE TABLE {qualified}.tasks (id bigint PRIMARY KEY, source text, source_item_id text, source_url text, title text, content text, status text, created_at timestamptz, updated_at timestamptz, metadata jsonb, raw_payload jsonb)")
        conn.execute(f"CREATE TABLE {qualified}.odaily_source_state_outbox (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text NOT NULL, source_table text NOT NULL, source_row_id text NOT NULL, operation text NOT NULL, payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), projected_at timestamptz)")
        conn.execute(f"INSERT INTO {qualified}.tasks VALUES (%s,'x_capture','post-1','https://example.invalid/post-1','Imported title','body','published','2030-01-01','2030-01-02', '{{}}'::jsonb, '{{}}'::jsonb)", (int(source_id),))
        imported = _snapshot(legacy_id)
        imported.update({"source_table": "tasks", "source_row_id": source_id, "source_database": "legacy-main.db", "source_id": "post-1", "status": "written", "result": "processing"})
        assert store.upsert_snapshot(imported, {"task": {"content": "old"}})
        assert store.register_imported_source_identities([imported], source_schema_map={"legacy-main.db": schema}) == 1
        conn.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'tasks',%s,'UPDATE','{{\"status\":\"writing\"}}'::jsonb),(%s,'tasks',%s,'UPDATE','{{\"status\":\"published\"}}'::jsonb)", (schema, source_id, schema, source_id))

    projector = PostgresSourceStateProjector(store, outbox_schema=schema, table_specs=(TableSpec(schema, "tasks", "unmapped_source", "main_pipeline"),))
    result = projector.run_once()
    assert result["read"] == 2
    detail = store.detail(legacy_id)
    assert detail is not None
    assert detail["status"] == "published"
    with store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS count FROM system_information_items WHERE source_table='tasks' AND source_row_id=%s", (source_id,)).fetchone()["count"] == 1
        assert conn.execute(f"SELECT projected_at FROM {Identifier(schema).as_string(conn)}.odaily_source_state_outbox").fetchone()["projected_at"] is not None
    projected = [event for event in detail["timeline"] if event["event_id"].startswith("source-state:")]
    assert [event["status"] for event in projected] == ["writing", "published"]


def test_native_source_outbox_delayed_delete_does_not_hide_reinserted_row(postgres_information_store) -> None:
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.projector import TableSpec

    store = postgres_information_store
    schema = store.schema
    source_id = str(800_000 + int(uuid.uuid4().hex[:4], 16))
    with store.connect() as conn:
        qualified = Identifier(schema).as_string(conn)
        conn.execute(f"CREATE TABLE {qualified}.tasks (id bigint PRIMARY KEY, source text, source_item_id text, source_url text, title text, content text, status text, created_at timestamptz, updated_at timestamptz, metadata jsonb, raw_payload jsonb)")
        conn.execute(f"CREATE TABLE {qualified}.odaily_source_state_outbox (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text NOT NULL, source_table text NOT NULL, source_row_id text NOT NULL, operation text NOT NULL, payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), projected_at timestamptz)")
        conn.execute(f"INSERT INTO {qualified}.tasks VALUES (%s,'x_capture','reused-id','https://example.invalid/post','Reinserted title','body','published',now(),now(),'{{}}'::jsonb,'{{}}'::jsonb)", (int(source_id),))
        conn.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'tasks',%s,'DELETE','{{}}'::jsonb)", (schema, source_id))
    projector = PostgresSourceStateProjector(store, outbox_schema=schema, table_specs=(TableSpec(schema, "tasks", "unmapped_source", "main_pipeline"),))
    projector.run_once()
    item_id = f"task:x_capture:reused-id"
    detail = store.detail(item_id)
    assert detail is not None
    assert detail["status"] == "published"
    delete_events = [event for event in detail["timeline"] if event["event_id"].startswith("source-state:")]
    assert delete_events[-1]["status"] == "deleted"


def test_native_source_outbox_late_low_id_is_serialized_across_projectors(postgres_information_store) -> None:
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.projector import TableSpec

    store = postgres_information_store
    schema = store.schema
    source_id = str(900_000 + int(uuid.uuid4().hex[:4], 16))
    qualified_schema = Identifier(schema)
    with store.connect() as conn:
        qualified = qualified_schema.as_string(conn)
        conn.execute(f"CREATE TABLE {qualified}.tasks (id bigint PRIMARY KEY, source text, source_item_id text, source_url text, title text, content text, status text, created_at timestamptz, updated_at timestamptz, metadata jsonb, raw_payload jsonb)")
        conn.execute(f"CREATE TABLE {qualified}.odaily_source_state_outbox (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text NOT NULL, source_table text NOT NULL, source_row_id text NOT NULL, operation text NOT NULL, payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), projected_at timestamptz)")
        conn.execute(f"INSERT INTO {qualified}.tasks VALUES (%s,'x_capture',%s,'https://example.invalid/post','Current title','body','published',now(),now(),'{{}}'::jsonb,'{{}}'::jsonb)", (int(source_id), f"late-{source_id}"))

    delayed_context = store.connect()
    delayed = delayed_context.__enter__()
    delayed_transaction = delayed.transaction()
    delayed_transaction.__enter__()
    delayed.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'tasks',%s,'UPDATE','{{\"status\":\"writing\"}}'::jsonb)", (schema, source_id))
    with store.connect() as conn:
        conn.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'tasks',%s,'UPDATE','{{\"status\":\"published\"}}'::jsonb)", (schema, source_id))

    first_projected = Event()
    release_first = Event()

    class PausingProjector(PostgresSourceStateProjector):
        def _project_event(self, conn, event, now):
            entry = super()._project_event(conn, event, now)
            first_projected.set()
            if not release_first.wait(5):
                raise TimeoutError("projector concurrency fixture timed out")
            return entry

    specs = (TableSpec(schema, "tasks", "unmapped_source", "main_pipeline"),)
    first = PausingProjector(store, outbox_schema=schema, table_specs=specs)
    second = PostgresSourceStateProjector(store, outbox_schema=schema, table_specs=specs)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_run = executor.submit(first.run_once)
        assert first_projected.wait(5)
        delayed_transaction.__exit__(None, None, None)
        delayed_context.__exit__(None, None, None)
        second_run = executor.submit(second.run_once)
        release_first.set()
        assert first_run.result(timeout=10)["read"] == 1
        assert second_run.result(timeout=10)["read"] == 1
    item_id = f"task:x_capture:late-{source_id}"
    detail = store.detail(item_id)
    assert detail is not None
    assert detail["status"] == "published"
    assert detail["source_revision"] == 2
    projected_events = [event for event in detail["timeline"] if event["event_id"].startswith("source-state:")]
    assert len(projected_events) == 2
    assert [event["status"] for event in projected_events] == ["writing", "published"]


def test_postgres_x_capture_attempts_project_to_latest_account_snapshot(postgres_information_store) -> None:
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.projector import TableSpec

    store = postgres_information_store
    schema = "test_xattempt_" + uuid.uuid4().hex[:10]
    with store.connect() as conn:
        qualified = Identifier(schema).as_string(conn)
        conn.execute(f"CREATE SCHEMA {qualified}")
        conn.execute(f"CREATE TABLE {qualified}.x_capture_attempts(id bigint PRIMARY KEY,username_lower text,status text,source text,started_at timestamptz,finished_at timestamptz,error text)")
        conn.execute(f"CREATE TABLE {qualified}.odaily_source_state_outbox(event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,source_schema text NOT NULL,source_table text NOT NULL,source_row_id text NOT NULL,operation text NOT NULL,payload jsonb NOT NULL,occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),projected_at timestamptz)")
        conn.execute(f"INSERT INTO {qualified}.x_capture_attempts VALUES (1,'alice','fetch_failed','fxtwitter',now()-interval '2 minutes',now()-interval '1 minute','temporary upstream failure'),(2,'alice','success','fxtwitter',now()-interval '20 seconds',now()-interval '10 seconds',NULL)")
        conn.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload,occurred_at) VALUES (%s,'x_capture_attempts','1','INSERT','{{\"id\":1,\"username_lower\":\"alice\",\"status\":\"fetch_failed\"}}'::jsonb,now()-interval '2 minutes'),(%s,'x_capture_attempts','2','INSERT','{{\"id\":2,\"username_lower\":\"alice\",\"status\":\"success\"}}'::jsonb,now()-interval '10 seconds')", (schema, schema))

    spec = TableSpec(schema, "x_capture_attempts", "x", "x_capture")
    projector = PostgresSourceStateProjector(store, outbox_schema=schema, table_specs=(spec,))
    assert projector.run_once()["read"] == 2
    with store.connect() as conn:
        projected_rows = conn.execute("SELECT item_id,business_key,source,source_id,source_table,created_at_ms,updated_at_ms,status FROM system_information_items").fetchall()
    page = store.query({"view": "diagnostic", "business": "x", "limit": 10})
    accounts = [item for item in page["items"] if item["source_table"] == "x_capture_attempts"]
    assert len(accounts) == 1, (page, projected_rows)
    assert accounts[0]["item_id"] == "main:x_capture_attempts:alice"
    assert accounts[0]["source_id"] == "alice"
    assert accounts[0]["status"] == "success"
    assert accounts[0]["result"] == "capture_success"
    detail = store.detail(accounts[0]["item_id"])
    assert detail is not None
    assert len([event for event in detail["timeline"] if event["event_type"] == "source_state_change"]) == 1
    with store.connect() as conn:
        identities = conn.execute("SELECT source_row_id,item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table='x_capture_attempts' ORDER BY source_row_id", (schema,)).fetchall()
        assert [(row["source_row_id"], row["item_id"]) for row in identities] == [("1", "main:x_capture_attempts:alice"), ("2", "main:x_capture_attempts:alice")]

    # A replay of an older source attempt cannot extend or roll back the timeline.
    older = dict(_snapshot("ignored"))
    older.update({"item_id": "ignored", "source_schema": schema, "source_table": "x_capture_attempts",
                  "source_identity_row_id": "1", "source_row_id": "1", "source_id": "alice",
                  "source_database": schema, "updated_at_ms": accounts[0]["updated_at_ms"] - 60_000,
                  "status": "fetch_failed", "result": "failed", "stage": "采集", "business_key": "x",
                  "source": "x_capture", "title": "alice", "source_name": "alice"})
    older_entry = {"snapshot": older, "event_id": f"old-attempt-replay:{uuid.uuid4().hex}", "stage": "采集",
                   "status": "fetch_failed", "result": "failed", "happened_at_ms": older["updated_at_ms"],
                   "summary": "temporary upstream failure", "evidence": {"error": "temporary upstream failure"},
                   "details": {"source_row_id": "1"}, "skip_timeline": True}
    store.apply_source_audit_batch([older_entry], watermark_key="test_x_attempt_replay", watermark=1)
    current = store.detail("main:x_capture_attempts:alice")
    assert current is not None and current["status"] == "success" and current["result"] == "capture_success"
    assert len([event for event in current["timeline"] if event["event_type"] == "source_state_change"]) == 1
    with store.connect() as conn:
        conn.execute("DROP SCHEMA " + Identifier(schema).as_string(conn) + " CASCADE")


def test_imported_source_identity_conflict_is_skipped_without_losing_items(postgres_information_store) -> None:
    store = postgres_information_store
    identity = str(91_000 + int(uuid.uuid4().hex[:4], 16))
    item_a = f"identity-a:{uuid.uuid4().hex}"
    item_b = f"identity-b:{uuid.uuid4().hex}"
    imported = []
    for item_id in (item_a, item_b):
        snapshot = _snapshot(item_id)
        snapshot.update({"source_table": "hl_news_dispatch", "source_row_id": identity,
                         "source_database": "/data/OdAIly/hyperliquid/hyperliquid.sqlite"})
        assert store.upsert_snapshot(snapshot, {"history": item_id})
        imported.append({"source_table": "hl_news_dispatch", "source_row_id": identity,
                         "source_database": "/data/OdAIly/hyperliquid/hyperliquid.sqlite",
                         "item_id": item_id})

    skipped = set()
    source_map = {"/data/OdAIly/hyperliquid/hyperliquid.sqlite": "hyperliquid_news"}
    assert store.register_imported_source_identities(
        imported[:1], source_schema_map=source_map, skip_identities=skipped,
    ) == 1
    assert store.register_imported_source_identities(
        imported[1:], source_schema_map=source_map, skip_identities=skipped,
    ) == 0
    assert skipped == {("hyperliquid_news", "hl_news_dispatch", identity)}
    assert store.resolve_source_item_id("hyperliquid_news", "hl_news_dispatch", identity, "fallback") == "fallback"
    assert store.has_item(item_a) and store.has_item(item_b)
def test_postgres_external_observation_cleanup_counts_and_redacts_old_errors(postgres_information_store) -> None:
    store = postgres_information_store
    item_id = f"pg-store-test:{uuid.uuid4().hex}"
    assert store.upsert_snapshot(_snapshot(item_id), None)
    assert store.append_event(
        item_id=item_id,
        event_id=f"{item_id}:external",
        event_type="stage_attempt",
        stage="writing",
        status="failed",
        result="failed",
        happened_at_ms=1_800_000_000_000,
        summary="HTTP 429 rate limit",
        reason="HTTP 429 rate limit",
        error_code="HTTP_429",
        evidence={"error": "HTTP 429 rate limit", "request_id": "secret"},
    )
    with store.connect() as conn:
        conn.execute("UPDATE system_information_events SET happened_at_ms=1700000000000 WHERE event_id=%s", (f"{item_id}:external",))
    result = store.cleanup_external_errors(now_ms=1_800_000_000_000, limit=50)
    assert result["cleared"] == 1
    assert result["counted"] == 1
    detail = store.detail(item_id)
    assert detail is not None
    event = next(row for row in detail["timeline"] if row["event_id"] == f"{item_id}:external")
    assert event["summary"] == ""
    assert event["reason"] is None
    assert "HTTP 429" not in str(event["evidence"])
    assert any(row["category"] == "upstream_auth" and row["occurrences"] == 1 for row in store.external_error_totals())
