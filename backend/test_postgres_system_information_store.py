from __future__ import annotations

import os
import time
import uuid
from threading import Event, Thread
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


def test_business_source_trigger_tracks_changes_but_not_polling_or_deletes(postgres_information_store):
    from packages.system_observability.postgres_schema import SourceStateTriggerSpec
    from packages.system_observability.postgres_sources import ensure_business_source_triggers

    store = postgres_information_store
    schema = store.schema
    table = Identifier(schema, "business_message_inbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {table} (id bigint PRIMARY KEY, status text, updated_at timestamptz)")
    spec = SourceStateTriggerSpec(schema, "business_message_inbox", ("id",), ("status",), "trg_feed_test")
    ensure_business_source_triggers(specs=(spec,))
    ensure_business_source_triggers(specs=(spec,))
    with store.connect() as conn:
        conn.execute(f"INSERT INTO {table} VALUES (1, 'pending', now())")
        conn.execute(f"UPDATE {table} SET updated_at=now() WHERE id=1")
        conn.execute(f"UPDATE {table} SET status='done' WHERE id=1")
        conn.execute(f"DELETE FROM {table} WHERE id=1")
        rows = conn.execute("SELECT operation,payload FROM main.odaily_source_state_outbox WHERE source_schema=%s AND source_table='business_message_inbox' ORDER BY event_id", (schema,)).fetchall()
        assert [(row["operation"], row["payload"]) for row in rows] == [("INSERT", {"status": "pending"}), ("UPDATE", {"status": "done"})]
        conn.execute("DELETE FROM main.odaily_source_state_outbox WHERE source_schema=%s", (schema,))


def test_source_outbox_cleanup_preserves_pending_and_recent(postgres_information_store):
    from packages.system_observability.postgres_schema import prune_projected_source_events

    store = postgres_information_store
    relation = Identifier(store.schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {relation} (event_id bigint PRIMARY KEY, occurred_at timestamptz NOT NULL, projected_at timestamptz)")
        conn.execute(f"INSERT INTO {relation} VALUES (1,now()-interval '25 hours',now()-interval '25 hours'), (2,now(),now()), (3,now()-interval '25 hours',NULL), (4,now()-interval '25 hours',NULL)")
        conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('feed_history_reset_event_id','3',%s)", (int(time.time() * 1000),))
        assert prune_projected_source_events(conn, outbox_schema=store.schema, limit=10) == 2
        assert [row["event_id"] for row in conn.execute(f"SELECT event_id FROM {relation} ORDER BY event_id").fetchall()] == [2, 4]


def test_source_outbox_reset_barrier_cleanup_resumes_in_bounded_batches(postgres_information_store):
    from packages.system_observability.postgres_schema import prune_projected_source_events

    store = postgres_information_store
    relation = Identifier(store.schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {relation} (event_id bigint PRIMARY KEY, occurred_at timestamptz NOT NULL, projected_at timestamptz)")
        conn.execute(f"INSERT INTO {relation} VALUES "
                     "(1,now()-interval '30 hours',NULL),(2,now()-interval '29 hours',NULL),"
                     "(3,now()-interval '28 hours',NULL),(4,now(),NULL),(5,now()-interval '30 hours',NULL)")
        conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('feed_history_reset_event_id','3',%s)", (int(time.time() * 1000),))
        assert prune_projected_source_events(conn, outbox_schema=store.schema, limit=2) == 2
        assert [row["event_id"] for row in conn.execute(f"SELECT event_id FROM {relation} ORDER BY event_id").fetchall()] == [3, 4, 5]
        assert prune_projected_source_events(conn, outbox_schema=store.schema, limit=2) == 1
        assert [row["event_id"] for row in conn.execute(f"SELECT event_id FROM {relation} ORDER BY event_id").fetchall()] == [4, 5]


def test_source_outbox_cleanup_skips_rows_locked_by_active_writer(postgres_information_store):
    from packages.system_observability.postgres_schema import prune_projected_source_events

    store = postgres_information_store
    relation = Identifier(store.schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {relation} (event_id bigint PRIMARY KEY, occurred_at timestamptz NOT NULL, projected_at timestamptz)")
        conn.execute(f"INSERT INTO {relation} VALUES (1,now()-interval '30 hours',NULL),(2,now()-interval '29 hours',NULL),(3,now()-interval '28 hours',NULL)")
        conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('feed_history_reset_event_id','3',%s)", (int(time.time() * 1000),))
    with store.connect() as locked:
        with locked.transaction():
            locked.execute(f"SELECT event_id FROM {relation} WHERE event_id=1 FOR UPDATE")
            with store.connect() as cleanup:
                assert prune_projected_source_events(cleanup, outbox_schema=store.schema, limit=2) == 2
            assert [row["event_id"] for row in locked.execute(f"SELECT event_id FROM {relation} ORDER BY event_id").fetchall()] == [1]


def test_projection_cutover_discards_feed_and_review_history_preserving_business_cases(postgres_information_store):
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector

    store = postgres_information_store
    schema = store.schema
    relation = Identifier(schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_items').as_string()}(item_key text PRIMARY KEY)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_items').as_string()} VALUES('preserve-me')")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_decisions').as_string()}(id bigint PRIMARY KEY,item_key text)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_decisions').as_string()} VALUES(1,'preserve-me')")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_analysis_attempts').as_string()}(id bigint PRIMARY KEY,item_key text,raw_response_ref text,status text)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_analysis_attempts').as_string()} VALUES(1,'preserve-me',NULL,'completed')")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_runs').as_string()}(run_key text PRIMARY KEY)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_runs').as_string()} VALUES('old-run')")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_state').as_string()}(state_key text PRIMARY KEY,state_value text,updated_at text)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_state').as_string()} VALUES('config','keep','now')")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_usage').as_string()}(usage_date text PRIMARY KEY,calls integer)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_usage').as_string()} VALUES('today',3)")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'system_review_channel_observations').as_string()}(component text,worker_id text,last_bucket text,consecutive_failures integer)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_review_channel_observations').as_string()} VALUES('worker','one','bucket',2)")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'ai_text_self_upgrade_items').as_string()}(case_key text PRIMARY KEY)")
        conn.execute(f"INSERT INTO {Identifier(schema, 'ai_text_self_upgrade_items').as_string()} VALUES('preserve-upgrade')")
        conn.execute("CREATE TABLE IF NOT EXISTS main.x_processing_attempts(id bigint PRIMARY KEY,status text,response_ref text)")
        conn.execute("CREATE TABLE IF NOT EXISTS main.x_processing_attempt_events(id bigint PRIMARY KEY,status text,response_ref text)")
        conn.execute("TRUNCATE main.x_processing_attempts,main.x_processing_attempt_events")
        conn.execute(f"CREATE TABLE {relation} (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text, source_table text, source_row_id text, operation text, payload jsonb, occurred_at timestamptz DEFAULT now(), projected_at timestamptz)")
        conn.execute(f"INSERT INTO {relation}(source_schema,source_table,source_row_id,operation,payload,occurred_at) VALUES ('main','test_jobs','1','INSERT','{{}}',now())")
    evidence_ref = store.save_evidence({"response": "business-owned response"})
    with store.connect() as conn:
        conn.execute("INSERT INTO main.x_processing_attempts VALUES(101,'succeeded',%s)", (evidence_ref,))
        conn.execute("INSERT INTO main.x_processing_attempt_events VALUES(101,'succeeded',%s)", (evidence_ref,))
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_information_evidence_pins').as_string()} VALUES('old-pin',%s,%s)", (evidence_ref, int(time.time() * 1000)))
        conn.execute(f"INSERT INTO {Identifier(schema, 'system_information_decision_samples').as_string()} VALUES('sample','2026-11-20',%s,%s,'cutover-item','x','x','id','writing','published','completed','','v1',%s,1)", (int(time.time() * 1000), int(time.time() * 1000) + 86_400_000, evidence_ref))
    store.upsert_snapshot({**_snapshot("cutover-item"), "source_table": "test_jobs", "status": "write_failed", "result": "failed", "evidence_ref": evidence_ref}, {"kept": True})
    projector = PostgresSourceStateProjector(store, outbox_schema=schema)
    result = projector.reset_projection_at_cutover()
    assert result == {"items_removed": 1, "pending_events_skipped": 1, "pins_removed": 1, "decision_samples_removed": 1,
                      "review_items_removed": 1, "review_decisions_removed": 1, "review_attempts_removed": 1, "review_runs_removed": 1}
    assert not store.has_item("cutover-item")
    with store.connect() as conn:
        assert conn.execute(f"SELECT projected_at IS NULL AS pending FROM {relation} WHERE event_id=1").fetchone()["pending"]
        assert conn.execute("SELECT count(*) AS n FROM system_information_items").fetchone()["n"] == 0
        assert conn.execute(f"SELECT count(*) AS n FROM {Identifier(schema, 'system_review_items').as_string()}").fetchone()["n"] == 0
        assert conn.execute(f"SELECT count(*) AS n FROM {Identifier(schema, 'system_review_decisions').as_string()}").fetchone()["n"] == 0
        assert conn.execute(f"SELECT count(*) AS n FROM {Identifier(schema, 'system_review_analysis_attempts').as_string()}").fetchone()["n"] == 0
        assert conn.execute(f"SELECT count(*) AS n FROM {Identifier(schema, 'system_review_runs').as_string()}").fetchone()["n"] == 0
        assert conn.execute(f"SELECT state_value FROM {Identifier(schema, 'system_review_state').as_string()} WHERE state_key='config'").fetchone()["state_value"] == "keep"
        assert conn.execute(f"SELECT calls FROM {Identifier(schema, 'system_review_usage').as_string()} WHERE usage_date='today'").fetchone()["calls"] == 3
        assert conn.execute(f"SELECT consecutive_failures FROM {Identifier(schema, 'system_review_channel_observations').as_string()} WHERE worker_id='one'").fetchone()["consecutive_failures"] == 2
        assert conn.execute(f"SELECT count(*) AS n FROM {Identifier(schema, 'ai_text_self_upgrade_items').as_string()} WHERE case_key='preserve-upgrade'").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM main.x_processing_attempts WHERE response_ref=%s", (evidence_ref,)).fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM system_information_evidence WHERE evidence_ref=%s", (evidence_ref,)).fetchone()["n"] == 1
    assert store.load_evidence(evidence_ref) == {"response": "business-owned response"}
    feed_ref = store.save_evidence({"feed-only": "discard after reset"})
    gc = store.prune_feed_history_evidence(limit=100)
    assert gc["removed"] >= 1
    assert store.load_evidence(evidence_ref) == {"response": "business-owned response"}
    with store.connect() as conn:
        assert conn.execute("SELECT 1 FROM system_information_evidence WHERE evidence_ref=%s", (feed_ref,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM small_evidence WHERE evidence_ref=%s", (feed_ref,)).fetchone() is None


def test_projection_cutover_reprojects_events_committed_after_barrier(postgres_information_store):
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector

    store = postgres_information_store
    schema = store.schema
    relation = Identifier(schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"DELETE FROM {Identifier(schema, 'system_information_events').as_string()}")
        conn.execute(f"CREATE TABLE {relation} (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text, source_table text, source_row_id text, operation text, payload jsonb, occurred_at timestamptz DEFAULT now(), projected_at timestamptz)")
        conn.execute(f"CREATE TABLE {Identifier(schema, 'test_jobs').as_string()} (id bigint PRIMARY KEY, status text, updated_at timestamptz DEFAULT now())")
        conn.execute(f"INSERT INTO {Identifier(schema, 'test_jobs').as_string()} VALUES (1,'ready')")
        conn.execute(f"INSERT INTO {relation}(source_schema,source_table,source_row_id,operation,payload,occurred_at) VALUES (%s,'test_jobs','1','UPDATE','{{}}',now())", (schema,))
    store.upsert_snapshot({**_snapshot("before-cutover"), "source_table": "test_jobs", "status": "published", "result": "completed"}, {})
    projector = PostgresSourceStateProjector(store, outbox_schema=schema)
    projector.reset_projection_at_cutover()
    with store.connect() as conn:
        conn.execute(f"INSERT INTO {relation}(source_schema,source_table,source_row_id,operation,payload,occurred_at) VALUES (%s,'test_jobs','1','UPDATE','{{}}',now())", (schema,))
    projector.specs[(schema, "test_jobs")] = __import__("packages.system_observability.projector", fromlist=["TableSpec"]).TableSpec(schema, "test_jobs", "x", "x_capture")
    result = projector.run_once()
    assert result["read"] == 1
    with store.connect() as conn:
        assert conn.execute("SELECT status FROM system_information_items WHERE item_id='before-cutover'").fetchone() is None
        assert conn.execute("SELECT count(*) AS n FROM system_information_items WHERE source_table='test_jobs'").fetchone()["n"] == 1


def test_projection_cutover_waits_for_delayed_outbox_writer(postgres_information_store):
    from packages.common.pg_native import connect_postgres
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    store = postgres_information_store
    schema = store.schema
    relation = Identifier(schema, "odaily_source_state_outbox").as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {relation} (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text, source_table text, source_row_id text, operation text, payload jsonb, occurred_at timestamptz DEFAULT now(), projected_at timestamptz)")
    writer_open = Event()
    release_writer = Event()

    def delayed_writer():
        with connect_postgres(schema="main", application_name="test-delayed-outbox-writer") as conn:
            with conn.transaction():
                conn.execute(f"INSERT INTO {relation}(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'test_jobs','1','UPDATE','{{}}')", (schema,))
                writer_open.set()
                assert release_writer.wait(3)

    writer = Thread(target=delayed_writer, daemon=True)
    writer.start()
    assert writer_open.wait(3)
    projector = PostgresSourceStateProjector(store, outbox_schema=schema)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(projector.reset_projection_at_cutover)
        time.sleep(0.15)
        assert not future.done()
        release_writer.set()
        result = future.result(timeout=3)
    writer.join(timeout=3)
    assert result["pending_events_skipped"] == 1
    with store.connect() as conn:
        assert conn.execute(f"SELECT state_value FROM system_information_state WHERE state_key='feed_history_reset_event_id'").fetchone()["state_value"] == "1"


def test_projection_cutover_resumes_after_interrupted_batch(postgres_information_store):
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector

    store = postgres_information_store
    store.upsert_snapshot(_snapshot("resume-a"), {})
    store.upsert_snapshot(_snapshot("resume-b"), {})
    with store.connect() as conn:
        conn.execute("INSERT INTO system_information_state(state_key,state_value,updated_at_ms) VALUES('feed_history_reset_in_progress','1',%s),('feed_reset_system_information_items','1',%s)", (int(time.time() * 1000), int(time.time() * 1000)))
        conn.execute("DELETE FROM system_information_items WHERE item_id='resume-a'")
    projector = PostgresSourceStateProjector(store)
    result = projector.run_once()
    assert result == {"read": 0, "changed": 0}
    assert store.get_state("feed_history_reset_in_progress", "0") == "0"
    assert store.get_state("feed_reset_system_information_items") == "2"
    assert not store.has_item("resume-b")


def test_feed_evidence_gc_batches_and_preserves_business_references(postgres_information_store):
    store = postgres_information_store
    protected = store.save_evidence({"response": "business reference"})
    orphaned = [store.save_evidence({"orphan": index}) for index in range(12)]
    with store.connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS main.x_processing_attempts(id bigint PRIMARY KEY,status text,response_ref text)")
        conn.execute("INSERT INTO main.x_processing_attempts VALUES(9901,'succeeded',%s)", (protected,))
    first = store.prune_feed_history_evidence(limit=5)
    assert first["scanned"] == 5
    second = store.prune_feed_history_evidence(limit=20)
    assert second["removed"] >= 7
    assert store.load_evidence(protected) == {"response": "business reference"}
    with store.connect() as conn:
        remaining = conn.execute("SELECT count(*) AS n FROM system_information_evidence WHERE evidence_ref=ANY(%s)", (orphaned,)).fetchone()["n"]
        assert remaining == 0


def test_external_evidence_file_gc_queue_recovers_after_interruption(postgres_information_store):
    store = postgres_information_store
    import hashlib
    ref = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    path = store.evidence_dir / ref[:2] / f"{ref}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"orphan")
    with store.connect() as conn:
        conn.execute("INSERT INTO system_information_evidence_file_gc(evidence_ref,path,queued_at_ms) VALUES(%s,%s,%s)", (ref, str(path), int(time.time() * 1000)))
    assert store._drain_evidence_file_gc(limit=10) == 1, store.get_state("evidence_file_gc_error")
    assert not path.exists()
    with store.connect() as conn:
        assert conn.execute("SELECT 1 FROM system_information_evidence_file_gc WHERE evidence_ref=%s", (ref,)).fetchone() is None

    protected = store.save_evidence({"response": "still referenced"})
    protected_path = store.evidence_dir / protected[:2] / f"{protected}.json.gz"
    with store.connect() as conn:
        conn.execute("INSERT INTO system_information_evidence_file_gc(evidence_ref,path,queued_at_ms) VALUES(%s,%s,%s)", (protected, str(protected_path), int(time.time() * 1000)))
    store._drain_evidence_file_gc(limit=10)
    assert store.load_evidence(protected) == {"response": "still referenced"}
    assert protected_path.exists()


def test_projection_cutover_removes_historical_pins_and_samples(postgres_information_store):
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector

    store = postgres_information_store
    with store.connect() as conn:
        conn.execute("INSERT INTO system_information_evidence(evidence_ref,path,byte_size,created_at_ms) VALUES(%s,%s,1,%s)", ("a" * 64, "postgres://fixture", int(time.time() * 1000)))
    snapshot = {**_snapshot("pinned-cutover"), "evidence_ref": "a" * 64}
    store.upsert_snapshot(snapshot, {})
    with store.connect() as conn:
        conn.execute("INSERT INTO system_information_evidence_pins VALUES('fixture-pin',%s,%s)", ("a" * 64, int(time.time() * 1000)))
    projector = PostgresSourceStateProjector(store)
    result = projector.reset_projection_at_cutover()
    assert result["items_removed"] == 1
    assert not store.has_item("pinned-cutover")
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM system_information_evidence_pins").fetchone()["n"] == 0


def test_business_inbox_postgres_schema_keeps_business_mapping():
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector

    projector = PostgresSourceStateProjector(object())
    assert projector.specs[("business_news", "business_message_inbox")].business_key == "business_news"


def test_source_projection_keeps_distinct_rows_and_supplies_missing_times(postgres_information_store, monkeypatch):
    from packages.system_observability.postgres_projector import PostgresSourceStateProjector
    from packages.system_observability.projector import TableSpec
    from packages.system_observability.postgres_schema import SourceStateTriggerSpec
    from packages.system_observability import postgres_sources

    store = postgres_information_store
    schema = store.schema
    qualified = Identifier(schema).as_string()
    with store.connect() as conn:
        conn.execute(f"CREATE TABLE {qualified}.test_jobs (id bigint PRIMARY KEY, source_item_id text, status text, raw_payload text)")
        conn.execute(f"CREATE TABLE {qualified}.odaily_source_state_outbox (event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source_schema text NOT NULL, source_table text NOT NULL, source_row_id text NOT NULL, operation text NOT NULL, payload jsonb NOT NULL, occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(), projected_at timestamptz)")
        conn.execute(f"INSERT INTO {qualified}.test_jobs VALUES (1,'same','pending','large raw data'),(2,'same','done','large raw data')")
        conn.execute(f"INSERT INTO {qualified}.odaily_source_state_outbox(source_schema,source_table,source_row_id,operation,payload) VALUES (%s,'test_jobs','1','INSERT','{{}}'),(%s,'test_jobs','2','INSERT','{{}}')", (schema, schema))
    store.set_state("postgres_retention_last_ms", int(time.time() * 1000))
    monkeypatch.setattr(postgres_sources, "BUSINESS_SOURCES", (SourceStateTriggerSpec(schema, "test_jobs", ("id",), ("status",)),))
    projector = PostgresSourceStateProjector(store, outbox_schema=schema, table_specs=(TableSpec(schema, "test_jobs", "meme", "meme_scanner"),))
    assert projector.run_once()["read"] == 2
    with store.connect() as conn:
        rows = conn.execute("SELECT item_id,created_at_ms,updated_at_ms,status FROM system_information_items WHERE source_table='test_jobs' ORDER BY item_id").fetchall()
    assert len(rows) == 2
    assert {row["status"] for row in rows} == {"pending", "done"}
    assert all(row["created_at_ms"] and row["updated_at_ms"] for row in rows)
    detail = store.detail(rows[0]["item_id"])
    assert "raw_payload" not in detail["evidence"]["current_row"]


def test_information_retention_removes_old_feed_copies_regardless_of_legacy_protection(postgres_information_store):
    store = postgres_information_store
    now = int(time.time() * 1000)
    old = now - 30 * 60 * 60 * 1000
    recent = now - 60 * 1000
    for name, stamp in (("old", old), ("old-no-time", old), ("recent", recent), ("sampled", old), ("pinned", old), ("reviewed", old)):
        snapshot = _snapshot(name)
        snapshot.update(created_at_ms=stamp, updated_at_ms=stamp, task_id=None, source_row_id=name)
        store.upsert_snapshot(snapshot, {"name": name})
    store.append_event(item_id="old", event_id="old-event", event_type="state", stage="write", status="done", result="done", happened_at_ms=old, summary="old")
    with store.connect() as conn:
        conn.execute("UPDATE system_information_items SET updated_at_ms=NULL,observed_at_ms=%s WHERE item_id='old-no-time'", (old,))
        conn.execute("INSERT INTO system_information_decision_samples(sample_key,sample_date,sampled_at_ms,raw_expires_at_ms,item_id,business_key,source,stage,status,result) VALUES ('sample','today',%s,%s,'sampled','x','x','write','done','done')", (now, now + 1))
        conn.execute("INSERT INTO system_information_evidence_pins(pin_key,evidence_ref,pinned_at_ms) SELECT 'pin',evidence_ref,%s FROM system_information_items WHERE item_id='pinned'", (now,))
        conn.execute("CREATE TABLE system_review_items(scope_json text,evidence_json text,counter_evidence_json text)")
        conn.execute("INSERT INTO system_review_items VALUES (%s,'[]','[]')", ('{"stream_item_ids":["reviewed"]}',))
    result = store.trim_information_items(now_ms=now, limit=100)
    assert result["removed"] == 5
    assert not store.has_item("old")
    assert not store.has_item("old-no-time")
    assert store.has_item("recent")
    assert all(not store.has_item(name) for name in ("sampled", "pinned", "reviewed"))
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM system_information_events WHERE item_id='old'").fetchone()["n"] == 0


def test_postgres_task_evidence_retention_waits_five_days(postgres_information_store):
    store = postgres_information_store
    now = int(time.time() * 1000)
    created = now - 4 * 86_400_000
    schema = "test_task_source_" + uuid.uuid4().hex[:10]
    with store.connect() as conn:
        qualified = Identifier(schema).as_string(conn)
        conn.execute(f"CREATE SCHEMA {qualified}")
        conn.execute(f"CREATE TABLE {qualified}.tasks(id bigint PRIMARY KEY,content text,raw_payload jsonb)")
        conn.execute(f"INSERT INTO {qualified}.tasks VALUES(17,'','{{}}')")
    snapshot = _snapshot("recent-task-evidence")
    snapshot.update(source_database=schema, source_table="tasks", source_row_id="17", status="published", result="direct_publish", evidence_ref=None,
                    created_at_ms=created, updated_at_ms=created)
    store.upsert_snapshot(snapshot, {"task": {"content": "source copy"}, "pipeline": {"search_result": {"duplicate": False}}})
    result = store.trim_task_evidence(now_ms=now)
    assert result["changed"] == 0
    assert store.detail("recent-task-evidence")["evidence"]["task"]["content"] == "source copy"


def test_postgres_feed_retention_removes_expired_items_including_active_states(postgres_information_store):
    store = postgres_information_store
    now = int(time.time() * 1000)
    old = now - 7 * 60 * 60 * 1000
    snapshot = _snapshot("expired-active-feed")
    snapshot.update(status="processing", result="processing", created_at_ms=old, updated_at_ms=old)
    store.upsert_snapshot(snapshot, {"detail": "feed-only"})
    result = store.trim_information_items(now_ms=now)
    assert result["removed"] == 1
    assert not store.has_item("expired-active-feed")


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


def test_postgres_query_uses_account_snapshot_over_imported_attempts(postgres_information_store) -> None:
    store = postgres_information_store
    now = int(time.time() * 1000)
    for item_id, stamp in (("main:x_capture_attempts:alice", now - 2000), ("legacy-attempt", now - 1000)):
        snapshot = _snapshot(item_id)
        snapshot.update(source_table="x_capture_attempts", source_id="alice", source_row_id=item_id,
                        task_id=None, created_at_ms=stamp, updated_at_ms=stamp)
        store.upsert_snapshot(snapshot, None)
    result = store.query({"view": "diagnostic"})
    assert [item["item_id"] for item in result["items"]] == ["main:x_capture_attempts:alice"]


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
    assert [event for event in detail["timeline"] if event["event_type"] == "source_state_change"] == []
    with store.connect() as conn:
        identities = conn.execute("SELECT source_row_id,item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table='x_capture_attempts' ORDER BY source_row_id", (schema,)).fetchall()
        assert [(row["source_row_id"], row["item_id"]) for row in identities] == [("alice", "main:x_capture_attempts:alice")]

    # A replay of an older source attempt cannot extend or roll back the timeline.
    older = dict(_snapshot("ignored"))
    older.update({"item_id": "ignored", "source_schema": schema, "source_table": "x_capture_attempts",
                  "source_identity_row_id": "alice", "source_row_id": "1", "source_id": "alice",
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
    assert [event for event in current["timeline"] if event["event_type"] == "source_state_change"] == []
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
