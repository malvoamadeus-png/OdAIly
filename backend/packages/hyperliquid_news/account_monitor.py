"""Shared account profit / total-loss cohort; one scheduler and request budget."""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from .client import NewsClient
from .detector import evaluate_address
from .discovery import LEADERBOARD_WINDOWS, trade_candidates
from .history import fill_key
from .models import Rules, Position, number
from .store import NewsStore, encode

HOUR = 3600000
DAY = 24 * HOUR


def _log(event: str, **fields: Any) -> None:
    print(encode({'event': event, **fields}), flush=True)


def qualifying_leaderboard(rows: list[dict[str, Any]], rules: Rules, now_ms: int, *, include_loss: bool = False):
    """All returned qualifying rows, without the test seed top-N truncation."""
    for row in rows:
        windows = dict(row.get('windowPerformances', []))
        for window in LEADERBOARD_WINDOWS:
            data = windows.get(window)
            if isinstance(data, dict) and (number(data.get('pnl', '0')) >= rules.account_profit or
                    (include_loss and window == 'allTime' and number(data.get('pnl', '0')) <= -rules.historical_loss)):
                yield row['ethAddress'], {'kind': 'leaderboard', 'window': window,
                    'side': 'loss' if number(data['pnl']) < 0 else 'profit', 'rank': None, 'pnl': str(number(data['pnl'])),
                    'observed_ms': now_ms, 'pnl_scope': 'official_leaderboard_not_assumed_realized'}


def portfolio_performance(payload: Any, now_ms: int) -> dict[str, Any]:
    if not isinstance(payload, list):
        raise ValueError('Unexpected portfolio payload')
    names = {'perpDay': 'day', 'perpWeek': 'week', 'perpMonth': 'month', 'perpAllTime': 'allTime'}
    windows = {}
    for name, data in payload:
        if name not in names:
            continue
        points = data.get('pnlHistory', [])
        if not points:
            continue
        stamp, pnl = max(points, key=lambda p: int(p[0]))
        stamp = int(stamp)
        if stamp > now_ms or now_ms - stamp > DAY:
            continue
        windows[names[name]] = {'pnl': str(number(pnl)), 'observed_ms': stamp,
            'history_sample_ms': stamp, 'rank': None, 'scope': 'official_portfolio_window_change'}
    if not windows:
        raise ValueError('No fresh perpetual portfolio performance')
    return {'source': 'hyperliquid_portfolio', 'fetched_ms': now_ms, 'windows': windows,
            'pnl_scope': 'official_portfolio_including_unrealized'}


def portfolio_all_time_at(payload: Any, cutoff_ms: int) -> dict[str, Any] | None:
    if not isinstance(payload, list):
        raise ValueError('Unexpected portfolio payload')
    for name, data in payload:
        if name != 'perpAllTime':
            continue
        points = [point for point in data.get('pnlHistory', []) if int(point[0]) <= cutoff_ms]
        if not points:
            return None
        stamp, pnl = max(points, key=lambda point: int(point[0]))
        return {'source': 'hyperliquid_portfolio', 'pnl': str(number(pnl)),
                'observed_ms': int(stamp), 'scope': 'perpAllTime_including_unrealized'}
    return None


def qualified(performance: dict[str, Any], rules: Rules, *, include_loss: bool = False) -> bool:
    from .account_loss import loss_qualified
    return (include_loss and loss_qualified(performance, rules)) or any(number(w['pnl']) >= rules.account_profit for w in performance.get('windows', {}).values())


def account_event(event, rules: Rules | None = None, *, include_loss: bool = False):
    """Do not expose other directions' rules or historical labels to writers."""
    rules = rules or Rules()
    labels = event.evidence.get('highlights', [])
    loss = include_loss and 'historical_loss' in labels
    profit = 'official_account_high_profit' in labels
    if 'highlighted_address_action' not in event.kinds or not (loss or profit):
        return None
    if loss:
        old, current = event.evidence.get('previous_position'), event.evidence.get('position')
        before = number(old['size']) if old else number(0)
        after = number(current['size']) if current else number(0)
        reference = current or old
        if not reference or abs(after-before)*number(reference['mark']) < rules.action:
            return None
    evidence = {k: v for k, v in event.evidence.items() if k not in (
        'previous_reported_floating_pnl', 'previous_reported_floating_pnl_ms',
        'episode_peak_unrealized_pnl', 'episode_peak_ms', 'completed_round_results')}
    evidence.update(highlights=['historical_loss' if loss else 'official_account_high_profit'],
                    production_direction='historical_loss' if loss else 'account_profit')
    if loss:
        evidence['net_action_notional_usd'] = str(abs(after-before)*number(reference['mark']))
    return replace(event, kinds=('highlighted_address_action',), evidence=evidence)


class AccountProfitMonitor:
    def __init__(self, store: NewsStore, client: NewsClient, *, addresses_per_pass: int = 10,
                 rules: Rules | None = None) -> None:
        if not 1 <= addresses_per_pass <= 100:
            raise ValueError('Address batch must be 1..100')
        self.store, self.client = store, client
        self.rules = rules or Rules()
        self.addresses_per_pass = addresses_per_pass

    def run_once(self, *, now_ms: int | None = None) -> dict[str, Any]:
        pass_started = time.monotonic()
        fixed_now = now_ms
        def clock_ms():
            return fixed_now if fixed_now is not None else int(time.time() * 1000)
        now_ms = clock_ms()
        self.store.initialize()
        deployment = self.store.metadata('production_account_profit', {})
        if not deployment.get('enabled') or not deployment.get('started_ms'):
            raise ValueError('Enable account-profit production before running the worker')
        started = int(deployment['started_ms'])
        include_loss = 'historical_loss' in deployment.get('enabled_scenarios', [])
        def is_qualified(performance):
            return qualified(performance, self.rules, include_loss=include_loss)
        def event_history(performance, coverage):
            return {'official_account_performance': performance, 'api_coverage': coverage,
                    **({'official_account_loss': performance} if include_loss else {})}
        found = set()
        report = {'observed_ms': now_ms, 'direction': 'account_profit_and_loss' if include_loss else 'account_profit', 'processed': 0,
                  'candidates': 0, 'failed': {}, 'discovered': 0, 'attribute_queries': 0,
                  'coverage': 'qualifying_public_leaderboard_and_sampled_trades'}
        _log('account_monitor.pass_started', observed_ms=now_ms, direction=report['direction'],
             addresses_per_pass=self.addresses_per_pass)
        # Hourly discovery refresh; preserve prior cache and retry on failures.
        if now_ms - self.store.metadata('account_leaderboard_checked_ms', 0) >= HOUR:
            stage_started = time.monotonic()
            _log('account_monitor.stage_started', stage='leaderboard_refresh', observed_ms=clock_ms())
            try:
                rows = self.client.leaderboard()
                before = len(found)
                for address, source in qualifying_leaderboard(rows, self.rules, now_ms, include_loss=include_loss):
                    if not self.store.discover(address, source=source, now_ms=now_ms, limit=None):
                        continue
                    self.store.schedule_account(address, now_ms=now_ms, leaderboard_qualified=True)
                    found.add(address.lower())
                self.store.save_global('account_leaderboard_checked_ms', now_ms, [])
                _log('account_monitor.stage_completed', stage='leaderboard_refresh', row_count=len(rows),
                     discovered_count=len(found) - before, elapsed_ms=int((time.monotonic() - stage_started) * 1000))
            except Exception as exc:
                report['failed']['discovery:leaderboard'] = str(exc)
                _log('account_monitor.stage_failed', stage='leaderboard_refresh', error=str(exc),
                     elapsed_ms=int((time.monotonic() - stage_started) * 1000))
        # One sampled market per five minutes. Discovery is distinct from qualification.
        universe = self.store.metadata('account_market_universe', {})
        if not universe or now_ms - universe.get('observed_ms', 0) >= HOUR:
            stage_started = time.monotonic()
            _log('account_monitor.stage_started', stage='market_universe_refresh', observed_ms=clock_ms())
            try:
                dexs = self.client.dex_names()
                markets = {}
                for dex in dexs:
                    dex_started = time.monotonic()
                    dex_markets = self.client.markets(dex)
                    markets.update(dex_markets)
                    _log('account_monitor.market_catalog_completed', dex=dex, market_count=len(dex_markets),
                         elapsed_ms=int((time.monotonic() - dex_started) * 1000))
                universe = {'observed_ms': now_ms, 'dexs': dexs, 'markets': markets}
                self.store.save_global('account_market_universe', universe, [])
                _log('account_monitor.stage_completed', stage='market_universe_refresh', dex_count=len(dexs),
                     market_count=len(markets), elapsed_ms=int((time.monotonic() - stage_started) * 1000))
            except Exception as exc:
                _log('account_monitor.stage_failed', stage='market_universe_refresh', error=str(exc),
                     elapsed_ms=int((time.monotonic() - stage_started) * 1000))
                raise
        dexs, markets = universe['dexs'], universe['markets']
        if now_ms - self.store.metadata('account_trade_discovery_ms', 0) >= 300000 and markets:
            coins = sorted(markets)
            cursor = self.store.metadata('account_trade_cursor', 0) % len(coins)
            coin = coins[cursor]
            stage_started = time.monotonic()
            _log('account_monitor.stage_started', stage='trade_discovery', coin=coin, observed_ms=clock_ms())
            try:
                before = len(found)
                for address, source in trade_candidates(self.client.recent_trades(coin), coin, self.rules, now_ms):
                    if not self.store.discover(address, source=source, now_ms=now_ms, limit=None):
                        continue
                    self.store.schedule_account(address, now_ms=now_ms)
                    found.add(address.lower())
                self.store.save_global('account_trade_discovery_ms', now_ms, [])
                self.store.save_global('account_trade_cursor', (cursor + 1) % len(coins), [])
                _log('account_monitor.stage_completed', stage='trade_discovery', coin=coin,
                     discovered_count=len(found) - before, elapsed_ms=int((time.monotonic() - stage_started) * 1000))
            except Exception as exc:
                report['failed']['discovery:' + coin] = str(exc)
                _log('account_monitor.stage_failed', stage='trade_discovery', coin=coin, error=str(exc),
                     elapsed_ms=int((time.monotonic() - stage_started) * 1000))
        for row in self.store.account_due(now_ms, self.addresses_per_pass):
            address = row['address']
            if self.store.blocked(address):
                _log('account_monitor.address_skipped', address=address, reason='blocked', observed_ms=clock_ms())
                continue
            # Wall time is required in production; fixed time is for offline tests.
            observed = clock_ms()
            address_started = time.monotonic()
            stage = 'load_baseline'
            _log('account_monitor.address_started', address=address, tier=row.get('tier'),
                 qualified=bool(row.get('qualified')), observed_ms=observed)
            try:
                old = self.store.load(address)
                performance = self.store.metadata('account_performance:' + address, {})
                if not performance or observed - performance.get('fetched_ms', 0) >= HOUR:
                    stage = 'portfolio_qualification'
                    stage_started = time.monotonic()
                    _log('account_monitor.address_stage_started', address=address, stage=stage, observed_ms=clock_ms())
                    performance = portfolio_performance(self.client.portfolio(address), clock_ms())
                    self.store.save_global('account_performance:' + address, performance, [])
                    _log('account_monitor.address_stage_completed', address=address, stage=stage,
                         elapsed_ms=int((time.monotonic() - stage_started) * 1000), qualified=is_qualified(performance))
                if not is_qualified(performance):
                    self.store.update_account_schedule(address, now_ms=observed, tier='unqualified',
                                                       interval_ms=DAY, qualified=False)
                    report['processed'] += 1
                    _log('account_monitor.address_completed', address=address, outcome='unqualified',
                         elapsed_ms=int((time.monotonic() - address_started) * 1000))
                    continue
                positions = {}
                snapshot_times = {}
                stage = 'positions'
                stage_started = time.monotonic()
                _log('account_monitor.address_stage_started', address=address, stage=stage, dex_count=len(dexs), observed_ms=clock_ms())
                for dex in dexs:
                    payload = self.client.positions(address, dex)
                    snapshot_times[dex] = payload.get('time')
                    for item in payload['assetPositions']:
                        raw = dict(item['position'])
                        if number(raw['szi']) == 0:
                            continue
                        if dex and not str(raw['coin']).startswith(dex + ':'):
                            raw['coin'] = dex + ':' + str(raw['coin'])
                        p = Position.from_payload(raw)
                        if p.coin in positions:
                            raise ValueError('Duplicate market position')
                        positions[p.coin] = p
                _log('account_monitor.address_stage_completed', address=address, stage=stage,
                     position_count=len(positions), elapsed_ms=int((time.monotonic() - stage_started) * 1000))
                # No historical statistical backfill: only actions since this deployment.
                # Requalification starts a fresh baseline after an unqualified period.
                from .account_loss import loss_qualified
                loss_only = include_loss and loss_qualified(performance, self.rules)
                direction_started = int(deployment.get('loss_started_ms', started)) if loss_only else started
                baseline = old if (not loss_only or old and old['state'].get('account_monitoring_since_ms', 0) >= direction_started) and row['qualified'] and old and old['state'].get('account_production_started_ms') == started else None
                pending = self.store.metadata('account_pending_fills:' + address, {}) if baseline else {}
                coverage = pending.get('coverage') or (baseline or {}).get('coverage', {})
                history_checked = (baseline or {}).get('state', {}).get('account_history_checked_ms', observed)
                since = max(started, observed - DAY,
                    int(coverage.get('next_start_ms', started)) if not coverage.get('pagination_complete', True)
                    else int(history_checked) - 60000)
                observed = clock_ms()
                previous_positions = (baseline or {}).get('state', {}).get('positions', {})
                size_changed = ({c: str(p.size) for c, p in positions.items()} !=
                    {c: p['size'] for c, p in previous_positions.items()})
                query_history = bool(baseline and (size_changed or pending or observed-history_checked >= 900000))
                history_end_ms = observed
                if query_history:
                    stage = 'fill_history'
                    stage_started = time.monotonic()
                    _log('account_monitor.address_stage_started', address=address, stage=stage,
                         since_ms=since, until_ms=observed, observed_ms=clock_ms())
                    fetched, coverage = self.client.history(address, since, observed, max_pages=2)
                    _log('account_monitor.address_stage_completed', address=address, stage=stage,
                         fill_count=len(fetched), pagination_complete=coverage.get('pagination_complete', True),
                         elapsed_ms=int((time.monotonic() - stage_started) * 1000))
                else:
                    fetched = []
                    coverage = coverage if baseline else {'pagination_complete': True, 'next_start_ms': observed,
                        'coverage': 'production_action_monitoring_no_historical_backfill'}
                fetched = list({fill_key(f): f for f in pending.get('fills', []) + fetched}.values())
                if not coverage.get('pagination_complete', True):
                    # Never publish a partial action and then publish its remaining
                    # pages as another action. Keep the successful baseline intact.
                    self.store.save_global('account_pending_fills:' + address,
                        {'coverage': coverage, 'fills': fetched}, [])
                    self.store.update_account_schedule(address, now_ms=observed, tier='backfill',
                        interval_ms=30000, qualified=True)
                    report.setdefault('incomplete_history', []).append(address)
                    _log('account_monitor.address_deferred', address=address, reason='incomplete_history',
                         fill_count=len(fetched), next_start_ms=coverage.get('next_start_ms'),
                         elapsed_ms=int((time.monotonic() - address_started) * 1000))
                    continue
                known = {fill_key(f) for f in self.store.fills(address,
                    min([since] + [int(f['time']) for f in fetched]), observed)}
                new_fills = [f for f in fetched if baseline and fill_key(f) not in known
                    and int(f['time']) >= baseline['state'].get('account_monitoring_since_ms', started)]
                history = event_history(performance, coverage)
                events, state = evaluate_address(address=address, observed_ms=observed,
                    first_seen_ms=observed, positions=positions,
                    previous=baseline['state'] if baseline else None, markets=markets,
                    history=history, new_fills=new_fills, rules=self.rules)
                selected = [e for e in (account_event(e, self.rules, include_loss=include_loss) for e in events) if e]
                if selected:
                    # Verify official profitability again immediately before creating a reportable event.
                    stage = 'candidate_requalification'
                    stage_started = time.monotonic()
                    _log('account_monitor.address_stage_started', address=address, stage=stage, observed_ms=clock_ms())
                    portfolio = self.client.portfolio(address)
                    performance = portfolio_performance(portfolio, clock_ms())
                    self.store.save_global('account_performance:' + address, performance, [])
                    if not is_qualified(performance):
                        selected = []
                    else:
                        history = event_history(performance, coverage)
                        observed = clock_ms()
                        events, state = evaluate_address(address=address, observed_ms=observed,
                            first_seen_ms=observed, positions=positions,
                            previous=baseline['state'], markets=markets, history=history,
                            new_fills=new_fills, rules=self.rules)
                        for event in events:
                            action = event.evidence.get('actions') or {}
                            cutoff = int(action.get('first_fill_ms') or observed)
                            total = portfolio_all_time_at(portfolio, cutoff)
                            if total:
                                event.evidence.setdefault('history', {})['official_account_total_pnl'] = total
                        selected = [e for e in (account_event(e, self.rules, include_loss=include_loss) for e in events) if e]
                    _log('account_monitor.address_stage_completed', address=address, stage=stage,
                         candidate_count=len(selected), elapsed_ms=int((time.monotonic() - stage_started) * 1000))
                state['account_history_checked_ms'] = history_end_ms if query_history or not baseline else history_checked
                state['snapshot_times'] = snapshot_times
                for e in selected:
                    e.evidence['snapshot_times'] = snapshot_times
                state['account_production_started_ms'] = started
                state['account_monitoring_since_ms'] = baseline['state']['account_monitoring_since_ms'] if baseline else observed
                self.store.commit_observation(address=address, now_ms=observed, state=state,
                    history=history, coverage=coverage, fills=fetched, candidates=selected)
                self.store.save_global('account_pending_fills:' + address, {}, [])
                last_action = max([int(f['time']) for f in new_fills] + [row['last_action_ms']])
                active = any(p.notional >= self.rules.action for p in positions.values()) or observed - last_action <= HOUR
                tier = ('active' if active else 'idle') if is_qualified(performance) else 'unqualified'
                self.store.update_account_schedule(address, now_ms=observed, tier=tier,
                    interval_ms=(180000 if active else 1800000) if is_qualified(performance) else DAY, qualified=is_qualified(performance),
                    last_action_ms=last_action)
                report['attribute_queries'] += int(query_history)
                report['processed'] += 1
                report['candidates'] += len(selected)
                _log('account_monitor.address_completed', address=address, outcome='checked',
                     candidate_count=len(selected), elapsed_ms=int((time.monotonic() - address_started) * 1000))
            except Exception as exc:
                self.store.record_error(address, observed, str(exc))
                self.store.update_account_schedule(address, now_ms=observed, tier='retry',
                    interval_ms=300000, qualified=bool(row['qualified']))
                report['failed'][address] = str(exc)
                _log('account_monitor.address_failed', address=address, stage=stage, error=str(exc),
                     elapsed_ms=int((time.monotonic() - address_started) * 1000))
        report['discovered'] = len(found)
        report['completed_ms'] = clock_ms()
        report['pool'] = self.store.account_pool_stats(clock_ms())
        report['request_weight_total'] = getattr(self.client, 'request_weight', 0)
        self.store.save_global('last_pass', report, [])
        _log('account_monitor.pass_completed', observed_ms=report['completed_ms'], processed=report['processed'],
             candidates=report['candidates'], discovered=report['discovered'], failed_count=len(report['failed']),
             incomplete_history_count=len(report.get('incomplete_history', [])),
             request_weight_total=report['request_weight_total'], elapsed_ms=int((time.monotonic() - pass_started) * 1000))
        return report
