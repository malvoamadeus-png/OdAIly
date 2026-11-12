"""Incremental position episodes; callers must supply unique, ordered fills."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import ZERO, number


def _new_episode(coin: str, before: Decimal, stamp: int, direction: int) -> dict[str, Any]:
    return {
        'coin': coin, 'direction': 'long' if direction > 0 else 'short',
        'start_ms': stamp, 'end_ms': None, 'last_fill_ms': stamp,
        'opening_quantity': '0', 'opening_notional': '0',
        'closing_quantity': '0', 'closing_notional': '0',
        'closed_pnl': '0', 'fees_usd': '0', 'fill_count': 0,
        'complete_start': before == 0, 'continuous': True,
        'unsupported_fee_currency': False, 'position': str(before),
    }


def _add(episode: dict[str, Any], key: str, value: Decimal) -> None:
    episode[key] = str(number(episode[key]) + value)


def advance_episode(current: dict[str, Any] | None, fill: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Apply one ordered fill. A reversal finishes one episode and starts another.

    A missing start or position gap is retained as incomplete; it must not be
    counted as a complete win-rate sample. Deduplication and atomic persistence
    are the caller's responsibility.
    """
    before = number(fill['startPosition'])
    size = number(fill['sz'])
    price = number(fill['px'])
    side = fill['side']
    stamp = int(fill['time'])
    coin = str(fill['coin'])
    if not coin or size <= 0 or price <= 0 or side not in ('B', 'A'):
        raise ValueError('Invalid fill')
    if fill.get('_ambiguous_order'):
        raise ValueError('Ambiguous fill order')
    if current and (current['coin'] != coin or stamp < int(current['last_fill_ms'])):
        raise ValueError('Fill does not follow current episode')
    current = dict(current) if current else None
    completed = []
    if current and before != number(current['position']):
        current['continuous'] = False
        current['end_ms'] = None
        completed.append(current)
        current = None
    delta = size if side == 'B' else -size
    after = before + delta
    closing = min(abs(before), size) if before * delta < 0 else ZERO
    opening = size - closing
    fee_supported = fill.get('feeToken', 'USDC') in ('USDC', 'USDH')
    fee = number(fill.get('fee', '0')) if fee_supported else ZERO
    closing_fee = fee * closing / size
    opening_fee = fee - closing_fee
    pnl = number(fill.get('closedPnl', '0'))

    if closing:
        current = current or _new_episode(coin, before, stamp, 1 if before > 0 else -1)
        _add(current, 'closing_quantity', closing)
        _add(current, 'closing_notional', closing * price)
        _add(current, 'closed_pnl', pnl)
        _add(current, 'fees_usd', closing_fee)
        current['unsupported_fee_currency'] |= not fee_supported
        current['last_fill_ms'] = stamp
        current['fill_count'] += 1
        current['position'] = str(before + (closing if delta > 0 else -closing))
        if number(current['position']) == 0:
            current['end_ms'] = stamp
            completed.append(current)
            current = None
    if opening:
        current = current or _new_episode(coin, ZERO if closing else before, stamp, 1 if delta > 0 else -1)
        _add(current, 'opening_quantity', opening)
        _add(current, 'opening_notional', opening * price)
        _add(current, 'fees_usd', opening_fee)
        current['unsupported_fee_currency'] |= not fee_supported
        current['last_fill_ms'] = stamp
        current['fill_count'] += 1
        current['position'] = str(after)
    return current, completed
