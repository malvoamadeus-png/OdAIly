"""Bounded public Hyperliquid loss scan; local evidence files only, no product DB."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from packages.hyperliquid_news.account_loss import DAY, loss_performance, replay_loss_actions
from packages.hyperliquid_news.client import NewsClient, address_key
from packages.hyperliquid_news.models import Rules, number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--address-limit', type=int, default=32)
    parser.add_argument('--lookback-days', type=int, default=7)
    parser.add_argument('--max-pages', type=int, default=2)
    parser.add_argument('--address', action='append', default=[], help='Explicit public address seed; repeatable, still bounded.')
    args = parser.parse_args()
    if not (1 <= args.address_limit <= 100 and 1 <= args.lookback_days <= 30 and 1 <= args.max_pages <= 6):
        parser.error('Scan bounds: 1..100 addresses, 1..30 days, 1..6 pages')
    client = NewsClient(timeout_seconds=25, max_attempts=2, backoff_seconds=1)
    now = int(time.time()*1000)
    rows = client.leaderboard()
    seeds = []
    # Union the negative ends of multiple periods; this is bounded sampling.
    for window in ('allTime', 'month', 'week', 'day'):
        ordered = sorted(rows, key=lambda r: number(dict(r.get('windowPerformances', [])).get(window, {}).get('pnl', 0)))
        for row in ordered[:max(12, args.address_limit)]:
            address = row['ethAddress']
            if address not in seeds:
                seeds.append(address)
    # Interleave periods to avoid allTime consuming the entire discovery budget.
    ranks = {}
    for window in ('allTime', 'month', 'week', 'day'):
        ordered = sorted(rows, key=lambda r: number(dict(r.get('windowPerformances', [])).get(window, {}).get('pnl', 0)))
        for rank, row in enumerate(ordered[:args.address_limit]):
            ranks[row['ethAddress']] = min(rank, ranks.get(row['ethAddress'], rank))
    seeds.sort(key=lambda a: ranks.get(a, args.address_limit))
    explicit = list(dict.fromkeys(address_key(a) for a in args.address))
    seeds = explicit + [a for a in seeds if a not in explicit]
    report = {'observed_ms': now, 'rules': asdict(Rules()), 'lookback_days': args.lookback_days,
              'max_pages': args.max_pages, 'scanned': 0, 'failures': {}, 'cases': [],
              'addresses': [], 'scope': 'bounded_public_leaderboard_sample_not_entire_network'}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for address in seeds[:args.address_limit]:
        try:
            portfolio = client.portfolio(address)
            performance = loss_performance(portfolio, cutoff_ms=now)
            # Each historical action is qualified against its own prior allTime sample.
            fills, coverage = client.history(address, now-args.lookback_days*DAY, now, max_pages=args.max_pages)
            events = replay_loss_actions(address, fills, portfolio)
            # A page cap must not turn an unfinished last window into a news case.
            frontier = max((int(f['time']) for f in fills), default=0)
            if not coverage.get('pagination_complete', True):
                events = [e for e in events if int(e['evidence']['actions']['first_fill_ms'])+3600000 < frontier]
            for event in events:
                event['evidence']['test_replay']['real_public_data'] = True
                report['cases'].append({'label': '真实公开成交回放（真实地址可核验）', 'event': event})
            report['addresses'].append({'address': address, 'performance': performance,
                'coverage': coverage, 'fills': len(fills), 'cases': len(events)})
            report['scanned'] += 1
            print(json.dumps({'scanned': report['scanned'], 'cases': len(report['cases'])}), flush=True)
        except Exception as exc:
            report['failures'][address] = str(exc)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
