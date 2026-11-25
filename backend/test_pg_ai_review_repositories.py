from __future__ import annotations

import os
import json
import uuid

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def isolated_postgres(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run native review repository checks")
    database_name = f"odaily_review_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    yield
    from packages.common.pg_runtime import close_postgres_pools

    close_postgres_pools()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_ai_review_repository_parity(isolated_postgres):
    from datetime import UTC, datetime, timedelta

    from packages.ai_review_postgres import PostgresAIReviewRepository

    repository = PostgresAIReviewRepository()
    key = "event-source:fixture:competitor_first_ai:source"
    assert repository.insert_candidate(key=key, category="competitor_first_ai", title="Fixture", evidence=[{"url": "https://example.test"}], clues={"domains": ["example.test"]})
    assert not repository.insert_candidate(key=key, category="competitor_first_ai", title="Updated", evidence=[], refresh_existing=False)
    row = repository.pending_analysis()[0]
    assert row["itemKey"] == key and row["analysisStatus"] == "queued"
    repository.complete_analysis(row["id"], recommendation="priority", reason="supported", model="test", input_tokens=10, output_tokens=3)
    listed = repository.list_items()
    assert listed["items"][0]["title"] == "Fixture"
    assert listed["weekly"]["candidates"] == 1
    repository.decide(key, decision="accepted", actor="fixture", note="ok")
    assert repository.history_examples("competitor_first_ai")[0]["decision"] == "accepted"
    assert repository.update_usage("2026-11-18", reserve=100)
    assert repository.update_usage("2026-11-18", settle=(100, 50))
    assert repository.usage("2026-11-18")["event"]["usedTokens"] == 50
    stale_key = "legacy-key"
    repository.insert_candidate(key=stale_key, category="competitor_first_ai", title="Legacy", evidence=[])
    assert repository.delete_stale_event_candidates(datetime.now(UTC) - timedelta(days=1), {key}) == 1
    repository.close()


def test_ai_review_service_reads_native_postgres_event_sources(isolated_postgres, monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    from packages.ai_review import AIReviewService
    from packages.ai_review_postgres import PostgresAIReviewRepository

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    repository = PostgresAIReviewRepository()
    now = datetime.now(UTC)
    with repository.connect() as conn:
        conn.execute("CREATE TABLE main.newsflash_items(id bigint PRIMARY KEY,source text,source_item_id text,source_url text,title text,content text,published_at timestamptz,raw_payload jsonb,metadata jsonb)")
        conn.execute("CREATE TABLE main.newsflash_events(event_id text PRIMARY KEY,representative_title text,event_time timestamptz,first_published_at timestamptz,first_sources text[],first_source text,competitor_source_count integer,has_odaily boolean,status text)")
        conn.execute("CREATE TABLE main.newsflash_event_sources(event_id text,item_id bigint,source text,source_item_id text)")
        conn.execute("INSERT INTO main.newsflash_events VALUES('event-1','Fixture event',%s,%s,ARRAY['coindesk','decrypt'],'coindesk',2,false,'active')", (now, now))
        conn.execute("INSERT INTO main.newsflash_items VALUES(1,'coindesk','one','https://www.coindesk.com/news/1','Headline one','Project launches.',%s, '{}'::jsonb, %s::jsonb)", (now, '{"original_url":"https://x.com/fixture"}'))
        conn.execute("INSERT INTO main.newsflash_items VALUES(2,'decrypt','two','https://decrypt.co/2','Headline two','Project launches.',%s, '{}'::jsonb, %s::jsonb)", (now + timedelta(minutes=1), '{"source_url":"https://x.com/fixture"}'))
        conn.execute("INSERT INTO main.newsflash_event_sources VALUES('event-1',1,'coindesk','one'),('event-1',2,'decrypt','two')")
    service = AIReviewService(database_path=tmp_path / "unused.sqlite", primary_database_path=tmp_path / "unused-main.sqlite", hottopic_database_path=tmp_path / "unused-hot.sqlite")
    try:
        assert service._queue_events(now - timedelta(days=1)) == 1
        candidate = service.repository.pending_analysis()[0]
        assert candidate["title"].startswith("@fixture")
        assert candidate["evidence"][0]["originSources"][0]["sourceAccount"] == "fixture"
    finally:
        service.close()


def test_postgres_self_upgrade_keeps_auditor_identities_and_decision_history(isolated_postgres):
    from packages.ai_text_self_upgrade.postgres_repository import PostgresAITextSelfUpgradeRepository

    repository = PostgresAITextSelfUpgradeRepository()
    assert repository.list_items({})["total"] == 0
    assert repository.recent_runs() == []
    with repository.connect() as conn:
        conn.execute("UPDATE ai_text_self_upgrade_state SET state_value='2026-11-01T00:00:00+00:00' WHERE state_key='enabled_at'")
        conn.execute("UPDATE ai_text_self_upgrade_state SET state_value='2026-11-02T00:00:00+00:00' WHERE state_key='fact_enabled_at'")
    assert repository.enabled_at() != repository.fact_enabled_at()
    case = {
        "case_key": "case-fixture", "source_type": "fact_auditor", "auditor_check_id": 41,
        "fact_check_id": 73, "source_item_id": "news-9", "content_hash": "digest",
        "audited_content": "published", "source_content": "source", "task_id": 12,
        "fact_result": {"summary": "fact result"}, "audit_result": {"summary": "text result"},
    }
    assert repository.insert_case(case)
    assert repository.list_items({"status": "pending"})["total"] == 1
    assert repository.start_run("run-fixture")
    repository.finish_run("run-fixture", status="completed", cases_found=1)
    assert repository.recent_runs(since=repository.enabled_at())[0]["run_key"] == "run-fixture"
    claimed = repository.claim_next()
    assert claimed is not None
    assert claimed["source_type"] == "fact_auditor"
    assert claimed["auditor_check_id"] == 41
    assert claimed["fact_check_id"] == 73
    assert claimed["source_item_id"] == "news-9"
    repository.complete_analysis("case-fixture", model="test", prompt_version="v1", result={"verdict": "writer1_error", "confidence": "high", "rationale": "evidence", "evidence": []})
    repository.decide("case-fixture", decision="reviewer_correct", actor="editor", note="verified")
    detail = repository.detail("case-fixture")
    assert detail["fact_result"]["summary"] == "fact result"
    assert detail["decisions"][-1]["decision"] == "reviewer_correct"
    repository.close()


def test_postgres_editor_feedback_transaction_writes_manual_rejection_outbox(isolated_postgres):
    from packages.editor_plugin_postgres_local_store import PostgresEditorPluginLocalStore

    store = PostgresEditorPluginLocalStore()
    result = store.record_feedback(
        feed_item_id="auditor:41", feed_kind="auditor_alert", feedback="reject",
        actor_user_id="editor", actor_email="editor@example.com", actor_display_name="Editor",
        session_id=None, extra_json={},
    )
    pending = store.pending_manual_rejections()
    assert [(row.id, row.auditor_check_id, row.actor_email, row.status) for row in pending] == [
        (result["feedback_id"], 41, "editor@example.com", "pending")
    ]
    store.mark_manual_rejection_queued(result["feedback_id"])
    assert store.pending_manual_rejections()[0].status == "queued"
    store.mark_manual_rejection_complete(result["feedback_id"])
    assert store.pending_manual_rejections() == []


def test_postgres_self_upgrade_discovers_cases_from_native_source_tables(isolated_postgres, monkeypatch, tmp_path):
    from datetime import UTC, datetime

    from packages.ai_text_self_upgrade.postgres_repository import PostgresAITextSelfUpgradeRepository
    from packages.ai_text_self_upgrade.service import AITextSelfUpgradeService
    from packages.auditor.repository import calculate_content_hash
    from packages.common.pg_native import connect_postgres

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    repository = PostgresAITextSelfUpgradeRepository()
    stamp = datetime.now(UTC)
    title, content = "Published title", "Published body."
    digest = calculate_content_hash(title, content)
    with connect_postgres(schema="main", application_name="test-self-upgrade-source") as conn:
        conn.execute("CREATE TABLE main.newsflash_production_metadata(delivery_key text PRIMARY KEY,title text,odaily_id text,id_match_status text)")
        conn.execute("CREATE TABLE main.tasks(id bigint PRIMARY KEY,source_url text,title text,content text)")
        conn.execute("""CREATE TABLE main.x_task_pipeline(
            task_id bigint PRIMARY KEY,writer_model text,prompt_template_key text,prompt_version_id bigint,
            draft_title text,draft_content text,final_title text,final_content text,write_completed_at timestamptz,
            publish_completed_at timestamptz)""")
        conn.execute("""CREATE TABLE main.auditor_checks(
            id bigint PRIMARY KEY,source_item_id text,content_hash text,title text,content text,source_url text,
            published_at timestamptz,prompt_version text,model text,audit_result jsonb,status text,
            alerted_at timestamptz,created_at timestamptz)""")
        conn.execute("INSERT INTO main.tasks VALUES(12,'https://source.test/item',%s,%s)", (title, "source body"))
        conn.execute("""INSERT INTO main.x_task_pipeline VALUES
            (12,'writer','brief',3,'Draft title','Draft body',%s,%s,%s,%s)""",
            (title, content, stamp, stamp))
        conn.execute("INSERT INTO main.newsflash_production_metadata VALUES('odaily-task-12',%s,'odaily-12','matched')", (title,))
        conn.execute("""INSERT INTO main.auditor_checks VALUES
            (41,'odaily-12',%s,%s,%s,'https://source.test/item',%s,'audit-v1','reviewer',%s::jsonb,
             'flagged',%s,%s)""", (digest, title, content, stamp,
                                     '{"summary":"review","stages":{"external":{"result":{"status":"flagged"}}}}',
                                     stamp, stamp))
    service = AITextSelfUpgradeService(tmp_path / "unused.sqlite", repository, object())
    discovered, unlinked = service._discover_cases()
    assert unlinked == 0
    assert len(discovered) == 1
    assert discovered[0]["source_type"] == "text_auditor"
    assert discovered[0]["auditor_check_id"] == 41
    assert discovered[0]["task_id"] == 12
    assert discovered[0]["audit_result"]["summary"] == "review"
    assert discovered[0]["audit_result"]["stages"]["external"]["result"]["status"] == "flagged"


def test_postgres_manual_rejection_snapshots_ambiguous_link_and_respects_tombstone(isolated_postgres, monkeypatch):
    from datetime import UTC, datetime

    from packages.ai_text_self_upgrade.postgres_repository import PostgresAITextSelfUpgradeRepository
    from packages.auditor.repository import calculate_content_hash
    from packages.common.pg_native import connect_postgres

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    repository = PostgresAITextSelfUpgradeRepository()
    stamp = datetime.now(UTC)
    title, content = "Published title", "Published body."
    digest = calculate_content_hash(title, content)
    with connect_postgres(schema="main", application_name="test-manual-rejection") as conn:
        conn.execute("CREATE TABLE main.newsflash_production_metadata(delivery_key text PRIMARY KEY,odaily_id text,id_match_status text)")
        conn.execute("CREATE TABLE main.tasks(id bigint PRIMARY KEY,source_url text,title text,content text)")
        conn.execute("""CREATE TABLE main.x_task_pipeline(
            task_id bigint PRIMARY KEY,writer_model text,prompt_template_key text,prompt_version_id bigint,
            draft_title text,draft_content text,final_title text,final_content text,write_completed_at timestamptz,
            publish_completed_at timestamptz)""")
        conn.execute("""CREATE TABLE main.auditor_checks(
            id bigint PRIMARY KEY,source_item_id text,content_hash text,title text,content text,source_url text,
            published_at timestamptz,prompt_version text,model text,audit_result jsonb,status text)""")
        conn.execute("INSERT INTO main.tasks VALUES(12,'https://source.test/item','Source title','Source body')")
        conn.execute("""INSERT INTO main.x_task_pipeline VALUES
            (12,'writer','brief',3,'Draft title','Draft body',%s,%s,%s,%s)""",
            (title, content, stamp, stamp))
        conn.execute("INSERT INTO main.newsflash_production_metadata VALUES('odaily-task-12','odaily-12','ambiguous')")
        conn.execute("""INSERT INTO main.auditor_checks VALUES
            (41,'odaily-12',%s,%s,%s,'https://source.test/item',%s,'audit-v1','reviewer',%s::jsonb,'flagged')""",
            (digest, title, content, stamp, '{"issues":["manual check"]}'))

    queue_id = repository.queue_manual_rejection(41, actor="editor@example.com")
    assert queue_id == "manual-rejection:41"
    assert repository.list_items({})["total"] == 0
    first_attempt = repository.process_manual_rejection_queue()
    assert first_attempt == {"processed": 1, "completed": 1, "retrying": 0, "deleted": 0}
    case_key_value = repository.list_items({})["items"][0]["case_key"]
    case = repository.detail(case_key_value)
    assert case["task_id"] is None
    assert case["draft_title"] is None
    assert case["final_content"] == content
    assert case["audit_result"]["manual_rejection"]["authoring_path"] == "unknown_publication"
    assert case["audit_result"]["manual_rejection"]["actor"] == "editor@example.com"
    with connect_postgres(schema="main", application_name="test-manual-rejection") as conn:
        conn.execute("INSERT INTO main.auditor_checks VALUES(42,'odaily-12',%s,%s,%s,'https://source.test/item',%s,'audit-v1','reviewer','{}'::jsonb,'flagged')",
                     (digest, title, content, stamp))
    repository.queue_manual_rejection(42, actor="editor@example.com")
    with repository.connect() as conn:
        conn.execute("INSERT INTO ai_text_self_upgrade_deleted_cases(case_key,deleted_by) VALUES(%s,'admin') ON CONFLICT DO NOTHING", (case_key_value,))
    assert repository.process_manual_rejection_queue() == {"processed": 1, "completed": 0, "retrying": 0, "deleted": 1}


@pytest.mark.parametrize("with_manual_metadata", [True, False])
def test_postgres_manual_rejection_creates_independent_case_without_writer_snapshot(isolated_postgres, monkeypatch, with_manual_metadata):
    from datetime import UTC, datetime

    from packages.ai_text_self_upgrade.postgres_repository import PostgresAITextSelfUpgradeRepository
    from packages.ai_text_self_upgrade.prompts import build_prompt, parse_output
    from packages.auditor.repository import calculate_content_hash
    from packages.common.pg_native import connect_postgres

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    repository = PostgresAITextSelfUpgradeRepository()
    stamp = datetime.now(UTC)
    title, content = "Manual published title", "Manual published body."
    digest = calculate_content_hash(title, content)
    with connect_postgres(schema="main", application_name="test-manual-publication") as conn:
        if with_manual_metadata:
            conn.execute("""CREATE TABLE main.newsflash_production_metadata(
                delivery_key text PRIMARY KEY,odaily_id text,id_match_status text,title text,outlet_category text)""")
            conn.execute("INSERT INTO main.newsflash_production_metadata VALUES('manual:post-88','odaily-manual-88','matched',%s,'manual')", (title,))
        conn.execute("""CREATE TABLE main.auditor_checks(
            id bigint PRIMARY KEY,source_item_id text,content_hash text,title text,content text,source_url text,
            published_at timestamptz,prompt_version text,model text,audit_result jsonb,status text)""")
        conn.execute("""INSERT INTO main.auditor_checks VALUES
            (88,'odaily-manual-88',%s,%s,%s,'https://source.test/manual',%s,'audit-v1','reviewer','{}'::jsonb,'flagged')""",
            (digest, title, content, stamp))

    repository.queue_manual_rejection(88, actor="editor@example.com")
    assert repository.process_manual_rejection_queue() == {
        "processed": 1, "completed": 1, "retrying": 0, "deleted": 0,
    }
    item = repository.list_items({})["items"][0]
    case = repository.detail(item["case_key"])
    assert case["task_id"] is None
    assert case["writer_model"] is None
    assert case["draft_content"] is None
    assert case["final_content"] == content
    expected_path = "manual_publication"
    assert case["audit_result"]["manual_rejection"]["authoring_path"] == expected_path
    assert "manual_publication_without_writer1_snapshot" in build_prompt(case)

    result = parse_output(json.dumps({
        "verdict": "reviewer_correct_unattributed", "confidence": "medium", "rationale": "审核指出的问题成立，但无 Writer1 草稿可供归因。",
        "evidence": [{"location": "published_text", "quote": content, "explanation": "已发布文本支持审核意见。"}],
    }, ensure_ascii=False), case)
    repository.complete_analysis(item["case_key"], model="gpt-6-luna", prompt_version="test", result=result)
    completed = repository.detail(item["case_key"])
    assert completed["analysis_status"] == "completed"
    assert completed["verdict"] == "reviewer_correct_unattributed"
    assert repository.manual_rejection_status(88) == "completed"
