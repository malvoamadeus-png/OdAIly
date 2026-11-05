"""Trim terminal task transport payloads only after their archive has been verified."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly

TERMINAL_STATUSES = ('discarded', 'duplicate', 'auto_published', 'published', 'notified')
SOURCE_TEXT_TYPES = ('x', 'binance_square', 'blockbeats', 'panews', 'jinse', 'jin10',
                     'non_mainstream_media', 'crypto_source', 'ai_source', 'mixed_source',
                     'mainstream_media', 'external_media_alert', 'ai_source_alert', 'event_tracking')


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
            if (row['status'] not in TERMINAL_STATUSES or row['source'] not in SOURCE_TEXT_TYPES or not row['source_url']
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
    cutoff = now_ms - 3 * 86_400_000
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
    cutoff = now_ms - 3 * 86_400_000
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
            if (row['source'] == 'odaily' or not row['source_url'] or row['age_ms'] is None or row['age_ms'] >= cutoff
                    or (not row['content'] and row['raw_payload'] in ('', '{}', 'null'))):
                continue
            if conn.execute("SELECT 1 FROM newsflash_event_sources s JOIN newsflash_events e ON e.event_id=s.event_id WHERE s.item_id=? AND (e.updated_at IS NULL OR (julianday(e.updated_at)-2440587.5)*86400000>=?) LIMIT 1", (row['id'], cutoff)).fetchone():
                continue
            cleared += conn.execute("UPDATE newsflash_items SET content='',raw_payload='{}' WHERE id=? AND content IS ? AND raw_payload IS ?", (row['id'], row['content'], row['raw_payload'])).rowcount
        conn.commit()
    store.set_state('competitor_text_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


def trim_attempt_details(source: Path, store: Any, *, now_ms: int, limit: int = 100) -> dict[str, int]:
    cutoff = now_ms - 3 * 86_400_000
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
        cleared = 0
        for row in rows:
            if row['task_status'] not in TERMINAL_STATUSES or row['status'] in {'running', 'pending'} or row['age_ms'] is None or row['age_ms'] >= cutoff:
                continue
            if conn.execute('SELECT 1 FROM x_processing_attempt_events WHERE task_id=? AND attempt_id=? AND id>? LIMIT 1', (row['task_id'], row['id'], watermark)).fetchone():
                continue
            if row['stage'] == 'write':
                cleared += conn.execute("UPDATE x_processing_attempts SET input_json='{}',rule_json='{}' WHERE id=? AND (input_json<>'{}' OR rule_json<>'{}')", (row['id'],)).rowcount
            else:
                cleared += conn.execute("UPDATE x_processing_attempts SET input_json='{}',rule_json='{}',response_gzip=NULL,response_ref=NULL,response_excerpt=NULL WHERE id=?", (row['id'],)).rowcount
                conn.execute('UPDATE x_processing_attempt_events SET response_gzip=NULL,response_ref=NULL,response_excerpt=NULL WHERE task_id=? AND attempt_id=? AND id<=?', (row['task_id'], row['id'], watermark))
        conn.commit()
    store.set_state('attempt_retention_cursor', int(rows[-1]['id']) if rows else 0)
    return {'scanned': len(rows), 'cleared': cleared}


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
