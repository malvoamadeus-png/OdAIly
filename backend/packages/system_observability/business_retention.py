"""Trim terminal task transport payloads only after their archive has been verified."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection, connect_sqlite_readonly

TERMINAL_STATUSES = ('discarded', 'duplicate', 'auto_published', 'published', 'notified')


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
