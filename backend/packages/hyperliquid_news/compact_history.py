"""Build and query compact historical statistics without retaining raw fills.

This module is an offline building block. It does not manage ingestion cursors,
deduplication across batches, persistence, or removal of existing fills.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from decimal import Decimal
import json
from typing import Any

from .episodes import advance_episode
from .history import _ordered_by_position, fill_key, perp_fill, summarize
from .models import ZERO, number


def compact_history(fills: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact one complete, deduplicated history; ambiguous ordering fails closed."""
    return extend_compact({'version': 1, 'assets': {}}, fills)


def extend_compact(data: dict[str, Any], fills: list[dict[str, Any]], *, copy_data: bool = True) -> dict[str, Any]:
    """Append unique, strictly newer fills; leave input intact on any failure.

    The caller must deduplicate against already processed batches atomically
    with persistence. Equal/older timestamps are rejected because their true
    execution order cannot be reconstructed from aggregate data alone.
    """
    if data.get('version') != 1:
        raise ValueError('Unsupported compact history')
    unique = {fill_key(fill): fill for fill in fills if perp_fill(fill)}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in unique.values():
        grouped[str(fill['coin'])].append(fill)
    result = deepcopy(data) if copy_data else data
    assets = result['assets']
    for coin, items in grouped.items():
        previous = assets.get(coin)
        if previous and min(int(fill['time']) for fill in items) <= previous['moments'][-1]['time_ms']:
            raise ValueError(f'Late or same-timestamp fill for {coin}')
        ordered = _ordered_by_position(items)
        if previous and previous.get('ordering_expected') is not None:
            prior = number(previous['ordering_expected'])
            anchor = {'coin': coin, 'time': int(ordered[0]['time']) - 1, 'tid': '__anchor__',
                      'startPosition': str(prior - 1), 'sz': '1', 'side': 'B'}
            ordered = _ordered_by_position([anchor, *items])[1:]
        episodes = previous['episodes'] if previous else []
        current = None
        if episodes and episodes[-1]['end_ms'] is None and episodes[-1]['continuous']:
            current = episodes.pop()
        moments: dict[int, dict[str, Any]] = {}
        gaps = previous['gaps'] if previous else []
        expected = number(previous['expected']) if previous and 'expected' in previous else (
            number(current['position']) if current else (ZERO if previous else None))
        previous_ms = previous['moments'][-1]['time_ms'] if previous else None
        for fill in ordered:
            stamp = int(fill['time'])
            before = number(fill['startPosition'])
            if expected is not None and before != expected:
                gaps.append([previous_ms, stamp])
            size = number(fill['sz'])
            delta = size if fill['side'] == 'B' else -size
            if before * delta >= 0 and number(fill.get('closedPnl', '0')) != 0:
                raise ValueError(f'Unexpected opening PnL for {coin} at {stamp}')
            expected = before + (size if fill['side'] == 'B' else -size)
            previous_ms = stamp
            unsupported = fill.get('feeToken', 'USDC') not in ('USDC', 'USDH')
            fee = ZERO if unsupported else number(fill.get('fee', '0'))
            moment = moments.setdefault(stamp, {'time_ms': stamp, 'count': 0,
                'pnl_less_fees': '0', 'unsupported_fee_currency': False, 'ambiguous_count': 0})
            moment['count'] += 1
            moment['ambiguous_count'] += int(bool(fill.get('_ambiguous_order')))
            moment['pnl_less_fees'] = str(number(moment['pnl_less_fees']) + number(fill.get('closedPnl', '0')) - fee)
            moment['unsupported_fee_currency'] |= unsupported
            if fill.get('_ambiguous_order'):
                if current:
                    current['continuous'] = False
                    episodes.append(current)
                    current = None
            else:
                current, completed = advance_episode(current, fill)
                episodes.extend(completed)
        if current:
            episodes.append(current)
        last_stamp = int(ordered[-1]['time'])
        last_ambiguous = any(f.get('_ambiguous_order') for f in ordered if int(f['time']) == last_stamp)
        assets[coin] = {'moments': (previous['moments'] if previous else []) + list(moments.values()),
                        'episodes': episodes, 'gaps': gaps, 'expected': str(expected),
                        'ordering_expected': None if last_ambiguous else str(expected),
                        'round_summaries': previous.get('round_summaries', []) if previous else []}
    return result


def summarize_segment(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce a whole segment for queries whose boundaries do not split it."""
    result = {'version': 1, 'assets': {}}
    for coin, asset in data['assets'].items():
        moments = asset['moments']
        reduced = []
        if moments:
            first, last = moments[0]['time_ms'], moments[-1]['time_ms']
            reduced.append({'time_ms': first, 'count': sum(m['count'] for m in moments),
                'pnl_less_fees': str(sum((number(m['pnl_less_fees']) for m in moments), ZERO)),
                'unsupported_fee_currency': any(m['unsupported_fee_currency'] for m in moments),
                'ambiguous_count': sum(m.get('ambiguous_count', 0) for m in moments)})
            if last != first:
                reduced.append({'time_ms': last, 'count': 0, 'pnl_less_fees': '0',
                                'unsupported_fee_currency': False, 'ambiguous_count': 0})
        rounds = [episode for episode in asset['episodes'] if episode['end_ms'] is not None
                  and episode['complete_start'] and episode['continuous']]
        summaries = []
        if rounds:
            pnls = [number(episode['closed_pnl']) - number(episode['fees_usd']) for episode in rounds]
            trailing = 0
            for pnl in reversed(pnls):
                if pnl >= 0:
                    break
                trailing += 1
            summaries.append({'start_ms': rounds[0]['end_ms'], 'end_ms': rounds[-1]['end_ms'],
                              'rounds': len(rounds), 'wins': sum(pnl > 0 for pnl in pnls),
                              'trailing_losses': trailing})
        result['assets'][coin] = {'moments': reduced, 'episodes': [], 'gaps': asset['gaps'],
                                  'round_summaries': summaries}
    return result


def summarize_compact(data: dict[str, Any], *, start_ms: int, end_ms: int,
                      coverage: str = 'retained_api_history') -> dict[str, Any]:
    """Reproduce the existing history fields for a closed time interval."""
    if data.get('version') != 1 or end_ms < start_ms:
        raise ValueError('Unsupported compact history or time interval')
    assets = {}
    count = 0
    for coin, compact in data['assets'].items():
        moments = [m for m in compact['moments'] if start_ms <= m['time_ms'] <= end_ms]
        if not moments:
            continue
        count += sum(m['count'] for m in moments)
        rounds = []
        for episode in compact['episodes']:
            if (episode['end_ms'] is not None and episode['complete_start'] and episode['continuous']
                    and start_ms <= episode['end_ms'] <= end_ms):
                pnl = number(episode['closed_pnl']) - number(episode['fees_usd'])
                rounds.append({'pnl': str(pnl), 'close_notional': episode['closing_notional'],
                               'end_ms': episode['end_ms']})
        chunks = [{'start_ms': round_['end_ms'], 'end_ms': round_['end_ms'], 'rounds': 1,
                   'wins': int(number(round_['pnl']) > 0),
                   'trailing_losses': int(number(round_['pnl']) < 0)} for round_ in rounds]
        for summary in compact.get('round_summaries', []):
            if summary['end_ms'] < start_ms or summary['start_ms'] > end_ms:
                continue
            if summary['start_ms'] < start_ms or summary['end_ms'] > end_ms:
                raise ValueError('Query boundary splits a round summary')
            chunks.append(summary)
        chunks.sort(key=lambda item: item['end_ms'])
        round_count = sum(item['rounds'] for item in chunks)
        wins = sum(item['wins'] for item in chunks)
        streak = 0
        for item in reversed(chunks):
            streak += item['trailing_losses']
            if item['trailing_losses'] != item['rounds']:
                break
        assets[coin] = {
            'first_observed_fill_ms': moments[0]['time_ms'],
            'last_observed_fill_ms': moments[-1]['time_ms'],
            'closed_pnl_less_fees': str(sum((number(m['pnl_less_fees']) for m in moments), ZERO)),
            'rounds': round_count, 'wins': wins,
            'win_rate': str(Decimal(wins) / round_count) if round_count else None,
            'completed_rounds': rounds, 'completed_round_pnl': [r['pnl'] for r in rounds],
            'consecutive_losing_rounds': streak,
            'ambiguous_order_fills': sum(m.get('ambiguous_count', 0) for m in moments),
            'position_discontinuities': sum(start_ms <= previous and current <= end_ms
                                            for previous, current in compact['gaps']),
            'unsupported_fee_currency': any(m['unsupported_fee_currency'] for m in moments),
        }
    rounds = sum(asset['rounds'] for asset in assets.values())
    wins = sum(asset['wins'] for asset in assets.values())
    return {
        'start_ms': start_ms, 'end_ms': end_ms, 'coverage': coverage, 'fills': count,
        'first_observed_fill_ms': min((a['first_observed_fill_ms'] for a in assets.values()), default=None),
        'last_observed_fill_ms': max((a['last_observed_fill_ms'] for a in assets.values()), default=None),
        'assets': assets, 'account_rounds': rounds, 'account_wins': wins,
        'account_win_rate': str(Decimal(wins) / rounds) if rounds else None,
        'account_closed_pnl_less_fees': str(sum((number(a['closed_pnl_less_fees']) for a in assets.values()), ZERO)) if assets else None,
        'pnl_scope': 'observed_perp_fills_excluding_funding',
        'fee_currency_complete': not any(a['unsupported_fee_currency'] for a in assets.values()),
    }


def verify_compact_history(fills: list[dict[str, Any]], compact: dict[str, Any],
                           windows: list[tuple[int, int]]) -> None:
    """Fail if any requested window changes an existing history fact."""
    def normalize(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            try:
                return Decimal(value)
            except Exception:
                return value
        return value

    for start_ms, end_ms in windows:
        expected = summarize(fills, start_ms=start_ms, end_ms=end_ms)
        full = summarize(fills, start_ms=0, end_ms=end_ms)
        for coin, asset in expected['assets'].items():
            completed = [r for r in full['assets'][coin]['completed_rounds'] if r['end_ms'] >= start_ms]
            wins = sum(number(r['pnl']) > 0 for r in completed)
            streak = 0
            for round_ in reversed(completed):
                if number(round_['pnl']) >= 0:
                    break
                streak += 1
            asset.update(rounds=len(completed), wins=wins,
                win_rate=str(Decimal(wins) / len(completed)) if completed else None,
                completed_rounds=completed, completed_round_pnl=[r['pnl'] for r in completed],
                consecutive_losing_rounds=streak)
            asset.pop('ambiguous_order_fills')
            asset.pop('position_discontinuities')
        expected['account_rounds'] = sum(a['rounds'] for a in expected['assets'].values())
        expected['account_wins'] = sum(a['wins'] for a in expected['assets'].values())
        expected['account_win_rate'] = (str(Decimal(expected['account_wins']) / expected['account_rounds'])
                                        if expected['account_rounds'] else None)
        expected = normalize(expected)
        actual = normalize(summarize_compact(compact, start_ms=start_ms, end_ms=end_ms))
        for asset in actual['assets'].values():
            asset.pop('ambiguous_order_fills')
            asset.pop('position_discontinuities')
        if actual != expected:
            raise ValueError(f'Compacted history differs from source for window {start_ms}..{end_ms}')


def estimate_compaction(fills: list[dict[str, Any]], compact: dict[str, Any]) -> dict[str, int]:
    """Compare serialized payload sizes, excluding SQLite indexes and page overhead."""
    encode = lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    unique = {fill_key(fill): fill for fill in fills if perp_fill(fill)}
    assets = compact['assets'].values()
    return {
        'source_fills': len(unique),
        'source_payload_bytes': sum(encode(fill) for fill in unique.values()),
        'compact_payload_bytes': encode(compact),
        'moments': sum(len(asset['moments']) for asset in assets),
        'episodes': sum(len(asset['episodes']) for asset in compact['assets'].values()),
    }
