from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def isolated_pg(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL store checks")
    database_name = f"odaily_remaining_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name))
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    yield
    from packages.common.pg_runtime import close_postgres_pools
    close_postgres_pools()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_x_capture_attempts_are_projected_without_retention_delete_events(isolated_pg):
    from packages.x_capture.repository import PostgresXCaptureRepository

    PostgresXCaptureRepository.initialize_schema()
    with psycopg.connect(os.environ["ODAILY_POSTGRES_DSN"]) as conn:
        conn.execute("INSERT INTO main.x_capture_attempts(username_lower,status,started_at,finished_at) VALUES('alice','success',now(),now())")
        row = conn.execute("SELECT operation,payload FROM main.odaily_source_state_outbox WHERE source_table='x_capture_attempts'").fetchone()
        assert row == ('INSERT', {'username_lower': 'alice', 'status': 'success', 'source': 'fxtwitter', 'candidate_count': 0, 'seeded_count': 0, 'new_count': 0, 'saved_count': 0, 'error': None, 'started_at': row[1]['started_at'], 'finished_at': row[1]['finished_at']})
        conn.execute("DELETE FROM main.x_capture_attempts WHERE username_lower='alice'")
        assert conn.execute("SELECT count(*) FROM main.odaily_source_state_outbox WHERE source_table='x_capture_attempts'").fetchone()[0] == 1


def test_msx_native_store_preserves_seen_tasks_and_heartbeat(isolated_pg):
    from packages.postgres_msx_notice_store import PostgresMSXNoticeStore

    store = PostgresMSXNoticeStore()
    store.ensure_state()
    assert store.is_unseeded()
    store.seed(["msx:old"])
    assert not store.is_unseeded()
    assert store.unseen(["msx:old", "msx:new"]) == {"msx:new"}
    assert store.mark_seen("msx:new", False)
    assert not store.mark_seen("msx:new", False)
    task_id = store.save_task(source_item_id="msx:new", source_url="https://msx.com/a", title="公告", content="正文",
        published_at=datetime.now(UTC), raw={"id": 2}, metadata={"site_key": "msx_notice_center"})
    store.record_heartbeat(worker_id="msx-test", status="ok", success=True, error=None, metadata={"saved_count": 1})
    with psycopg.connect(os.environ["ODAILY_POSTGRES_DSN"]) as conn:
        assert conn.execute("SELECT source_item_id FROM main.tasks WHERE id=%s", (task_id,)).fetchone()[0] == "msx:new"
        assert conn.execute("SELECT status FROM main.pipeline_worker_heartbeats WHERE worker_id='msx-test'").fetchone()[0] == "ok"


def test_failure_diagnostics_reads_native_task_queue_and_worker_state(isolated_pg):
    from packages.failure_diagnostics import PostgresFailureDiagnosticsStore
    from packages.postgres_msx_notice_store import PostgresMSXNoticeStore

    task_store = PostgresMSXNoticeStore()
    task_id = task_store.save_task(
        source_item_id="diagnostic-1", source_url="https://example.test/a", title="Failure",
        content="body", published_at=datetime.now(UTC), raw={}, metadata={},
    )
    with psycopg.connect(os.environ["ODAILY_POSTGRES_DSN"]) as conn:
        conn.execute("UPDATE main.tasks SET status='search_failed' WHERE id=%s", (task_id,))
        conn.execute(
            "INSERT INTO main.x_task_pipeline(task_id,last_error) VALUES(%s,%s)",
            (task_id, "OpenAI request failed: model=gpt-6-luna timeout_seconds=90 error=Read timed out"),
        )
        conn.execute(
            "INSERT INTO main.pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,metadata) "
            "VALUES('local_pipeline','diag-worker','running',now(),now(),'{}'::jsonb)"
        )
        conn.execute("CREATE SCHEMA local_pipeline")
        conn.execute("""CREATE TABLE local_pipeline.local_pipeline_jobs(
            id bigint PRIMARY KEY,job_type text,task_id bigint,source text,source_item_id text,status text,
            attempt_count int,last_error text,next_attempt_at timestamptz,created_at timestamptz,updated_at timestamptz,
            storage_epoch text)""")
        conn.execute("""INSERT INTO local_pipeline.local_pipeline_jobs VALUES
            (19,'write_flow',%s,'msx_notice','diagnostic-1','exhausted',3,NULL,now(),now(),now(),'test-epoch')""", (task_id,))

    result = PostgresFailureDiagnosticsStore(Path("unused"), Path("unused"), storage_epoch="test-epoch").get_task(task_id)
    assert result is not None
    assert result["diagnosis"]["code"] == "ai_request_timeout"
    assert result["diagnosis"]["evidence"]["model"] == "gpt-6-luna"
    assert result["queue"]["id"] == 19
    assert result["worker"]["worker_id"] == "diag-worker"


def test_hyperliquid_native_test_case_store_preserves_isolated_preview_rows(isolated_pg):
    from packages.hyperliquid_news.postgres_store import PostgresNewsStore
    from packages.hyperliquid_news.test_cases import clear_test_cases, save_cases
    from packages.common.storage import load_hyperliquid_storage_path

    store = PostgresNewsStore(load_hyperliquid_storage_path())
    event = {
        "key": "test:fixture", "observed_ms": 10, "addresses": ["0x" + "1" * 40],
        "coin": "BTC", "kinds": ["large_reversal"],
        "evidence": {"highlights": [], "actions": {}, "history": {},
                     "test_replay": {"real_address": True}},
    }
    save_cases(store, [event])
    store.save_global("editorial_preview:" + event["key"], {"title": "preview"}, [])
    store.save_global("editorial_preview:live-event", {"title": "live"}, [])
    assert store.test_case_rows()[0]["payload"]["key"] == event["key"]
    assert clear_test_cases(store) == {"cases_removed": 1, "previews_removed": 1}
    assert store.metadata("editorial_preview:live-event", {}) == {"title": "live"}


def test_hyperliquid_observation_commits_decimal_candidate_and_baseline(isolated_pg):
    from decimal import Decimal
    from packages.hyperliquid_news.postgres_store import PostgresNewsStore
    from packages.common.storage import load_hyperliquid_storage_path
    from packages.common.pg_native import connect_postgres

    store = PostgresNewsStore(load_hyperliquid_storage_path())
    address = "0x" + "1" * 40
    candidate = {"key": "decimal-event", "observed_ms": 100, "addresses": [address],
                 "coin": "BTC", "kinds": ["large_reversal"],
                 "evidence": {"threshold_rules": {"minimum": Decimal("123.45")}, "highlights": []}}
    store.commit_observation(address=address, now_ms=100, state={"positions": {}},
                             history={}, coverage={}, fills=[], candidates=[candidate])
    assert store.candidates()[0]["evidence"]["threshold_rules"]["minimum"] == "123.45"
    with connect_postgres(schema="hyperliquid_news", application_name="test-hl-baseline") as conn:
        assert conn.execute("SELECT observed_ms FROM hl_news_state WHERE address=%s", (address,)).fetchone()["observed_ms"] == 100


@pytest.mark.parametrize("handle", [None, "alice"])
def test_hottopic_dismiss_primary_tasks_accepts_optional_handle(isolated_pg, handle):
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_event_tracking import PostgresEventTrackingRepository

    with connect_postgres(schema="main", application_name="test-event-dismiss") as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS main")
        conn.execute("CREATE TABLE tasks (id bigint PRIMARY KEY, source text, status text, metadata jsonb, locked_by text, locked_until timestamptz, updated_at timestamptz)")
        conn.execute("INSERT INTO tasks(id,source,status,metadata) VALUES (1,'event_tracking','queued',%s::jsonb)",
                     ('{"event_tracking":{"event_id":"event-1","candidate_handle":"Alice"}}',))
    result = PostgresEventTrackingRepository().dismiss_primary_tasks(
        event_id="event-1", dismissed_at=datetime.now(UTC).isoformat(), dismissed_by="test", handle=handle)
    assert result["cancelledTaskIds"] == [1]


def test_hyperliquid_preview_uses_native_prompt_repository_without_network(isolated_pg, monkeypatch):
    from packages.common.paths import get_paths
    from packages.hyperliquid_news import test_cases
    from packages.hyperliquid_news.postgres_store import PostgresNewsStore
    from packages.hyperliquid_news import preview as preview_module
    from packages.x_processing.repository import PostgresXProcessingRepository
    from packages.common.storage import load_hyperliquid_storage_path

    repository = PostgresXProcessingRepository()
    repository.initialize_schema()
    repository.seed_prompt_templates(root_dir=get_paths().root_dir)
    with repository._connect() as conn:
        conn.execute(
            "UPDATE prompt_template_versions SET content='outdated prompt' WHERE id=("
            "SELECT active_version_id FROM prompt_templates WHERE template_key='hyperliquid_writer')"
        )
    store = PostgresNewsStore(load_hyperliquid_storage_path())
    event = {
        "key": "test:new-address-preview", "observed_ms": 10, "addresses": ["0x" + "3" * 40],
        "coin": "BTC", "kinds": ["newly_observed_address_position"],
        "evidence": {"highlights": [], "actions": {}, "history": {},
                     "test_replay": {"real_address": True}},
    }
    store.upsert_test_case(event)
    monkeypatch.setattr(test_cases, "enrich_cached_case_records", lambda _store: 0)
    monkeypatch.setattr(test_cases, "enrich_test_account_totals", lambda _store: {"addresses": 0, "enriched": 0, "failed": {}})
    seen = {}

    def preview_stub(*, task, prompt, settings, ai_client, prompt_source):
        seen.update({"prompt": prompt, "prompt_source": prompt_source, "ai_client": ai_client})
        return {"title": "preview", "content": "body", "prompt_version_id": prompt.id}

    monkeypatch.setattr(preview_module, "generate_preview", preview_stub)
    report = test_cases.preview_cases(store, limit=1)

    assert report["generated"] == 1
    assert seen["prompt"].template_key == "hyperliquid_new_address_writer"
    assert seen["prompt"].id > 0
    assert seen["prompt_source"] == "active_prompt"
    assert store.metadata("editorial_preview:" + event["key"], {})["title"] == "preview"


def test_hyperliquid_prompt_activation_uses_postgres_without_sqlite(isolated_pg, monkeypatch, tmp_path):
    from packages.common.paths import get_paths
    from packages.hyperliquid_news.commands import activate_writer_prompt
    from packages.x_processing.repository import PostgresXProcessingRepository

    repository = PostgresXProcessingRepository()
    repository.initialize_schema()
    repository.seed_prompt_templates(root_dir=get_paths().root_dir)
    with repository._connect() as conn:
        conn.execute(
            "UPDATE prompt_template_versions SET content='outdated prompt' WHERE id=("
            "SELECT active_version_id FROM prompt_templates WHERE template_key='hyperliquid_writer')"
        )

    def sqlite_forbidden(*_args, **_kwargs):
        raise AssertionError("PostgreSQL prompt activation must not open SQLite")

    monkeypatch.setattr("packages.common.storage.connect_sqlite", sqlite_forbidden)
    result = activate_writer_prompt(tmp_path / "unused.sqlite", get_paths().root_dir)
    assert result == {"version_number": 2, "changed": True}
    active = repository.get_active_prompt("hyperliquid_writer")
    assert active.version_number == 2
    assert activate_writer_prompt(tmp_path / "unused.sqlite", get_paths().root_dir) == {
        "version_number": 2, "changed": False,
    }


def test_local_pipeline_write_flow_processes_native_postgres_claims(isolated_pg, monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from packages.common.config import ExternalMediaAlertSettings, XProcessingSettings
    from packages.common.paths import get_paths
    from packages.local_pipeline.processor import LocalPipelineProcessor
    from packages.local_pipeline.queue import LocalPipelineJob
    from packages.x_processing.models import PromptTemplateVersion, TaskRecord
    from packages.x_processing.repository import PostgresXProcessingRepository

    repository = PostgresXProcessingRepository()
    repository.initialize_schema()
    repository.seed_prompt_templates(root_dir=get_paths().root_dir)
    task_id = 991_771
    task = TaskRecord(
        id=task_id, source="hyperliquid_news", source_item_id="pg-flow-fixture",
        source_url="https://example.test/fixture", title="Fixture", content="Fixture content",
        published_at=datetime.now(UTC), raw_payload={"key": "fixture", "observed_ms": 1},
        metadata={"writer_template_key": "hyperliquid_writer"},
    )
    with repository._connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id,source,source_item_id,source_url,title,content,published_at,raw_payload,metadata) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (task.id, task.source, task.source_item_id, task.source_url, task.title, task.content,
             task.published_at, repository._Jsonb(task.raw_payload), repository._Jsonb(task.metadata)),
        )
        conn.execute("SELECT setval(pg_get_serial_sequence('tasks','id'),GREATEST(%s,(SELECT max(id) FROM tasks)),true)", (task_id,))

    class StageWorker:
        def __init__(self, stage):
            self.stage = stage
            self.worker_id = f"pg-integration-{stage}"

        def _resolve_task_author_name(self, value):
            return value

        def _expire_task_if_stale(self, _task):
            return False

        def set_claim_context(self, claimed):
            repository.set_claim_context(claimed)

        def process_claimed_task(self, claimed):
            repository.set_claim_context(claimed)
            if self.stage == "search":
                repository.complete_search(claimed.id)
            elif self.stage == "write":
                repository.complete_write(
                    claimed.id,
                    prompt=repository.get_active_prompt("hyperliquid_writer"),
                    model="fixture-model", draft_title="BTC fixture", draft_content="Fixture draft.",
                    raw_output="{}", trace={},
                )
            elif self.stage == "format_publish":
                repository.complete_format_publish(claimed.id, final_title="BTC fixture", final_content="Fixture final.")
            elif self.stage == "publish":
                repository.complete_publish(
                    claimed.id, publisher_channel=None, publisher_model=None, publisher_category=None,
                    publisher_decision="manual_review", publisher_reason_code="hyperliquid_backend_only",
                    publisher_output={"fixture": True}, push_result={}, telegram_result={},
                    decided_at=datetime.now(UTC), status="ready_review",
                )
            return type("Result", (), {"failed": False, "message": "ok"})()

    processor = LocalPipelineProcessor(
        database_url=None, x_settings=XProcessingSettings(), alert_settings=ExternalMediaAlertSettings(),
    )
    monkeypatch.setattr(processor, "_x_worker", lambda stage: StageWorker(stage))
    try:
        result = processor.process(LocalPipelineJob(
            id=1, job_type="write_flow", task_id=task_id, source=task.source,
            source_item_id=task.source_item_id, status="running", attempt_count=1, payload={}, storage_epoch="test",
        ))
        assert result.status == "ready_review"
        pipeline = repository.get_pipeline(task_id)
        assert pipeline.prompt_template_key == "hyperliquid_writer"
        assert pipeline.final_content == "Fixture final."
    finally:
        processor.stop_background_tasks()


def test_hyperliquid_compaction_calculates_outside_lock_and_fences_cursor(isolated_pg, monkeypatch):
    from packages.hyperliquid_news.postgres_store import PostgresNewsStore
    from packages.hyperliquid_news.models import Candidate
    from packages.common.storage import load_hyperliquid_storage_path
    from packages.hyperliquid_news import compact_history

    address = "0x" + "2" * 40
    store = PostgresNewsStore(load_hyperliquid_storage_path())
    fills = [
        {"tid": 1, "time": 10, "coin": "BTC", "startPosition": "0", "sz": "1", "side": "B", "px": "100", "closedPnl": "0", "fee": "0", "feeToken": "USDC"},
        {"tid": 2, "time": 20, "coin": "BTC", "startPosition": "1", "sz": "1", "side": "B", "px": "100", "closedPnl": "0", "fee": "0", "feeToken": "USDC"},
    ]
    store.commit_observation(address=address, now_ms=500000,
        state={"observed_ms": 500000, "history_last_queried_ms": 500000}, history={},
        coverage={"pagination_complete": True}, fills=fills, candidates=[], state_table="hl_news_normal_state")
    original = compact_history.extend_compact
    calls = 0

    def interleaved(data, source_fills, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            store.commit_observation(address=address, now_ms=600000,
                state={"observed_ms": 600000, "history_last_queried_ms": 600000}, history={},
                coverage={"pagination_complete": True}, fills=[], candidates=[], state_table="hl_news_normal_state")
        return original(data, source_fills, **kwargs)

    monkeypatch.setattr(compact_history, "extend_compact", interleaved)
    result = store.compact_fills(address, cutoff_ms=500000)
    assert result["phase"] == "retry"
    assert len(store.fills(address, 0, 500000)) == 2


def test_predict_html_store_claims_and_persists_json(isolated_pg):
    from packages.predict_market_discovery.postgres_html_store import PostgresHtmlDiscoveryStore

    store = PostgresHtmlDiscoveryStore()
    now = datetime.now(UTC)
    store.record_feed("tech", {"nodes": [], "total_count": 0, "has_next_page": False}, now)
    node = {"id": "pg-html", "slug": "pg-html", "title": "A joins B?", "createdAt": now.isoformat()}
    store.record_feed("tech", {"nodes": [node], "total_count": 1, "has_next_page": False}, now)
    rows = store.pending_topics()
    assert len(rows) == 1 and rows[0]["raw_json"] == node
    assert store.pending_topics() == []
    store.update_topic("pg-html", {"status": "ready", "detail_json": '{"source":"official"}'})
    with store._connect("fixture-read") as conn:
        row = conn.execute("SELECT status,detail_json FROM predict_html_topics WHERE category_id='pg-html'").fetchone()
    assert row["status"] == "ready"
    assert row["detail_json"] == {"source": "official"}

    store.record_feed("tech", {"nodes": [{**node, "id": "expired", "slug": "expired"}], "total_count": 1, "has_next_page": False}, now)
    assert len(store.pending_topics()) == 1
    with store._connect("fixture-expire") as conn:
        conn.execute("UPDATE predict_html_topics SET claim_until=now()-interval '1 second' WHERE category_id='expired'")
    with pytest.raises(RuntimeError, match="claim expired"):
        store.update_topic("expired", {"status": "ready"})


def test_postgres_search_cache_roundtrips_vectors_lifecycle_and_documents(isolated_pg):
    from packages.x_processing.postgres_search_cache import PostgresSearchCache
    from packages.x_processing.searcher import SearchDocument

    cache = PostgresSearchCache()
    cache.set_embedding(cache_key="query:q1", model="test", text_hash="hash", vector=[0.25, -1.5, 8.0])
    assert cache.get_embedding(cache_key="query:q1", model="test", text_hash="hash") == [0.25, -1.5, 8.0]
    cache.set_embeddings([
        ("query:q2", "test", "h2", [1.0, 2.0]),
        ("query:q2", "test", "h2b", [3.0, 4.0]),
        ("query:q3", "test", "h3", [5.0, 6.0]),
    ])
    batch = cache.get_embeddings([
        ("query:q1", "test", "hash"),
        ("query:q2", "test", "h2b"),
        ("query:q3", "test", "h3"),
        ("query:missing", "test", "none"),
    ])
    assert batch == {
        ("query:q1", "test", "hash"): [0.25, -1.5, 8.0],
        ("query:q2", "test", "h2b"): [3.0, 4.0],
        ("query:q3", "test", "h3"): [5.0, 6.0],
    }
    now = datetime.now(UTC)
    cache.upsert_active_candidate(candidate_id=7, task_id=9, title="候选", content="内容", source="x", source_item_id="x:9", source_url=None)
    assert [item.candidate_id for item in cache.list_active_candidate_documents()] == [7]
    cache.promote_candidate_for_task(task_id=9, retention_seconds=3600, release_reason="published")
    assert len(cache.list_recent_processed_documents()) == 1
    doc = SearchDocument("odaily_reference", "ref1", "引用", "正文", "odaily", published_at=now,
        created_at=now, updated_at=now, metadata={"kept": True})
    cache.upsert_documents([doc, doc])
    cache.upsert_documents([doc])
    assert cache.list_odaily_reference_documents(since=now - timedelta(minutes=1))[0].metadata == {"kept": True}


def test_writer3_index_postgres_keeps_references_and_confirmations(isolated_pg):
    from packages.writer3.models import OdailyReference
    from packages.writer3.postgres_index import PostgresWriter3Index

    index = PostgresWriter3Index()
    now = datetime.now(UTC)
    item = OdailyReference("ref-1", "https://odaily.news/1", "标题", "正文", now, {"m": 1}, {"raw": True})
    assert index.upsert_references([item]) == 1
    from packages.common.pg_native import connect_postgres
    with connect_postgres(schema="main", application_name="writer3-index-test") as conn:
        row = conn.execute("SELECT content,metadata_json,raw_payload_json FROM writer3_odaily_references WHERE source_item_id='ref-1'").fetchone()
    assert row["content"] == "正文" and row["metadata_json"] == {"m": 1} and row["raw_payload_json"] == {"raw": True}
    index.upsert_telegram_confirmation(message_id=123, chat_id="chat", context_id=8, current_source="x",
        current_source_item_id="x:8", current_message_text="message", sent_at=now)
    assert index.confirm_telegram_message(message_id=123, confirmed_at=now + timedelta(seconds=2),
        confirmed_by_id="42", confirmed_by_username="editor", confirmed_by_name="Editor")
    assert index.get_telegram_confirmation_by_message_id(123)["confirmed_by_id"] == "42"
