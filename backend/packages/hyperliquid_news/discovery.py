"""Bounded discovery and explicit eligibility for expensive asset statistics."""
from __future__ import annotations

from typing import Any

from .client import address_key
from .models import Position, Rules, ZERO, number

LEADERBOARD_WINDOWS = ('day', 'week', 'month', 'allTime')


def leaderboard_candidates(rows: list[dict[str, Any]], per_side: int, now_ms: int):
    """Top/bottom N of each official time window; union by address later."""
    for window in LEADERBOARD_WINDOWS:
        eligible = [(row, dict(row.get('windowPerformances', [])).get(window)) for row in rows]
        eligible = [(row, data) for row, data in eligible if isinstance(data, dict) and data.get('pnl') is not None]
        eligible.sort(key=lambda item: number(item[1]['pnl']), reverse=True)
        positive = [(row, data) for row, data in eligible if number(data['pnl']) > 0][:per_side]
        negative = [(row, data) for row, data in reversed(eligible) if number(data['pnl']) < 0][:per_side]
        for side, selected in [('profit', positive), ('loss', negative)]:
            for rank, (row, data) in enumerate(selected, 1):
                yield address_key(row['ethAddress']), {
                    'kind': 'leaderboard', 'window': window, 'side': side, 'rank': rank,
                    'pnl': str(number(data['pnl'])), 'observed_ms': now_ms,
                    'pnl_scope': 'official_leaderboard_not_assumed_realized',
                }


def trade_candidates(trades: list[dict[str, Any]], coin: str, rules: Rules, now_ms: int):
    totals: dict[str, Any] = {}
    seen = set()
    for trade in trades:
        if str(trade.get('coin')) != coin:
            raise ValueError('Trade market mismatch')
        key = (int(trade['time']), coin, str(trade['tid']))
        if key in seen:
            continue
        seen.add(key)
        value = number(trade['px']) * number(trade['sz'])
        if value <= 0:
            raise ValueError('Invalid trade notional')
        for address in set(address_key(a) for a in trade.get('users', [])):
            totals[address] = totals.get(address, ZERO) + value
    for address, value in totals.items():
        if value >= min(rules.action, rules.win_rate_action):
            yield address, {'kind': 'large_observed_trade', 'coin': coin,
                            'sample_notional': str(value), 'observed_ms': now_ms,
                            'basis': 'recent_trades_sample_not_complete_window'}


def enrichment_reasons(positions: dict[str, Position], sources: list[dict[str, Any]],
                       markets: dict[str, dict[str, Any]], rules: Rules) -> list[str]:
    reasons = set()
    for source in sources:
        if source['kind'] in ('leaderboard', 'large_observed_trade', 'manual', 'asset_leaderboard'):
            reasons.add(source['kind'])
    for coin, p in positions.items():
        if p.notional >= min(rules.action, rules.win_rate_action):
            reasons.add('important_position')
        if abs(p.unrealized_pnl) >= rules.pnl_line(coin):
            reasons.add('large_unrealized_pnl')
        for field, threshold in [('oi_notional', rules.oi_share), ('market_cap', rules.market_cap_share)]:
            denominator = number(markets.get(coin, {}).get(field, '0'))
            if denominator > 0 and p.notional / denominator >= threshold:
                reasons.add(field + '_share')
    return sorted(reasons)
