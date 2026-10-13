from __future__ import annotations

import hashlib
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .history import fill_key, perp_fill
from .models import Candidate, Position, Rules, ZERO, number


def highlights(history: dict[str, Any], coin: str, rules: Rules) -> list[str]:
    asset = history.get('assets', {}).get(coin, {})
    labels = []
    if (asset.get('rounds', 0) >= rules.min_rounds and not asset.get('unsupported_fee_currency')
            and asset.get('win_rate') is not None and number(asset['win_rate']) >= rules.win_rate):
        labels.append('asset_high_win_rate')
    if not asset.get('unsupported_fee_currency'):
        pnl = number(asset.get('closed_pnl_less_fees', '0'))
        if pnl >= rules.asset_profit:
            labels.append('asset_high_profit')
        if pnl <= -rules.historical_loss or asset.get('consecutive_losing_rounds', 0) >= rules.losing_rounds:
            labels.append('historical_loss')
    if (history.get('fee_currency_complete', False) and history.get('account_closed_pnl_less_fees') is not None
            and number(history['account_closed_pnl_less_fees']) >= rules.account_profit):
        labels.append('account_high_profit')
    official = history.get('official_account_performance', {})
    if any(number(data['pnl']) >= rules.account_profit for data in official.get('windows', {}).values()):
        labels.append('official_account_high_profit')
    return labels


def level(value: Decimal, threshold: Decimal, rules: Rules) -> int:
    if value < threshold:
        return 0
    result = 1
    while value >= threshold * rules.escalation:
        threshold *= rules.escalation
        result += 1
    return result


def evaluate_address(*, address: str, observed_ms: int, first_seen_ms: int,
                     positions: dict[str, Position], previous: dict[str, Any] | None,
                     markets: dict[str, dict[str, Any]], history: dict[str, Any],
                     new_fills: list[dict[str, Any]], rules: Rules) -> tuple[list[Candidate], dict[str, Any]]:
    old_positions = {coin: Position.restore(p) for coin, p in (previous or {}).get('positions', {}).items()}
    old_signals = (previous or {}).get('signals', {})
    old_peaks = (previous or {}).get('peaks', {})
    signals: dict[str, dict[str, int]] = {}
    peaks: dict[str, str] = {}
    peak_times = {}
    episodes = {}
    actions: dict[str, dict[str, Any]] = {}
    for fill in new_fills:
        if not perp_fill(fill):
            continue
        coin = str(fill['coin'])
        data = actions.setdefault(coin, {'open_notional': ZERO, 'close_notional': ZERO,
                                          'closed_pnl': ZERO, 'liquidated_notional': ZERO, 'fill_keys': []})
        before = number(fill['startPosition'])
        size = number(fill['sz'])
        price = number(fill['px'])
        if size <= 0 or price <= 0 or fill['side'] not in ('B', 'A'):
            raise ValueError('Invalid fill action')
        delta = size if fill['side'] == 'B' else -size
        closing_size = min(abs(before), size) if before * delta < 0 else ZERO
        data['close_notional'] += closing_size * price
        data['open_notional'] += (size - closing_size) * price
        data['closed_pnl'] += number(fill.get('closedPnl', '0'))
        liquidation = fill.get('liquidation')
        if isinstance(liquidation, dict) and str(liquidation.get('liquidatedUser', '')).lower() == address.lower():
            data['liquidated_notional'] += size * price
        data['fill_keys'].append(fill_key(fill))
    candidates = []
    for coin in sorted(set(positions) | set(old_positions) | set(actions)):
        p = positions.get(coin)
        old = old_positions.get(coin)
        labels = highlights(history, coin, rules)
        kinds: list[str] = []
        facts: dict[str, Any] = {'position': p.facts() if p else None,
                                  'previous_position': old.facts() if old else None,
                                  'history': history, 'highlights': labels,
                                  'first_observation': previous is None, 'threshold_rules': asdict(rules)}
        current: dict[str, int] = {}
        same_episode = p is not None and old is not None and p.size * old.size > 0
        peak = max(number(old_peaks.get(coin, '0')) if same_episode else ZERO,
                   p.unrealized_pnl if p else ZERO)
        if p:
            episode = (previous or {}).get('episodes', {}).get(coin, observed_ms) if same_episode else observed_ms
            episodes[coin] = episode
            facts['episode_started_ms'] = episode
            old_peak = number(old_peaks.get(coin, '0')) if same_episode else ZERO
            peak_time = (previous or {}).get('peak_times', {}).get(coin, observed_ms) if same_episode and old_peak >= p.unrealized_pnl else observed_ms
            peak_times[coin] = peak_time
            peaks[coin] = str(peak)
            market = markets.get(coin, {})
            oi = number(market.get('oi_notional', '0'))
            cap = number(market.get('market_cap', '0'))
            facts['market'] = market
            ratios = {'oi_share': p.notional / oi if oi > 0 else None,
                      'market_cap_share': p.notional / cap if cap > 0 else None}
            facts['ratios'] = {k: str(v) if v is not None else None for k, v in ratios.items()}
            metrics = {
                'large_position': (p.notional, rules.large_position),
                'oi_share': (ratios['oi_share'] or ZERO, rules.oi_share),
                'market_cap_share': (ratios['market_cap_share'] or ZERO, rules.market_cap_share),
                'large_profit': (p.unrealized_pnl, rules.pnl_line(coin)),
                'large_loss': (-p.unrealized_pnl, rules.pnl_line(coin)),
            }
            for kind, (value, threshold) in metrics.items():
                current[kind] = level(value, threshold, rules)
                if current[kind] > (old_signals.get(coin, {}).get(kind, 0) if same_episode else 0):
                    kinds.append(kind)
            # Liquidation distance is an observation, not a prediction.
            if p.liquidation is not None and p.liquidation > 0:
                distance = abs(p.mark - p.liquidation) / p.mark
                facts['liquidation_distance'] = str(distance)
                current['near_liquidation'] = int(p.notional >= rules.action and distance <= rules.liquidation_distance)
                if current['near_liquidation'] > old_signals.get(coin, {}).get('near_liquidation', 0):
                    kinds.append('near_liquidation')
            if same_episode and peak >= rules.pnl_line(coin):
                giveback = peak - p.unrealized_pnl
                facts['episode_peak_unrealized_pnl'] = str(peak)
                facts['episode_peak_ms'] = peak_time
                current['profit_giveback'] = level(giveback, peak * rules.giveback_fraction, rules)
                current['profit_to_loss'] = int(p.unrealized_pnl < 0)
                for kind in ('profit_giveback', 'profit_to_loss'):
                    if current[kind] > (old_signals.get(coin, {}).get(kind, 0) if same_episode else 0):
                        kinds.append(kind)
            if previous is not None and old is not None and p.size * old.size < 0 and min(old.notional, p.notional) >= rules.action:
                kinds.append('large_reversal')
                facts['reversal_basis'] = 'net_direction_between_successful_snapshots'
        action = actions.get(coin)
        if action:
            if max(p.notional if p else ZERO, old.notional if old else ZERO) >= rules.large_position and max(action['open_notional'], action['close_notional']) >= rules.action:
                kinds.append('large_position_action')
            facts['actions'] = {k: str(v) if isinstance(v, Decimal) else v for k, v in action.items()}
            if labels and max(action['open_notional'], action['close_notional']) >= rules.action:
                kinds.append('highlighted_address_action')
            if action['close_notional'] >= rules.action and abs(action['closed_pnl']) >= rules.pnl_line(coin):
                kinds.append('large_realized_result')
            if action['liquidated_notional'] >= rules.action:
                kinds.append('actual_liquidation')
            if (old is not None and p is not None and same_episode and old.unrealized_pnl <= -rules.pnl_line(coin)
                    and action['open_notional'] >= rules.action and abs(p.size) > abs(old.size)):
                kinds.append('loss_addition')
            if (previous is not None and old is None and p is not None and p.notional >= rules.action
                    and action['open_notional'] >= rules.action and observed_ms - first_seen_ms <= rules.new_address_window_ms):
                kinds.append('newly_observed_address_position')
        if previous is not None:
            completed = [r for r in history.get('assets', {}).get(coin, {}).get('completed_rounds', [])
                         if int(r['end_ms']) in {int(f['time']) for f in new_fills if f['coin'] == coin} and
                         number(r['close_notional']) >= rules.action and abs(number(r['pnl'])) >= rules.pnl_line(coin)]
            if completed and action:
                kinds.append('large_realized_result')
                facts['completed_round_results'] = completed
        # Magnitude qualifies a change; discovering an old state is not a change.
        magnitude_kinds = {'large_position', 'oi_share', 'market_cap_share', 'large_profit', 'large_loss', 'near_liquidation'}
        if previous is None or (p is not None and old is None):
            kinds = [k for k in kinds if k not in magnitude_kinds]
        elif p is not None and old is not None:
            important_size_change = abs(abs(p.size) - abs(old.size)) * p.mark >= rules.action
            if not important_size_change:
                kinds = [k for k in kinds if k not in {'large_position', 'oi_share', 'market_cap_share'}]
            if p.unrealized_pnl == old.unrealized_pnl:
                kinds = [k for k in kinds if k not in {'large_profit', 'large_loss'}]
        if kinds and previous is not None:
            facts['news_change'] = {
                'previous_observed_ms': previous['observed_ms'],
                'observed_ms': observed_ms,
                'previous_floating_pnl': str(old.unrealized_pnl) if old else None,
                'previous_position_value': str(old.notional) if old else None,
                'previous_size': str(old.size) if old else None,
                'current_size': str(p.size) if p else '0',
            }
        facts['trigger_levels'] = current
        if not p and old:
            facts['episode_started_ms'] = (previous or {}).get('episodes', {}).get(coin, (previous or {}).get('observed_ms'))
        signals[coin] = current
        if kinds:
            kinds = sorted(set(kinds))
            identity = f'{address}|{coin}|{observed_ms}'
            candidates.append(Candidate(hashlib.sha256(identity.encode()).hexdigest(), observed_ms,
                                        (address,), coin, tuple(kinds), facts))
    state = {'positions': {coin: p.facts() for coin, p in positions.items()},
             'signals': signals, 'peaks': peaks, 'peak_times': peak_times, 'episodes': episodes, 'observed_ms': observed_ms}
    return candidates, state


def evaluate_divergence(*, observed_ms: int, states: dict[str, dict[str, Any]],
                        histories: dict[str, dict[str, Any]], previous: dict[str, Any],
                        rules: Rules) -> tuple[list[Candidate], dict[str, Any]]:
    sides: dict[str, dict[str, list[str]]] = {}
    for address, state in states.items():
        for coin, raw in state['positions'].items():
            p = Position.restore(raw)
            if p.notional >= rules.action and highlights(histories.get(address, {}), coin, rules):
                side = 'long' if p.size > 0 else 'short'
                sides.setdefault(coin, {'long': [], 'short': []})[side].append(address)
    candidates = []
    active = {}
    for coin, group in sides.items():
        if not group['long'] or not group['short']:
            continue
        group = {k: sorted(v) for k, v in group.items()}
        sizes = {a: level(number(states[a]['positions'][coin]['notional']), rules.action, rules)
                 for a in group['long'] + group['short']}
        fingerprint = repr((group, sizes))
        active[coin] = {'fingerprint': fingerprint, 'addresses': sorted(sizes)}
        if previous.get(coin, {}).get('fingerprint') == fingerprint:
            continue
        addresses = tuple(sorted(group['long'] + group['short']))
        key = hashlib.sha256(f'divergence|{coin}|{observed_ms}|{fingerprint}'.encode()).hexdigest()
        candidates.append(Candidate(key, observed_ms, addresses, coin, ('address_divergence',),
                                    {'threshold_rules': asdict(rules), 'sides': group, 'positions': {a: states[a]['positions'][coin] for a in addresses},
                                     'histories': {a: histories[a] for a in addresses}}))
    return candidates, active
