import sqlite3
from datetime import UTC, datetime, timedelta

from packages.system_observability.business_retention import queue_unreferenced_capture_evidence, reclaim_completed_attempt_evidence
from packages.system_observability.store import SystemInformationStore


def test_successful_capture_snapshot_uses_separate_database_and_reads_back(tmp_path):
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    evidence = {'recorded_scope': 'source_table_snapshot', 'row': {
        'id': 42, 'username_lower': 'alice', 'candidate_count': 0, 'status': 'success', 'error': None,
    }}
    ref = store.save_evidence(evidence)
    assert store.load_evidence(ref) == evidence
    assert (store.evidence_dir.parent / 'small_evidence.sqlite').exists()
    assert not list(store.evidence_dir.glob('[0-9a-f][0-9a-f]/*.json.gz'))


def test_capture_failure_and_large_payload_keep_file_storage(tmp_path):
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    for row in (
        {'username_lower': 'alice', 'candidate_count': 0, 'status': 'fetch_failed', 'error': 'offline'},
        {'username_lower': 'alice', 'candidate_count': 0, 'status': 'success', 'error': None, 'large': 'x' * 9000},
    ):
        evidence = {'recorded_scope': 'source_table_snapshot', 'row': row}
        ref = store.save_evidence(evidence)
        assert store.load_evidence(ref) == evidence
        assert (store.evidence_dir / ref[:2] / f'{ref}.json.gz').exists()


def test_small_evidence_is_reclaimed_only_after_references_are_released(tmp_path):
    source = tmp_path / 'source.sqlite'
    with sqlite3.connect(source) as conn:
        conn.executescript('CREATE TABLE x_processing_attempts(response_ref TEXT); CREATE TABLE x_processing_attempt_events(response_ref TEXT);')
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    evidence = {'recorded_scope': 'source_table_snapshot', 'row': {
        'id': 42, 'username_lower': 'alice', 'candidate_count': 0, 'status': 'success', 'error': None,
    }}
    ref = store.save_evidence(evidence)
    store.pin_evidence(ref, 'in-use')
    now = int(datetime.now(UTC).timestamp() * 1000)
    with store.connect() as conn:
        conn.execute('INSERT INTO system_information_expired_evidence_candidates VALUES(?,?)', (ref, now))
        conn.execute('UPDATE system_information_evidence SET created_at_ms=0 WHERE evidence_ref=?', (ref,))
    assert reclaim_completed_attempt_evidence(source, store, now_ms=now)['removed'] == 0
    assert store.load_evidence(ref) == evidence
    store.unpin_evidence('in-use')
    store.set_state('attempt_evidence_reclaim_cursor', 0)
    assert reclaim_completed_attempt_evidence(source, store, now_ms=now)['removed'] == 1
    with sqlite3.connect(store.evidence_dir.parent / 'small_evidence.sqlite') as conn:
        assert conn.execute('SELECT count(*) FROM evidence').fetchone()[0] == 0


def test_old_successful_capture_snapshot_releases_item_reference(tmp_path, monkeypatch):
    monkeypatch.setenv('ODAILY_COMPLETED_EVIDENCE_RETENTION_ENABLED', '1')
    source = tmp_path / 'source.sqlite'
    with sqlite3.connect(source) as conn:
        conn.executescript('CREATE TABLE x_processing_attempts(response_ref TEXT); CREATE TABLE x_processing_attempt_events(response_ref TEXT);')
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    evidence = {'recorded_scope': 'source_table_snapshot', 'row': {
        'id': 42, 'username_lower': 'alice', 'candidate_count': 0, 'status': 'success', 'error': None,
        'started_at': old.isoformat(), 'finished_at': old.isoformat(),
    }}
    snapshot = {'item_id': 'capture:42', 'business_key': 'x', 'source': 'x_capture', 'source_id': '42',
                'source_name': 'alice', 'source_database': str(source), 'source_table': 'x_capture_attempts',
                'source_row_id': '42', 'status': 'success', 'result': 'direct_publish',
                'created_at_ms': int(old.timestamp() * 1000), 'updated_at_ms': int(old.timestamp() * 1000)}
    store.upsert_snapshot(snapshot, evidence)
    with store.connect() as conn:
        ref = conn.execute('SELECT evidence_ref FROM system_information_items').fetchone()[0]
        conn.execute('UPDATE system_information_evidence SET created_at_ms=?', (int(old.timestamp() * 1000),))
    store.prune_task_status_events(now_ms=int(now.timestamp() * 1000))
    with store.connect() as conn:
        assert conn.execute('SELECT evidence_ref FROM system_information_items').fetchone()[0] is None
        assert conn.execute('SELECT evidence_ref FROM system_information_expired_evidence_candidates').fetchone()[0] == ref
    assert reclaim_completed_attempt_evidence(source, store, now_ms=int(now.timestamp() * 1000))['removed'] == 1


def test_interrupted_batch_staging_can_be_reclaimed(tmp_path):
    import pytest
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    evidence = {'recorded_scope': 'source_table_snapshot', 'row': {
        'id': 42, 'username_lower': 'alice', 'candidate_count': 0, 'status': 'success', 'error': None,
    }}
    with pytest.raises(KeyError):
        store.apply_source_audit_batch([{'evidence': evidence}], watermark_key='test', watermark=1)
    assert store.prune_unregistered_small_evidence() == {'scanned': 1, 'removed': 1}


def test_only_identifiable_successful_old_orphan_capture_files_are_queued(tmp_path, monkeypatch):
    store = SystemInformationStore(tmp_path / 'information.sqlite')
    store.initialize()
    monkeypatch.setattr(store, '_is_small_capture', lambda *_: False)
    now = datetime.now(UTC)
    old = now - timedelta(days=2)
    row = {'id': 42, 'username_lower': 'alice', 'candidate_count': 0, 'source': 'fxtwitter',
           'status': 'success', 'error': None, 'started_at': old.isoformat(), 'finished_at': old.isoformat()}
    refs = [store.save_evidence({'recorded_scope': 'source_table_snapshot', 'row': value}) for value in (
        row, {**row, 'id': 43, 'status': 'fetch_failed', 'error': 'offline'},
        {**row, 'id': 44, 'username_lower': ''}, {**row, 'id': 45},
    )]
    store.pin_evidence(refs[3], 'still-in-use')
    with store.connect() as conn:
        conn.execute('UPDATE system_information_evidence SET created_at_ms=?', (int(old.timestamp() * 1000),))
    result = queue_unreferenced_capture_evidence(store, now_ms=int(now.timestamp() * 1000))
    assert result['queued'] == 2
    source = tmp_path / 'source.sqlite'
    with sqlite3.connect(source) as conn:
        conn.executescript('CREATE TABLE x_processing_attempts(response_ref TEXT); CREATE TABLE x_processing_attempt_events(response_ref TEXT);')
    assert reclaim_completed_attempt_evidence(source, store, now_ms=int(now.timestamp() * 1000))['removed'] == 1
    assert store.load_evidence(refs[0]) is None
    assert all(store.load_evidence(ref) is not None for ref in refs[1:])
