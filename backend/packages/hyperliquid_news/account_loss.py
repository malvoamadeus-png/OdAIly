"""Account total loss: exact negative counterpart of official account profit."""
from __future__ import annotations

from typing import Any
from .models import Rules, number

DAY = 86400000


def loss_qualified(performance: dict[str, Any], rules: Rules) -> bool:
    return number(performance.get('windows', {}).get('allTime', {}).get('pnl', 0)) <= -rules.historical_loss


def loss_performance(payload: list, *, cutoff_ms: int, rules: Rules | None = None) -> dict[str, Any]:
    windows = {}
    for name, data in payload:
        if name != 'perpAllTime':
            continue
        points = [p for p in data.get('pnlHistory', []) if int(p[0]) <= cutoff_ms]
        if not points:
            continue
        stamp, pnl = max(points, key=lambda p: int(p[0]))
        if cutoff_ms - int(stamp) <= DAY:
            windows['allTime'] = {'pnl': str(number(pnl)), 'observed_ms': int(stamp)}
    return {'source': 'hyperliquid_perpetual_portfolio', 'cutoff_ms': cutoff_ms,
            'windows': windows, 'pnl_scope': 'official_account_including_unrealized'}


def replay_loss_actions(address: str, fills: list[dict], portfolio: list, *, rules: Rules | None = None) -> list[dict]:
    """One-hour real/synthetic action windows, independently qualified before each.

    No task, database, active prompt or publication side effects. Historical
    quantities use execution prices for valuation; floating PnL is omitted.
    """
    from dataclasses import asdict
    from .detector import evaluate_address
    from .history import _ordered_by_position, fill_key
    from .models import Position, ZERO
    from .test_cases import valid_test_fill
    rules = rules or Rules()
    groups = {}
    for f in {fill_key(f): f for f in fills if valid_test_fill(f)}.values():
        groups.setdefault(f['coin'], []).append(f)
    result = []
    for coin, raw in groups.items():
        batches = []
        batch = []
        expected = None
        for f in _ordered_by_position(raw):
            before = number(f['startPosition'])
            if batch and (int(f['time']) - int(batch[0]['time']) > 3600000 or
                          before != expected or f.get('_ambiguous_order')):
                batches.append(batch)
                batch = []
            if f.get('_ambiguous_order'):
                expected = None
                continue
            batch.append(f)
            expected = before + (number(f['sz']) if f['side'] == 'B' else -number(f['sz']))
        if batch:
            batches.append(batch)
        for batch in batches:
            start, end = int(batch[0]['time']), int(batch[-1]['time'])
            performance = loss_performance(portfolio, cutoff_ms=start-1, rules=rules)
            if not loss_qualified(performance, rules):
                continue
            before = number(batch[0]['startPosition'])
            last = batch[-1]
            after = number(last['startPosition']) + (number(last['sz']) if last['side'] == 'B' else -number(last['sz']))
            px = number(last['px'])
            # Opposing fills cancel in quantity at one common price. Gross
            # turnover and repricing unchanged exposure cannot qualify an action.
            net_action = abs(after - before) * px
            if net_action < rules.action:
                continue
            def position(size):
                return Position(coin, size, abs(size)*px, px, ZERO, None) if size else None
            old, current = position(before), position(after)
            events, _ = evaluate_address(address=address, observed_ms=end, first_seen_ms=0,
                positions={coin: current} if current else {},
                previous={'observed_ms': start-1, 'positions': {coin: old.facts()} if old else {}},
                markets={}, history={'official_account_loss': performance}, new_fills=batch, rules=rules)
            for event in events:
                if 'highlighted_address_action' not in event.kinds:
                    continue
                item = asdict(event)
                item['key'] = 'test-account-loss:' + item['key']
                item['kinds'] = ['highlighted_address_action']
                evidence = item['evidence']
                evidence['highlights'] = ['historical_loss']
                evidence['net_action_notional_usd'] = str(net_action)
                evidence['test_replay'] = {'mode': 'account_loss_action_replay', 'attribute_cutoff_ms': start-1,
                    'purpose': 'prelaunch_case_review_only', 'valuation_basis': 'window_last_execution_price'}
                for field in ('position', 'previous_position'):
                    if evidence.get(field):
                        evidence[field]['unrealized_pnl'] = None
                        evidence[field]['mark'] = None
                result.append(item)
    return result
