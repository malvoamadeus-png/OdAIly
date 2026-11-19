from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture
def postgres_settings():
    dsn = os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set ODAILY_TEST_POSTGRES_DSN to run PostgreSQL integration checks")
    from packages.common.pg_runtime import PostgresSettings

    return PostgresSettings(
        dsn=dsn, connect_timeout_seconds=5, statement_timeout_ms=10_000, lock_timeout_ms=2_000,
        idle_transaction_timeout_ms=10_000, migration_statement_timeout_ms=60_000,
        migration_lock_timeout_ms=5_000, migration_idle_transaction_timeout_ms=30_000,
        pool_max_size=2, pool_checkout_timeout_seconds=5, pool_max_waiting=16,
        application_name="odaily-hottopic-schema-test",
    )


@pytest.fixture
def hottopic_schema(postgres_settings):
    from packages.common.pg_native import connect_postgres
    from psycopg.sql import Identifier

    schema = "test_" + uuid.uuid4().hex[:16]
    with connect_postgres(schema="main", application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        conn.execute("CREATE SCHEMA " + Identifier(schema).as_string(conn))
    try:
        yield schema
    finally:
        with connect_postgres(schema="main", application_name="odaily-hottopic-schema-cleanup", settings=postgres_settings) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component IN ('hottopic','investment_leads')", (schema,))
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")


def test_hottopic_and_investment_schemas_apply_in_dependency_order(postgres_settings, hottopic_schema) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_schema import initialize_hottopic_schema
    from packages.investment_leads.postgres_schema import initialize_investment_leads_schema

    assert initialize_hottopic_schema(schema=hottopic_schema, settings=postgres_settings) == (1, 2)
    assert initialize_hottopic_schema(schema=hottopic_schema, settings=postgres_settings) == ()
    assert initialize_investment_leads_schema(schema=hottopic_schema, settings=postgres_settings) == (1,)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        tables = {row["tablename"] for row in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (hottopic_schema,)).fetchall()}
        assert {"hottopic_accounts", "hottopic_inbox", "topics", "claims", "investment_lead_tasks"} <= tables
        assert conn.execute("SELECT singleton_key FROM hottopic_meta").fetchone() is None


def test_hottopic_postgres_repository_reads_optional_maintenance_timestamp(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    assert repository.last_maintenance_at() is None
    expected = datetime(2026, 11, 19, 4, 0, tzinfo=UTC)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-maintenance-test", settings=postgres_settings) as conn:
        with conn.transaction():
            conn.execute("UPDATE hottopic_meta SET last_maintenance_at=%s WHERE singleton_key='global'", (expected,))
    assert repository.last_maintenance_at() == expected


def test_hottopic_public_reads_use_postgres_repository(postgres_settings, hottopic_schema, monkeypatch, tmp_path) -> None:
    from packages.hottopic import service as service_module
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", postgres_settings.dsn)
    monkeypatch.setenv("ODAILY_COLLECT_SPOOL_DIR", "/data/OdAIly/collector-spool")
    monkeypatch.setenv("HOTTOPIC_CLAIM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", "0")
    monkeypatch.setattr(service_module, "open_worker_database", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("opened SQLite")))
    spool_directory = tmp_path / "spool"
    spool_directory.mkdir()
    from packages.hottopic.collector_spool import CollectorSpool
    monkeypatch.setattr(service_module, "CollectorSpool", lambda _directory, max_bytes, max_record_bytes: CollectorSpool(
        spool_directory, max_bytes=max_bytes, max_record_bytes=max_record_bytes,
    ))
    monkeypatch.setattr(service_module, "load_postgres_settings", lambda: postgres_settings)
    monkeypatch.setattr(service_module, "HotTopicPostgresRepository", lambda *, settings: HotTopicPostgresRepository(
        schema=hottopic_schema, settings=settings,
    ))

    service = service_module.HotTopicService(workers=1)
    try:
        assert service.dashboard()["topics"] == []
        assert service.topic_detail("missing") is None
        assert service.x_agent_dashboard()["accounts"]["total"] == 0
        assert service.list_x_agent_accounts()["items"] == []
        assert service.list_market_sentiment()["items"] == []
        assert service.market_sentiment_history("missing")["items"] == []
        assert service.market_sentiment_detail("missing")["items"] == []
        assert service.list_project_promotions()["items"] == []
        assert service.investment_leads_dashboard()["accounts"]["investmentLeadsEnabled"] == 0
        assert service.list_investment_leads()["items"] == []
        assert service.investment_lead_detail("missing") is None
    finally:
        service.close()
def test_public_hottopic_service_selects_postgres_from_storage_backend(
    postgres_settings, hottopic_schema, monkeypatch, tmp_path,
) -> None:
    from packages.hottopic import service as service_module
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("ODAILY_POSTGRES_DSN", postgres_settings.dsn)
    monkeypatch.setenv("ODAILY_COLLECT_SPOOL_DIR", "/data/OdAIly/collector-spool")
    monkeypatch.setenv("HOTTOPIC_CLAIM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", "0")
    def forbidden_sqlite(*_args, **_kwargs):
        raise AssertionError("PostgreSQL HotTopic entry point must not open SQLite")

    monkeypatch.setattr(service_module, "open_worker_database", forbidden_sqlite)
    spool_directory = tmp_path / "spool"
    spool_directory.mkdir()
    monkeypatch.setattr(
        service_module, "CollectorSpool",
        lambda _directory, max_bytes, max_record_bytes: __import__(
            "packages.hottopic.collector_spool", fromlist=["CollectorSpool"],
        ).CollectorSpool(spool_directory, max_bytes=max_bytes, max_record_bytes=max_record_bytes),
    )
    monkeypatch.setattr(
        service_module, "load_postgres_settings", lambda: postgres_settings,
    )
    monkeypatch.setattr(
        service_module, "HotTopicPostgresRepository",
        lambda *, settings: HotTopicPostgresRepository(schema=hottopic_schema, settings=settings),
    )
    service = service_module.HotTopicService(workers=1)
    try:
        assert isinstance(service.postgres_repository, HotTopicPostgresRepository)
        assert service.db is None
        assert service.list_accounts() == []
    finally:
        service.close()
def test_hottopic_worker_once_uses_postgres_service_factory(monkeypatch, tmp_path, capsys) -> None:
    from packages.hottopic import service as service_module

    seen = {}

    class FakeService:
        def __init__(self, database_path, *, model, workers):
            seen.update(database_path=database_path, model=model, workers=workers)

        def list_accounts(self):
            return []

        def poll_once(self):
            return {"backend": "postgres"}

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(service_module, "HotTopicService", FakeService)
    monkeypatch.setattr("dotenv.load_dotenv", lambda: None)
    assert service_module.run_worker(database_path=tmp_path / "unused.sqlite", once=True, model="fixture", workers=1) == 0
    assert seen == {"database_path": tmp_path / "unused.sqlite", "model": "fixture", "workers": 1, "closed": True}
    assert '"backend": "postgres"' in capsys.readouterr().out


def test_postgres_transient_prune_preserves_permanent_evidence(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime, timedelta
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    old = datetime.now(UTC) - timedelta(days=3)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-prune-test", settings=postgres_settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO batches(batch_id,batch_time,input_count,committed_at,result_json) VALUES(%s,%s,1,%s,'{}')", ("batch-old", old.isoformat(), old))
            conn.execute("INSERT INTO content_items(content_item_id,tweet_id,activity_account,author,activity_type,content_text,expanded_text,created_at,source_url,metrics_json,references_json,raw_payload_json,fingerprint) VALUES(%s,%s,'a','a','original','x','x',%s,'u','{}','{}','{}','f')", ("content-old", "tweet-old", old))
            conn.execute("INSERT INTO claims(claim_id,content_item_id,claim_text,claim_kind,entities_json,action_or_issue,stance,evidence_span,confidence,information_value,created_at) VALUES('claim-old','content-old','x','event','[]','x','x','x',1,1,%s)", (old,))
            conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,matching_status,visibility,retention_tier) VALUES('topic-old','x','x','[]','x',%s,%s,%s,%s,'archived','hidden','transient')", (old, old, old, old))
            conn.execute("INSERT INTO memberships(membership_id,claim_id,topic_id,topic_role,role,match_score,decision_source,decision_reason,created_at) VALUES('membership-old','claim-old','topic-old','event','support',1,'test','x',%s)", (old,))
        assert repository.prune_transient_state(at=datetime.now(UTC), batch_size=10)["deleted_topics"] == 1
        assert conn.execute("SELECT count(*) AS n FROM topics WHERE topic_id='topic-old'").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM content_items WHERE content_item_id='content-old'").fetchone()["n"] == 0


def test_topic_aggregator_postgres_read_slice_uses_native_repository(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-read-test", settings=postgres_settings) as conn:
        conn.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,matching_status,visibility,retention_tier) "
            "VALUES('topic-read','Topic','asset','[]','issue',%s,%s,%s,'seed','hidden','transient')",
            (now, now, now),
        )
        conn.execute(
            "INSERT INTO retrieval_state(scope_type,scope_id,tokens_json,hard_keys_json,entities_json,representative_claim_ids_json,recent_claim_ids_json,updated_at) "
            "VALUES('topic','topic-read','[\"asset\"]','[]','[]','[]','[]',%s)", (now,),
        )
    aggregator = TopicAggregator(tmp_path / "must-not-open.sqlite", postgres_repository=repository)
    try:
        assert aggregator.connection is None
        assert "topic-read" in aggregator._retrieval_cache
        assert aggregator.counts()["topics"] == 1
        assert aggregator._due_topic_ids(now.isoformat()) == {"topic-read"}
        assert aggregator.export_topics()[0]["topic_id"] == "topic-read"
        assert aggregator._refresh_all_topics(now.isoformat(), topic_ids={"topic-read"})["refreshed_count"] == 1
    finally:
        aggregator.close()


def test_native_topic_batch_primitives_preserve_json_and_deduplicate(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import Claim, ContentItem

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC).isoformat()
    item = ContentItem("content:primitive", "tweet:primitive", "alice", "alice", "original", "Text", "Text", now,
                       "https://x.com/alice/status/primitive", {"likes": 1}, {"quote_id": None}, {"payload": "原文"})
    import hashlib
    fingerprint = hashlib.sha256((item.expanded_text + "\x1f" + item.activity_account).encode()).hexdigest()
    assert repository.insert_content_item(item=item, fingerprint=fingerprint)
    assert not repository.insert_content_item(item=item, fingerprint=fingerprint)
    claim = Claim("claim:primitive", item.content_item_id, "Text says X", "event", ("X",), "launch", "neutral", 0.8, 0.5, "X")
    repository.insert_topic_claim(claim=claim, created_at=now)
    repository.insert_topic_claim(claim=claim, created_at=now)
    assert repository.content_exists(item.tweet_id)
    assert repository.topic_counts()["claims"] == 1


def test_topic_aggregator_postgres_retrieval_state_materialization(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import Claim, ContentItem, TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC)
    item = ContentItem("content-retrieval", "tweet-retrieval", "alice", "alice", "original", "$ABC launched", "$ABC launched", now.isoformat(), "url", {}, {}, {})
    claim = Claim("claim-retrieval", item.content_item_id, "$ABC launched", "reported_fact", ("abc",), "launch", "neutral", 0.9, 0.7, "$ABC launched")
    fingerprint = "f"
    repository.insert_content_item(item=item, fingerprint=fingerprint)
    repository.insert_topic_claim(claim=claim, created_at=now.isoformat())
    repository.create_topic_seed(topic_id="topic-retrieval", claim=claim, item=item)
    batch_at = now + __import__("datetime").timedelta(minutes=1)
    repository.attach_topic_claim(claim=claim, topic_id="topic-retrieval", score=1.0, reason="seed", source="test", item=item,
                                  at=batch_at.isoformat(),
                                  relations=["progress"], membership_role="new_fact")
    from packages.common.pg_native import connect_postgres
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-time-test", settings=postgres_settings) as conn:
        assert conn.execute("SELECT created_at FROM memberships WHERE claim_id='claim-retrieval'").fetchone()["created_at"] == batch_at
        assert conn.execute("SELECT last_participation_at FROM topic_participations WHERE topic_id='topic-retrieval'").fetchone()["last_participation_at"] == now
    aggregator = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository)
    try:
        aggregator._update_retrieval("topic-retrieval", now.isoformat())
        assert "topic-retrieval" in aggregator._retrieval_cache
        assert "asset:abc" in aggregator._retrieval_cache["topic-retrieval"]["hard_keys"]
    finally:
        aggregator.close()


def test_aggregation_transaction_unit_of_work_is_atomic(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime
    import pytest
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import Claim, ContentItem

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC).isoformat()
    item = ContentItem("content-uow", "tweet-uow", "alice", "alice", "original", "Text", "Text", now, "url", {}, {}, {})
    claim = Claim("claim-uow", item.content_item_id, "Text", "event", (), "issue", "neutral", 0.8, 0.5, "Text")
    import hashlib
    with pytest.raises(RuntimeError, match="rollback test"):
        with repository.aggregation_transaction() as unit:
            assert unit.insert_content_item(item=item, fingerprint=hashlib.sha256(b"uow").hexdigest())
            unit.insert_claim(claim=claim, created_at=now)
            unit.create_seed(topic_id="topic-uow", claim=claim, item=item)
            raise RuntimeError("rollback test")
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-uow-test", settings=postgres_settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM content_items WHERE tweet_id='tweet-uow'").fetchone()["n"] == 0
    with repository.aggregation_transaction() as unit:
        assert unit.insert_content_item(item=item, fingerprint=hashlib.sha256(b"uow").hexdigest())
        unit.insert_claim(claim=claim, created_at=now)
        unit.create_seed(topic_id="topic-uow", claim=claim, item=item)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-uow-test", settings=postgres_settings) as conn:
        assert conn.execute("SELECT count(*) AS n FROM topics WHERE topic_id='topic-uow'").fetchone()["n"] == 1


def test_topic_aggregator_postgres_process_batch_is_atomic_and_idempotent(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    aggregator = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository)
    now = datetime.now(UTC)
    item = {"tweet_id": "tweet-batch", "activity_account": "alice", "author": "alice", "created_at": now.isoformat(),
            "text": "$ABC announces a new launch", "expanded_text": "$ABC announces a new launch", "metrics": {}, "references": {}, "raw_payload": {}}
    try:
        result = aggregator.process_batch([item], now)
        assert result["idempotent_replay"] is False
        replay = aggregator.process_batch([item], now)
        assert replay["idempotent_replay"] is True
        assert aggregator.counts()["content_items"] == 1
        assert aggregator.counts()["claims"] == 1
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-batch-test", settings=postgres_settings) as conn:
            assert conn.execute("SELECT count(*) AS n FROM decision_audit").fetchone()["n"] == 1
    finally:
        aggregator.close()


def test_topic_aggregator_candidate_resolver_runs_without_postgres_checkout(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import postgres_pool_stats
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    observed = []
    def resolver(cases):
        observed.append(postgres_pool_stats()["borrowed"])
        return [{"case_id": case["case_id"], "assignments": [{
            "claim_id": claim["claim_id"], "action": "new_topic", "new_topic_group": "main",
            "reason": "test resolver",
        } for claim in case["claims"]]} for case in cases]
    aggregator = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository, resolver=resolver)
    aggregator._needs_candidate_resolution = lambda _candidates: True
    now = datetime.now(UTC)
    try:
        aggregator.process_batch([{"tweet_id":"tweet-resolver","activity_account":"alice","author":"alice",
                                   "created_at":now.isoformat(),"text":"$ABC announcement launch details",
                                   "expanded_text":"$ABC announcement launch details","metrics":{},"references":{},"raw_payload":{}}], now)
        assert observed == [0]
    finally:
        aggregator.close()


def test_postgres_semantic_review_calls_model_without_checkout_and_persists(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    observed = []

    def reviewer(cases):
        observed.append(postgres_pool_stats()["borrowed"])
        return [{"case_id": case["case_id"], "decision": "context", "confidence": 0.9,
                 "reason": "same event context", "model": "test-model", "reasoning_effort": "high"}
                for case in cases]

    aggregator = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository, semantic_reviewer=reviewer)
    now = datetime.now(UTC)
    item = {"tweet_id":"tweet-semantic", "activity_account":"alice", "author":"alice", "created_at":now.isoformat(),
            "text":"$ABC launch announcement with details", "expanded_text":"$ABC launch announcement with details",
            "metrics":{}, "references":{}, "raw_payload":{}}
    try:
        aggregator.process_batch([item], now)
        targets = repository.unreviewed_claim_targets(topic_ids=["topic-does-not-exist"], limit=10)
        # Create a direct review target from the deterministic seed written by process_batch.
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-semantic-test", settings=postgres_settings) as conn:
            claim_id = conn.execute("SELECT claim_id FROM claims WHERE content_item_id=(SELECT content_item_id FROM content_items WHERE tweet_id='tweet-semantic')").fetchone()["claim_id"]
            topic_id = conn.execute("SELECT topic_id FROM topics WHERE matching_status='seed' LIMIT 1").fetchone()["topic_id"]
            conn.execute("INSERT INTO memberships(membership_id,claim_id,topic_id,topic_role,role,match_score,decision_source,decision_reason,created_at) "
                         "VALUES('semantic-membership',%s,%s,'primary','new_fact',1,'test','test',%s)", (claim_id, topic_id, now))
        result = aggregator._review_memberships({(claim_id, topic_id)}, now.isoformat())
        assert result["context"] == 1
        assert observed == [0]
        assert repository.count_pending_claim_reviews() == 0
        aggregator._rebuild_topic_participations({topic_id}, now.isoformat())
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-semantic-test", settings=postgres_settings) as conn:
            assert conn.execute("SELECT count(*) AS n FROM topic_participations WHERE topic_id=%s", (topic_id,)).fetchone()["n"] == 0
    finally:
        aggregator.close()


def test_postgres_brief_generation_runs_model_without_checkout_and_writes_revision(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime, timedelta
    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    borrowed = []

    def writer(connection, topic_id, at, evidence):
        borrowed.append(postgres_pool_stats()["borrowed"])
        topic = connection.execute("SELECT * FROM topics WHERE topic_id=?", (topic_id,)).fetchone()
        assert topic["working_title"]
        return {"title": topic["working_title"], "brief": "项目发布了新的上线安排。", "source_claim_ids": [evidence[0]["claim_id"]]}

    aggregator = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository, brief_writer=writer)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        item = {"tweet_id":"brief-tweet", "activity_account":"alice", "author":"alice", "created_at":now.isoformat(),
                "text":"ABC launch schedule announced", "expanded_text":"ABC launch schedule announced",
                "metrics":{}, "references":{}, "raw_payload":{}}
        aggregator.process_batch([item], now)
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-brief-test", settings=postgres_settings) as conn:
            conn.execute("UPDATE topics SET matching_status='active',visibility='hidden' WHERE matching_status='seed'")
        # The batch seed is active only after lifecycle qualification; write the
        # fixture directly to exercise the brief contract independently.
        with repository.aggregation_transaction() as unit:
            row = unit.connection.execute("SELECT topic_id FROM topics LIMIT 1").fetchone()
            topic_id = row["topic_id"]
            unit.connection.execute("UPDATE topics SET matching_status='active' WHERE topic_id=%s", (topic_id,))
        requests = aggregator._refresh_briefs({topic_id}, (now + timedelta(minutes=1)).isoformat(), {})
        assert requests[0]["brief_status"] == "ready"
        assert borrowed == [0]
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-brief-test", settings=postgres_settings) as conn:
            assert conn.execute("SELECT brief_status FROM topics WHERE topic_id=%s", (topic_id,)).fetchone()["brief_status"] == "ready"
            assert conn.execute("SELECT count(*) AS n FROM brief_revisions WHERE topic_id=%s", (topic_id,)).fetchone()["n"] == 1
    finally:
        aggregator.close()


def test_lifecycle_refresh_matches_sqlite_for_same_source_times(postgres_settings, hottopic_schema, tmp_path) -> None:
    from datetime import UTC, datetime, timedelta
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.topic_aggregator import TopicAggregator

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    pg = TopicAggregator(tmp_path / "unused.sqlite", postgres_repository=repository)
    sqlite = TopicAggregator(tmp_path / "reference.sqlite")
    now = datetime.now(UTC).replace(microsecond=0)
    topic_id = "topic:parity"
    contract = "0xabffa443547b34ab6c3b3173d26e233900527777"
    try:
        with sqlite.connection:
            sqlite.connection.execute(
                "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,identity_revision,participant_count_1h,participant_count_6h,participant_count_24h,participant_velocity,hotness_score,retention_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (topic_id,"$TRUMAN","truman","[]","AI/模型",now.isoformat(),now.isoformat(),(now+timedelta(hours=24)).isoformat(),now.isoformat(),now.isoformat(),"seed","hidden",1,0,0,0,0.0,0.0,"transient"),
            )
            for index, account in enumerate(("alpha","bravo","charlie","delta","echo")):
                at = now + timedelta(minutes=index)
                item_id, claim_id = f"content:{index}", f"claim:{index}"
                text = f"$TRUMAN project launches at {contract}"
                sqlite.connection.execute("INSERT INTO content_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",(item_id,f"tweet:{index}",account,account,"original",text,text,at.isoformat(),"url","{}","{}","{}",f"fp:{index}"))
                sqlite.connection.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?,?)",(claim_id,item_id,text,"reported_fact","[]","launch","neutral",text,1.0,1.0,at.isoformat()))
                sqlite.connection.execute("INSERT INTO memberships VALUES (?,?,?,?,?,?,?,?,?,?,?)",(f"m:{index}",claim_id,topic_id,None,"primary","new_fact",1.0,"test","test",at.isoformat(),None))
                sqlite.connection.execute("INSERT INTO topic_participations VALUES (?,?,?,?)",(topic_id,account,at.isoformat(),item_id))
        with connect_postgres(schema=hottopic_schema,application_name="odaily-hottopic-parity-test",settings=postgres_settings) as conn:
            with conn.transaction():
                conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,identity_revision) VALUES(%s,%s,%s,'[]',%s,%s,%s,%s,%s,%s,'seed','hidden',1)",(topic_id,"$TRUMAN","truman","AI/模型",now,now,now+timedelta(hours=24),now,now))
                for index, account in enumerate(("alpha","bravo","charlie","delta","echo")):
                    at=now+timedelta(minutes=index); item_id,claim_id=f"content:{index}",f"claim:{index}"; text=f"$TRUMAN project launches at {contract}"
                    conn.execute("INSERT INTO content_items VALUES(%s,%s,%s,%s,'original',%s,%s,%s,'url','{}','{}','{}',%s)",(item_id,f"tweet:{index}",account,account,text,text,at,f"fp:{index}"))
                    conn.execute("INSERT INTO claims VALUES(%s,%s,%s,'reported_fact','[]','launch','neutral',%s,1,1,%s)",(claim_id,item_id,text,text,at))
                    conn.execute("INSERT INTO memberships VALUES(%s,%s,%s,NULL,'primary','new_fact',1,'test','test',%s,NULL)",(f"m:{index}",claim_id,topic_id,at))
                    conn.execute("INSERT INTO topic_participations VALUES(%s,%s,%s,%s)",(topic_id,account,at,item_id))
        refresh_at=(now+timedelta(minutes=5)).isoformat()
        sqlite._refresh_all_topics(refresh_at)
        pg._refresh_all_topics(refresh_at,topic_ids={topic_id})
        sqlite_row=sqlite.connection.execute("SELECT matching_status,retention_tier,last_participation_at,participant_count_1h,participant_count_6h,participant_count_24h,participant_velocity,hotness_score FROM topics WHERE topic_id=?",(topic_id,)).fetchone()
        with connect_postgres(schema=hottopic_schema,application_name="odaily-hottopic-parity-test",settings=postgres_settings) as conn:
            pg_row=conn.execute("SELECT matching_status,retention_tier,last_participation_at,participant_count_1h,participant_count_6h,participant_count_24h,participant_velocity,hotness_score FROM topics WHERE topic_id=%s",(topic_id,)).fetchone()
        assert dict(sqlite_row) == {**dict(pg_row), "last_participation_at":pg_row["last_participation_at"].isoformat()}
    finally:
        pg.close()
        sqlite.close()


def test_hot_topic_postgres_account_and_poll_operations(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime, timedelta, timezone

    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC)
    at = now.isoformat()
    assert repository.seed_accounts([{"screen_name": "Alice", "display_name": "Alice A"}], now=at) == 1
    assert repository.seed_accounts([{"screen_name": "Alice"}], now=at) == 0
    account = repository.get_account("alice")
    assert account is not None and account["screen_name"] == "Alice"
    assert repository.list_accounts(query="ali", status="followed")[0]["screen_name_lower"] == "alice"
    assert repository.due_accounts(at=at)[0]["screen_name_lower"] == "alice"

    deployment_start = now - timedelta(days=1)
    post = {
        "tweet_id": "tweet:1", "created_at_iso": (deployment_start + timedelta(hours=2)).astimezone(timezone(timedelta(hours=-5))).isoformat(),
        "text": "A material event", "investment_only": False,
    }
    accepted = repository.store_poll(
        account_lower="alice", posts=[post], error=None, now=at,
        deployment_started_at=deployment_start.isoformat(), poll_delay_seconds=600,
    )
    assert accepted == 1
    assert repository.store_poll(
        account_lower="alice", posts=[post], error=None, now=at,
        deployment_started_at=deployment_start.isoformat(), poll_delay_seconds=600,
    ) == 0
    with connect_postgres(
        schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings,
    ) as conn:
        assert conn.execute("SELECT count(*) AS n FROM hottopic_inbox").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM x_agent_analysis_jobs").fetchone()["n"] == 0

    source_material = {
        "material_id": "material:investment-only", "role": "source_post", "relation": "original",
        "created_at_iso": at, "text": "An investment-only source", "author_screen_name": "alice",
        "url": "https://x.com/alice/status/investment-only", "links": [], "attachments": [],
    }
    investment_only = {
        "tweet_id": "tweet:investment-only", "created_at_iso": at,
        "text": "", "investment_only": True, "investment_materials": [source_material],
    }
    assert repository.store_poll(
        account_lower="alice", posts=[investment_only], error=None, now=at,
        deployment_started_at=deployment_start.isoformat(), poll_delay_seconds=600,
    ) == 0
    with connect_postgres(
        schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings,
    ) as conn:
        assert conn.execute("SELECT count(*) AS n FROM hottopic_inbox").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_materials").fetchone()["n"] == 1
        assert conn.execute("SELECT count(*) AS n FROM investment_lead_tasks WHERE stage='scan'").fetchone()["n"] == 1

    changed = repository.set_account_status("alice", "unfollowed", now=at)
    assert changed is not None and changed["next_due_at"] is None
    assert repository.due_accounts(at=(now + timedelta(days=1)).isoformat()) == []


def test_hot_topic_import_manifest_decodes_json_and_defaults_new_cursor_fields() -> None:
    from datetime import UTC, datetime

    from packages.hottopic.postgres_import_manifest import encode_hot_topic_import_row

    columns, values = encode_hot_topic_import_row(
        "topics",
        {"topic_id": "topic:1", "core_entities_json": '["Acme"]', "first_seen_at": "2026-11-18T00:00:00Z",
         "lifecycle_refresh_at": None},
        ("topic_id", "core_entities_json", "first_seen_at", "lifecycle_refresh_at"),
    )
    assert columns == ("topic_id", "core_entities_json", "first_seen_at")
    assert values[0] == "topic:1" and values[2] == datetime(2026, 11, 18, tzinfo=UTC)
    assert values[1].obj == ["Acme"]
    account_columns, account_values = encode_hot_topic_import_row(
        "hottopic_accounts", {"screen_name_lower": "alice", "protected": 1, "investment_leads_enabled": 0},
        ("screen_name_lower", "protected", "investment_leads_enabled"),
    )
    assert account_columns == ("screen_name_lower", "protected", "investment_leads_enabled")
    assert account_values == ("alice", True, False)

    batch_columns, batch_values = encode_hot_topic_import_row(
        "batches", {"batch_id": "b1", "batch_time": "2026-11-18T00:00:00Z", "result_json": "null"},
        ("batch_id", "batch_time", "result_json"),
    )
    assert batch_columns == ("batch_id", "batch_time", "result_json")
    assert batch_values[1] == "2026-11-18T00:00:00Z"
    assert batch_values[2].obj is None
    _, precision_values = encode_hot_topic_import_row(
        "batches", {"result_json": '{"price":0.123456789012345678901234567890123456789}'}, ("result_json",),
    )
    assert precision_values[0].dumps(precision_values[0].obj) == '{"price":0.123456789012345678901234567890123456789}'
    null_columns, null_values = encode_hot_topic_import_row(
        "x_agent_project_observations", {"direct_promotion_json": None}, ("direct_promotion_json",),
    )
    assert null_columns == ("direct_promotion_json",) and null_values == (None,)
    with pytest.raises(ValueError, match="invalid SQLite boolean"):
        encode_hot_topic_import_row("hottopic_accounts", {"protected": 2}, ("protected",))
    with pytest.raises(ValueError, match="source columns are not in the topics import contract"):
        encode_hot_topic_import_row("topics", {"topic_id": "topic:1", "unexpected_payload": "must not drop"}, ("topic_id",))
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        encode_hot_topic_import_row("batches", {"result_json": '{"price":1,"price":2}'}, ("result_json",))
    with pytest.raises(ValueError, match="non-standard JSON number"):
        encode_hot_topic_import_row("batches", {"result_json": '{"price":NaN}'}, ("result_json",))
    with pytest.raises(ValueError, match="non-standard JSON number"):
        encode_hot_topic_import_row("batches", {"result_json": '{"price":Infinity}'}, ("result_json",))
    _, retry_values = encode_hot_topic_import_row(
        "topics", {"brief_retry_after": "not-a-timestamp"}, ("brief_retry_after",),
    )
    assert retry_values == ("not-a-timestamp",)


def test_hot_topic_postgres_x_agent_claim_and_finish(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC)
    stamp = now.isoformat()
    repository.seed_accounts([{"screen_name": "Alice"}], now=stamp)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        with conn.transaction():
            conn.execute(
                "UPDATE hottopic_accounts SET market_sentiment_enabled=true WHERE screen_name_lower='alice'"
            )
    post = {
        "tweet_id": "tweet:x-agent", "created_at_iso": stamp, "account_screen_name": "Alice",
        "url": "https://x.com/Alice/status/tweet-x-agent", "text": "BTC outlook",
    }
    repository.store_poll(
        account_lower="alice", posts=[post], error=None, now=stamp,
        deployment_started_at=(now - timedelta(days=1)).isoformat(), poll_delay_seconds=600,
    )
    jobs = repository.claim_x_agent_jobs(now=stamp)
    assert len(jobs) == 1
    assert jobs[0]["module"] == "market_sentiment"
    assert jobs[0]["payload_json"]["text"] == "BTC outlook"
    result = {
        "actual_model": "test-model", "fallback_reason": None,
        "items": [{"instrument_name": "Bitcoin", "ticker": "BTC", "scope": "Crypto具体标的",
                   "sentiment": "偏多/乐观", "reason": "Test reason"}],
    }
    assert repository.finish_x_agent_job(
        jobs[0]["id"], module="market_sentiment", source=jobs[0]["payload_json"],
        result=result, error=None, attempts=jobs[0]["attempts"], now=stamp,
        claim_updated_at=jobs[0]["claim_updated_at"],
    )
    assert not repository.finish_x_agent_job(
        jobs[0]["id"], module="market_sentiment", source=jobs[0]["payload_json"],
        result=result, error=None, attempts=jobs[0]["attempts"], now=stamp,
        claim_updated_at=jobs[0]["claim_updated_at"],
    )
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        job = conn.execute("SELECT status,actual_model FROM x_agent_analysis_jobs").fetchone()
        assert (job["status"], job["actual_model"]) == ("succeeded", "test-model")
        saved = conn.execute("SELECT instrument_key,account_lower FROM x_agent_sentiment_results").fetchone()
        assert (saved["instrument_key"], saved["account_lower"]) == ("Crypto具体标的:btc", "alice")
        account = conn.execute("SELECT last_x_agent_analyzed_at,last_x_agent_error FROM hottopic_accounts WHERE screen_name_lower='alice'").fetchone()
        assert account["last_x_agent_analyzed_at"] is not None and account["last_x_agent_error"] is None


def test_hot_topic_postgres_x_agent_reclaims_lease_and_ignores_disabled_result(postgres_settings, hottopic_schema) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    now = datetime.now(UTC)
    stamp = now.isoformat()
    repository.seed_accounts([{"screen_name": "Alice"}], now=stamp)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        with conn.transaction():
            conn.execute("UPDATE hottopic_accounts SET market_sentiment_enabled=true WHERE screen_name_lower='alice'")
    post = {"tweet_id": "tweet:lease", "created_at_iso": stamp, "account_screen_name": "Alice", "text": "BTC outlook"}
    repository.store_poll(
        account_lower="alice", posts=[post], error=None, now=stamp,
        deployment_started_at=(now - timedelta(days=1)).isoformat(), poll_delay_seconds=600,
    )
    first_claim = repository.claim_x_agent_jobs(now=stamp)
    assert first_claim[0]["attempts"] == 1
    stale_finish_at = now + timedelta(minutes=6)
    assert not repository.finish_x_agent_job(
        first_claim[0]["id"], module="market_sentiment", source=first_claim[0]["payload_json"],
        result={"actual_model": "stale-worker", "items": []}, error=None,
        attempts=first_claim[0]["attempts"], now=stale_finish_at,
        claim_updated_at=first_claim[0]["claim_updated_at"],
    )
    reclaimed = repository.claim_x_agent_jobs(
        now=stale_finish_at.isoformat(), processing_lease=timedelta(minutes=5),
    )
    assert len(reclaimed) == 1 and reclaimed[0]["attempts"] == 2
    disabled_at = now + timedelta(minutes=7)
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        with conn.transaction():
            conn.execute("UPDATE hottopic_accounts SET market_sentiment_enabled=false WHERE screen_name_lower='alice'")
    assert repository.finish_x_agent_job(
        reclaimed[0]["id"], module="market_sentiment", source=reclaimed[0]["payload_json"],
        result={"actual_model": "test-model", "items": []}, error=None,
        attempts=reclaimed[0]["attempts"], now=disabled_at,
        claim_updated_at=reclaimed[0]["claim_updated_at"],
    )
    with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-schema-test", settings=postgres_settings) as conn:
        job = conn.execute("SELECT status FROM x_agent_analysis_jobs").fetchone()
        assert job["status"] == "ignored"
        assert conn.execute("SELECT count(*) AS n FROM x_agent_sentiment_results").fetchone()["n"] == 0


def test_hottopic_service_postgres_collection_and_x_agent_never_open_sqlite(
    postgres_settings, hottopic_schema, tmp_path, monkeypatch,
) -> None:
    from dataclasses import asdict
    from datetime import UTC, datetime, timedelta

    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic import service as service_module
    from packages.hottopic.capture import ContentItem
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.service import HotTopicService
    from packages.x_agent.analysis import AnalysisResult

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    opened_sqlite = False

    def forbid_sqlite(_path):
        nonlocal opened_sqlite
        opened_sqlite = True
        raise AssertionError("PostgreSQL HotTopic service must not open SQLite")

    monkeypatch.setattr(service_module, "open_worker_database", forbid_sqlite)
    monkeypatch.setenv("HOTTOPIC_CLAIM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", "0")
    (tmp_path / "collector-spool").mkdir()
    monkeypatch.setattr(
        service_module, "CollectorSpool",
        lambda _directory, max_bytes, max_record_bytes: __import__("packages.hottopic.collector_spool", fromlist=["CollectorSpool"]).CollectorSpool(
            tmp_path / "collector-spool", max_bytes=max_bytes, max_record_bytes=max_record_bytes,
        ),
    )
    service = HotTopicService(
        tmp_path / "unused.sqlite", primary_database_path=tmp_path / "primary.sqlite", workers=1,
        postgres_repository=repository,
    )
    try:
        service.add_x_agent_account("Alice", "Alice A")
        service.set_x_agent_subscriptions(["alice"], {"market_sentiment_enabled": True})

        now = datetime.now(UTC)
        content = ContentItem(
            account_screen_name="Alice", account_name="Alice A", tweet_id="tweet:service-pg",
            activity_type="original", author_screen_name="Alice", author_name="Alice A",
            created_at=now.strftime("%a %b %d %H:%M:%S +0000 %Y"), created_at_iso=now.isoformat(),
            url="https://x.com/Alice/status/tweet-service-pg", text="Bitcoin is bullish", expanded_text="Bitcoin is bullish",
            quote_id=None, quote_author_screen_name=None, quote_text=None, reply_to=None, replies=0,
            reposts=0, quotes=0, likes=1, views=10,
        )

        def fake_scan(*_args, **_kwargs):
            assert postgres_pool_stats()["borrowed"] == 0
            return [content], content.created_at_iso, None

        monkeypatch.setattr(service_module, "scan_account", fake_scan)
        assert service.poll_due_accounts_once() == {"polled": 1, "accepted": 1, "errors": 0}
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-service-test", settings=postgres_settings) as conn:
            baseline_count = conn.execute("SELECT cumulative_content_count FROM hottopic_accounts WHERE screen_name_lower='alice'").fetchone()["cumulative_content_count"]
            assert conn.execute("SELECT count(*) AS n FROM hottopic_collect_spool_receipts").fetchone()["n"] == 1
        duplicate = {
            "spool_id": "alice:duplicate-replay", "account": "alice", "items": [asdict(content)],
            "error": None, "at": content.created_at_iso,
        }
        service.collector_spool.append(duplicate)
        service._replay_collector_spool()
        service._replay_collector_spool()
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-service-test", settings=postgres_settings) as conn:
            assert conn.execute("SELECT cumulative_content_count FROM hottopic_accounts WHERE screen_name_lower='alice'").fetchone()["cumulative_content_count"] == baseline_count

        class Analyzer:
            def analyze(self, module, _source):
                assert module == "market_sentiment"
                assert postgres_pool_stats()["borrowed"] == 0
                return AnalysisResult(
                    module, "test-model", None,
                    [{"instrument_name": "Bitcoin", "ticker": "BTC", "scope": "Crypto具体标的",
                      "sentiment": "偏多/乐观", "reason": "Test"}],
                )

        service.x_agent_analyzer = Analyzer()
        assert service.process_x_agent_jobs() == 1
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-service-test", settings=postgres_settings) as conn:
            job = conn.execute("SELECT status,actual_model FROM x_agent_analysis_jobs WHERE tweet_id='tweet:service-pg'").fetchone()
            assert (job["status"], job["actual_model"]) == ("succeeded", "test-model")
            assert conn.execute("SELECT count(*) AS n FROM x_agent_sentiment_results WHERE tweet_id='tweet:service-pg'").fetchone()["n"] == 1
        assert service.list_accounts(status="followed")[0]["screen_name_lower"] == "alice"
        # Exercise the public worker/read surfaces while the SQLite tripwire is active.
        assert service.health()["databasePath"] == "postgresql"
        assert service.dashboard()["topics"] == []
        assert service.topic_detail("missing-topic") is None
        assert service.auto_newsflash_dashboard()["events"] == []
        assert service.list_market_sentiment(window="24h")["total"] == 1
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-service-test", settings=postgres_settings) as conn:
            instrument_key = conn.execute("SELECT instrument_key FROM x_agent_sentiment_results WHERE tweet_id='tweet:service-pg'").fetchone()["instrument_key"]
        assert service.market_sentiment_history(instrument_key)["items"] == []
        assert service.market_sentiment_detail(instrument_key)["items"]
        assert service.list_project_promotions(window="24h")["items"] == []
        assert service.maintain(force=True)["deleted_topics"] == 0
        assert not opened_sqlite
    finally:
        service.close()


def test_collection_does_not_spin_when_retry_has_no_spool_reservation(
    postgres_settings, hottopic_schema, tmp_path, monkeypatch,
) -> None:
    from datetime import UTC, datetime
    from packages.hottopic import service as service_module
    from packages.hottopic.capture import AccountRow
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.service import HotTopicService

    repo = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repo.initialize()
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    monkeypatch.setattr(service_module, "CollectorSpool", lambda _directory, max_bytes, max_record_bytes: __import__(
        "packages.hottopic.collector_spool", fromlist=["CollectorSpool"]
    ).CollectorSpool(spool_dir, max_bytes=100, max_record_bytes=60))
    monkeypatch.setenv("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_CLAIM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", "0")
    service = HotTopicService(tmp_path / "unused.sqlite", postgres_repository=repo, workers=1)
    try:
        repo.seed_accounts([{"screen_name": "Alice"}], now=datetime.now(UTC).isoformat())
        monkeypatch.setattr(service, "_due_accounts", lambda _db, limit=None: [AccountRow("Alice", "", "", 0, 0, False, "")])
        calls = 0
        def fake_scan(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                (spool_dir / "fills-capacity.json").write_text(
                    '{"spool_id":"fills-capacity","account":"alice","items":[],"error":null,"at":"2026-01-01T00:00:00+00:00"}',
                    encoding="utf-8",
                )
            return [], None, {"retryable": True, "error": "network"}
        monkeypatch.setattr(service_module, "scan_account", fake_scan)
        monkeypatch.setattr(service_module, "COLLECT_RETRY_DELAY_SECONDS", 0)
        result = service.poll_due_accounts_once()
        assert result["polled"] == 1
        assert result["errors"] == 0
    finally:
        service.close()


def test_postgres_yesterday_discussion_claim_and_status(postgres_settings, hottopic_schema, monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.yesterday_hot_discussion import YesterdayHotDiscussion

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    service = YesterdayHotDiscussion(postgres_repository=repository, push_client=object())
    now = datetime(2026, 11, 18, 4, 0, tzinfo=UTC)  # 12:00 in Shanghai
    result = service.run_if_due(now)
    assert result is not None and result.status == "no_candidate"
    assert service.run_if_due(now) is None
    status = service.dashboard_status(now)
    assert status["localDate"] == "2026-11-18"
    assert status["status"] == "no_candidate"
    assert status["candidateCount"] == 0

    class PushResult:
        ok = True
        error = None

    class PushClient:
        def __init__(self):
            self.calls = 0

        def push(self, **_kwargs):
            self.calls += 1
            return PushResult()

    push = PushClient()
    publishing = YesterdayHotDiscussion(postgres_repository=repository, push_client=push)
    original_select = publishing._select_topic
    publishing._select_topic = lambda _start, _end: ({
        "topic_id": "topic:daily", "title": "Acme announced a new product", "brief": "Acme announced a new product.",
    }, {"checked": 1, "excluded_analysis": 0, "excluded_no_action": 0})
    published = publishing.run_if_due(now + timedelta(days=1))
    assert published is not None and published.status == "backstage_created"
    assert push.calls == 1
    assert repository.yesterday_run("2026-11-19")["status"] == "backstage_created"
    publishing._select_topic = original_select


def test_postgres_hot_service_poll_cleanup_and_project_detail_use_native_repository(
    postgres_settings, hottopic_schema, tmp_path, monkeypatch,
) -> None:
    from datetime import UTC, datetime
    from packages.common.pg_native import connect_postgres
    from packages.hottopic import service as service_module
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.service import HotTopicService

    repository = HotTopicPostgresRepository(schema=hottopic_schema, settings=postgres_settings)
    repository.initialize()
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setattr(service_module, "CollectorSpool", lambda *_args, **_kwargs: __import__(
        "packages.hottopic.collector_spool", fromlist=["CollectorSpool"]
    ).CollectorSpool(spool, max_bytes=1024, max_record_bytes=512))
    monkeypatch.setenv("HOTTOPIC_CANDIDATE_RESOLVER_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_CLAIM_REVIEW_ENABLED", "0")
    monkeypatch.setenv("HOTTOPIC_TOPIC_MERGE_REVIEW_ENABLED", "0")
    monkeypatch.setattr(service_module, "open_worker_database", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("PostgreSQL HotTopic service opened SQLite")
    ))
    service = HotTopicService(tmp_path / "unused.sqlite", primary_database_path=tmp_path / "primary.sqlite",
                              workers=1, postgres_repository=repository)
    now = datetime.now(UTC)
    try:
        repository.seed_accounts([{"screen_name": "CleanupUser", "status": "blacklisted"}], now=now)
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-poll-cleanup-test", settings=postgres_settings) as conn:
            with conn.transaction():
                conn.execute("INSERT INTO hottopic_account_cleanup(screen_name_lower,queued_at) VALUES('cleanupuser',%s)", (now,))
                conn.execute(
                    "INSERT INTO content_items(content_item_id,tweet_id,activity_account,author,activity_type,content_text,expanded_text,created_at,source_url,metrics_json,references_json,raw_payload_json,fingerprint) "
                    "VALUES('cleanup-content','cleanup-tweet','cleanupuser','cleanupuser','original','text','text',%s,'url','{}','{}','{}','cleanup-fingerprint')",
                    (now,),
                )
                conn.execute(
                    "INSERT INTO x_agent_project_observations(identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,logic_hash,direct_promotion_json,operation_advice_json,account_lower,source_tweet_id,source_url,source_text,first_mentioned_at,last_mentioned_at,actual_model,created_at,updated_at) "
                    "VALUES('ca:0xabc','Project','PRJ','Base','0xABC','https://project.test','logic','hash1',%s,%s,'alice','project-tweet','https://x.test/1','A project update',%s,%s,'model',%s,%s)",
                    ('{"what":"launch"}', '{"action":"watch","profit_source":"fees"}', now, now, now, now),
                )

        monkeypatch.setattr(service, "_run_ai_review_if_due", lambda: {"status": "not_due"})
        for method in ("reconcile_topic_merges", "observe_topics", "retry_pending_identity_discoveries", "reverify_pending_account_bindings"):
            monkeypatch.setattr(service.event_tracker, method, lambda: None)
        monkeypatch.setattr(service, "_advance_event_tracking", lambda **_kwargs: {"polling": {"accepted": 0}})
        monkeypatch.setattr(service, "scan_visible_topic_merges", lambda: None)
        monkeypatch.setattr(service, "_due_accounts", lambda: [])
        monkeypatch.setattr(service, "aggregate", lambda: None)
        monkeypatch.setattr(service.yesterday_hot_discussion, "run_if_due", lambda: None)
        monkeypatch.setattr(service, "process_x_agent_jobs", lambda: 0)
        monkeypatch.setattr(service, "maintain", lambda: None)
        assert service.poll_once()["polled"] == 0
        detail = service.project_promotion_detail("ca:0xabc", window="30d")
        assert detail["total"] == 1
        assert detail["items"][0]["directPromotions"] == [{"what": "launch"}]
        assert detail["items"][0]["operationAdvices"][0]["profitSource"] == "fees"
        with connect_postgres(schema=hottopic_schema, application_name="odaily-hottopic-poll-cleanup-test", settings=postgres_settings) as conn:
            assert conn.execute("SELECT count(*) AS n FROM hottopic_account_cleanup").fetchone()["n"] == 0
            assert conn.execute("SELECT count(*) AS n FROM content_items WHERE content_item_id='cleanup-content'").fetchone()["n"] == 0
    finally:
        service.close()


