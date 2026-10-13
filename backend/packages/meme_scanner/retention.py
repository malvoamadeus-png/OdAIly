"""Bound Meme detail history while preserving permanent trigger identities."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

RETENTION_DAYS = 3
KEY_LEVEL = 3_000_000
TERMINAL = "('publisher_pending','discarded','publish_failed')"


def prune(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    cutoff = ((now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)).isoformat()
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_token_snapshots_retention ON token_snapshots(observed_at);
        CREATE TABLE IF NOT EXISTS key_breakthrough_results (
            trigger_key TEXT PRIMARY KEY, status TEXT NOT NULL, reason TEXT,
            title TEXT, completed_at TEXT
        );
    """)
    counts: dict[str, int] = {}
    # Save only the outcome, rather than the large narrative/evidence/publish payloads.
    with conn:
        conn.execute(f"""INSERT OR REPLACE INTO key_breakthrough_results
            SELECT j.trigger_key,j.status,j.reason,j.title,COALESCE(j.completed_at,j.updated_at)
            FROM jobs j JOIN market_cap_milestones m ON m.trigger_key=j.trigger_key
            WHERE m.level>=? AND j.status IN {TERMINAL}
              AND COALESCE(j.completed_at,j.updated_at)<?""", (KEY_LEVEL, cutoff))
        counts['jobs'] = conn.execute(f"""DELETE FROM jobs WHERE status IN {TERMINAL}
            AND COALESCE(completed_at,updated_at)<?""", (cutoff,)).rowcount
        conn.execute("""UPDATE market_cap_milestones SET job_id=NULL
            WHERE job_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM jobs WHERE id=job_id)""")
        counts['tg_candidates'] = conn.execute("""DELETE FROM tg_candidates WHERE detected_at<?
            AND NOT EXISTS(SELECT 1 FROM jobs WHERE trigger_key='tg_burst:' || tg_candidates.id)""", (cutoff,)).rowcount
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ca_mentions'").fetchone():
            counts['ca_mentions'] = conn.execute('DELETE FROM ca_mentions WHERE sent_at<?', (cutoff,)).rowcount
        # Keep the identity, lifecycle and high-water mark: deleting them restarts old coins.
        counts['observations'] = conn.execute("""UPDATE observations SET
            platform='',symbol='',last_volume_24h=0,last_token_info_error=NULL,
            token_info_failures=0,last_token_info_at=NULL,next_token_info_at=NULL,
            tracking_interval_seconds=NULL,last_completed_seen_at=NULL
            WHERE tracking_status!='active' AND last_seen_at<?
              AND (symbol!='' OR last_token_info_error IS NOT NULL OR last_completed_seen_at IS NOT NULL)""", (cutoff,)).rowcount
    # Small commits limit writer blocking and WAL growth on a running scanner.
    removed = 0
    while True:
        with conn:
            n = conn.execute("""DELETE FROM token_snapshots WHERE id IN (
                SELECT id FROM token_snapshots WHERE observed_at<?
                AND id NOT IN (SELECT snapshot_id FROM market_cap_milestones WHERE level>=?)
                LIMIT 1000)""", (cutoff, KEY_LEVEL)).rowcount
        removed += n
        if n < 1000:
            break
    counts['token_snapshots'] = removed
    with conn:
        conn.execute("""UPDATE market_cap_milestones SET snapshot_id=0 WHERE level<?
            AND snapshot_id!=0 AND NOT EXISTS(SELECT 1 FROM token_snapshots WHERE id=snapshot_id)""", (KEY_LEVEL,))
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--vacuum', action='store_true', help='Offline only: stop all database writers first.')
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error('Database must already exist')
    with sqlite3.connect(args.db, timeout=30) as conn:
        print(json.dumps(prune(conn), sort_keys=True), flush=True)
        if args.vacuum:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute('VACUUM')
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            print('vacuum complete', flush=True)


if __name__ == '__main__':
    main()
