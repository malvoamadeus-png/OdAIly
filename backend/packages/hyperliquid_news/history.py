from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from itertools import groupby
from typing import Any

from .models import ZERO, number


def fill_key(fill: dict[str, Any]) -> str:
    if fill.get('tid') is None or not fill.get('coin') or fill.get('time') is None:
        raise ValueError('Fill requires coin, time and tid')
    return f"{fill['time']}:{fill['coin']}:{fill['tid']}"


def perp_fill(fill: dict[str, Any]) -> bool:
    coin = str(fill.get('coin', ''))
    return bool(coin) and not coin.startswith('@') and '/' not in coin


def _ordered_by_position(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # tid is a hash, not a sequence. Reconstruct same-time paths from position
    # continuity. Ambiguous paths contribute PnL, but never win-rate rounds.
    ordered = []
    expected = None
    for _, group in groupby(sorted(items, key=lambda f: int(f['time'])), key=lambda f: int(f['time'])):
        remaining = list(group)
        if len(remaining) == 1:
            f = remaining[0]
            expected = number(f['startPosition']) + (number(f['sz']) if f['side'] == 'B' else -number(f['sz']))
            ordered.append(f)
            continue
        bucket = []
        cursor = expected
        if cursor is None or not any(number(f['startPosition']) == cursor for f in remaining):
            zero_starts = [f for f in remaining if number(f['startPosition']) == 0]
            if len(zero_starts) == 1:
                cursor = ZERO
            else:
                afters = {number(f['startPosition']) + (number(f['sz']) if f['side'] == 'B' else -number(f['sz'])) for f in remaining}
                sources = {number(f['startPosition']) for f in remaining} - afters
                cursor = next(iter(sources)) if len(sources) == 1 else None
        ambiguous = False
        while remaining:
            matches = [f for f in remaining if cursor is not None and number(f['startPosition']) == cursor]
            if len(matches) != 1:
                ambiguous = True
                bucket.extend(remaining)
                break
            f = matches[0]
            bucket.append(f)
            remaining.remove(f)
            cursor += number(f['sz']) if f['side'] == 'B' else -number(f['sz'])
        ordered.extend([{**f, '_ambiguous_order': True} for f in bucket] if ambiguous else bucket)
        expected = None if ambiguous else cursor
    return ordered


def summarize(fills: list[dict[str, Any]], *, start_ms: int, end_ms: int,
              coverage: str = 'retained_api_history') -> dict[str, Any]:
    """Round = flat -> exposure -> flat/reversal; incomplete starts excluded.

    Closed PnL minus USD stablecoin fees, excluding funding. Not portfolio PnL.
    """
    unique = {fill_key(f): f for f in fills if perp_fill(f) and start_ms <= int(f['time']) <= end_ms}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for fill in unique.values():
        grouped[str(fill['coin'])].append(fill)
    assets: dict[str, Any] = {}
    for coin, items in grouped.items():
        items = _ordered_by_position(items)
        total = ZERO
        round_pnl = ZERO
        expected = None
        complete = False
        rounds: list[Decimal] = []
        round_details = []
        closing_notional = ZERO
        gaps = 0
        unsupported_fees = False
        ambiguous_fills = 0
        for f in items:
            before = number(f['startPosition'])
            size = number(f['sz'])
            if size <= 0 or f['side'] not in ('B', 'A'):
                raise ValueError('Invalid fill size or side')
            after = before + (size if f['side'] == 'B' else -size)
            closed = number(f.get('closedPnl', '0'))
            fee = number(f.get('fee', '0'))
            if f.get('feeToken', 'USDC') not in ('USDC', 'USDH'):
                unsupported_fees = True
                fee = ZERO
            total += closed - fee
            ambiguous = bool(f.get('_ambiguous_order'))
            if ambiguous:
                ambiguous_fills += 1
                complete = False
                round_pnl = ZERO
                closing_notional = ZERO
            if expected is not None and before != expected:
                gaps += 1
                complete = False
                round_pnl = ZERO
                closing_notional = ZERO
            if before == 0 and not ambiguous:
                complete = True
                round_pnl = ZERO
                closing_notional = ZERO
            delta = size if f['side'] == 'B' else -size
            close_size = min(abs(before), size) if before * delta < 0 else ZERO
            closing_notional += close_size * number(f['px'])
            reverse = before * after < 0
            if reverse:
                closing_fee = fee * abs(before) / size
                round_pnl += closed - closing_fee
                if complete:
                    rounds.append(round_pnl)
                    round_details.append({'pnl': str(round_pnl), 'close_notional': str(closing_notional), 'end_ms': int(f['time'])})
                closing_notional = ZERO
                round_pnl = -(fee - closing_fee)
                complete = not ambiguous
            else:
                round_pnl += closed - fee
                if after == 0:
                    if complete:
                        rounds.append(round_pnl)
                        round_details.append({'pnl': str(round_pnl), 'close_notional': str(closing_notional), 'end_ms': int(f['time'])})
                    closing_notional = ZERO
                    complete = False
                    round_pnl = ZERO
            expected = after
        wins = sum(p > 0 for p in rounds)
        streak = 0
        for pnl in reversed(rounds):
            if pnl >= 0:
                break
            streak += 1
        assets[coin] = {
            'closed_pnl_less_fees': str(total), 'rounds': len(rounds), 'wins': wins,
            'win_rate': str(Decimal(wins) / len(rounds)) if rounds else None,
            'completed_rounds': round_details, 'completed_round_pnl': [str(p) for p in rounds], 'consecutive_losing_rounds': streak,
            'ambiguous_order_fills': ambiguous_fills, 'position_discontinuities': gaps, 'unsupported_fee_currency': unsupported_fees,
        }
    return {
        'start_ms': start_ms, 'end_ms': end_ms, 'coverage': coverage,
        'fills': len(unique), 'first_observed_fill_ms': min((int(f['time']) for f in unique.values()), default=None),
        'last_observed_fill_ms': max((int(f['time']) for f in unique.values()), default=None), 'assets': assets,
        'account_closed_pnl_less_fees': str(sum((number(a['closed_pnl_less_fees']) for a in assets.values()), ZERO)),
        'pnl_scope': 'observed_perp_fills_excluding_funding',
        'fee_currency_complete': not any(a['unsupported_fee_currency'] for a in assets.values()),
    }
