"""Compare compacted and raw fills from JSON lines without changing the source."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from packages.hyperliquid_news.compact_history import summarize_compact, verify_compact_history
from packages.hyperliquid_news.store import NewsStore


def main() -> None:
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    if not rows:
        raise ValueError('No fills supplied')
    address = '0x' + '1' * 40
    newest = max(int(row['time']) for row in rows)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='hl-compact-audit-') as directory:
        store = NewsStore(Path(directory) / 'audit.sqlite')
        store.initialize()
        stamp = newest + 120000
        store.commit_observation(address=address, now_ms=stamp,
            state={'observed_ms': stamp, 'history_last_queried_ms': stamp},
            history={}, coverage={'pagination_complete': True}, fills=rows, candidates=[],
            state_table='hl_news_normal_state')
        while True:
            result = store.compact_fills(address, cutoff_ms=stamp, batch_size=500)
            if not result['fills'] and result.get('phase') != 'activated':
                break
        stamps = sorted({int(row['time']) for row in rows})
        windows = [(0, newest)]
        for offset in (0, len(stamps) // 4, len(stamps) // 2, 3 * len(stamps) // 4):
            windows.append((stamps[offset], newest))
        cuts = [value for start, end in windows for value in (start, end + 1)]
        archive = store.compact_history(address, min(cuts), cuts=cuts)
        if not archive:
            raise ValueError('No compacted history produced')
        verify_compact_history(rows, archive[1], windows)
        reduced = store.compact_history(address, min(cuts), cuts=cuts, aggregate_rounds=True)
        for start, end in windows:
            exact = summarize_compact(archive[1], start_ms=start, end_ms=end)
            aggregate = summarize_compact(reduced[1], start_ms=start, end_ms=end)
            for result in (exact, aggregate):
                for asset in result['assets'].values():
                    asset.pop('completed_rounds')
                    asset.pop('completed_round_pnl')
            if exact != aggregate:
                raise ValueError(f'Aggregated history differs for window {start}..{end}')
        print(json.dumps({'fills': len(rows), 'windows_verified': len(windows),
                          'database_bytes': store.path.stat().st_size,
                          'elapsed_seconds': round(time.monotonic() - started, 2)}))


if __name__ == '__main__':
    main()
