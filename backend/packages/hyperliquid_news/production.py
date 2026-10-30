"""Persisted account profit / total-loss rollout boundary, shared by every dispatcher."""
from __future__ import annotations

from typing import Any

from .models import Rules, number
from .scenarios import PRODUCTION_SCENARIOS, event_scenarios
from .actions import snapshot_net_change, action_scenario_allowed


def production_allowed(event: dict[str, Any], policy: dict[str, Any] | None) -> bool:
    if policy is None:
        return True  # Offline/legacy limited test flow before production activation.
    if not policy.get('enabled'):
        return False
    evidence = event.get('evidence', {})
    if evidence.get('history', {}).get('api_coverage', {}).get('pagination_complete') is False:
        return False
    observed = int(event['observed_ms'])
    direction = evidence.get('production_direction')
    loss = direction == 'historical_loss'
    enabled = set(policy.get('enabled_scenarios', ['account_profit']))
    if direction not in enabled and direction is not None:
        return False
    if event.get('key', '').startswith('test:') or evidence.get('test_replay'):
        return False
    if direction not in ('account_profit', 'historical_loss'):
        normal_enabled = enabled.intersection(PRODUCTION_SCENARIOS) - {'account_profit', 'historical_loss'}
        starts = policy.get('scenario_started_ms_by_direction', {})
        for scenario in normal_enabled.intersection(event_scenarios(event)):
            if not action_scenario_allowed(scenario, evidence):
                continue
            started = int(starts.get(scenario, policy.get('scenario_started_ms', policy['started_ms'])))
            if observed < started:
                continue
            # Newly approved directions require a successful live baseline after activation.
            if scenario in starts and int(evidence.get('previous_snapshot_ms', 0)) < started:
                continue
            return True
        return False
    started = int(policy.get('loss_started_ms', policy['started_ms'])) if loss else int(policy['started_ms'])
    if (observed < started or
            direction not in ('account_profit', 'historical_loss') or
            set(event.get('kinds', [])) != {'highlighted_address_action'} or
            set(evidence.get('highlights', [])) != ({'historical_loss'} if loss else {'official_account_high_profit'})):
        return False
    performance = evidence.get('history', {}).get('official_account_loss' if loss else 'official_account_performance', {})
    fetched = int(performance.get('fetched_ms', 0))
    if not 0 <= observed-fetched <= 3600000:
        return False
    change = snapshot_net_change(evidence)
    if not change or number(change['estimated_value']) < Rules().action:
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
