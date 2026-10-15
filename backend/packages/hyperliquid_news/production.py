"""Persisted account profit / total-loss rollout boundary, shared by every dispatcher."""
from __future__ import annotations

from typing import Any

from .models import Rules, number


def production_allowed(event: dict[str, Any], policy: dict[str, Any] | None) -> bool:
    if policy is None:
        return True  # Offline/legacy limited test flow before production activation.
    if not policy.get('enabled'):
        return False
    evidence = event.get('evidence', {})
    observed = int(event['observed_ms'])
    direction = evidence.get('production_direction')
    loss = direction == 'historical_loss'
    if direction not in policy.get('enabled_scenarios', ['account_profit']):
        return False
    started = int(policy.get('loss_started_ms', policy['started_ms'])) if loss else int(policy['started_ms'])
    if (event.get('key', '').startswith('test:') or evidence.get('test_replay') or
            observed < started or
            direction not in ('account_profit', 'historical_loss') or
            set(event.get('kinds', [])) != {'highlighted_address_action'} or
            set(evidence.get('highlights', [])) != ({'historical_loss'} if loss else {'official_account_high_profit'})):
        return False
    performance = evidence.get('history', {}).get('official_account_loss' if loss else 'official_account_performance', {})
    fetched = int(performance.get('fetched_ms', 0))
    if not 0 <= observed-fetched <= 3600000:
        return False
    if loss:
        if number(evidence.get('net_action_notional_usd', 0)) < Rules().action:
            return False
        data = performance.get('windows', {}).get('allTime', {})
        return (number(data.get('pnl', 0)) <= -Rules().historical_loss and
                0 <= observed-int(data.get('observed_ms', 0)) <= 86400000)
    return any(number(data['pnl']) >= Rules().account_profit and
               0 <= observed-int(data['observed_ms']) <= 86400000
               for data in performance.get('windows', {}).values())
