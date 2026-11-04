from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from packages.system_observability import MainTaskProjector, SystemInformationStore
from packages.system_observability.error_policy import day_start_ms, failure_category
from packages.system_observability.error_retention import cleanup_dependency_errors, cleanup_observation_errors, dependency_error_totals
from packages.system_observability.news import task_admission
from packages.system_observability.projector import TABLE_SPECS
from packages.system_observability.store import timestamp_ms
from test_system_observability import _source_db


def _store(root: Path) -> SystemInformationStore:
    store = SystemInformationStore(root / 'observability.sqlite')
    store.initialize()
    return store


def test_detail_batches_evidence_lookup_and_decompresses_each_reference_once(tmp_path, monkeypatch):
    store = _store(tmp_path)
    now = int(datetime.now(UTC).timestamp() * 1000)
    evidence = {'task': {'content': 'large source material' * 1000}}
    store.upsert_snapshot({
        'item_id': 'news:batch', 'business_key': 'x', 'source': 'x', 'title': 'Batch',
        'status': 'published', 'result': 'direct_publish', 'created_at_ms': now,
        'updated_at_ms': now, 'source_database': 'fixture', 'source_table': 'tasks',
        'source_row_id': 'batch', 'news_visible': True,
    }, evidence)
    for index in range(20):
        store.append_event(item_id='news:batch', event_id=f'batch:{index}', event_type='stage',
                           stage='writing', status='published', result='direct_publish',
                           happened_at_ms=now, summary='Evidence', evidence=evidence)
    connections = []
    original = store.connect
    def connect():
        connections.append(True)
        return original()
    monkeypatch.setattr(store, 'connect', connect)
    from unittest.mock import patch
    import gzip
    with patch('packages.system_observability.store.gzip.decompress', wraps=gzip.decompress) as decompress:
        detail = store.detail('news:batch')
    assert detail['evidence'] == evidence
    assert all(event['evidence'] == evidence for event in detail['timeline'])
    assert len(detail['timeline']) >= 20
    assert len(connections) == 1
    assert decompress.call_count == 1


def test_news_view_hides_low_value_and_technical_rows_but_keeps_search_first_duplicates(tmp_path: Path) -> None:
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute('ALTER TABLE x_task_pipeline ADD COLUMN search_result TEXT')
        conn.execute("INSERT INTO tasks VALUES(4,'blockbeats','dup-4','https://example/4','已有新闻','正文','duplicate','2026-01-01',NULL,'{}')")
        conn.execute('INSERT INTO x_task_pipeline(task_id,search_result) VALUES(4,?)', (json.dumps({'duplicate_target': {'title': '命中稿件', 'target_id': '123'}}),))
    store = _store(tmp_path)
    MainTaskProjector(source, store).run_once()
    page = store.query({'view': 'news'})
    assert {row['task_id'] for row in page['items']} == {1, 2, 4}
    duplicate = next(row for row in page['items'] if row['task_id'] == 4)
    assert duplicate['admission'] == 'duplicate'
    assert duplicate['duplicate_target']['title'] == '命中稿件'
    assert store.query({'view': 'news', 'q': '未知来源'})['total'] == 0
    assert store.query({'view': 'diagnostic', 'q': '未知来源'})['total'] == 1
    assert next(row for row in page['items'] if row['task_id'] == 2)['result'] == 'processing'


def test_deduped_is_a_passed_prerequisite_not_duplicate() -> None:
    assert task_admission('deduped', {}, 'x') == 'accepted'
    assert task_admission('searched', {}, 'blockbeats') == 'unassessed'
    assert task_admission('duplicate', {}, 'blockbeats') == 'duplicate'
    assert task_admission('discarded', {'judge_output': {'route': 'discard'}}, 'x') == 'rejected'


def test_latest_phase_time_wins_and_clock_refresh_does_not_append_history(tmp_path: Path) -> None:
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE tasks SET updated_at='2026-01-01T00:00:00Z' WHERE id=1")
        conn.execute("UPDATE x_task_pipeline SET write_completed_at='2026-01-02T00:00:00Z' WHERE task_id=1")
    store = _store(tmp_path)
    projector = MainTaskProjector(source, store)
    projector.run_once()
    detail = store.detail('task:x:post-1')
    assert detail['updated_at_ms'] == timestamp_ms('2026-01-02T00:00:00Z')
    events = len(detail['timeline'])
    evidence = detail['evidence_ref']
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE x_task_pipeline SET write_completed_at='2026-01-03T00:00:00Z' WHERE task_id=1")
    projector.run_once()
    detail = store.detail('task:x:post-1')
    assert detail['updated_at_ms'] == timestamp_ms('2026-01-03T00:00:00Z')
    assert len(detail['timeline']) == events
    assert detail['evidence_ref'] == evidence
    assert store.query({'view': 'news', 'limit': 1})['items'][0]['task_id'] == 1


def test_numeric_title_uses_source_text_instead_of_internal_id(tmp_path: Path) -> None:
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE tasks SET title='2117816400630947922' WHERE id=2")
    store = _store(tmp_path)
    MainTaskProjector(source, store).run_once()
    assert store.detail('task:jin10:item-2')['title'] == '美股市场收盘'


def test_post_publish_audit_is_only_in_original_news_detail(tmp_path: Path) -> None:
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.executescript('''
            CREATE TABLE newsflash_production_metadata(delivery_key TEXT,odaily_id TEXT,id_match_status TEXT);
            INSERT INTO newsflash_production_metadata VALUES('odaily-task-1','official-1','matched');
            CREATE TABLE fact_checks(id INTEGER PRIMARY KEY,source_item_id TEXT,title TEXT,status TEXT,updated_at TEXT);
            INSERT INTO fact_checks VALUES(1,'official-1','定稿标题','processing','2026-01-04');
        ''')
    store = _store(tmp_path)
    projector = MainTaskProjector(source, store, additional_databases={'main': source},
                                  table_specs=tuple(s for s in TABLE_SPECS if s.table == 'fact_checks'))
    projector.run_once()
    assert not any(row['source'] == 'fact_audit' for row in store.query({'view': 'news'})['items'])
    detail = store.detail('task:x:post-1')
    assert any(event['stage'] == '发布后审查' for event in detail['timeline'])
    assert detail['updated_at_ms'] == timestamp_ms('2026-01-04')
    assert detail['status'] == 'write_failed'  # Auditing never replaces the news API's business state.


def test_external_cleanup_counts_attempts_once_and_preserves_recovery_inputs_and_local_errors(tmp_path: Path) -> None:
    path = tmp_path / 'source.sqlite'
    now = timestamp_ms('2026-11-04T01:00:00Z')
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE tasks(id INTEGER PRIMARY KEY,source TEXT);
            INSERT INTO tasks VALUES(1,'x');
            CREATE TABLE x_task_pipeline(task_id INTEGER PRIMARY KEY,last_error TEXT,updated_at TEXT,push_result TEXT);
            INSERT INTO x_task_pipeline VALUES(1,'HTTP 503 secret supplier body','2026-11-03T15:59:59Z','{"error":"HTTP 503 secret supplier body"}');
            CREATE TABLE x_processing_attempts(id INTEGER PRIMARY KEY,task_id INTEGER,stage TEXT,status TEXT,finished_at TEXT,error_message TEXT,error_code TEXT,input_json TEXT);
            INSERT INTO x_processing_attempts VALUES(1,1,'write','failed','2026-11-03T15:59:59Z','HTTP 503 secret supplier body','HTTPError','{"original":"新闻原文"}');
            INSERT INTO x_processing_attempts VALUES(2,1,'write','failed','2026-11-03T15:59:59Z','database is locked','OperationalError','{}');
            INSERT INTO x_processing_attempts VALUES(3,1,'write','failed','2026-11-03T16:00:01Z','HTTP 403 keep today','HTTPError','{}');
            CREATE TABLE x_processing_attempt_events(id INTEGER PRIMARY KEY,attempt_id INTEGER,status TEXT,happened_at TEXT,error_message TEXT,error_code TEXT,response_excerpt TEXT,response_ref TEXT);
            INSERT INTO x_processing_attempt_events VALUES(1,1,'running','2026-11-03T15:59:58Z',NULL,NULL,'HTTP 503 secret supplier body','old-ref');
        ''')
    result = cleanup_dependency_errors(path, tables=[], database_key='main', now_ms=now)
    assert result['counted'] == 1
    assert cleanup_dependency_errors(path, tables=[], database_key='main', now_ms=now)['counted'] == 0
    totals = dependency_error_totals(path)
    assert totals == [{'error_date': '2026-11-03', 'source': 'x', 'stage': 'write', 'category': 'upstream_http', 'occurrences': 1}]
    with sqlite3.connect(path) as conn:
        attempt = conn.execute('SELECT status,error_message,error_code,input_json FROM x_processing_attempts WHERE id=1').fetchone()
        assert attempt == ('failed', '', '', '{"original":"新闻原文"}')
        assert conn.execute('SELECT error_message FROM x_processing_attempts WHERE id=2').fetchone()[0] == 'database is locked'
        assert conn.execute('SELECT error_message FROM x_processing_attempts WHERE id=3').fetchone()[0] == 'HTTP 403 keep today'
        assert conn.execute('SELECT response_excerpt,response_ref FROM x_processing_attempt_events').fetchone() == (None, None)
        assert 'secret supplier body' not in conn.execute('SELECT push_result FROM x_task_pipeline').fetchone()[0]


def test_beijing_day_boundary_and_expected_results() -> None:
    assert day_start_ms(timestamp_ms('2026-11-03T16:00:01Z')) == timestamp_ms('2026-11-03T16:00:00Z')
    assert failure_category('database is locked (timeout 30s)') == 'local_storage'
    assert failure_category('freshness gate expired') == 'expected_result'
    assert failure_category('no target-author posts') == 'expected_result'


def test_observation_cleanup_removes_error_evidence_index_and_attempt_responses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = int(datetime.now(UTC).timestamp()*1000)
    old = day_start_ms(now) - 1000
    # Seed yesterday's data while the policy still considers it current-day.
    from unittest.mock import patch
    snapshot = {'item_id': 'news:1', 'business_key': 'x', 'source': 'x', 'title': '真实新闻',
                'summary': 'HTTP 503 privatefailureword', 'status': 'write_failed', 'result': 'failed',
                'created_at_ms': old, 'updated_at_ms': old, 'source_database': 'fixture',
                'source_table': 'tasks', 'source_row_id': '1', 'news_visible': True}
    with patch('packages.system_observability.store.day_start_ms', return_value=old-1000), patch('packages.system_observability.error_policy.day_start_ms', return_value=old-1000):
        store.upsert_snapshot(snapshot, {'task': {'content': '真实原文'}, 'pipeline': {'last_error': 'HTTP 503 privatefailureword'}})
        store.append_event(item_id='news:1', event_id='response', event_type='stage_attempt', stage='写作', status='running',
                           result='unknown', happened_at_ms=old, summary='响应', evidence={'raw_model_response': 'privatefailureword'}, details={'attempt_id': 7})
        store.append_event(item_id='news:1', event_id='failure', event_type='stage_attempt', stage='写作', status='failed',
                           result='failed', happened_at_ms=old, summary='失败', reason='HTTP 503 privatefailureword', details={'attempt_id': 7})
    # The API already hides expired material before the bounded physical sweep finishes.
    assert store.query({'q': 'privatefailureword'})['total'] == 0
    assert 'privatefailureword' not in json.dumps(store.detail('news:1'))
    cleanup_observation_errors(store, now_ms=now)
    detail = store.detail('news:1')
    assert detail['status'] == 'write_failed'
    assert detail['summary'] == ''
    assert detail['evidence']['task']['content'] == '真实原文'
    assert 'privatefailureword' not in json.dumps(detail)
    assert store.query({'q': 'privatefailureword'})['total'] == 0
    for file in store.evidence_dir.glob('*/*.json.gz'):
        import gzip
        assert b'privatefailureword' not in gzip.decompress(file.read_bytes())


def test_dependency_counters_count_retries_transactionally_without_counting_copies(tmp_path: Path) -> None:
    path = tmp_path / 'source.sqlite'
    now = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,status TEXT,reason TEXT,attempts INTEGER,updated_at TEXT);
        ''')
    cleanup_dependency_errors(path, tables=['jobs'], database_key='meme')
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO jobs(id,status,reason,attempts,updated_at) VALUES(1,'failed','HTTP 503',1,?)", (now,))
        conn.execute("UPDATE jobs SET reason='HTTP 503' WHERE id=1")
        conn.execute("UPDATE jobs SET updated_at=? WHERE id=1", (now,))
        assert conn.execute('SELECT SUM(occurrences) FROM odaily_external_error_daily').fetchone()[0] == 1
        conn.execute("UPDATE jobs SET status='processing' WHERE id=1")
        conn.execute("UPDATE jobs SET status='retry_wait',attempts=2 WHERE id=1")
        assert conn.execute('SELECT SUM(occurrences) FROM odaily_external_error_daily').fetchone()[0] == 2
        conn.execute("INSERT INTO jobs(id,status,reason,attempts,updated_at) VALUES(2,'failed','database is locked (timeout)',1,?)", (now,))
        assert conn.execute('SELECT SUM(occurrences) FROM odaily_external_error_daily').fetchone()[0] == 2
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO jobs(id,status,reason,attempts,updated_at) VALUES(3,'failed','HTTP 429',1,?)", (now,))
        conn.rollback()
    assert sum(row['occurrences'] for row in dependency_error_totals(path)) == 2


def test_three_day_payload_cleanup_requires_verified_archive_and_preserves_decisions(tmp_path: Path) -> None:
    from packages.system_observability.business_retention import trim_archived_task_payloads
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    with sqlite3.connect(source) as conn:
        conn.execute("ALTER TABLE tasks ADD COLUMN raw_payload TEXT DEFAULT '{}'")
        conn.execute("UPDATE tasks SET raw_payload='original-payload',status='auto_published' WHERE id=1")
        conn.execute("UPDATE tasks SET raw_payload='pending-payload',status='ready_review' WHERE id=2")
        conn.execute("UPDATE tasks SET raw_payload='failed-payload',status='write_failed' WHERE id=3")
    store = _store(tmp_path)
    now = int(datetime.now(UTC).timestamp()*1000)
    assert trim_archived_task_payloads(source, store, now_ms=now, dry_run=False) == {'cleared': 0, 'protected': 1}
    MainTaskProjector(source, store).run_once()
    assert trim_archived_task_payloads(source, store, now_ms=now, dry_run=True)['cleared'] == 1
    assert trim_archived_task_payloads(source, store, now_ms=now, dry_run=False)['cleared'] == 1
    with sqlite3.connect(source) as conn:
        assert conn.execute('SELECT raw_payload FROM tasks WHERE id=1').fetchone()[0] == '{}'
        assert conn.execute('SELECT raw_payload FROM tasks WHERE id=2').fetchone()[0] == 'pending-payload'
        assert conn.execute('SELECT raw_payload FROM tasks WHERE id=3').fetchone()[0] == 'failed-payload'
        assert conn.execute('SELECT content FROM tasks WHERE id=1').fetchone()[0] == '区块链交易所发布重要公告'
        assert conn.execute('SELECT COUNT(*) FROM x_task_pipeline').fetchone()[0] == 1
    assert store.detail('task:x:post-1')['evidence']['task']['raw_payload'] == 'original-payload'


def test_public_list_defaults_to_news_and_technical_view_is_explicit(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from packages.editor_plugin_api import EditorPluginNewsGenService
    source = tmp_path / 'source.sqlite'
    _source_db(source)
    store = _store(tmp_path)
    MainTaskProjector(source, store).run_once()
    service = SimpleNamespace(system_information=store)
    result = EditorPluginNewsGenService.execute_system_information(service, None, {'action': 'list'})
    assert {row['task_id'] for row in result['items']} == {1, 2}
    result = EditorPluginNewsGenService.execute_system_information(service, None, {'action': 'list', 'view': 'diagnostic'})
    assert {row['task_id'] for row in result['items']} == {1, 2, 3}


def test_legacy_maintenance_preserves_business_history_and_pending_work(tmp_path: Path) -> None:
    from packages.maintenance.sqlite_repository import SQLiteMaintenanceRepository

    path = tmp_path / 'maintenance.sqlite'
    with sqlite3.connect(path) as conn:
        conn.executescript('''
            CREATE TABLE tasks(id INTEGER PRIMARY KEY,status TEXT,raw_payload TEXT,metadata TEXT,updated_at TEXT);
            INSERT INTO tasks VALUES(1,'ready_review','{"body":"待处理"}','{"sourceUrl":"https://source"}','2020-01-01');
            INSERT INTO tasks VALUES(2,'duplicate','{"body":"重复稿"}','{"sourceUrl":"https://source2"}','2020-01-01');
            CREATE TABLE x_task_pipeline(task_id INTEGER,judge_output TEXT,search_result TEXT,writer_output TEXT,publisher_output TEXT,push_result TEXT,telegram_result TEXT,updated_at TEXT);
            INSERT INTO x_task_pipeline VALUES(2,'{"route":"regular"}','{"duplicate_target":{"title":"原稿"}}','{}','{}','{}','{}','2020-01-01');
            CREATE TABLE odaily_reference_items(raw_payload TEXT,metadata TEXT,updated_at TEXT);
            INSERT INTO odaily_reference_items VALUES('{"sourceUrl":"https://original"}','{"attribution":"author"}','2020-01-01');
        ''')
        before = list(conn.iterdump())
    result = SQLiteMaintenanceRepository(path).cleanup(dry_run=False)
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before
    assert result.cleared['tasks_payloads'] == 0
    assert result.cleared['x_task_pipeline_outputs'] == 0


def test_clock_only_child_processing_refreshes_original_news(tmp_path: Path) -> None:
    store = _store(tmp_path)
    parent = {'item_id': 'news', 'title': '新闻', 'business_key': 'x', 'source': 'x', 'status': 'published',
              'result': 'direct_publish', 'updated_at_ms': 100, 'news_visible': True,
              'source_database': 'synthetic', 'source_table': 'tasks', 'source_row_id': '1'}
    child = {**parent, 'item_id': 'audit', 'parent_item_id': 'news', 'news_visible': False, 'updated_at_ms': 200}
    store.upsert_snapshot(parent, {})
    store.upsert_snapshot(child, {})
    before = len(store.detail('news')['timeline'])
    store.upsert_snapshot({**child, 'updated_at_ms': 300}, {})
    assert store.detail('news')['updated_at_ms'] == 300
    assert len(store.detail('news')['timeline']) == before


def test_expired_legacy_supplier_review_is_hidden_before_cleanup(tmp_path: Path) -> None:
    import pytest
    from packages.system_observability.reviews import SystemReviewRepository

    repo = SystemReviewRepository(tmp_path / 'reviews.sqlite')
    key = repo.upsert_issue({'task_kind': 'F', 'category': 'failure_recovery_cluster', 'title': '旧错误',
                            'scope': {'error_signature': '#'}, 'actual': '错误码=#',
                            'evidence': [{'error_message': '503', 'status': 'failed'}]})
    with repo.connect() as conn:
        conn.execute("UPDATE system_review_items SET last_seen_at='2020-01-01' WHERE item_key=?", (key,))
    assert repo.list_items({'status': 'all'})['total'] == 0
    assert not repo.pending_analysis()
    with pytest.raises(ValueError, match='留存期'):
        repo.detail(key)
    cleanup_observation_errors(repo.information_store)
    with repo.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM system_review_items').fetchone()[0] == 0


def test_news_queries_and_retention_lookups_use_indexes_before_statistics(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    original_connect = store.connect
    statements = []

    def traced_connect():
        conn = original_connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, 'connect', traced_connect)
    assert store.query({'view': 'news'})['items'] == []
    selects = [sql for sql in statements if 'FROM system_information_items i' in sql]
    assert len(selects) == 2
    with original_connect() as conn:
        for sql in selects:
            plan = ' '.join(str(row[3]) for row in conn.execute('EXPLAIN QUERY PLAN '+sql))
            assert 'idx_system_information_news_activity' in plan
            assert 'TEMP B-TREE' not in plan
        for sql, expected in (
            ("SELECT evidence_ref FROM system_information_items WHERE source_database='main' AND source_table='tasks' AND source_row_id='1'", 'idx_system_information_origin'),
            ("UPDATE system_information_events SET evidence_ref='new' WHERE evidence_ref='old'", 'idx_system_information_events_evidence'),
        ):
            plan = ' '.join(str(row[3]) for row in conn.execute('EXPLAIN QUERY PLAN '+sql))
            assert expected in plan
            assert 'SCAN ' not in plan
