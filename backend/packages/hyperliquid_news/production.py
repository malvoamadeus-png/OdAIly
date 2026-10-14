"""Persisted single-direction rollout boundary, shared by every dispatcher."""
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
    if (event.get('key', '').startswith('test:') or evidence.get('test_replay') or
            observed < int(policy['started_ms']) or
            evidence.get('production_direction') != 'account_profit' or
            set(event.get('kinds', [])) != {'highlighted_address_action'} or
            set(evidence.get('highlights', [])) != {'official_account_high_profit'}):
        return False
    performance = evidence.get('history', {}).get('official_account_performance', {})
    fetched = int(performance.get('fetched_ms', 0))
    if not 0 <= observed-fetched <= 3600000:
        return False
    return any(number(data['pnl']) >= Rules().account_profit and
               0 <= observed-int(data['observed_ms']) <= 86400000
               for data in performance.get('windows', {}).values())
