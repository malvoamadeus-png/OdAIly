from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from scripts.reconcile_frozen_increment import reconcile


@pytest.fixture
def isolated_pg():
    dsn = "postgresql://odaily_test:odaily_test_local_only@127.0.0.1:55432/postgres"
    name = "odaily_reconcile_" + uuid.uuid4().hex[:10]
    admin = psycopg.connect(dsn, autocommit=True)
    database = name
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    admin.close()
    conn = psycopg.connect(dsn.replace("/postgres", f"/{database}"), autocommit=True, row_factory=dict_row)
    conn.execute("CREATE SCHEMA main")
    conn.execute("CREATE SCHEMA hottopic")
    conn.execute("""
        CREATE TABLE main.tasks(id bigint primary key,source text not null,source_item_id text not null,
          source_url text,title text,content text not null,published_at timestamptz,raw_payload jsonb not null,
          metadata jsonb not null,status text not null,created_at timestamptz not null,updated_at timestamptz not null,
          locked_by text,locked_until timestamptz,attempt_count integer not null,unique(source,source_item_id));
        CREATE TABLE main.newsflash_items(id bigint primary key,source text not null,source_item_id text not null,
          source_url text,source_url_key text,title text,content text not null,content_hash text not null,
          published_at timestamptz,first_seen_at timestamptz not null,raw_payload jsonb not null,metadata jsonb not null,
          created_at timestamptz not null,updated_at timestamptz not null,unique(source,source_item_id));
        CREATE TABLE main.x_capture_accounts(id bigint primary key);
        CREATE TABLE main.x_capture_attempts(id bigint primary key,account_id bigint references main.x_capture_accounts(id),
          username_lower text not null,status text not null,source text not null,candidate_count integer not null,
          seeded_count integer not null,new_count integer not null,saved_count integer not null,error text,
          started_at timestamptz not null,finished_at timestamptz not null,metadata jsonb not null);
        CREATE TABLE main.search_event_candidates(id bigint primary key,primary_task_id bigint references main.tasks(id),
          status text not null,title text,content text not null,content_hash text not null,metadata jsonb not null,
          expires_at timestamptz,created_at timestamptz not null,updated_at timestamptz not null);
        CREATE TABLE main.search_event_sources(id bigint primary key,candidate_id bigint not null references main.search_event_candidates(id),
          task_id bigint references main.tasks(id),source text not null,source_item_id text not null,source_url text,title text,
          content text not null,role text not null,metadata jsonb not null,created_at timestamptz not null,updated_at timestamptz not null,
          unique(candidate_id,task_id));
        CREATE TABLE main.x_task_pipeline(task_id bigint primary key references main.tasks(id),candidate_id bigint references main.search_event_candidates(id),
          prompt_template_key text,prompt_version_id bigint,writer_feature_mode_enabled boolean,judge_output jsonb not null,search_result jsonb not null,
          writer_output jsonb not null,publisher_output jsonb not null,push_result jsonb not null,telegram_result jsonb not null,
          created_at timestamptz not null,updated_at timestamptz not null);
        CREATE TABLE main.x_processing_attempts(id bigint primary key,task_id bigint not null references main.tasks(id),stage text not null,
          attempt_no integer not null,executor text not null,status text not null,started_at timestamptz not null,
          response_received_at timestamptz,finished_at timestamptz,input_json jsonb not null,rule_json jsonb not null,
          response_gzip bytea,response_sha256 text,response_bytes bigint,response_ref text,response_excerpt text,
          response_archive_error text,result_json jsonb not null,error_code text,error_message text,
          odaily_external_error_counted integer not null,unique(task_id,stage,attempt_no));
        CREATE TABLE main.x_processing_attempt_events(id bigint primary key,attempt_id bigint not null references main.x_processing_attempts(id),
          task_id bigint not null references main.tasks(id),event_type text not null,status text not null,happened_at timestamptz not null,
          error_code text,error_message text,call_no integer,executor text,response_gzip bytea,response_sha256 text,
          response_bytes bigint,response_ref text,response_excerpt text,response_archive_error text);
        CREATE TABLE hottopic.hottopic_inbox(tweet_id text primary key,screen_name_lower text not null,payload_json jsonb not null,
          collected_at timestamptz not null,processed_at timestamptz,error text);
        CREATE TABLE hottopic.x_agent_analysis_jobs(id bigint primary key,tweet_id text not null,module text not null,status text not null,
          attempts integer not null,next_attempt_at timestamptz,actual_model text,fallback_reason text,last_error text,
          created_at timestamptz not null,updated_at timestamptz not null,completed_at timestamptz,unique(tweet_id,module));
    """)
    conn.execute("INSERT INTO main.x_capture_accounts VALUES (3)")
    try:
        yield conn
    finally:
        conn.close()
        admin = psycopg.connect(dsn, autocommit=True)
        admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database),))
        admin.close()


def _sources(tmp_path: Path):
    main_path, hot_path = tmp_path / "main.sqlite", tmp_path / "hot.sqlite"
    with sqlite3.connect(main_path) as db:
        db.executescript("""
          CREATE TABLE odaily_observability_outbox(seq integer primary key,table_name text,source_row_id text,operation text);
          CREATE TABLE tasks(id integer primary key,source text,source_item_id text,source_url text,title text,content text,
            published_at text,raw_payload text,metadata text,status text,created_at text,updated_at text,locked_by text,locked_until text,attempt_count integer);
          CREATE TABLE newsflash_items(id integer primary key,source text,source_item_id text,source_url text,source_url_key text,
            title text,content text,content_hash text,published_at text,first_seen_at text,raw_payload text,metadata text,created_at text,updated_at text);
          CREATE TABLE x_capture_attempts(id integer primary key,account_id integer,username_lower text,status text,source text,
            candidate_count integer,seeded_count integer,new_count integer,saved_count integer,error text,started_at text,finished_at text,metadata text);
          CREATE TABLE x_capture_accounts(id integer primary key);
          CREATE TABLE x_task_pipeline(task_id integer primary key,candidate_id integer,prompt_template_key text,prompt_version_id integer,writer_feature_mode_enabled integer,
            judge_output text,search_result text,writer_output text,publisher_output text,push_result text,telegram_result text,created_at text,updated_at text);
          CREATE TABLE x_processing_attempts(id integer primary key,task_id integer,stage text,attempt_no integer,executor text,status text,
            started_at text,response_received_at text,finished_at text,input_json text,rule_json text,response_gzip blob,response_sha256 text,
            response_bytes integer,response_ref text,response_excerpt text,response_archive_error text,result_json text,error_code text,error_message text,odaily_external_error_counted integer);
          CREATE TABLE x_processing_attempt_events(id integer primary key,attempt_id integer,task_id integer,event_type text,status text,
            happened_at text,error_code text,error_message text,call_no integer,executor text,response_gzip blob,response_sha256 text,
            response_bytes integer,response_ref text,response_excerpt text,response_archive_error text);
          CREATE TABLE search_event_candidates(id integer primary key,primary_task_id integer,status text,title text,content text,content_hash text,metadata text,expires_at text,created_at text,updated_at text);
          CREATE TABLE search_event_sources(id integer primary key,candidate_id integer,task_id integer,source text,source_item_id text,source_url text,title text,content text,role text,metadata text,created_at text,updated_at text);
        """)
        db.execute("INSERT INTO tasks VALUES(1,'msx','msx:1',null,'t','body',null,'{}','{}','ready_review','2026-01-01','2026-01-01',null,null,3)")
        db.execute("INSERT INTO tasks VALUES(2,'x','tweet',null,'pending','body',null,'{}','{}','pending','2026-01-01','2026-01-01',null,null,0)")
        db.execute("INSERT INTO x_task_pipeline VALUES(1,7,'tpl',9,0,'{}','{}','{}','{}','{}','{}','2026-01-01','2026-01-01')")
        db.execute("INSERT INTO search_event_candidates VALUES(7,1,'active','t','body','hash','{}',null,'2026-01-01','2026-01-01')")
        db.execute("INSERT INTO search_event_sources VALUES(8,7,1,'msx','msx:1',null,'t','body','primary','{}','2026-01-01','2026-01-01')")
        db.execute("INSERT INTO x_processing_attempts VALUES(10,1,'format_publish',1,'worker','succeeded','2026-01-01',null,'2026-01-01','{}','{}',null,null,null,null,null,null,'{}',null,null,0)")
        db.execute("INSERT INTO x_processing_attempt_events VALUES(11,10,1,'completed','succeeded','2026-01-01',null,null,1,'worker',null,null,null,null,null,null)")
        db.execute("INSERT INTO x_capture_attempts VALUES(20,3,'user','success','fxtwitter',0,0,0,0,null,'2026-01-01','2026-01-01','{}')")
        db.execute("INSERT INTO x_capture_accounts VALUES(3)")
        db.execute("INSERT INTO newsflash_items VALUES(30,'feed','item',null,null,'n','body','hash',null,'2026-01-01','{}','{}','2026-01-01','2026-01-01')")
        db.executemany("INSERT INTO odaily_observability_outbox VALUES(?,?,?,'insert')",[(2,'tasks','1'),(3,'tasks','2'),(4,'newsflash_items','30'),(5,'x_capture_attempts','20')])
    with sqlite3.connect(hot_path) as db:
        db.executescript("""
          CREATE TABLE odaily_observability_outbox(seq integer primary key,table_name text,source_row_id text,operation text);
          CREATE TABLE x_agent_analysis_jobs(id integer primary key,tweet_id text,module text,status text,attempts integer,next_attempt_at text,actual_model text,fallback_reason text,last_error text,created_at text,updated_at text,completed_at text);
          CREATE TABLE hottopic_inbox(tweet_id text primary key,screen_name_lower text,payload_json text,collected_at text,processed_at text,error text);
        """)
        db.execute("INSERT INTO hottopic_inbox VALUES('tweet-1','user','{}','2026-01-01',null,null)")
        db.execute("INSERT INTO x_agent_analysis_jobs VALUES(40,'tweet-1','market_sentiment','pending',0,null,null,null,null,'2026-01-01','2026-01-01',null)")
        db.execute("INSERT INTO odaily_observability_outbox VALUES(2,'x_agent_analysis_jobs','40','insert')")
    return {"main": main_path, "hottopic": hot_path}


def test_frozen_increment_import_preserves_links_skips_pending_and_is_idempotent(tmp_path, isolated_pg):
    sources = _sources(tmp_path)
    bounds = {"main": (1, 5), "hottopic": (1, 2)}
    dry = reconcile(sources=sources, bounds=bounds, apply=False, source_writes_stopped=False, connection=isolated_pg)
    assert dry["counts"]["planned_inserts"] == 10
    assert dry["counts"]["pending_tasks"] == 1
    assert dry["counts"]["conflicts"] == 0
    with pytest.raises(RuntimeError, match="pending tasks require explicit"):
        reconcile(sources=sources, bounds=bounds, apply=True, source_writes_stopped=True, connection=isolated_pg)
    applied = reconcile(sources=sources, bounds=bounds, apply=True, source_writes_stopped=True,
                        allow_pending_tasks=True, connection=isolated_pg)
    assert applied["counts"]["inserted"] == 11
    assert isolated_pg.execute("select status,attempt_count from main.tasks where id=1").fetchone() == {"status": "ready_review", "attempt_count": 3}
    assert isolated_pg.execute("select count(*) n from main.x_processing_attempt_events").fetchone()["n"] == 1
    assert isolated_pg.execute("select count(*) n from hottopic.hottopic_inbox").fetchone()["n"] == 1
    assert isolated_pg.execute("select status from main.tasks where id=2").fetchone()["status"] == "pending"
    rerun = reconcile(sources=sources, bounds=bounds, apply=False, source_writes_stopped=False, connection=isolated_pg)
    assert rerun["counts"]["conflicts"] == 0
    assert rerun["counts"]["existing"] == 5


def test_frozen_increment_conflict_fails_closed(tmp_path, isolated_pg):
    sources = _sources(tmp_path)
    isolated_pg.execute("insert into main.newsflash_items values(30,'wrong','item',null,null,'n','body','hash',null,'2026-01-01','{}','{}','2026-01-01','2026-01-01')")
    report = reconcile(sources=sources, bounds={"main": (1, 5), "hottopic": (1, 2)},
                       apply=False, source_writes_stopped=False, connection=isolated_pg)
    assert report["counts"]["conflicts"] == 1
