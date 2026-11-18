from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from packages.system_observability import MainTaskProjector, SystemInformationStore
from packages.system_observability.projector import SourceAuditInstaller, TABLE_SPECS


def _store(path: Path) -> SystemInformationStore:
    store = SystemInformationStore(path / 'information.sqlite')
    store.initialize()
    return store


def test_six_hour_window_is_fixed_across_pages_and_ignores_search(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = int(datetime.now(UTC).timestamp() * 1000)
    for item_id, stamp in (('first', now - 1_000), ('second', now - 2_000), ('expired', now - 7 * 60 * 60 * 1000)):
        store.upsert_snapshot({
            'item_id': item_id, 'business_key': 'x', 'source': 'x', 'title': item_id,
            'status': 'published', 'result': 'direct_publish', 'created_at_ms': stamp,
            'updated_at_ms': stamp, 'source_database': 'fixture', 'source_table': 'tasks',
            'source_row_id': item_id, 'news_visible': True,
        }, None)

    page = store.query({'view': 'news', 'q': 'not-matched', 'limit': 1})
    assert page['total'] == 2
    assert [item['item_id'] for item in page['items']] == ['first']
    next_page = store.query({'view': 'news', 'q': 'another-term', 'limit': 1, 'cursor': page['next_cursor']})
    assert [item['item_id'] for item in next_page['items']] == ['second']


def test_sqlite_store_does_not_create_search_index(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connect() as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_information_search'").fetchone() is None
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_information_search_positions'").fetchone() is None
    assert store.rebuild_search_index() == 0


def test_x_capture_outbox_projects_account_and_latest_terminal_attempt_status(tmp_path: Path) -> None:
    source = tmp_path / 'main.sqlite'
    with sqlite3.connect(source) as conn:
        conn.execute('CREATE TABLE x_capture_attempts(id INTEGER PRIMARY KEY,username_lower TEXT,status TEXT,started_at TEXT,finished_at TEXT,error_message TEXT)')
        conn.execute("INSERT INTO x_capture_attempts VALUES(1,'alice','running','2026-11-19T00:00:00Z',NULL,NULL)")
    spec = next(spec for spec in TABLE_SPECS if spec.table == 'x_capture_attempts')
    SourceAuditInstaller().install(source, (spec,))
    with sqlite3.connect(source) as conn:
        conn.execute("UPDATE x_capture_attempts SET status='fetch_failed',finished_at='2026-11-19T00:02:00Z',error_message='HTTP 503' WHERE id=1")
        conn.execute("UPDATE x_capture_attempts SET status='success',finished_at='2026-11-19T00:03:00Z',error_message=NULL WHERE id=1")
        payloads = [json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM odaily_observability_outbox ORDER BY seq')]
    assert len(payloads) == 2
    assert all(payload['username_lower'] == 'alice' for payload in payloads)

    store = _store(tmp_path)
    projector = MainTaskProjector(source, store, additional_databases={'main': source}, table_specs=(spec,))
    assert projector._project_source_outbox('main', source, [spec], int(datetime.now(UTC).timestamp() * 1000)) == (2, 1)
    item = store.detail('main:x_capture_attempts:1')
    assert item['source_name'] == 'alice'
    assert item['status'] == 'success'
    assert item['result'] == 'capture_success'


def test_x_capture_retention_keeps_inspection_refs_and_recent_decision_samples(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = int(datetime.now(UTC).timestamp() * 1000)
    old = now - 7 * 60 * 60 * 1000
    for item_id, stamp in (('reviewed', old), ('sampled', old), ('ordinary', old)):
        store.upsert_snapshot({
            'item_id': item_id, 'business_key': 'x', 'source': 'x_capture', 'source_name': item_id,
            'status': 'success', 'result': 'capture_success', 'created_at_ms': stamp,
            'updated_at_ms': stamp, 'source_database': 'fixture', 'source_table': 'x_capture_attempts',
            'source_row_id': item_id,
        }, None)
    with store.connect() as conn:
        conn.execute('CREATE TABLE system_review_items(item_key TEXT PRIMARY KEY,scope_json TEXT,evidence_json TEXT,counter_evidence_json TEXT)')
        conn.execute("INSERT INTO system_review_items VALUES('review',?, '{}', '{}')", (json.dumps({'stream_item_ids': ['reviewed']}),))
        conn.execute("INSERT INTO system_information_decision_samples(sample_key,sample_date,sampled_at_ms,raw_expires_at_ms,item_id,business_key,source,stage,status,result) VALUES('sample','today',?,?,'sampled','x','x_capture','采集','success','capture_success')", (now, now + 14 * 86_400_000))

    assert store.trim_information_items(now_ms=now) == {'scanned': 1, 'removed': 1, 'protected': 0}
    assert store.has_item('reviewed')
    assert store.has_item('sampled')
    assert not store.has_item('ordinary')
