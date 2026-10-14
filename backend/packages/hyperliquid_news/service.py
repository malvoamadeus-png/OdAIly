from __future__ import annotations

import json
import time
from typing import Any

from .client import NewsClient, address_key
from .discovery import enrichment_reasons, leaderboard_candidates, trade_candidates
from .detector import evaluate_address, evaluate_divergence
from .history import fill_key, summarize
from .market_cap import CoinGeckoMarketCaps
from .models import Position, Rules, number
from .store import NewsStore


class NewsMonitor:
    """One bounded collection pass. Only structured evidence is persisted.

    Intended for eventual server scheduling; local execution is offline tests only.
    """
    def __init__(self, *, store: NewsStore, client: NewsClient, rules: Rules | None = None,
                 address_limit: int = 500, addresses_per_pass: int = 10,
                 markets_per_discovery: int = 5, leaderboard_seeds: int = 30,
                 history_window_ms: int = 90 * 86400000, divergence_freshness_ms: int = 900000,
                 market_cap_client: CoinGeckoMarketCaps | None = None,
                 market_cap_mapping: dict[str, str] | None = None) -> None:
        if min(address_limit, addresses_per_pass, markets_per_discovery, history_window_ms, divergence_freshness_ms) < 1 or leaderboard_seeds < 0:
            raise ValueError('Invalid collection budget')
        self.store, self.client, self.rules = store, client, rules or Rules()
        self.address_limit, self.addresses_per_pass = address_limit, addresses_per_pass
        self.markets_per_discovery, self.leaderboard_seeds = markets_per_discovery, leaderboard_seeds
        self.history_window_ms = history_window_ms
        self.divergence_freshness_ms = divergence_freshness_ms
        self.market_cap_client = market_cap_client
        self.market_cap_mapping = market_cap_mapping or {}

    def run_once(self, *, now_ms: int | None = None,
                 market_caps: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.store.initialize()
        result: dict[str, Any] = {'observed_ms': now_ms, 'processed': 0, 'candidates': 0, 'failed': {},
                                  'discovery_sources': [], 'discovery_rejected': 0, 'address_limit': self.address_limit,
                                  'coverage': 'bounded_discovery_and_address_polling', 'attribute_queries': 0}
        dexs = self.client.dex_names()
        markets = {}
        # A failed universe fetch aborts this pass; no partial market deletion.
        for dex in dexs:
            markets.update(self.client.markets(dex))
        caps = dict(market_caps or {})
        if self.market_cap_client and self.market_cap_mapping:
            try:
                mapping = {coin: coin_id for coin, coin_id in self.market_cap_mapping.items() if coin in markets}
                fetched_caps = self.market_cap_client.fetch(mapping, now_ms=now_ms,
                    cached=self.store.metadata('coingecko_market_caps', {}))
                self.store.save_global('coingecko_market_caps',
                    {cap['coin_id']: cap for cap in fetched_caps.values()}, [])
                caps = {**fetched_caps, **caps}
            except Exception as exc:
                result['failed']['market_caps:coingecko'] = str(exc)
        for coin, cap in caps.items():
            if coin in markets:
                if not cap.get('source') or cap.get('observed_ms') is None or number(cap['value']) <= 0:
                    raise ValueError('Market cap requires positive value, source and timestamp')
                markets[coin].update(market_cap=str(number(cap['value'])), market_cap_source=cap['source'],
                                     market_cap_observed_ms=int(cap['observed_ms']))
                if cap.get('coin_id'):
                    markets[coin]['market_cap_coin_id'] = cap['coin_id']
                if cap.get('provider_updated_ms') is not None:
                    markets[coin]['market_cap_provider_updated_ms'] = int(cap['provider_updated_ms'])
        self._discover(now_ms, markets, result)
        for row in self.store.addresses(self.addresses_per_pass):
            address = row['address']
            if self.store.blocked(address):
                continue
            try:
                old = self.store.load(address)
                positions = {}
                snapshot_times = {}
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
                start_ms = max(0, now_ms - self.history_window_ms)
                cached = self.store.fills(address, start_ms, now_ms)
                # Unfinished initial backfill resumes at its inclusive page boundary.
                if old and old['coverage'].get('coverage') == 'not_queried_no_eligibility':
                    request_start = start_ms
                elif old and not old['coverage']['pagination_complete']:
                    request_start = max(start_ms, old['coverage']['next_start_ms'])
                elif old:
                    request_start = max(start_ms, old['state']['observed_ms'] - 60000)
                else:
                    request_start = start_ms
                sources = json.loads(row['sources'])
                reasons = enrichment_reasons(positions, sources, markets, self.rules)
                official_windows = {s['window']: {'pnl': s['pnl'], 'rank': s['rank'], 'side': s['side'],
                                    'observed_ms': s['observed_ms']} for s in sources
                                    if s['kind'] == 'leaderboard' and s.get('window') and
                                    now_ms - int(s['observed_ms']) <= 86400000}
                if reasons:
                    fetched, coverage = self.client.history(address, request_start, now_ms)
                    result['attribute_queries'] += 1
                else:
                    fetched = []
                    coverage = {'pagination_complete': True, 'next_start_ms': start_ms,
                                'coverage': 'not_queried_no_eligibility'}
                known = {fill_key(f) for f in cached}
                new_fills = [f for f in fetched if old and fill_key(f) not in known
                             and int(f['time']) >= old['state'].get('monitoring_since_ms', old['state']['observed_ms'])]
                all_fills = list({fill_key(f): f for f in cached + fetched}.values())
                history = summarize(all_fills, start_ms=start_ms, end_ms=now_ms)
                history['api_coverage'] = coverage
                history['enrichment_reasons'] = reasons
                if official_windows:
                    history['official_account_performance'] = {
                        'source': 'hyperliquid_public_leaderboard', 'windows': official_windows,
                        'pnl_scope': 'official_leaderboard_not_assumed_realized'}
                events, state = evaluate_address(
                    address=address, observed_ms=now_ms, first_seen_ms=row['first_seen_ms'], positions=positions,
                    previous=old['state'] if old else None, markets=markets, history=history,
                    new_fills=new_fills, rules=self.rules)
                state['snapshot_times'] = snapshot_times
                state['monitoring_since_ms'] = old['state'].get('monitoring_since_ms', old['state']['observed_ms']) if old else now_ms
                for event in events:
                    event.evidence['address_sources'] = json.loads(row['sources'])
                    event.evidence['snapshot_times'] = snapshot_times
                self.store.commit_observation(address=address, now_ms=now_ms, state=state, history=history,
                                              coverage=coverage, fills=fetched, candidates=events)
                result['processed'] += 1
                result['candidates'] += len(events)
            except Exception as exc:
                self.store.record_error(address, now_ms, str(exc))
                result['failed'][address] = str(exc)
        # Compare successful snapshots within the freshness window. Failed or
        # expired participants cannot reset an existing divergence baseline.
        recent = self.store.recent_states(now_ms - self.divergence_freshness_ms)
        successful_states = {a: r['state'] for a, r in recent.items() if a not in result['failed']}
        histories = {a: r['history'] for a, r in recent.items() if a not in result['failed']}
        previous_divergence = self.store.metadata('divergence', {})
        events, active = evaluate_divergence(observed_ms=now_ms, states=successful_states,
                                             histories=histories, previous=previous_divergence, rules=self.rules)
        for coin, old_group in previous_divergence.items():
            if coin not in active and not set(old_group['addresses']).issubset(successful_states):
                active[coin] = old_group
        self.store.save_global('divergence', active, events)
        result['candidates'] += len(events)
        self.store.save_global('last_pass', result, [])
        return result

    def _discover(self, now_ms: int, markets: dict[str, Any], result: dict[str, Any]) -> None:
        if self.leaderboard_seeds and now_ms - self.store.metadata('leaderboard_checked_ms', 0) >= 86400000:
            try:
                rows = self.client.leaderboard()
                for address, source in leaderboard_candidates(rows, self.leaderboard_seeds, now_ms):
                    accepted = self.store.discover(address, source=source, now_ms=now_ms, limit=self.address_limit)
                    result['discovery_rejected'] += int(not accepted)
                self.store.save_global('leaderboard_checked_ms', now_ms, [])
                result['discovery_sources'].append('leaderboard')
            except Exception as exc:
                result['failed']['discovery:leaderboard'] = str(exc)
        coins = sorted(markets)
        if not coins:
            return
        cursor = self.store.metadata('market_discovery_cursor', 0) % len(coins)
        count = min(self.markets_per_discovery, len(coins))
        for i in range(count):
            coin = coins[(cursor + i) % len(coins)]
            try:
                trades = self.client.recent_trades(coin)
                for address, source in trade_candidates(trades, coin, self.rules, now_ms):
                    accepted = self.store.discover(address, source=source, now_ms=now_ms, limit=self.address_limit)
                    result['discovery_rejected'] += int(not accepted)
                result['discovery_sources'].append(coin)
            except Exception as exc:
                result['failed'][f'discovery:{coin}'] = str(exc)
        self.store.save_global('market_discovery_cursor', (cursor + count) % len(coins), [])
