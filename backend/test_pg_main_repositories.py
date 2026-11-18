from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import psycopg
from psycopg import sql

from packages.x_capture.models import CaptureRecord
from packages.x_capture.repository import PostgresXCaptureRepository
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.repository import PostgresCompetitorMonitorRepository
from packages.x_processing.repository import PostgresXProcessingRepository


def _repositories(order: str):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    capture = PostgresXCaptureRepository()
    processing = PostgresXProcessingRepository()
    competitor = PostgresCompetitorMonitorRepository()
    repos = (capture, processing, competitor) if order == "capture-first" else (competitor, processing, capture)
    for repository in repos:
        repository.init_schema()
    return capture, processing, competitor


@pytest.mark.parametrize("order", ["capture-first", "processing-first"])
def test_pg_main_schema_and_capture_to_attempt_round_trip(monkeypatch, order):
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    database_name = f"odaily_test_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} ").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    try:
        capture, processing, competitor = _repositories(order)
        _exercise_round_trip(capture, processing, competitor)
    finally:
        from packages.common.pg_runtime import close_postgres_pools

        close_postgres_pools()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def _exercise_round_trip(capture, processing, competitor):
    suffix = uuid.uuid4().hex[:12]
    username = f"pg{suffix[:10]}"
    tweet_id = f"pg-test-{suffix}"
    account = capture.create_account(username_or_url=username)
    task_id = capture.save_task(
        account,
        CaptureRecord(
            platform="x",
            tweet_id=tweet_id,
            author_username=username,
            author_display_name="Postgres Test",
            url=f"https://x.com/{username}/status/{tweet_id}",
            text="A synthetic PostgreSQL repository round trip.",
            created_at=datetime.now(UTC).isoformat(),
            reply_count=0,
            retweet_count=0,
            like_count=0,
            bookmark_count=0,
            view_count=0,
            media_urls=[],
            metadata={"test_marker": suffix},
            raw_payload={"test_marker": suffix, "nested": {"value": 7}},
        ),
    )

    task = processing.claim_task_by_id("judge", task_id=task_id, worker_id=f"test-{suffix}")
    assert task is not None
    assert task.raw_payload == {"test_marker": suffix, "nested": {"value": 7}}
    assert task.metadata["test_marker"] == suffix
    assert task.status == "judging"

    with processing._connect(autocommit=True) as connection:
        source_events = connection.execute(
            "SELECT source_row_id,payload FROM odaily_source_state_outbox "
            "WHERE source_schema='main' AND source_table='tasks' AND source_row_id=%s "
            "ORDER BY event_id",
            (str(task_id),),
        ).fetchall()
    assert source_events
    assert all(set(event["payload"]) <= {
        "source", "source_item_id", "source_url", "title", "content", "status",
        "created_at", "updated_at", "metadata", "raw_payload",
    } for event in source_events)
    assert any(event["payload"].get("status") == "judging" for event in source_events)

    with processing._connect() as connection:
        connection.execute(
            "UPDATE x_task_pipeline SET final_title=%s,final_content=%s WHERE task_id=%s",
            ("Synthetic pipeline title", "Synthetic pipeline content", task_id),
        )
    with processing._connect(autocommit=True) as connection:
        pipeline_events = connection.execute(
            "SELECT source_row_id,payload FROM odaily_source_state_outbox "
            "WHERE source_schema='main' AND source_table='x_task_pipeline' AND source_row_id=%s "
            "ORDER BY event_id",
            (str(task_id),),
        ).fetchall()
    assert len(pipeline_events) == 2
    assert pipeline_events[-1]["payload"]["final_title"] == "Synthetic pipeline title"
    assert pipeline_events[-1]["payload"]["final_content"] == "Synthetic pipeline content"

    stale_attempt = processing.start_stage_attempt(
        task_id,
        stage="judge",
        executor="stale-test",
        input_evidence={"title": "stale"},
        rule_evidence={},
    )
    with processing._connect() as connection:
        connection.execute(
            "UPDATE tasks SET locked_until=now()-interval '1 second' WHERE id=%s",
            (task_id,),
        )
    reclaimed = processing.claim_task_by_id("judge", task_id=task_id, worker_id=f"reclaimed-{suffix}")
    assert reclaimed is not None
    processing.set_claim_context(reclaimed)
    with processing._connect(autocommit=True) as connection:
        candidate_count = connection.execute("SELECT count(*) AS n FROM search_event_candidates").fetchone()["n"]
    with pytest.raises(RuntimeError, match="claim is stale"):
        processing.create_candidate_for_task(task, search_result={"source": "stale"})
    with processing._connect(autocommit=True) as connection:
        assert connection.execute("SELECT count(*) AS n FROM search_event_candidates").fetchone()["n"] == candidate_count
    processing.record_stage_attempt_response(stale_attempt, "must not persist")
    processing.complete_stage_attempt(stale_attempt, result={"stale": True})
    attempt_id = processing.start_stage_attempt(
        task_id,
        stage="judge",
        executor="test",
        input_evidence={"title": "synthetic"},
        rule_evidence={"version": 2},
    )
    processing.record_stage_attempt_response(attempt_id, "synthetic model response")
    processing.record_stage_attempt_context(
        attempt_id,
        input_evidence={"title": "updated"},
        rule_evidence={"version": 3},
    )
    with processing._connect() as connection:
        processing._set_task_status(connection, task_id, "judged")
        assert connection.execute("SELECT locked_until FROM tasks WHERE id=%s", (task_id,)).fetchone()["locked_until"] is None
    processing.complete_stage_attempt(attempt_id, result={"verdict": "passed"})

    with processing._connect(autocommit=True) as connection:
        attempt = connection.execute(
            "SELECT status,input_json,rule_json,response_gzip,response_sha256,result_json "
            "FROM x_processing_attempts WHERE id=%s",
            (attempt_id,),
        ).fetchone()
    assert attempt["status"] == "succeeded"
    assert attempt["input_json"] == {"title": "updated"}
    assert attempt["rule_json"] == {"version": 3}
    assert attempt["response_gzip"]
    assert len(attempt["response_sha256"]) == 64
    assert attempt["result_json"] == {"task_status": "judged"}
    with processing._connect(autocommit=True) as connection:
        assert connection.execute("SELECT locked_until FROM tasks WHERE id=%s", (task_id,)).fetchone()["locked_until"] is None

    newsflash = NewsflashItem(
        source="blockbeats",
        source_item_id=f"news-{suffix}",
        source_url=f"https://www.theblockbeats.info/flash/{suffix}?utm_source=test",
        title="Synthetic event title",
        content="Synthetic event content",
        published_at=datetime.now(UTC).isoformat(),
        raw_payload={"test_marker": suffix},
        metadata={"test_marker": suffix},
    )
    item = competitor.upsert_newsflash_items([newsflash])[0]
    event_id = competitor.create_event_with_source(item)
    assert competitor.list_event_sources_by_urls(
        source_urls={f"https://www.theblockbeats.info/flash/{suffix}"},
        exclude_item_ids=set(),
    )[0].event_id == event_id
    assert competitor.list_event_anchors(event_ids={event_id})[0].item.id == item.id
    competitor.record_event_exclusions([
        NewsflashItem(source="blockbeats", source_item_id=f"excluded-{suffix}", title="Excluded", content="content")
    ])

    with capture._connect() as connection:
        connection.execute("DELETE FROM newsflash_events WHERE event_id=%s", (event_id,))
        connection.execute("DELETE FROM newsflash_items WHERE id=%s", (item.id,))
        connection.execute("DELETE FROM newsflash_event_exclusions WHERE source_item_id=%s", (f"excluded-{suffix}",))
        connection.execute("DELETE FROM tasks WHERE id=%s", (task_id,))
        connection.execute("DELETE FROM x_capture_accounts WHERE id=%s", (account.id,))
