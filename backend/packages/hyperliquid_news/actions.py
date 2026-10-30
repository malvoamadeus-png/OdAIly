"""Net exposure and verified execution order shared by selection and writing."""
from typing import Any

from .models import ZERO, number


def snapshot_net_change(evidence: dict[str, Any]) -> dict[str, Any] | None:
    previous = evidence.get('previous_position') or {}
    current = evidence.get('position') or {}
    before = number(previous.get('size', 0))
    after = number(current.get('size', 0))
    reference = current or previous
    price = reference.get('mark')
    if price is None and reference.get('notional') is not None and number(reference.get('size', 0)):
        price = number(reference['notional']) / abs(number(reference['size']))
    if not reference or price is None or number(price) <= 0:
        return None
    reversal = before * after < 0
    return {'direction': '反手' if reversal else '增持' if abs(after) > abs(before) else '减少',
            'side': '多单' if (after or before) > 0 else '空单',
            'previous_side': '多单' if before > 0 else '空单',
            'quantity': str(abs(after-before)),
            'estimated_value': str(abs(after-before)*number(price)),
            'closed': after == ZERO, 'valuation_basis': 'snapshot_reference_price'}


def execution_sequence(fills: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Only emit a chronology when every fill forms one unambiguous position path."""
    from .history import _ordered_by_position
    ordered = _ordered_by_position(fills)
    sequence = []
    expected = None
    for fill in ordered:
        before = number(fill['startPosition'])
        after = before + (number(fill['sz']) if fill['side'] == 'B' else -number(fill['sz']))
        if fill.get('_ambiguous_order') or (expected is not None and before != expected):
            return None
        delta = after-before
        closed = min(abs(before), abs(delta)) if before*delta < 0 else ZERO
        legs = [('close_long' if before > 0 else 'close_short', closed),
                ('open_long' if delta > 0 else 'open_short', abs(delta)-closed)]
        for action, quantity in legs:
            if not quantity:
                continue
            if not sequence or sequence[-1]['action'] != action:
                sequence.append({'action': action, 'first_fill_ms': int(fill['time']),
                                 'last_fill_ms': int(fill['time']), 'quantity': ZERO, 'notional': ZERO})
            leg = sequence[-1]
            leg['last_fill_ms'] = int(fill['time'])
            leg['quantity'] += quantity
            leg['notional'] += quantity*number(fill['px'])
        expected = after
    return sequence


def single_reversal_legs(evidence: dict[str, Any]) -> tuple[Any, Any] | None:
    """Window totals prove one reversal only when its complete path has two legs."""
    old = evidence.get('previous_position') or {}
    current = evidence.get('position') or {}
    before, after = number(old.get('size', 0)), number(current.get('size', 0))
    sequence = (evidence.get('actions') or {}).get('execution_sequence') or []
    expected = ['close_long' if before > 0 else 'close_short',
                'open_long' if after > 0 else 'open_short']
    if (before*after < 0 and [leg['action'] for leg in sequence] == expected and
            number(sequence[0]['quantity']) == abs(before) and
            number(sequence[1]['quantity']) == abs(after)):
        return number(sequence[0]['notional']), number(sequence[1]['notional'])
    return None


def action_scenario_allowed(scenario: str, evidence: dict[str, Any]) -> bool:
    """Recheck operation scenarios without suppressing realized or liquidation facts."""
    from .models import Rules
    rule = {'asset_win_rate': 'win_rate_action', 'asset_profit': 'asset_profit_action',
            'account_profit': 'action', 'historical_loss': 'action', 'loss_addition': 'action'}.get(scenario)
    if not rule:
        return True
    net = snapshot_net_change(evidence)
    threshold = number(evidence.get('threshold_rules', {}).get(rule, getattr(Rules(), rule)))
    if not net or number(net['estimated_value']) < threshold:
        return False
    if scenario == 'loss_addition':
        old = evidence.get('previous_position') or {}
        current = evidence.get('position') or {}
        before, after = number(old.get('size', 0)), number(current.get('size', 0))
        return (before*after > 0 and abs(after) > abs(before) and number(old.get('unrealized_pnl', 0)) < 0
                and not (evidence.get('actions') or {}).get('position_episode_closed'))
    return True
