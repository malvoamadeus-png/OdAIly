"""Reset Hyperliquid fill history and monitoring baselines while the worker is stopped."""
from __future__ import annotations

import argparse
import json
import time

from packages.common.storage import connect_sqlite, load_hyperliquid_storage_path
from packages.hyperliquid_news.store import NewsStore


def reset_history(path, *, now_ms: int, lookback_ms: int) -> dict:
    if lookback_ms < 0:
        raise ValueError('Lookback must be nonnegative')
    with connect_sqlite(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('DROP TABLE hl_news_fills')
        conn.execute('''CREATE TABLE hl_news_fills (
            address TEXT NOT NULL, fill_key TEXT NOT NULL, time_ms INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(address, fill_key))''')
        conn.execute('CREATE INDEX hl_news_fills_time ON hl_news_fills(address,time_ms)')
        for table in ('hl_news_monitor_fill_keys', 'hl_news_compact_history',
                      'hl_news_compact_work', 'hl_news_compact_segments',
                      'hl_news_normal_state', 'hl_news_state', 'hl_news_normal_schedule'):
            conn.execute(f'DELETE FROM {table}')
        conn.execute("DELETE FROM hl_news_metadata WHERE key LIKE 'normal_pending_fills:%' "
                     "OR key LIKE 'account_pending_fills:%' OR key='divergence'")
        start_ms = max(0, now_ms - lookback_ms)
        for key, value in (('compact_history_enabled', True),
                           ('compact_history_reset_start_ms', start_ms)):
            conn.execute('''INSERT INTO hl_news_metadata(key,payload) VALUES(?,?)
                ON CONFLICT(key) DO UPDATE SET payload=excluded.payload''', (key, json.dumps(value)))
    return {'reset_ms': now_ms, 'history_start_ms': start_ms, 'online_compaction_enabled': True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Actually discard history and reset baselines')
    parser.add_argument('--lookback-minutes', type=int, default=60)
    args = parser.parse_args()
    if not args.execute:
        parser.error('The worker must be stopped; pass --execute to discard history')
    path = load_hyperliquid_storage_path()
    NewsStore(path).initialize()
    print(json.dumps(reset_history(path, now_ms=int(time.time() * 1000),
                                   lookback_ms=args.lookback_minutes * 60000)), flush=True)


if __name__ == '__main__':
    main()
