"""Structural change evidence, distinct from news-value judging and deduplication."""
from __future__ import annotations

from typing import Any

from .models import number


def news_eligibility(event: dict[str, Any]) -> dict[str, Any]:
    kinds = set(event['kinds'])
    e = event['evidence']
    if 'address_divergence' in kinds:
        return {'eligible': True, 'reason': 'static_divergence_exception'}
    if e.get('news_change'):
        return {'eligible': True, 'reason': 'observed_change'}
    previous, current = e.get('previous_position'), e.get('position')
    if previous and current and not e.get('first_observation'):
        size_before, size_now = number(previous['size']), number(current['size'])
        amount = number(e.get('threshold_rules', {}).get('action', '3000000'))
        if kinds & {'large_position', 'oi_share', 'market_cap_share'} and abs(abs(size_now)-abs(size_before))*number(current['mark']) >= amount:
            return {'eligible': True, 'reason': 'legacy_important_position_change'}
        if kinds & {'large_profit', 'large_loss', 'profit_giveback', 'profit_to_loss', 'pnl_sign_flip'} and number(previous['unrealized_pnl']) != number(current['unrealized_pnl']):
            return {'eligible': True, 'reason': 'legacy_floating_pnl_change'}
        if 'large_reversal' in kinds and size_before*size_now < 0:
            return {'eligible': True, 'reason': 'legacy_reversal'}
        if 'near_liquidation' in kinds and current.get('liquidation') is not None and previous.get('liquidation') is not None:
            old_distance = abs(number(previous['mark'])-number(previous['liquidation']))/number(previous['mark'])
            new_distance = abs(number(current['mark'])-number(current['liquidation']))/number(current['mark'])
            if new_distance < old_distance:
                return {'eligible': True, 'reason': 'legacy_liquidation_distance_change'}
    action_kinds = {'highlighted_address_action', 'large_position_action', 'loss_addition',
                    'large_realized_result', 'actual_liquidation', 'newly_observed_address_position'}
    if e.get('actions') and kinds & action_kinds:
        return {'eligible': True, 'reason': 'explicit_action'}
    return {'eligible': False, 'reason': 'baseline_without_change'}
