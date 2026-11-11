"""Trim terminal task transport payloads only after their archive has been verified."""
from __future__ import annotations

import sqlite3
import json
import re
import time
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly

TERMINAL_STATUSES = ('discarded', 'duplicate', 'auto_published', 'published', 'notified')
SOURCE_TEXT_TYPES = ('x', 'binance_square', 'blockbeats', 'panews', 'jinse', 'jin10',
                     'non_mainstream_media', 'crypto_source', 'ai_source', 'mixed_source',
                     'mainstream_media', 'external_media_alert', 'ai_source_alert', 'event_tracking')


def _active_audits(conn: sqlite3.Connection, tables: set[str]) -> list[str] | None:
    active = [table for table in ('fact_checks', 'auditor_checks')
              if table in tables and conn.execute(f"SELECT 1 FROM {table} WHERE status IN ('pending','processing') LIMIT 1").fetchone()]
    if active:
        columns = {r['name'] for r in conn.execute('PRAGMA table_info(newsflash_production_metadata)')}
        if not {'delivery_key', 'odaily_id', 'id_match_status'} <= columns or any(
                'source_item_id' not in {r['name'] for r in conn.execute(f'PRAGMA table_info({table})')} for table in active):
            return None
    return active


def _audit_protects(conn: sqlite3.Connection, active: list[str], task_id: int) -> bool:
    if not active:
        return False
    published = conn.execute("SELECT odaily_id FROM newsflash_production_metadata WHERE delivery_key=? AND id_match_status='matched'", (f"odaily-task-{task_id}",)).fetchone()
    if not published or not published['odaily_id']:
        return True
    return any(conn.execute(f"SELECT 1 FROM {table} WHERE source_item_id=? AND status IN ('pending','processing') LIMIT 1", (published['odaily_id'],)).fetchone() for table in active)


def trim_source_text(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    """Remove expired source text only after work is terminal and while audits are idle."""
    cutoff = now_ms - 30 * 86_400_000
    cursor = int(store.get_state('source_text_retention_cursor', '0') or 0)
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'tasks' not in tables:
            return {'scanned': 0, 'cleared': 0, 'audit_busy': 0}
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(tasks)')}
        if not {'id', 'source', 'source_url', 'status', 'updated_at', 'content', 'raw_payload', 'metadata'} <= columns:
            return {'scanned': 0, 'cleared': 0, 'audit_busy': 0}
        conn.execute('BEGIN IMMEDIATE')
        active_audits = 'fact_checks' in tables and conn.execute("SELECT 1 FROM fact_checks WHERE status IN ('pending','processing') LIMIT 1").fetchone()
        can_match_audits = {'newsflash_production_metadata', 'x_task_pipeline'} <= tables
        if active_audits and not can_match_audits:
            conn.rollback()
            return {'scanned': 0, 'cleared': 0, 'audit_busy': 1}
        rows = conn.execute(
            "SELECT id,source,source_url,status,content,raw_payload,metadata,(julianday(updated_at)-2440587.5)*86400000 AS age_ms "
            "FROM tasks WHERE id>? ORDER BY id LIMIT ?", (cursor, max(1, min(limit, 500))),
        ).fetchall()
        cleared = protected = 0
        for row in rows:
            if (row['status'] not in TERMINAL_STATUSES or row['source'] not in SOURCE_TEXT_TYPES
                    or row['age_ms'] is None or row['age_ms'] >= cutoff or (not row['content'] and row['raw_payload'] in ('', '{}', 'null'))):
                continue
            if active_audits and conn.execute(
                "SELECT 1 FROM fact_checks f WHERE f.status IN ('pending','processing') AND ("
                "f.source_item_id IN (SELECT odaily_id FROM newsflash_production_metadata WHERE delivery_key=? AND odaily_id IS NOT NULL) "
                "OR EXISTS (SELECT 1 FROM x_task_pipeline p WHERE p.task_id=? AND p.publish_completed_at IS NOT NULL "
                "AND abs((julianday(p.publish_completed_at)-julianday(f.published_at))*86400)<=300)) LIMIT 1",
                (f"odaily-task-{row['id']}", row['id']),
            ).fetchone():
                protected += 1
                continue
            try:
                metadata = json.loads(row['metadata'] or '{}')
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            for key in ('context_chain', 'context_raw_layers', 'excerpt', 'content', 'text', 'raw_payload'):
                metadata.pop(key, None)
            cleared += conn.execute("UPDATE tasks SET content='',raw_payload='{}',metadata=? WHERE id=? AND content IS ? AND raw_payload IS ? AND metadata IS ?",
                                    (json.dumps(metadata, ensure_ascii=False, separators=(',', ':')), row['id'], row['content'], row['raw_payload'], row['metadata'])).rowcount
        conn.commit()
    store.set_state('source_text_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared, 'audit_busy': protected}


def trim_search_details(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    """Retain duplicate identities while dropping old completed-task comparison details."""
    cutoff = now_ms - 7 * 86_400_000
    cursor = int(store.get_state('search_detail_retention_cursor', '0') or 0)
    keep = {'decision', 'is_duplicate', 'duplicate', 'duplicate_target_type', 'duplicate_target_id', 'target_type', 'target_id'}
    target_keep = {'target_type', 'target_id', 'title', 'target_title', 'source_url', 'candidate_id'}
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'x_task_pipeline'} <= tables:
            return {'scanned': 0, 'cleared': 0}
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(x_task_pipeline)')}
        if 'search_result' not in columns:
            return {'scanned': 0, 'cleared': 0}
        rows = conn.execute(
            "SELECT p.task_id,p.search_result,t.status,(julianday(t.updated_at)-2440587.5)*86400000 AS age_ms "
            "FROM x_task_pipeline p JOIN tasks t ON t.id=p.task_id WHERE p.task_id>? "
            "ORDER BY p.task_id LIMIT ?", (cursor, max(1, min(limit, 500))),
        ).fetchall()
        cleared = 0
        for row in rows:
            if row['status'] not in TERMINAL_STATUSES or row['age_ms'] is None or row['age_ms'] >= cutoff:
                continue
            if 'x_processing_attempts' in tables and conn.execute("SELECT 1 FROM x_processing_attempts WHERE task_id=? AND status<>'succeeded' LIMIT 1", (row['task_id'],)).fetchone():
                continue
            try:
                value = json.loads(row['search_result'] or '{}')
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict):
                continue
            retained = {key: value[key] for key in keep if key in value}
            target = value.get('duplicate_target')
            if isinstance(target, dict):
                retained['duplicate_target'] = {key: target[key] for key in target_keep if key in target}
            if retained == value:
                continue
            result = json.dumps(retained, ensure_ascii=False, separators=(',', ':'))
            cleared += conn.execute('UPDATE x_task_pipeline SET search_result=? WHERE task_id=? AND search_result=?',
                                    (result, row['task_id'], row['search_result'])).rowcount
        conn.commit()
    store.set_state('search_detail_retention_cursor', int(rows[-1]['task_id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


def trim_candidate_details(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    cutoff = now_ms - 7 * 86_400_000
    source_cutoff = now_ms - 30 * 86_400_000
    cursor = int(store.get_state('candidate_retention_cursor', '0') or 0)
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'search_event_candidates', 'search_event_sources'} <= tables:
            return {'scanned': 0, 'cleared': 0}
        rows = conn.execute(
            "SELECT id,primary_task_id,(julianday(created_at)-2440587.5)*86400000 AS created_ms,"
            "(julianday(expires_at)-2440587.5)*86400000 AS expires_ms FROM search_event_candidates WHERE id>? ORDER BY id LIMIT ?",
            (cursor, max(1, min(limit, 500))),
        ).fetchall()
        cleared = 0
        for row in rows:
            if row['expires_ms'] is None or row['expires_ms'] >= cutoff:
                continue
            marks = ','.join('?' for _ in TERMINAL_STATUSES)
            if row['primary_task_id'] and conn.execute(f"SELECT 1 FROM tasks WHERE id=? AND status NOT IN ({marks})", (row['primary_task_id'], *TERMINAL_STATUSES)).fetchone():
                continue
            if conn.execute(f"SELECT 1 FROM search_event_sources s JOIN tasks t ON t.id=s.task_id WHERE s.candidate_id=? AND t.status NOT IN ({marks}) LIMIT 1", (row['id'], *TERMINAL_STATUSES)).fetchone():
                continue
            if 'x_processing_attempts' in tables and conn.execute(
                "SELECT 1 FROM x_processing_attempts a WHERE a.status<>'succeeded' AND (a.task_id=? OR a.task_id IN (SELECT task_id FROM search_event_sources WHERE candidate_id=?)) LIMIT 1",
                (row['primary_task_id'], row['id']),
            ).fetchone():
                continue
            cleared += conn.execute("UPDATE search_event_sources SET metadata=json_remove(metadata,'$.search_result') WHERE candidate_id=? AND json_valid(metadata) AND json_type(metadata,'$.search_result') IS NOT NULL", (row['id'],)).rowcount
            if row['created_ms'] is not None and row['created_ms'] < source_cutoff:
                conn.execute("UPDATE search_event_sources SET content='' WHERE candidate_id=?", (row['id'],))
                conn.execute("UPDATE search_event_candidates SET content='' WHERE id=?", (row['id'],))
        conn.commit()
    store.set_state('candidate_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


def trim_competitor_text(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    cutoff = now_ms - 30 * 86_400_000
    cursor = int(store.get_state('competitor_text_retention_cursor', '0') or 0)
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'newsflash_items', 'newsflash_event_sources', 'newsflash_events'} <= tables:
            return {'scanned': 0, 'cleared': 0}
        rows = conn.execute(
            "SELECT id,source,source_url,content,raw_payload,(julianday(updated_at)-2440587.5)*86400000 AS age_ms "
            "FROM newsflash_items WHERE id>? ORDER BY id LIMIT ?", (cursor, max(1, min(limit, 500))),
        ).fetchall()
        cleared = 0
        for row in rows:
            if (row['source'] == 'odaily' or row['age_ms'] is None or row['age_ms'] >= cutoff
                    or (not row['content'] and row['raw_payload'] in ('', '{}', 'null'))):
                continue
            if conn.execute("SELECT 1 FROM newsflash_event_sources s JOIN newsflash_events e ON e.event_id=s.event_id WHERE s.item_id=? AND (e.updated_at IS NULL OR (julianday(e.updated_at)-2440587.5)*86400000>=?) LIMIT 1", (row['id'], cutoff)).fetchone():
                continue
            cleared += conn.execute("UPDATE newsflash_items SET content='',raw_payload='{}' WHERE id=? AND content IS ? AND raw_payload IS ?", (row['id'], row['content'], row['raw_payload'])).rowcount
        conn.commit()
    store.set_state('competitor_text_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


def trim_attempt_details(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    cutoff = now_ms - 7 * 86_400_000
    cursor = int(store.get_state('attempt_retention_cursor', '0') or 0)
    watermark = int(store.get_state('main_attempt_events_watermark', '0') or 0)
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'x_processing_attempts', 'x_processing_attempt_events'} <= tables:
            return {'scanned': 0, 'cleared': 0}
        rows = conn.execute(
            "SELECT a.id,a.task_id,a.stage,a.status,t.status AS task_status,(julianday(t.updated_at)-2440587.5)*86400000 AS age_ms "
            "FROM x_processing_attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id>? ORDER BY a.id LIMIT ?",
            (cursor, max(1, min(limit, 500))),
        ).fetchall()
        possible = [row for row in rows if row['task_status'] in TERMINAL_STATUSES and row['status'] == 'succeeded'
                    and row['age_ms'] is not None and row['age_ms'] < cutoff]
        if not possible:
            store.set_state('attempt_retention_cursor', int(rows[-1]['id']) if rows else 0)
            return {'scanned': len(rows), 'cleared': 0}
        # Serialize the final eligibility checks with task, attempt, and audit writes.
        conn.execute('BEGIN IMMEDIATE')
        deadline = time.monotonic() + 1.0
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        active_audits = _active_audits(conn, tables)
        if active_audits is None:
            return {'scanned': 0, 'cleared': 0}
        cleared = 0
        for previous in possible:
            row = conn.execute("SELECT a.id,a.task_id,a.status,t.status AS task_status,(julianday(t.updated_at)-2440587.5)*86400000 AS age_ms FROM x_processing_attempts a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (previous['id'],)).fetchone()
            if row is None:
                continue
            if row['task_status'] not in TERMINAL_STATUSES or row['status'] != 'succeeded' or row['age_ms'] is None or row['age_ms'] >= cutoff:
                continue
            if _audit_protects(conn, active_audits, row['task_id']):
                continue
            # A successful stage can still be useful in diagnosing a failed retry
            # elsewhere in the same task. Keep the whole task's evidence then.
            if conn.execute("SELECT 1 FROM x_processing_attempts WHERE task_id=? AND status<>'succeeded' LIMIT 1", (row['task_id'],)).fetchone():
                continue
            if conn.execute('SELECT 1 FROM x_processing_attempt_events WHERE task_id=? AND attempt_id=? AND id>? LIMIT 1', (row['task_id'], row['id'], watermark)).fetchone():
                continue
            cleared += conn.execute("UPDATE x_processing_attempts SET input_json='{}',rule_json='{}',response_gzip=NULL,response_ref=NULL,response_excerpt=NULL WHERE id=? AND (input_json<>'{}' OR rule_json<>'{}' OR response_gzip IS NOT NULL OR response_ref IS NOT NULL OR response_excerpt IS NOT NULL)", (row['id'],)).rowcount
            conn.execute('UPDATE x_processing_attempt_events SET response_gzip=NULL,response_ref=NULL,response_excerpt=NULL WHERE task_id=? AND attempt_id=? AND id<=?', (row['task_id'], row['id'], watermark))
        conn.commit()
    store.set_state('attempt_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


def trim_completed_attempt_events(source: Path, store: Any, *, now_ms: int, limit: int = 50) -> dict[str, int]:
    """Remove old successful attempt payloads, keeping the timeline and identities."""
    cursor = int(store.get_state('attempt_event_retention_cursor', '0') or 0)
    watermark = int(store.get_state('main_attempt_events_watermark', '0') or 0)
    with store.connect() as obs:
        rows = obs.execute("SELECT e.rowid,e.event_id,e.item_id,e.event_type,e.evidence_ref,e.details_json,i.status AS item_status,i.updated_at_ms,i.source_database,i.source_table FROM system_information_events e LEFT JOIN system_information_items i ON i.item_id=e.item_id WHERE e.rowid>? ORDER BY e.rowid LIMIT ?",
                           (cursor, max(1, min(limit, 100)))).fetchall()
    if not rows:
        store.set_state('attempt_event_retention_cursor', 0)
        return {'scanned': 0, 'cleared': 0}
    if not any(row['event_type'] == 'stage_attempt' and str(row['event_id']).startswith('stage-attempt:')
               and row['source_table'] == 'tasks' and row['source_database'] == str(source)
               and row['item_status'] in TERMINAL_STATUSES and row['updated_at_ms'] is not None
               and row['updated_at_ms'] < now_ms - 7 * 86_400_000
               and (row['evidence_ref'] or '"input"' in row['details_json'] or '"rules"' in row['details_json']) for row in rows):
        store.set_state('attempt_event_retention_cursor', int(rows[-1]['rowid']))
        return {'scanned': len(rows), 'cleared': 0}
    with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as main:
        main.row_factory = sqlite3.Row
        tables = {r[0] for r in main.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'tasks', 'x_processing_attempts', 'x_processing_attempt_events'} <= tables:
            return {'scanned': 0, 'cleared': 0}
        main.execute('BEGIN IMMEDIATE')
        deadline = time.monotonic() + 1.0
        main.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        active = _active_audits(main, tables)
        if active is None:
            return {'scanned': 0, 'cleared': 0}
        cleared = 0
        with store.connect() as obs:
            obs.execute('PRAGMA busy_timeout=2000')
            obs.execute('BEGIN IMMEDIATE')
            obs.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            for candidate in rows:
                event = obs.execute("SELECT e.*,i.source_table,i.source_row_id,i.source_database FROM system_information_events e JOIN system_information_items i ON i.item_id=e.item_id WHERE e.rowid=? AND e.event_id=?", (candidate['rowid'], candidate['event_id'])).fetchone()
                if not event or event['event_type'] != 'stage_attempt' or event['source_table'] != 'tasks' or event['source_database'] != str(source) or not str(event['event_id']).startswith('stage-attempt:'):
                    continue
                event_id = str(event['event_id']).removeprefix('stage-attempt:')
                if not event_id.isdigit() or int(event_id) > watermark:
                    continue
                attempt = main.execute("SELECT a.id,a.task_id,a.status,t.status AS task_status,(julianday(t.updated_at)-2440587.5)*86400000 AS age_ms FROM x_processing_attempt_events e JOIN x_processing_attempts a ON a.id=e.attempt_id AND a.task_id=e.task_id JOIN tasks t ON t.id=a.task_id WHERE e.id=?", (int(event_id),)).fetchone()
                if not attempt or str(attempt['task_id']) != str(event['source_row_id']) or attempt['status'] != 'succeeded' or attempt['task_status'] not in TERMINAL_STATUSES or attempt['age_ms'] is None or attempt['age_ms'] >= now_ms - 7 * 86_400_000:
                    continue
                if _audit_protects(main, active, attempt['task_id']) or main.execute("SELECT 1 FROM x_processing_attempts WHERE task_id=? AND status<>'succeeded' LIMIT 1", (attempt['task_id'],)).fetchone():
                    continue
                if main.execute('SELECT 1 FROM x_processing_attempt_events WHERE task_id=? AND attempt_id=? AND id>? LIMIT 1', (attempt['task_id'], attempt['id'], watermark)).fetchone():
                    continue
                try:
                    details = json.loads(event['details_json'] or '{}')
                except (ValueError, TypeError):
                    continue
                if not isinstance(details, dict) or str(details.get('attempt_id')) != str(attempt['id']):
                    continue
                details.pop('input', None)
                details.pop('rules', None)
                detail_json = json.dumps(details, ensure_ascii=False, separators=(',', ':'))
                if event['evidence_ref'] or detail_json != event['details_json']:
                    if event['evidence_ref']:
                        obs.execute('INSERT OR IGNORE INTO system_information_expired_evidence_candidates(evidence_ref,eligible_at_ms) VALUES(?,?)', (event['evidence_ref'], now_ms))
                    obs.execute('UPDATE system_information_events SET evidence_ref=NULL,details_json=? WHERE event_id=?', (detail_json, event['event_id']))
                    cleared += 1
            obs.execute("INSERT INTO system_information_state VALUES('attempt_event_retention_cursor',?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (str(rows[-1]['rowid']), now_ms))
        main.rollback()
    return {'scanned': len(rows), 'cleared': cleared}


def reclaim_completed_attempt_evidence(source: Path, store: Any, *, now_ms: int, limit: int = 5) -> dict[str, int]:
    """Reclaim only references explicitly released by successful attempt retention."""
    cursor = int(store.get_state('attempt_evidence_reclaim_cursor', '0') or 0)
    with store.connect() as obs:
        rows = obs.execute('SELECT rowid,evidence_ref FROM system_information_expired_evidence_candidates WHERE rowid>? ORDER BY rowid LIMIT ?', (cursor, max(1, min(limit, 10)))).fetchall()
    if not rows:
        store.set_state('attempt_evidence_reclaim_cursor', 0)
        return {'scanned': 0, 'removed': 0, 'bytes': 0}
    # Do not create a missing source database or infer safety from a failed read.
    with sqlite3.connect(source.resolve().as_uri() + '?mode=rw', uri=True, timeout=2, factory=ClosingSQLiteConnection) as main:
        main.row_factory = sqlite3.Row
        for table in ('x_processing_attempts', 'x_processing_attempt_events'):
            if 'response_ref' not in {row['name'] for row in main.execute(f'PRAGMA table_info({table})')}:
                return {'scanned': 0, 'removed': 0, 'bytes': 0}
        main.execute('BEGIN IMMEDIATE')
        deadline = time.monotonic() + 1.0
        main.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        removed = total_bytes = 0
        with store.connect() as obs:
            obs.execute('PRAGMA busy_timeout=2000')
            obs.execute('BEGIN IMMEDIATE')
            obs.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            tables = {r[0] for r in obs.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for row in rows:
                ref = str(row['evidence_ref'])
                if not re.fullmatch(r'[0-9a-f]{64}', ref):
                    continue
                current = obs.execute('SELECT path,byte_size,created_at_ms FROM system_information_evidence WHERE evidence_ref=?', (ref,)).fetchone()
                if not current:
                    obs.execute('DELETE FROM system_information_expired_evidence_candidates WHERE evidence_ref=?', (ref,))
                    continue
                path = Path(current['path'])
                if path != store.evidence_dir / ref[:2] / f'{ref}.json.gz' or current['created_at_ms'] >= now_ms - 7 * 86_400_000:
                    continue
                if any(main.execute(f'SELECT 1 FROM {table} WHERE response_ref=? LIMIT 1', (ref,)).fetchone()
                       for table in ('x_processing_attempts', 'x_processing_attempt_events')):
                    continue
                references = [('system_information_items', 'evidence_ref'), ('system_information_events', 'evidence_ref'),
                              ('system_information_decision_samples', 'evidence_ref'), ('system_information_evidence_pins', 'evidence_ref'),
                              ('system_review_analysis_attempts', 'raw_response_ref')]
                if any(obs.execute(f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (ref,)).fetchone()
                       for table, column in references if table in tables):
                    continue
                if 'system_review_items' in tables and obs.execute(
                        'SELECT 1 FROM system_review_items WHERE scope_json LIKE ? OR evidence_json LIKE ? OR counter_evidence_json LIKE ? LIMIT 1',
                        (f'%{ref}%', f'%{ref}%', f'%{ref}%')).fetchone():
                    continue
                try:
                    allocated = path.stat().st_blocks * 512
                except FileNotFoundError:
                    allocated = 0
                path.unlink(missing_ok=True)
                obs.execute('DELETE FROM system_information_evidence WHERE evidence_ref=?', (ref,))
                obs.execute('DELETE FROM system_information_expired_evidence_candidates WHERE evidence_ref=?', (ref,))
                removed += 1
                total_bytes += allocated
            obs.execute("INSERT INTO system_information_state VALUES('attempt_evidence_reclaim_cursor',?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms", (str(rows[-1]['rowid']), now_ms))
        main.rollback()
    return {'scanned': len(rows), 'removed': removed, 'bytes': total_bytes}


def trim_archived_task_payloads(source: Path, store: Any, *, now_ms: int, retain_days: int = 30,
                                limit: int = 100, dry_run: bool = True) -> dict[str, int]:
    """Keep titles, text, source identity, metadata and all pipeline decisions searchable."""
    cutoff = now_ms - max(1, retain_days) * 86_400_000
    marks = ','.join('?' for _ in TERMINAL_STATUSES)
    after_id = int(store.get_state('task_payload_retention_cursor', '0') or 0) if not dry_run else 0
    with connect_sqlite_readonly(source, timeout_seconds=2) as conn:
        columns = {str(row['name']) for row in conn.execute('PRAGMA table_info(tasks)')}
        if not {'id', 'raw_payload', 'updated_at', 'status'} <= columns:
            return {'cleared': 0, 'protected': 0}
        age = 'COALESCE(updated_at,created_at)' if 'created_at' in columns else 'updated_at'
        rows = conn.execute(f"SELECT id,status,raw_payload,updated_at FROM tasks WHERE id>? AND status IN ({marks}) AND raw_payload IS NOT NULL AND raw_payload NOT IN ('','{{}}','null') AND (julianday({age})-2440587.5)*86400000<? ORDER BY id LIMIT ?",
                            (after_id, *TERMINAL_STATUSES, cutoff, max(1, min(limit, 500)))).fetchall()
    cleared = protected = 0
    for row in rows:
        with store.connect() as archive:
            item = archive.execute("SELECT item_id,evidence_ref,updated_at_ms FROM system_information_items WHERE source_table='tasks' AND source_row_id=? AND source_database=?", (str(row['id']), str(source))).fetchone()
            has_pending = item and archive.execute("SELECT 1 FROM system_information_items WHERE parent_item_id=? AND result IN ('processing','unknown','backoffice') LIMIT 1", (item['item_id'],)).fetchone()
        evidence = store.load_evidence(item['evidence_ref']) if item and not has_pending and item['updated_at_ms'] is not None and item['updated_at_ms'] < cutoff else None
        archived = evidence.get('task', {}).get('raw_payload') if isinstance(evidence, dict) else None
        if archived != row['raw_payload']:
            protected += 1
            continue
        # Optimistic comparison prevents deleting a payload changed since the verified snapshot.
        if dry_run:
            cleared += 1
            continue
        with sqlite3.connect(source, timeout=2, factory=ClosingSQLiteConnection) as conn:
            conn.execute('PRAGMA busy_timeout=2000')
            cursor = conn.execute(f"UPDATE tasks SET raw_payload='{{}}' WHERE id=? AND raw_payload=? AND updated_at IS ? AND status IN ({marks})",
                                  (row['id'], row['raw_payload'], row['updated_at'], *TERMINAL_STATUSES))
            cleared += cursor.rowcount
    if not dry_run:
        store.set_state('task_payload_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'cleared': cleared, 'protected': protected}
