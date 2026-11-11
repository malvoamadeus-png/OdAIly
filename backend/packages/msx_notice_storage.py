"""Separate MSX capture state without moving the shared news task queue."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from packages.common.storage import connect_sqlite_readonly

TABLES = {
    'msx_notice_state': ('singleton_key', 'seeded_at', 'last_polled_at', 'last_success_at', 'last_error', 'updated_at'),
    'msx_notice_seen_items': ('source_item_id', 'seeded', 'created_at'),
}
SCHEMA = """
CREATE TABLE msx_notice_state(singleton_key TEXT PRIMARY KEY CHECK(singleton_key='global'), seeded_at TEXT,
 last_polled_at TEXT,last_success_at TEXT,last_error TEXT,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE msx_notice_seen_items(source_item_id TEXT PRIMARY KEY,seeded INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE msx_notice_storage(singleton INTEGER PRIMARY KEY CHECK(singleton=1),schema_version INTEGER NOT NULL);
"""


def capture_path(main: Path, configured: str | None = None) -> Path:
    configured = os.getenv('ODAILY_MSX_NOTICE_SQLITE_PATH', '') if configured is None else configured
    if not configured.strip():
        return main
    path = Path(configured).expanduser().resolve()
    if not path.is_file() or path == main.resolve():
        raise RuntimeError('MSX capture database must be a verified separate migration')
    try:
        with connect_sqlite_readonly(path) as conn:
            marker = conn.execute('SELECT schema_version FROM msx_notice_storage WHERE singleton=1').fetchone()
            valid = all(tuple(row['name'] for row in conn.execute(f'PRAGMA table_info({table})')) == columns
                        for table, columns in TABLES.items())
    except sqlite3.Error as exc:
        raise RuntimeError('MSX capture database is not a verified migration') from exc
    if not marker or marker[0] != 1 or not valid:
        raise RuntimeError('MSX capture database is not a verified migration')
    return path


def migrate_msx_capture(source: Path, destination: Path) -> dict[str, int]:
    """Migrate while the MSX worker is stopped; never overwrite existing files."""
    source, destination = source.resolve(), destination.resolve()
    if not source.is_file() or source == destination or not destination.parent.is_dir():
        raise ValueError('Source must exist and destination must be a separate new file')
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    completed = False
    try:
        with sqlite3.connect(source, timeout=2) as main:
            main.execute('BEGIN IMMEDIATE')
            for table, columns in TABLES.items():
                if tuple(row[1] for row in main.execute(f'PRAGMA table_info({table})')) != columns:
                    raise RuntimeError(f'Unexpected {table} schema')
                if main.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name=? LIMIT 1", (table,)).fetchone():
                    raise RuntimeError(f'Unexpected {table} trigger')
            if main.execute('SELECT COUNT(*) FROM msx_notice_state WHERE singleton_key=?', ('global',)).fetchone()[0] != 1:
                raise RuntimeError('MSX baseline state is missing')
            with sqlite3.connect(destination) as target:
                target.executescript(SCHEMA)
                counts = {}
                for table, columns in TABLES.items():
                    names = ','.join(columns)
                    rows = main.execute(f'SELECT {names} FROM {table} ORDER BY {columns[0]}').fetchall()
                    target.executemany(f"INSERT INTO {table} ({names}) VALUES ({','.join('?' for _ in columns)})", rows)
                    copied = target.execute(f'SELECT {names} FROM {table} ORDER BY {columns[0]}').fetchall()
                    if rows != copied:
                        raise RuntimeError(f'MSX migration verification failed for {table}')
                    counts[table] = len(rows)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('MSX migration integrity check failed')
                target.execute('INSERT INTO msx_notice_storage VALUES(1,1)')
                target.commit()
            main.rollback()
        completed = True
        return counts
    finally:
        if not completed:
            destination.unlink(missing_ok=True)


def main() -> None:
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Migrate stopped MSX capture state')
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--worker-stopped', action='store_true', required=True)
    args = parser.parse_args()
    print(json.dumps(migrate_msx_capture(args.source, args.destination)))


if __name__ == '__main__':
    main()
