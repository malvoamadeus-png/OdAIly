from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def isolated_postgres(monkeypatch):
    admin_dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not admin_dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run Meme PostgreSQL checks")
    database_name = f"odaily_meme_{uuid.uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    child_dsn = psycopg.conninfo.make_conninfo(admin_dsn, dbname=database_name)
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", child_dsn)
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    yield
    from packages.common.pg_runtime import close_postgres_pools

    close_postgres_pools()
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=%s", (database_name,))
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))


def test_postgres_meme_dashboard_reads_native_tables(isolated_postgres):
    from packages.meme_dashboard import MemeDashboardStore
    from packages.meme_scanner.postgres_store import PostgresMemeStore

    PostgresMemeStore()
    from packages.common.pg_native import connect_postgres

    with connect_postgres(schema="meme", application_name="test-meme-fixture") as conn:
        conn.execute(
            """INSERT INTO jobs(address,trigger_key,payload_json,trigger_kind,queued_at,status,
                      narrative_json,evidence_json,title,content,updated_at)
               VALUES (%s,%s,%s,'daily_hot',%s,'queued',%s,%s,%s,%s,%s)""",
            ("0xfixture", "daily_hot:bsc:0xfixture", '{"chain":"bsc","symbol":"FIX"}',
             "2026-11-18T00:00:00+00:00", '{"status":"success"}', '{}', "Fixture", "Body",
             "2026-11-18T00:00:00+00:00"),
        )
    result = MemeDashboardStore().dashboard(column="daily_hot")
    assert result["available"] is True
    assert len(result["items"]) == 1
    assert result["items"][0]["symbol"] == "FIX"
    assert result["items"][0]["narrative_available"] is True


def test_postgres_meme_telegram_store_preserves_dedupe_and_candidate_identity(isolated_postgres):
    from datetime import UTC, datetime, timedelta

    from packages.meme_scanner.postgres_store import PostgresMemeStore

    store = PostgresMemeStore()
    stamp = datetime.now(UTC)
    for index in range(5):
        assert store.record_mention(
            address="0xtelegram", chat_id=index % 2, message_id=index + 1,
            sender_key=f"sender-{index % 3}", sent_at=(stamp - timedelta(minutes=1)).isoformat(),
            text_excerpt=f"mention {index}", dedupe_key=f"message:{index}", created_at=stamp.isoformat(),
        )
    assert not store.record_mention(
        address="0xtelegram", chat_id=0, message_id=1, sender_key="sender-0",
        sent_at=stamp.isoformat(), text_excerpt="duplicate", dedupe_key="message:0", created_at=stamp.isoformat(),
    )
    counts = store.mention_window_stats(address="0xtelegram", since=(stamp - timedelta(minutes=20)).isoformat())
    assert counts == {"mentions": 5, "chats": 2, "senders": 3}
    store.insert_tg_candidate(
        address="0xtelegram", chain="evm", source_chat="fixture", detected_at=stamp.isoformat(),
        window_start=(stamp - timedelta(minutes=20)).isoformat(), mention_count=5, chat_count=2,
        sender_count=3, evidence_json='{"messages":[]}', updated_at=stamp.isoformat(),
    )
    assert store.has_recent_tg_candidate(address="0xtelegram", since=(stamp - timedelta(hours=1)).isoformat())


def test_postgres_quote_audit_candidate_reader_uses_active_meme_rows(isolated_postgres, monkeypatch, tmp_path):
    from packages.common.pg_native import connect_postgres
    from packages.meme_scanner.postgres_store import PostgresMemeStore
    from packages.meme_scanner.quote_audit import active_candidates

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    PostgresMemeStore()
    with connect_postgres(schema="meme", application_name="test-meme-quote-source") as conn:
        conn.execute(
            """INSERT INTO observations(address,chain,platform,symbol,last_market_cap,highest_market_cap,
                      last_seen_at,tracking_status) VALUES('quote-active','solana','pump','QA',900000,1000000,
                      '2026-11-18T00:00:00+00:00','active')"""
        )
    rows = active_candidates(tmp_path / "unused.sqlite")
    assert len(rows) == 1
    assert rows[0]["address"] == "quote-active"
    assert rows[0]["market_cap"] == 900000


def test_postgres_meme_claims_are_exclusive_and_expiry_fenced(isolated_postgres):
    from packages.common.pg_native import connect_postgres
    from packages.meme_scanner.postgres_store import PostgresMemeStore
    from packages.meme_scanner.scanner import JobClaimLost, Token

    first = PostgresMemeStore()
    second = PostgresMemeStore()
    token = Token("claim-a", "test", "Claim", "CLM", 600_000, 300_000, None,
                  {"address": "claim-a", "chain": "bsc"})
    assert first.add_job(token, "test", "queued", trigger_key="claim-a")
    token2 = Token("claim-b", "test", "Claim", "CLM", 600_000, 300_000, None,
                   {"address": "claim-b", "chain": "bsc"})
    assert first.add_job(token2, "test", "queued", trigger_key="claim-b")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda store: store.claim_job(), (first, second)))
    assert all(claimed)
    assert claimed[0]["id"] != claimed[1]["id"]
    assert claimed[0]["claim_token"] != claimed[1]["claim_token"]

    job = claimed[0]
    with connect_postgres(schema="meme", application_name="test-meme-expire-claim") as conn:
        conn.execute("UPDATE jobs SET claim_expires_at=%s WHERE id=%s",
                     (datetime.now(UTC) - timedelta(seconds=1), job["id"]))
    assert first.recover_inflight() == 1
    with pytest.raises(JobClaimLost):
        first.assert_job_claim(job["id"], job["claim_token"], status="processing")
    with pytest.raises(JobClaimLost):
        first.update_job_evidence(job["id"], "{}", datetime.now(UTC).isoformat(), job["claim_token"])


def test_postgres_meme_claimed_job_can_finish_and_retry(isolated_postgres):
    from packages.meme_scanner.postgres_store import PostgresMemeStore
    from packages.meme_scanner.scanner import Token

    store = PostgresMemeStore()
    token = Token("finish", "test", "Finish", "FIN", 600_000, 300_000, None,
                  {"address": "finish", "chain": "bsc"})
    assert store.add_job(token, "test", "queued", trigger_key="finish")
    job = store.claim_job()
    store.update_job(job["id"], "discarded", reason="queue_expired", claim_token=job["claim_token"])
    assert store._one("SELECT status,reason FROM jobs WHERE id=%s", (job["id"],)) == {
        "status": "discarded", "reason": "queue_expired"}

    assert store.add_job(token, "test", "queued", trigger_key="retry")
    job = store.claim_job()
    assert store.retry_job(job, "temporary") == "retry_wait"
    assert store._one("SELECT status FROM jobs WHERE id=%s", (job["id"],))["status"] == "retry_wait"


def test_postgres_meme_tracking_lifecycle_and_token_info_failure(isolated_postgres):
    from packages.common.pg_native import connect_postgres
    from packages.meme_scanner.postgres_store import PostgresMemeStore
    from packages.meme_scanner.scanner import Token

    store = PostgresMemeStore()
    args = SimpleNamespace(token_info_high_interval=900, token_info_low_interval=14_400)
    token = Token("tracking-a", "test", "Tracking", "TRK", 800_000, 300_000, None,
                  {"address": "tracking-a", "chain": "bsc"})
    now = datetime.now(UTC)
    previous, started, status = store.start_or_observe_completed(
        token, observed_at=now.isoformat(), tracking_window_seconds=3600, args=args,
    )
    assert previous == 0
    assert started is True
    assert status == "active"
    with connect_postgres(schema="meme", application_name="test-meme-token-info-due") as conn:
        conn.execute("UPDATE observations SET next_token_info_at=%s WHERE address=%s AND chain='bsc'",
                     (now.isoformat(), token.address))
    row = store.due_token_info(now=(now + timedelta(seconds=1)).isoformat(), latest_completed_seen_at=None)
    assert row is not None and row["address"] == token.address
    store.record_token_info_failure(
        token.address, chain="bsc", error="rate limit", next_at=(now + timedelta(minutes=5)).isoformat(),
        observed_at=now.isoformat(),
    )
    assert store.observation(token.address, "bsc")["token_info_failures"] == 1
    assert store.expire_tracking((now + timedelta(hours=2)).isoformat()) == 1
    assert store.observation(token.address, "bsc")["tracking_status"] == "expired"
