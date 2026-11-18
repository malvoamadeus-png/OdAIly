from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest


@pytest.fixture
def postgres_settings():
    from packages.common.pg_runtime import PostgresSettings
    dsn=os.getenv("ODAILY_TEST_POSTGRES_DSN")
    if not dsn:pytest.skip("ODAILY_TEST_POSTGRES_DSN must point to an isolated test database")
    return PostgresSettings(dsn=dsn,connect_timeout_seconds=5,statement_timeout_ms=10000,lock_timeout_ms=2000,
        idle_transaction_timeout_ms=10000,migration_statement_timeout_ms=60000,migration_lock_timeout_ms=5000,
        migration_idle_transaction_timeout_ms=30000,pool_max_size=2,pool_checkout_timeout_seconds=5,pool_max_waiting=16,
        application_name="odaily-event-test")


@pytest.fixture
def event_pg(postgres_settings):
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.postgres_repository import HotTopicPostgresRepository
    from packages.hottopic.postgres_event_tracking import PostgresEventTrackingRepository
    from packages.hottopic.postgres_schema import initialize_hottopic_schema
    from psycopg.sql import Identifier

    schema = "test_event_" + uuid.uuid4().hex[:12]
    with connect_postgres(schema="main", application_name="odaily-event-test", settings=postgres_settings) as conn:
        conn.execute("CREATE SCHEMA " + Identifier(schema).as_string(conn))
    settings = postgres_settings
    hot = HotTopicPostgresRepository(schema=schema, settings=settings)
    hot.initialize()
    repo = PostgresEventTrackingRepository(schema=schema, settings=settings)
    try:
        yield schema, settings, hot, repo
    finally:
        with connect_postgres(schema="main", application_name="odaily-event-test-clean", settings=settings) as conn:
            with conn.transaction():
                conn.execute("DELETE FROM main._odaily_schema_migrations WHERE schema_name=%s AND component IN ('hottopic','investment_leads')", (schema,))
            conn.execute("DROP SCHEMA IF EXISTS " + Identifier(schema).as_string(conn) + " CASCADE")


def test_postgres_event_tracker_judges_visible_topic_and_rejects_stale_snapshot(event_pg, monkeypatch) -> None:
    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic.event_tracking import EventModelResult, EventTracker

    schema, settings, hot, repo = event_pg
    now = datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema, application_name="odaily-event-test-seed", settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,matching_status,visibility,brief_status,hotness_score) VALUES('topic-e2e','Nova launches','Nova','[]','launch',%s,%s,%s,%s,'active','visible','ready',4)", (now, now, now + timedelta(days=1), now))
            conn.execute("INSERT INTO brief_revisions(topic_id,revision,title,brief,source_claim_ids_json,source_bindings_json,generated_at,generation_reason,content_hash) VALUES('topic-e2e',1,'Nova launches','Nova announced launch','[]','[]',%s,'test','hash')", (now,))

    class AI:
        def generate_json(self, *, model, prompt):
            assert postgres_pool_stats()["borrowed"] == 0
            topics = json.loads(prompt.rsplit("输入热点：", 1)[1])
            output = [{"topic_id": item["topic_id"], "decision": "track", "tracking_type": "security_asset_incident",
                       "reader_value": "asset_trading", "event_identity": "Nova launch risk", "confirmed_facts": ["Nova launches"],
                       "unconfirmed_claims": [], "reason": "official response is likely",
                       "official_response_hypothesis": {"entities": ["Nova"], "why_likely": "project response", "next_information": "impact"},
                       "recommended_initial_window_hours": 24, "stop_conditions": ["resolved"], "confidence": .9} for item in topics]
            return EventModelResult(json.dumps(output), model, {"total_tokens": 1}, 1.0, {}, [], [])

        def web_search_json(self, *, model, prompt):
            raise AssertionError("not used in topic judgment")

    tracker = EventTracker(None, primary_database_path=__import__("pathlib").Path("/tmp/unused.sqlite"), ai=AI(),
                           postgres_repository=repo, enabled=True, now=lambda: now)
    try:
        original = repo.topic_snapshots
        assert len(original()) == 1
        assert tracker.observe_topics() == {"assessed": 1, "tracked": 1, "failed": 0}
        with connect_postgres(schema=schema, application_name="odaily-event-test-link", settings=settings) as conn:
            event_id = conn.execute("SELECT event_id FROM event_tracking_topic_links WHERE topic_id='topic-e2e'").fetchone()["event_id"]
        event_id = repo.event_detail(event_id)
        assert event_id is not None
        dashboard = tracker.dashboard()
        assert dashboard["summary"]["eventsByStatus"]["discovering"] == 1
        assert tracker.topic_assessment_status("topic-e2e")["status"] == "tracked"

        # A model result for an obsolete topic snapshot must not be persisted.
        snap = original()[0]
        with connect_postgres(schema=schema, application_name="odaily-event-test-change", settings=settings) as conn:
            with conn.transaction():
                conn.execute("UPDATE topics SET hotness_score=hotness_score+1 WHERE topic_id='topic-e2e'")
        prompt = tracker._prompt("topic_judgment")
        result = EventModelResult("{}", "test", {}, 0.0, {}, [], [])
        assert repo.save_observations([snap], {"topic-e2e": {"decision": "do_not_track", "tracking_type": "none", "event_identity": "", "reason": "stale"}}, prompt=prompt, result=result, at=now) == 0
        assert repo.assessment("topic-e2e")["decision"] == "track"
    finally:
        tracker.close()


def test_postgres_event_tracking_failure_is_retryable(event_pg) -> None:
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema, settings, _hot, repo = event_pg
    now = datetime.now(UTC)
    with connect_postgres(schema=schema, application_name="odaily-event-test-seed", settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,matching_status,visibility) VALUES('topic-fail','Nova','Nova','[]','launch',%s,%s,%s,'active','visible')", (now, now, now + timedelta(days=1)))

    class FailingAI:
        def generate_json(self, **_): raise RuntimeError("temporary model failure")
        def web_search_json(self, **_): raise AssertionError

    tracker=EventTracker(None,primary_database_path=__import__("pathlib").Path("/tmp/unused.sqlite"),ai=FailingAI(),postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        assert tracker.observe_topics() == {"assessed":0,"tracked":0,"failed":1}
        assessment=repo.assessment("topic-fail")
        assert assessment["decision"] == "failed" and assessment["next_attempt_at"] > now
        assert tracker.observe_topics() == {"assessed":0,"tracked":0,"failed":0}
    finally: tracker.close()


def test_postgres_event_tracker_discover_poll_classify_dispatch_and_dismiss(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic.capture import AccountRow, ContentItem
    from packages.hottopic.event_tracking import EventModelResult, EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-full-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,matching_status,visibility,brief_status,hotness_score) VALUES('topic-full','Nova security incident','Nova','[]','security incident',%s,%s,%s,%s,'active','visible','ready',4)",(now,now,now+timedelta(days=1),now))
            conn.execute("INSERT INTO brief_revisions(topic_id,revision,title,brief,source_claim_ids_json,source_bindings_json,generated_at,generation_reason,content_hash) VALUES('topic-full',1,'Nova security incident','Nova is investigating an incident','[]','[]',%s,'test','hash')",(now,))

    class AI:
        def generate_json(self,*,model,prompt):
            assert postgres_pool_stats()["borrowed"]==0
            if "新闻值班追踪判断器" in prompt:
                topics=json.loads(prompt.split("输入热点：",1)[1]);data=[{"topic_id":t["topic_id"],"decision":"track","reader_value":"asset_trading","tracking_type":"security_asset_incident","event_identity":"Nova incident","confirmed_facts":["Nova incident"],"unconfirmed_claims":[],"reason":"Updates likely","official_response_hypothesis":{"entities":["Nova"],"why_likely":"incident handling","next_information":"fund recovery"},"recommended_initial_window_hours":24,"stop_conditions":["resolved"],"confidence":.9} for t in topics]
                return EventModelResult(json.dumps(data),model,{},1,{},[],[])
            if "X 账号身份判断器" in prompt:
                data=json.loads(prompt.rsplit("账号简介与近期原创帖子：",1)[1]);return EventModelResult(json.dumps({"accounts":[{"handle":a["handle"],"decision":"subject","entity":"Nova","role":"official_project","reason":"official project account"} for a in data]},ensure_ascii=False),model,{},1,{},[],[])
            if "事件账号帖子进展判断器" in prompt:
                data={"classification":"material_progress","news_type":"onchain","fact_summary":"Nova froze affected assets","difference_from_timeline":"First concrete mitigation","confirmed_facts":["Nova froze affected assets"],"unconfirmed_claims":[],"confidence":.9,"reason":"New impact information"}
                return EventModelResult(json.dumps(data),model,{},1,{},[],[])
            raise AssertionError(prompt[:100])
        def web_search_json(self,*,model,prompt):
            assert postgres_pool_stats()["borrowed"]==0
            return EventModelResult(json.dumps({"accounts":[{"handle":"NovaOfficial","display_name":"Nova","role":"project owner"}],"reason":"official"}),model,{},1,{},["https://nova.example"],[{"type":"web_search"}])

    class Dispatcher:
        def __init__(self):self.submitted=[];self.dismissed=[]
        def ensure_task(self,**kwargs):
            assert postgres_pool_stats()["borrowed"]==0
            return 700
        def submit(self,*,task_id,tweet_id):self.submitted.append((task_id,tweet_id))
        def task_status(self,task_id):return "judged"
        def is_event_tracking_cancelled(self,task_id):return False
        def task_detail(self,task_id):return None
        def dismiss_event_tasks(self,**kwargs):self.dismissed.append(kwargs);return {"cancelledTaskIds":[700],"publishedTaskIds":[],"terminalTaskIds":[]}

    dispatcher=Dispatcher()
    def identity(handle):
        assert postgres_pool_stats()["borrowed"]==0
        return {"status":"succeeded","profile":{"handle":handle,"display_name":"Nova","bio":"Official Nova project","profile_url":f"https://x.com/{handle}","posts":[{"tweet_id":"old","text":"We are investigating"}]}}
    tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),ai=AI(),dispatcher=dispatcher,account_identity_fetcher=identity,postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        assert tracker.observe_topics()=={"assessed":1,"tracked":1,"failed":0}
        discovery=tracker.discover_official_accounts();assert discovery=={"discovered":1,"failed":0,"bound":1}
        account,cutoff=tracker.due_accounts()[0];assert account.screen_name=="NovaOfficial"
        item=ContentItem(account_screen_name="NovaOfficial",account_name="Nova",tweet_id="official-update",activity_type="original",author_screen_name="NovaOfficial",author_name="Nova",created_at=now.strftime("%a %b %d %H:%M:%S +0000 %Y"),created_at_iso=now.isoformat(),url="https://x.com/NovaOfficial/status/official-update",text="We froze affected assets",expanded_text="We froze affected assets",quote_id=None,quote_author_screen_name=None,quote_text=None,reply_to=None,replies=0,reposts=0,quotes=0,likes=2,views=100)
        assert tracker._store_event_poll(account,[item],None)==1
        assert tracker.classify_official_updates()=={"classified":1,"material":1,"failed":0}
        result=tracker.dispatch_publications();assert result["submitted"]==1 and dispatcher.submitted==[(700,"official-update")]
        with connect_postgres(schema=schema,application_name="odaily-event-full-check",settings=settings) as conn:
            assert conn.execute("SELECT status FROM event_tracking_updates WHERE tweet_id='official-update'").fetchone()["status"]=="succeeded"
            assert conn.execute("SELECT status,primary_task_id FROM event_tracking_publication_outbox WHERE tweet_id='official-update'").fetchone()["status"]=="submitted"
        dismissal=tracker.dismiss_event(repo.dashboard()["events"][0]["id"],actor="operator");assert dismissal["dismissed"] and dismissal["cancelledTasks"]==1
        assert dispatcher.dismissed
        assert tracker.event_detail(dismissal["eventId"]) is None
    finally:tracker.close()


def test_postgres_event_tracker_merge_reconciliation_and_cycle_maintenance(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-merge-seed",settings=settings) as conn:
        with conn.transaction():
            for topic in ("source-topic","target-topic"):
                conn.execute("INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,matching_status,visibility) VALUES(%s,%s,%s,'[]','incident',%s,%s,%s,'active','visible')",(topic,topic,topic,now,now,now+timedelta(days=1)))
                conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at) VALUES(%s,%s,'[]',%s,'security_asset_incident','asset_trading','reason','active',%s,%s,%s)",(f"event-{topic}",f"identity-{topic}",topic,f"cycle-{topic}",now,now))
                conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES(%s,%s,'active',%s,%s,%s,%s,%s,%s)",(f"cycle-{topic}",f"event-{topic}",now,now+timedelta(hours=24),now+timedelta(hours=24),now+timedelta(days=7),now,now))
                conn.execute("INSERT INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at) VALUES(%s,%s,'hash',%s,%s,%s)",(f"event-{topic}",topic,json.dumps({"topic_id":topic}),now,now))
                conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,display_name,official_entity,official_relation,status,created_at,updated_at) VALUES(%s,%s,%s,%s,'official_project','active',%s,%s)",(f"{topic}@x",f"{topic}X",topic,topic,now,now))
                conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,discovered_at) VALUES(%s,%s,%s,'official_project','active','verified',%s)",(f"cycle-{topic}",f"event-{topic}",f"{topic}@x",now))
            conn.execute("INSERT INTO topic_merges(merge_id,source_topic_id,target_topic_id,reason,created_at) VALUES('merge-1','source-topic','target-topic','same incident',%s)",(now,))
    tracker=EventTracker(None,primary_database_path=__import__("pathlib").Path("/tmp/unused.sqlite"),postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        result=tracker.reconcile_topic_merges();assert result=={"moved":0,"retired":1}
        with connect_postgres(schema=schema,application_name="odaily-event-merge-check",settings=settings) as conn:
            assert conn.execute("SELECT COUNT(*) AS n FROM event_tracking_topic_links WHERE event_id='event-target-topic'").fetchone()["n"]==1
            assert conn.execute("SELECT COUNT(*) AS n FROM event_tracking_topic_links WHERE topic_id='source-topic'").fetchone()["n"]==0
            loser=conn.execute("SELECT status,end_reason FROM event_tracking_events WHERE event_id='event-source-topic'").fetchone()
            assert (loser["status"],loser["end_reason"])==("ended","topic_merged")
            assert conn.execute("SELECT end_reason FROM event_tracking_cycles WHERE cycle_id='cycle-source-topic'").fetchone()["end_reason"]=="topic_merged"
        with connect_postgres(schema=schema,application_name="odaily-event-expiry",settings=settings) as conn:
            with conn.transaction():
                conn.execute("UPDATE event_tracking_cycles SET started_at=%s,last_material_progress_at=NULL,expires_at=%s,hard_expires_at=%s WHERE cycle_id='cycle-target-topic'",(now-timedelta(days=2),now-timedelta(hours=1),now+timedelta(days=2)))
        assert tracker.maintain()=={"ended":1}
        with connect_postgres(schema=schema,application_name="odaily-event-expiry-check",settings=settings) as conn:
            row=conn.execute("SELECT status,end_reason FROM event_tracking_cycles WHERE cycle_id='cycle-target-topic'").fetchone()
            assert row["status"]=="ended" and row["end_reason"]=="silence_24h"
    finally:tracker.close()


def test_postgres_retries_cancellation_for_rejected_identity_binding(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-cancel-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,created_at,updated_at) VALUES('cancel-event','cancel-identity','[]','Incident','security_asset_incident','asset_trading','reason','ended',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES('cancel-cycle','cancel-event','ended',%s,%s,%s,%s,%s,%s)",(now,now,now,now,now,now))
            conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,official_entity,official_relation,status,created_at,updated_at) VALUES('wrong@x','WrongAccount','Wrong','official_project','inactive',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,discovered_at,released_at) VALUES('cancel-cycle','cancel-event','wrong@x','official_project','released','rejected',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at) VALUES('cancel-tweet','wrong@x','{}',%s)",(now,))
            conn.execute("INSERT INTO event_tracking_updates(update_id,event_id,cycle_id,tweet_id,handle_lower,status,classification,created_at,updated_at) VALUES('cancel-update','cancel-event','cancel-cycle','cancel-tweet','wrong@x','succeeded','material_progress',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_publication_outbox(outbox_id,update_id,event_id,cycle_id,tweet_id,status,created_at,updated_at) VALUES('cancel-outbox','cancel-update','cancel-event','cancel-cycle','cancel-tweet','submitted',%s,%s)",(now,now))

    class Dispatcher:
        def __init__(self):self.calls=[]
        def dismiss_event_tasks(self,**kwargs):self.calls.append(kwargs);return {"cancelledTaskIds":[701],"publishedTaskIds":[],"terminalTaskIds":[]}
    dispatcher=Dispatcher();tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),dispatcher=dispatcher,postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        assert tracker.retry_rejected_identity_task_cancellations()=={"cancelled":1,"deferred":0}
        assert dispatcher.calls==[{"event_id":"cancel-event","dismissed_at":now.isoformat(),"dismissed_by":"identity_review","handle":"wrong@x"}]
        with connect_postgres(schema=schema,application_name="odaily-event-cancel-check",settings=settings) as conn:
            row=conn.execute("SELECT cancelled_by,cancelled_at,next_attempt_at FROM event_tracking_publication_outbox WHERE outbox_id='cancel-outbox'").fetchone()
            assert row["cancelled_by"]=="identity_review" and row["cancelled_at"] is not None and row["next_attempt_at"] is None
            assert conn.execute("SELECT COUNT(*) AS n FROM event_tracking_audit WHERE kind='event_account_identity_review_cancelled_tasks'").fetchone()["n"]==1
    finally:tracker.close()


def test_postgres_reverify_pending_binding_defers_profile_failure(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-reverify-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at) VALUES('reverify-event','reverify-identity','[]','Incident','security_asset_incident','asset_trading','reason','active','reverify-cycle',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES('reverify-cycle','reverify-event','active',%s,%s,%s,%s,%s,%s)",(now,now+timedelta(hours=72),now+timedelta(hours=72),now+timedelta(days=7),now,now))
            conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,display_name,official_entity,official_relation,status,created_at,updated_at) VALUES('pending@x','PendingAccount','Pending','Nova','official_project','active',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,discovered_at) VALUES('reverify-cycle','reverify-event','pending@x','official_project','active','pending',%s)",(now,))

    class AI:
        def generate_json(self,**kwargs):raise AssertionError("no profile means no identity model call")
        def web_search_json(self,**kwargs):raise AssertionError
    def unavailable(_handle):return {"status":"failed","error":"temporary profile outage"}
    tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),ai=AI(),account_identity_fetcher=unavailable,postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        assert tracker.reverify_pending_account_bindings()=={"verified":0,"rejected":0,"deferred":1}
        with connect_postgres(schema=schema,application_name="odaily-event-reverify-check",settings=settings) as conn:
            row=conn.execute("SELECT status,identity_status,identity_result_json FROM event_tracking_bindings WHERE cycle_id='reverify-cycle'").fetchone()
            assert (row["status"],row["identity_status"])==("active","pending")
            assert "temporary profile outage" in json.dumps(row["identity_result_json"],ensure_ascii=False)
            assert conn.execute("SELECT status FROM event_tracking_cycles WHERE cycle_id='reverify-cycle'").fetchone()["status"]=="active"
    finally:tracker.close()


def test_postgres_dispatch_respects_disabled_tracker_and_classification_lease(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventModelResult, EventTracker
    from packages.hottopic.postgres_event_tracking import _stable

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-lease-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at) VALUES('lease-event','lease-identity','[]','Incident','security_asset_incident','asset_trading','reason','active','lease-cycle',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES('lease-cycle','lease-event','active',%s,%s,%s,%s,%s,%s)",(now,now+timedelta(hours=72),now+timedelta(hours=72),now+timedelta(days=7),now,now))
            conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,official_entity,official_relation,status,created_at,updated_at) VALUES('lease@x','LeaseAccount','Nova','official_project','active',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,identity_result_json,discovered_at) VALUES('lease-cycle','lease-event','lease@x','official_project','active','verified','{}',%s)",(now,))
            conn.execute("INSERT INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at) VALUES('lease-tweet','lease@x',%s,%s)",(json.dumps({"tweet_id":"lease-tweet","author_screen_name":"LeaseAccount","created_at_iso":now.isoformat()}),now))
            conn.execute("INSERT INTO event_tracking_work_leases(work_key,claim_token,claimed_at,lease_until,attempts) VALUES(%s,'stale-token',%s,%s,1)",(f"classify:event-update-{_stable('lease-cycle','lease-tweet')}",now,now+timedelta(minutes=5)))
    class AI:
        def generate_json(self,*,model,prompt):raise AssertionError("owned lease must prevent model call")
        def web_search_json(self,**kwargs):raise AssertionError
    tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),ai=AI(),postgres_repository=repo,enabled=False,now=lambda:now)
    try:
        assert tracker.dispatch_publications()=={"submitted":0,"failed":0,"duplicate":0,"published":0}
        tracker.enabled=True
        assert tracker.classify_official_updates()=={"classified":0,"material":0,"failed":0}
    finally:tracker.close()


def test_postgres_initialization_reopens_recent_legacy_discovery(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0);old=now-timedelta(hours=20)
    with connect_postgres(schema=schema,application_name="odaily-event-reopen-discovery-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_prompt_versions(prompt_key,version_number,content,created_at) VALUES('official_discovery',1,'old prompt',%s)",(old,))
            prompt=conn.execute("SELECT id FROM event_tracking_prompt_versions WHERE prompt_key='official_discovery' AND version_number=1").fetchone()
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at,ended_at,end_reason) VALUES('legacy-reopen','legacy-reopen-identity','[]','Incident','security_asset_incident','asset_trading','reason','discovery_failed','legacy-cycle',%s,%s,%s,'discovery_failed')",(old,old,old))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,ended_at,end_reason,created_at,updated_at) VALUES('legacy-cycle','legacy-reopen','ended',%s,%s,%s,%s,%s,'discovery_failed',%s,%s)",(old,old+timedelta(hours=72),old+timedelta(hours=72),old+timedelta(days=7),old+timedelta(hours=1),old,old))
            conn.execute("INSERT INTO event_tracking_account_discoveries(discovery_id,event_id,cycle_id,status,attempts,prompt_version_id,result_json,error,created_at,completed_at) VALUES('legacy-discovery','legacy-reopen','legacy-cycle','failed',1,%s,'{}','old failure',%s,%s)",(prompt["id"],old,old))
    tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        with connect_postgres(schema=schema,application_name="odaily-event-reopen-discovery-check",settings=settings) as conn:
            event=conn.execute("SELECT status,current_cycle_id FROM event_tracking_events WHERE event_id='legacy-reopen'").fetchone()
            cycle=conn.execute("SELECT status,initial_expires_at FROM event_tracking_cycles WHERE cycle_id=%s",(event["current_cycle_id"],)).fetchone()
            assert event["status"]=="discovering" and event["current_cycle_id"]!="legacy-cycle"
            assert cycle["status"]=="discovering" and cycle["initial_expires_at"]-now==timedelta(hours=72)
            assert conn.execute("SELECT COUNT(*) AS n FROM event_tracking_audit WHERE kind='legacy_discovery_reopened'").fetchone()["n"]==1
    finally:tracker.close()


def test_postgres_classification_result_is_fenced_after_cycle_ends(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres, postgres_pool_stats
    from packages.hottopic.event_tracking import EventModelResult, EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-stale-classify-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at) VALUES('stale-event','stale-identity','[]','Incident','security_asset_incident','asset_trading','reason','active','stale-cycle',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES('stale-cycle','stale-event','active',%s,%s,%s,%s,%s,%s)",(now,now+timedelta(hours=72),now+timedelta(hours=72),now+timedelta(days=7),now,now))
            conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,official_entity,official_relation,status,created_at,updated_at) VALUES('stale@x','StaleAccount','Nova','official_project','active',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,identity_result_json,discovered_at) VALUES('stale-cycle','stale-event','stale@x','official_project','active','verified','{}',%s)",(now,))
            conn.execute("INSERT INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at) VALUES('stale-tweet','stale@x',%s,%s)",(json.dumps({"tweet_id":"stale-tweet","author_screen_name":"StaleAccount","created_at_iso":now.isoformat()}),now))
    class AI:
        def generate_json(self,*,model,prompt):
            assert postgres_pool_stats()["borrowed"]==0
            with connect_postgres(schema=schema,application_name="odaily-event-stale-classify-change",settings=settings) as conn:
                with conn.transaction():conn.execute("UPDATE event_tracking_cycles SET status='ended',end_reason='silence_24h' WHERE cycle_id='stale-cycle'")
            return EventModelResult(json.dumps({"classification":"material_progress","news_type":"regular","fact_summary":"new claim","difference_from_timeline":"new claim","confirmed_facts":[],"unconfirmed_claims":["new claim"],"confidence":.9,"reason":"new claim"}),model,{},1,{},[],[])
        def web_search_json(self,**kwargs):raise AssertionError
    tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),ai=AI(),postgres_repository=repo,enabled=True,now=lambda:now)
    try:
        assert tracker.classify_official_updates()=={"classified":0,"material":0,"failed":0}
        with connect_postgres(schema=schema,application_name="odaily-event-stale-classify-check",settings=settings) as conn:
            assert conn.execute("SELECT status FROM event_tracking_updates WHERE tweet_id='stale-tweet'").fetchone()["status"]=="processing"
            assert conn.execute("SELECT COUNT(*) AS n FROM event_tracking_publication_outbox WHERE event_id='stale-event'").fetchone()["n"]==0
    finally:tracker.close()


def test_postgres_dispatch_cancels_task_created_after_event_dismissal(event_pg) -> None:
    from pathlib import Path
    from packages.common.pg_native import connect_postgres
    from packages.hottopic.event_tracking import EventTracker

    schema,settings,_hot,repo=event_pg;now=datetime.now(UTC).replace(microsecond=0)
    with connect_postgres(schema=schema,application_name="odaily-event-dispatch-race-seed",settings=settings) as conn:
        with conn.transaction():
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,rationale,status,current_cycle_id,created_at,updated_at) VALUES('race-event','race-identity','[]','Incident','security_asset_incident','asset_trading','reason','active','race-cycle',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES('race-cycle','race-event','active',%s,%s,%s,%s,%s,%s)",(now,now+timedelta(hours=72),now+timedelta(hours=72),now+timedelta(days=7),now,now))
            conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,official_entity,official_relation,status,created_at,updated_at) VALUES('race@x','RaceAccount','Nova','official_project','active',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,identity_result_json,discovered_at) VALUES('race-cycle','race-event','race@x','official_project','active','verified','{}',%s)",(now,))
            conn.execute("INSERT INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at,processed_at) VALUES('race-tweet','race@x',%s,%s,%s)",(json.dumps({"tweet_id":"race-tweet","author_screen_name":"RaceAccount","text":"update"}),now,now))
            conn.execute("INSERT INTO event_tracking_updates(update_id,event_id,cycle_id,tweet_id,handle_lower,status,classification,news_type,fact_summary,difference_text,created_at,updated_at) VALUES('race-update','race-event','race-cycle','race-tweet','race@x','succeeded','material_progress','regular','new','new',%s,%s)",(now,now))
            conn.execute("INSERT INTO event_tracking_publication_outbox(outbox_id,update_id,event_id,cycle_id,tweet_id,status,attempts,next_attempt_at,created_at,updated_at) VALUES('race-outbox','race-update','race-event','race-cycle','race-tweet','pending',0,%s,%s,%s)",(now,now,now))
    holder={}
    class Dispatcher:
        def __init__(self):self.submitted=[];self.dismissed=[]
        def ensure_task(self,**_kwargs):
            holder["tracker"].dismiss_event("race-event",actor="race")
            return 902
        def submit(self,**kwargs):self.submitted.append(kwargs)
        def task_status(self,_task):return "judged"
        def is_event_tracking_cancelled(self,_task):return False
        def task_detail(self,_task):return None
        def dismiss_event_tasks(self,**kwargs):self.dismissed.append(kwargs);return {"cancelledTaskIds":[902],"publishedTaskIds":[],"terminalTaskIds":[]}
    dispatcher=Dispatcher();tracker=EventTracker(None,primary_database_path=Path("/tmp/unused.sqlite"),postgres_repository=repo,enabled=True,now=lambda:now);tracker.dispatcher=dispatcher;holder["tracker"]=tracker
    try:
        assert not repo._event_no_longer_publishable({"event_id":"race-event","cycle_id":"race-cycle","handle_lower":"race@x"})
        result=tracker.dispatch_publications()
        assert result["submitted"]==0 and not dispatcher.submitted
        with connect_postgres(schema=schema,application_name="odaily-event-dispatch-race-check",settings=settings) as conn:
            row=conn.execute("SELECT status,cancelled_at,cancelled_by FROM event_tracking_publication_outbox WHERE outbox_id='race-outbox'").fetchone()
            assert any(call.get("dismissed_by")=="event_tracking_fence" for call in dispatcher.dismissed), (result,dispatcher.dismissed)
            assert row["cancelled_at"] is not None and row["cancelled_by"]=="race"
    finally:tracker.close()
