"""Resume bounded, transactional Hyperliquid history compaction."""
from __future__ import annotations

import argparse
import json
import sys
import time

from packages.common.storage import connect_sqlite_readonly, load_hyperliquid_storage_path
from packages.hyperliquid_news.store import NewsStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--enable-online', action='store_true')
    mode.add_argument('--disable-online', action='store_true')
    parser.add_argument('--batch-size', type=int, default=5000)
    parser.add_argument('--pause-seconds', type=float, default=0.1)
    args = parser.parse_args()
    if args.batch_size < 1 or args.pause_seconds < 0:
        parser.error('Batch size must be positive and pause nonnegative')
    path = load_hyperliquid_storage_path()
    store = NewsStore(path)
    if args.execute or args.enable_online or args.disable_online:
        store.initialize()
    if args.enable_online or args.disable_online:
        enabled = bool(args.enable_online)
        store.save_global('compact_history_enabled', enabled, [])
        print(json.dumps({'online_compaction_enabled': enabled}))
        return
    with connect_sqlite_readonly(path) as conn:
        addresses = [row[0] for row in conn.execute('SELECT DISTINCT address FROM hl_news_fills ORDER BY address')]
    report = {'addresses': len(addresses), 'processed_fills': 0, 'deleted_fills': 0,
              'batches': 0, 'failed': 0, 'skipped': 0}
    if args.execute:
        remaining = list(enumerate(addresses, 1))
        rebuilds = {}
        while remaining:
            next_round = []
            for index, address in remaining:
                try:
                    result = store.compact_fills(address, cutoff_ms=int(time.time() * 1000), batch_size=args.batch_size)
                    count = result['fills']
                    if not count:
                        if result.get('phase') in ('activated', 'retry'):
                            next_round.append((index, address))
                            if result.get('phase') == 'retry':
                                time.sleep(max(args.pause_seconds, 0.1))
                        elif result.get('phase') == 'rebuild':
                            rebuilds[index] = rebuilds.get(index, 0) + 1
                            if rebuilds[index] <= 3:
                                next_round.append((index, address))
                            else:
                                report['failed'] += 1
                            print(json.dumps({'address_index': index, 'phase': 'rebuild',
                                              'attempt': rebuilds[index]}), flush=True)
                        report['skipped'] += int(bool(result.get('reason')))
                        continue
                    phase = result.get('phase')
                    if phase == 'cleanup':
                        report['deleted_fills'] += count
                    elif phase == 'incremental':
                        report['processed_fills'] += count
                        report['deleted_fills'] += count
                    else:
                        report['processed_fills'] += count
                    report['batches'] += 1
                    next_round.append((index, address))
                    print(json.dumps({'address_index': index, 'phase': phase, 'fills': count,
                                      'processed_total': report['processed_fills'],
                                      'deleted_total': report['deleted_fills']}), flush=True)
                    if args.pause_seconds:
                        time.sleep(args.pause_seconds)
                except Exception as exc:
                    report['failed'] += 1
                    print(json.dumps({'address_index': index, 'error': str(exc)}), flush=True)
            remaining = next_round
    print(json.dumps({'execute': args.execute, **report}), flush=True)
    if report['failed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
