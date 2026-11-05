"""Trim terminal task transport payloads only after their archive has been verified."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly

TERMINAL_STATUSES = ('discarded', 'duplicate', 'auto_published', 'published', 'notified')


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
        marks = ','.join('?' for _ in TERMINAL_STATUSES)
        rows = conn.execute(
            f"SELECT p.task_id,p.search_result FROM x_task_pipeline p JOIN tasks t ON t.id=p.task_id "
            f"WHERE p.task_id>? AND t.status IN ({marks}) AND (julianday(t.updated_at)-2440587.5)*86400000<? "
            "ORDER BY p.task_id LIMIT ?", (cursor, *TERMINAL_STATUSES, cutoff, max(1, min(limit, 500))),
        ).fetchall()
        cleared = 0
        for row in rows:
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
