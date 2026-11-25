"""Bounded discovery and explicit eligibility for expensive asset statistics."""
from __future__ import annotations

from typing import Any

from .client import address_key
from .models import Position, Rules, ZERO, number

LEADERBOARD_WINDOWS = ('day', 'week', 'month', 'allTime')
HFT_MAX_24H_OIDS = 3000
HFT_MAX_24H_FILLS = 10000
HFT_MAX_24H_ACTIVE_SECONDS = 3600
HFT_EARLY_MIN_OIDS = 1500
HFT_EARLY_MIN_FILLS = 2000
HFT_EARLY_EXTRAPOLATED_24H_OIDS = 6000
HFT_EARLY_EXTRAPOLATED_24H_FILLS = 20000
HFT_DISCOVERY_POSITION_PROBE_THRESHOLD = 80
HFT_AUTO_BLOCK_ACTOR = 'auto:high_frequency_quant'


def detect_high_frequency_quant(
    fills: list[dict[str, Any]],
    *,
    now_ms: int,
    pagination_complete: bool = True,
) -> dict[str, Any] | None:
    """Detect high-frequency quant/market-making addresses from 24h fill activity.

    Uses distinct order IDs (oid) as primary signal so single large directional TWAP orders
    split into many partial fills are not misclassified, plus hard fills/active-second caps
    and an early-stop extrapolated check when a bounded page fetch is already saturated.
    """
    window_start = now_ms - 86_400_000
    recent = [f for f in fills if int(f['time']) >= window_start]
    if not recent:
        return None
    fill_count = len(recent)
    oid_count = len({f.get('oid') for f in recent if f.get('oid') is not None})
    active_secs = len({int(f['time']) // 1000 for f in recent})
    coins = len({str(f.get('coin') or '') for f in recent if f.get('coin')})

    reason = None
    if oid_count >= HFT_MAX_24H_OIDS:
        reason = 'oids_24h_exceeded'
    elif fill_count >= HFT_MAX_24H_FILLS:
        reason = 'fills_24h_exceeded'
    elif active_secs >= HFT_MAX_24H_ACTIVE_SECONDS:
        reason = 'active_seconds_24h_exceeded'
    elif not pagination_complete and fill_count >= HFT_EARLY_MIN_FILLS and oid_count >= HFT_EARLY_MIN_OIDS:
        times = [int(f['time']) for f in recent]
        span_ms = max(60_000, max(times) - min(times))
        scale = 86_400_000 / span_ms
        if oid_count * scale >= HFT_EARLY_EXTRAPOLATED_24H_OIDS:
            reason = 'extrapolated_oids_24h_exceeded'
        elif fill_count * scale >= HFT_EARLY_EXTRAPOLATED_24H_FILLS:
            reason = 'extrapolated_fills_24h_exceeded'
    if not reason:
        return None
    return {
        'reason': reason,
        'fills_24h': fill_count,
        'oids_24h': oid_count,
        'active_seconds_24h': active_secs,
        'coins_24h': coins,
        'pagination_complete': bool(pagination_complete),
    }


def leaderboard_candidates(rows: list[dict[str, Any]], per_side: int, now_ms: int):
    """Top/bottom N of each official time window; union by address later."""
    for window in LEADERBOARD_WINDOWS:
        eligible = []
        for row in rows:
            windows = dict(row.get('windowPerformances', []))
            all_time = windows.get('allTime')
            if isinstance(all_time, dict) and all_time.get('vlm') is not None and number(all_time['vlm']) <= 0:
                continue
            data = windows.get(window)
            if not isinstance(data, dict) or data.get('pnl') is None:
                continue
            if data.get('vlm') is not None and number(data['vlm']) <= 0:
                continue
            eligible.append((row, data))
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
                       markets: dict[str, dict[str, Any]], rules: Rules,
                       *, account_qualified: bool | None = None) -> list[str]:
    reasons = set()
    for source in sources:
        kind = source.get('kind')
        if kind in ('leaderboard', 'asset_leaderboard') and account_qualified is False:
            continue
        if kind in ('leaderboard', 'large_observed_trade', 'manual', 'asset_leaderboard'):
            reasons.add(kind)
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
