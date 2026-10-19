"""Explicit pre-launch case mining. Never called by the normal monitor/dispatcher."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, replace
from decimal import Decimal
from typing import Any

from packages.common.storage import connect_sqlite
from .client import NewsClient
from .detector import evaluate_address
from .history import _ordered_by_position, fill_key, perp_fill, summarize
from .models import Candidate, Position, Rules, ZERO, number
from .scenarios import PRODUCTION_SCENARIOS, SCENARIOS, UNRESEARCHED_SCENARIOS
from .store import NewsStore, encode

PREVIEW_SCENARIOS = UNRESEARCHED_SCENARIOS
PREVIEWS_PER_SCENARIO = 5


def case_scenarios(event: dict[str, Any]) -> list[str]:
    kinds = set(event['kinds'])
    kinds = {kind for kind in kinds if kind not in {'large_profit', 'large_loss'}
             or _valid_large_pnl_movement(event, kind)}
    labels = set(event['evidence'].get('highlights', []))
    return [s[0] for s in SCENARIOS if kinds.intersection(s[3]) and (not s[4] or labels.intersection(s[4]))]


def _valid_large_pnl_movement(event: dict[str, Any], kind: str) -> bool:
    evidence = event.get('evidence', {})
    position = evidence.get('position') or {}
    current = position.get('unrealized_pnl')
    previous = evidence.get('previous_reported_floating_pnl')
    if previous is None:
        previous = evidence.get('previous_reported_floating_pnl_usd')
    if previous is None:
        previous = evidence.get('news_change', {}).get('previous_floating_pnl')
    if previous is None:
        previous = (evidence.get('previous_position') or {}).get('unrealized_pnl')
    if current is None or previous is None:
        return False
    coin = event.get('coin', '')
    key = 'major_pnl' if coin in ('BTC', 'ETH') else 'other_pnl'
    default = '5000000' if key == 'major_pnl' else '2000000'
    threshold = number(evidence.get('threshold_rules', {}).get(key, default))
    before, after = number(previous), number(current)
    if kind == 'large_profit':
        return before >= 0 and after >= threshold and after - before >= threshold
    return before <= 0 and after <= -threshold and before - after >= threshold


def select_preview_cases(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select up to five strong, distinct text samples for each requested direction."""
    representatives = representative_cases(events)
    counts = {scenario: 0 for scenario in PREVIEW_SCENARIOS}
    used_addresses = {scenario: set() for scenario in PREVIEW_SCENARIOS}
    selected: dict[str, dict[str, Any]] = {}

    def score(event: dict[str, Any], scenario: str) -> tuple[float, ...]:
        evidence = event.get('evidence', {})
        actions = evidence.get('actions', {})
        history = evidence.get('history', {})
        asset = history.get('assets', {}).get(event.get('coin'), {})
        current = evidence.get('position') or {}
        previous = evidence.get('previous_position') or {}
        amount = number(actions.get('open_notional', 0)) + number(actions.get('close_notional', 0))
        if scenario == 'large_pnl':
            return (float(amount), abs(float(number(current.get('unrealized_pnl') or 0))))
        if scenario == 'asset_win_rate':
            return (float(number(asset.get('win_rate') or 0)), float(asset.get('rounds') or 0), float(amount))
        if scenario == 'asset_profit':
            return (float(number(asset.get('last_30d_closed_pnl_less_fees') or 0)), float(amount))
        if scenario == 'reversal':
            old_notional = number(previous.get('notional') or 0)
            new_notional = number(current.get('notional') or 0)
            return (float(min(old_notional, new_notional)), float(amount))
        if scenario == 'giveback':
            peak = number(evidence.get('episode_peak_unrealized_pnl') or 0)
            pnl = number(current.get('unrealized_pnl') or 0)
            return (float(abs(peak - pnl)), float(amount))
        if scenario == 'loss_addition':
            return (abs(float(number(previous.get('unrealized_pnl') or 0))), float(amount))
        return (abs(float(number(actions.get('ordinary_closed_pnl', actions.get('closed_pnl', 0))))),
                float(number(actions.get('ordinary_close_notional', actions.get('close_notional', 0)))))

    for scenario in PREVIEW_SCENARIOS:
        candidates = sorted(
            (event for event in representatives if scenario in case_scenarios(event)),
            key=lambda event: score(event, scenario), reverse=True)
        for event in candidates:
            if counts[scenario] >= PREVIEWS_PER_SCENARIO:
                break
            if event['key'] in selected:
                continue
            address_asset = (tuple(sorted(str(a).lower() for a in event.get('addresses', []))),
                             str(event.get('coin', '')))
            covered = set(case_scenarios(event)).intersection(PREVIEW_SCENARIOS)
            if any(address_asset in used_addresses[key] for key in covered):
                continue
            if any(counts[key] >= PREVIEWS_PER_SCENARIO for key in covered):
                continue
            selected[event['key']] = event
            for key in covered:
                counts[key] += 1
                used_addresses[key].add(address_asset)
    return sorted(selected.values(), key=lambda event: (event.get('observed_ms', 0), event['key']))


def representative_cases(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one evidence-rich sample per address, asset and scenario."""
    selected: dict[tuple[tuple[str, ...], str, str], tuple[tuple[float, ...], dict[str, Any]]] = {}
    for event in events:
        evidence = event.get('evidence', {})
        actions = evidence.get('actions', {})
        history = evidence.get('history', {})
        asset = history.get('assets', {}).get(event.get('coin'), {})
        previous = evidence.get('previous_position') or {}
        action_size = number(actions.get('open_notional', 0)) + number(actions.get('close_notional', 0))
        for scenario in case_scenarios(event):
            if scenario == 'loss_addition':
                score = (abs(float(number(previous.get('unrealized_pnl') or 0))), float(action_size))
            elif scenario == 'asset_win_rate':
                score = (float(number(asset.get('win_rate') or 0)), float(asset.get('rounds') or 0), float(action_size))
            elif scenario == 'asset_profit':
                score = (float(number(asset.get('last_30d_closed_pnl_less_fees') or 0)), float(action_size))
            elif scenario == 'realized':
                score = (abs(float(number(actions.get('closed_pnl', 0)))), float(number(actions.get('close_notional', 0))))
            else:
                current = evidence.get('position') or {}
                score = (abs(float(number(current.get('unrealized_pnl') or 0))), float(action_size), float(event.get('observed_ms', 0)))
            key = (tuple(sorted(event.get('addresses', []))), str(event.get('coin', '')), scenario)
            if key not in selected or score > selected[key][0]:
                selected[key] = (score, event)
    unique = {event['key']: event for _, event in selected.values()}
    return sorted(unique.values(), key=lambda event: (event.get('observed_ms', 0), event['key']))


def initialize_cases(store: NewsStore) -> None:
    with connect_sqlite(store.path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS hl_news_test_cases (
            event_key TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL, payload TEXT NOT NULL,
            preview TEXT, created_ms INTEGER NOT NULL)''')


def clear_test_cases(store: NewsStore) -> dict[str, int]:
    """Clear test records and their previews, preserving live previews and source caches."""
    initialize_cases(store)
    with connect_sqlite(store.path) as conn:
        event_keys={row['event_key'] for row in conn.execute('SELECT event_key FROM hl_news_test_cases')}
        previews=[row['key'] for row in conn.execute(
            "SELECT key FROM hl_news_metadata WHERE key LIKE 'editorial_preview:%'")
            if row['key'].removeprefix('editorial_preview:') in event_keys or
            row['key'].startswith(('editorial_preview:test:', 'editorial_preview:test-price:',
                                   'editorial_preview:test-startup:'))]
        conn.execute('DELETE FROM hl_news_test_cases')
        for key in previews:
            conn.execute('DELETE FROM hl_news_metadata WHERE key=?',(key,))
    return {'cases_removed': len(event_keys), 'previews_removed': len(previews)}


def save_cases(store: NewsStore, events: list[dict[str, Any]]) -> None:
    initialize_cases(store)
    now = int(time.time()*1000)
    with connect_sqlite(store.path) as conn:
        for event in events:
            event['test_scenarios'] = case_scenarios(event)
            previous = conn.execute('SELECT payload FROM hl_news_test_cases WHERE event_key=?', (event['key'],)).fetchone()
            if previous:
                old = json.loads(previous['payload'])
                # Preserve separately enriched, pre-action official portfolio evidence.
                performance = old['evidence'].get('history', {}).get('official_account_performance')
                if performance:
                    event['evidence'].setdefault('history', {})['official_account_performance'] = performance
                    cutoff = old['evidence'].get('test_replay', {}).get('official_pnl_cutoff_ms')
                    if cutoff is not None:
                        event['evidence'].setdefault('test_replay', {})['official_pnl_cutoff_ms'] = cutoff
                    from .detector import highlights, action_highlights
                    action = event['evidence'].get('actions', {})
                    event['evidence']['highlights'] = action_highlights(
                        highlights(event['evidence']['history'], event['coin'], Rules()),
                        max(number(action.get('open_notional',0)), number(action.get('close_notional',0))), Rules())
                    if 'official_account_high_profit' in event['evidence']['highlights'] and 'highlighted_address_action' not in event['kinds']:
                        event['kinds'].append('highlighted_address_action')
                if previous['payload'] == encode(event):
                    continue
                conn.execute('DELETE FROM hl_news_metadata WHERE key=?', ('editorial_preview:'+event['key'],))
            conn.execute('''INSERT INTO hl_news_test_cases VALUES(?,?,?,NULL,?)
                ON CONFLICT(event_key) DO UPDATE SET payload=excluded.payload,preview=NULL''',
                (event['key'], event['observed_ms'], encode(event), now))


def prune_replayed_cases(store: NewsStore, address: str, events: list[dict[str, Any]]) -> int:
    """Remove stale per-fill replays after replacing an address with representatives."""
    keep = {event['key'] for event in events}
    removed = 0
    initialize_cases(store)
    with connect_sqlite(store.path) as conn:
        rows = conn.execute('SELECT event_key,payload FROM hl_news_test_cases').fetchall()
        for row in rows:
            event = json.loads(row['payload'])
            replay = event.get('evidence', {}).get('test_replay', {})
            addresses = event.get('addresses', [])
            if (address.lower() not in {str(item).lower() for item in addresses}
                    or not replay.get('real_address') or row['event_key'] in keep):
                continue
            conn.execute('DELETE FROM hl_news_test_cases WHERE event_key=?', (row['event_key'],))
            conn.execute('DELETE FROM hl_news_metadata WHERE key=?', ('editorial_preview:'+row['event_key'],))
            removed += 1
    return removed


def prune_tested_cases(store: NewsStore) -> dict[str, int]:
    """Remove reviewed directions and retain evidence for unresolved test directions."""
    store.initialize()
    initialize_cases(store)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute('SELECT event_key,payload FROM hl_news_test_cases').fetchall()
        removed=0
        retained=0
        previews_removed=0
        for row in rows:
            event=json.loads(row['payload'])
            unresolved=[key for key in case_scenarios(event) if key not in PRODUCTION_SCENARIOS]
            if not unresolved:
                conn.execute('DELETE FROM hl_news_test_cases WHERE event_key=?',(row['event_key'],))
                previews_removed += conn.execute('DELETE FROM hl_news_metadata WHERE key=?',
                    ('editorial_preview:'+row['event_key'],)).rowcount
                removed+=1
                continue
            event['test_scenarios']=unresolved
            conn.execute('UPDATE hl_news_test_cases SET payload=?,preview=NULL WHERE event_key=?',
                         (encode(event),row['event_key']))
            if conn.execute('DELETE FROM hl_news_metadata WHERE key=?',
                            ('editorial_preview:'+row['event_key'],)).rowcount:
                previews_removed+=1
            retained+=1
    return {'retained':retained,'removed':removed,'previews_removed':previews_removed}


def enrich_cached_case_records(store: NewsStore) -> int:
    """Attach address trading stats from cached fills available before each event."""
    initialize_cases(store)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute('SELECT event_key,payload FROM hl_news_test_cases').fetchall()
    enriched=0
    for row in rows:
        event=json.loads(row['payload'])
        evidence=event.get('evidence',{})
        history=evidence.get('history') or {}
        if history.get('fills',0):
            continue
        addresses=event.get('addresses',[])
        if not addresses:
            continue
        cached=store.metadata('test_fills:'+str(addresses[0]),{})
        fills=cached.get('fills',[])
        if not fills:
            continue
        actions=evidence.get('actions') or {}
        if actions.get('first_fill_ms') is not None:
            cutoff=int(actions['first_fill_ms'])-1
        else:
            cutoff=int(event.get('observed_ms',0))
            if evidence.get('test_replay',{}).get('phase') == 'before_fill_price':
                cutoff-=1
        eligible=[fill for fill in fills if valid_test_fill(fill) and int(fill['time']) <= cutoff]
        if not eligible:
            continue
        evidence['history']=summarize(eligible,start_ms=0,end_ms=cutoff,
                                      coverage='cached_test_fills_before_event')
        save_cases(store,[event])
        enriched+=1
    return enriched


def revalidate_test_cases(store: NewsStore) -> dict[str, int]:
    """Apply the current minimum sample to saved test cases; leave live evidence intact."""
    initialize_cases(store)
    updated = 0
    rules = Rules()
    with connect_sqlite(store.path) as conn:
        rows = conn.execute('SELECT event_key,payload FROM hl_news_test_cases').fetchall()
        for row in rows:
            event = json.loads(row['payload'])
            e = event['evidence']
            labels = list(e.get('highlights', []))
            kinds = list(event.get('kinds', []))
            asset = e.get('history', {}).get('assets', {}).get(event['coin'], {})
            rounds = asset.get('rounds', 0)
            invalid_win_rate = 'asset_high_win_rate' in labels and rounds < rules.min_rounds
            monthly_pnl = asset.get('last_30d_closed_pnl_less_fees')
            threshold = rules.asset_profit_line(event['coin'])
            actions = e.get('actions', {})
            action_amount = max(number(actions.get('open_notional', 0)), number(actions.get('close_notional', 0)))
            invalid_asset_profit = ('asset_high_profit' in labels and
                                    (monthly_pnl is None or number(monthly_pnl) < threshold or
                                     action_amount < rules.asset_profit_action))
            invalid_pnl_kinds = {kind for kind in ('large_profit', 'large_loss')
                                 if kind in kinds and not _valid_large_pnl_movement(event, kind)}
            if not invalid_win_rate and not invalid_asset_profit and not invalid_pnl_kinds:
                continue
            e['highlights'] = [label for label in labels if label not in (
                {'asset_high_win_rate'} if invalid_win_rate else set()) | (
                {'asset_high_profit'} if invalid_asset_profit else set())]
            event['kinds'] = [kind for kind in kinds if kind not in invalid_pnl_kinds]
            e.setdefault('threshold_rules', {}).update({
                'min_rounds': rules.min_rounds,
                'asset_profit': str(rules.asset_profit),
                'other_asset_profit': str(rules.other_asset_profit),
                'asset_profit_action': str(rules.asset_profit_action),
            })
            if not e['highlights']:
                event['kinds'] = [kind for kind in event['kinds'] if kind != 'highlighted_address_action']
            event['test_scenarios'] = case_scenarios(event)
            conn.execute('UPDATE hl_news_test_cases SET payload=?,preview=NULL WHERE event_key=?', (encode(event),row['event_key']))
            conn.execute('DELETE FROM hl_news_metadata WHERE key=?', ('editorial_preview:'+row['event_key'],))
            updated += 1
    return {'updated': updated}


def valid_test_fill(fill: dict[str, Any]) -> bool:
    # Outcome tokens are not perpetuals and can carry zero settlement prices.
    return (perp_fill(fill) and not str(fill['coin']).startswith('#')
            and fill.get('side') in ('B','A') and number(fill.get('sz',0)) > 0
            and number(fill.get('px',0)) > 0)


def replay_fills(address: str, fills: list[dict[str, Any]], *, rules: Rules | None = None) -> list[dict[str, Any]]:
    """Reconstruct quantity/action facts only; no invented mark, PnL, OI or liquidation.

    Fill prices value quantities at the actual execution time. Unknown historical
    floating PnL remains omitted. Attributes are computed strictly before actions.
    One-hour action windows allow split orders; discontinuities break replay state.
    """
    rules = rules or Rules()
    unique = {fill_key(f): f for f in fills if valid_test_fill(f)}
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in unique.values():
        groups.setdefault(f['coin'], []).append(f)
    cases = []
    for coin, items in groups.items():
        ordered = _ordered_by_position(items)
        preceding = []
        batch = []
        expected = None
        for f in ordered:
            before = number(f['startPosition'])
            if f.get('_ambiguous_order') or expected is not None and before != expected:
                if batch:
                    cases.extend(_action_case(address, coin, batch, preceding, rules))
                    preceding.extend(batch)
                batch = []
                if f.get('_ambiguous_order'):
                    preceding.append(f)
                    expected = None
                    continue
            # Finish a time window before adding the following fill, avoiding lookahead.
            if batch and int(f['time']) - int(batch[0]['time']) > 3600000:
                cases.extend(_action_case(address, coin, batch, preceding, rules))
                preceding.extend(batch)
                batch = []
            batch.append(f)
            expected = before + (number(f['sz']) if f['side']=='B' else -number(f['sz']))
        if batch:
            cases.extend(_action_case(address, coin, batch, preceding, rules))
    # Account high-profit needs cross-asset history, not a per-asset substitute.
    all_ordered = sorted(unique.values(), key=lambda f: int(f['time']))
    for event in cases:
        start = event['evidence']['actions']['first_fill_ms']
        history = summarize([f for f in all_ordered if int(f['time']) < start],
                            start_ms=0, end_ms=start-1)
        month_start = max(0, start - 30 * 86400000)
        month = summarize([f for f in all_ordered if month_start <= int(f['time']) < start],
                          start_ms=month_start, end_ms=start-1, coverage='pre_action_30d')
        month_asset = month.get('assets', {}).get(event['coin'], {})
        history_asset = history.setdefault('assets', {}).setdefault(event['coin'], {})
        history_asset.update(
            last_30d_closed_pnl_less_fees=month_asset.get('closed_pnl_less_fees', '0'),
            last_30d_first_observed_fill_ms=month_asset.get('first_observed_fill_ms'),
            last_30d_last_observed_fill_ms=month_asset.get('last_observed_fill_ms'),
            last_30d_start_ms=month_start,
            last_30d_end_ms=start-1,
        )
        event['evidence']['history'] = history
        from .detector import highlights, action_highlights
        labels = action_highlights(highlights(history, event['coin'], rules),
            max(number(event['evidence']['actions']['open_notional']),
                number(event['evidence']['actions']['close_notional'])), rules)
        event['evidence']['highlights'] = labels
        if labels:
            if 'highlighted_address_action' not in event['kinds']:
                event['kinds'].append('highlighted_address_action')
        event['test_scenarios'] = case_scenarios(event)
    return cases


def _action_case(address: str, coin: str, batch: list[dict], preceding: list[dict], rules: Rules) -> list[dict]:
    first, last = batch[0], batch[-1]
    start, end = int(first['time']), int(last['time'])
    before = number(first['startPosition'])
    after = number(last['startPosition']) + (number(last['sz']) if last['side']=='B' else -number(last['sz']))
    # Execution reference is explicit test evidence, not a historical mark snapshot.
    ref = number(last['px'])
    old = Position(coin, before, abs(before)*ref, ref, ZERO, None) if before else None
    current = Position(coin, after, abs(after)*ref, ref, ZERO, None) if after else None
    previous = {'observed_ms': max(0,start-1), 'positions': {coin: old.facts()} if old else {}}
    hist = summarize(preceding, start_ms=0, end_ms=start-1)
    events, _ = evaluate_address(address=address, observed_ms=end, first_seen_ms=0,
        positions={coin:current} if current else {}, previous=previous, markets={}, history=hist,
        new_fills=batch, rules=rules)
    # Multiple fills can close one side and establish the other. Also inspect
    # intra-window sign transitions when both endpoints happen to share a side.
    transitions=[]
    for f in batch:
        b=number(f['startPosition']); a=b+(number(f['sz']) if f['side']=='B' else -number(f['sz']))
        if b*a<0 and min(abs(b),abs(a))*number(f['px']) >= rules.action:
            transitions.append(f)
    result=[]
    for candidate in events:
        event=asdict(candidate)
        event['kinds']=[k for k in event['kinds'] if k not in {
            'large_profit','large_loss','profit_giveback','profit_to_loss','loss_addition',
            'near_liquidation','oi_share','market_cap_share','newly_observed_address_position'}]
        if not event['kinds']:
            continue
        # Delete placeholders: missing historical floating PnL must not become zero.
        for field in ('position','previous_position'):
            if event['evidence'].get(field):
                event['evidence'][field]['unrealized_pnl']=None
                event['evidence'][field]['mark']=None
        change=event['evidence'].get('news_change',{})
        change['previous_floating_pnl']=None
        event['evidence']['first_observation']=False
        event['evidence']['test_replay']={
            'mode':'historical_fills', 'valuation_basis':'quantity_times_window_last_execution_price',
            'reference_execution_price':str(ref), 'start_ms':start,'end_ms':end,
            'no_historical_floating_pnl':True, 'real_address':True,
            'attribute_cutoff_ms':start-1,'fill_count':len(batch),
            'purpose':'prelaunch_case_review_only'}
        event['key']='test:'+hashlib.sha256(encode({'address':address,'coin':coin,'fills':[fill_key(f) for f in batch]}).encode()).hexdigest()
        result.append(event)
    # Preserve exact single-fill reversals if the hourly net view would conceal them.
    if transitions and len(batch)>1:
        for f in transitions:
            result.extend(_action_case(address,coin,[f],[*preceding,*[x for x in batch if int(x['time'])<int(f['time'])]],rules))
    return result


def mine_cases(store: NewsStore, *, address_limit: int = 40, per_side: int = 12, addresses: list[str] | None = None) -> dict[str, Any]:
    """Bounded sequential test scan; resumable cache separate from live history."""
    if not 1 <= address_limit <= 200 or not 1 <= per_side <= 50:
        raise ValueError('Invalid test mining budget')
    initialize_cases(store)
    revalidate_test_cases(store)
    client=NewsClient()
    now=int(time.time()*1000)
    from .discovery import leaderboard_candidates
    rows=client.leaderboard() if not addresses else []
    discoveries=list(leaderboard_candidates(rows,per_side,now))
    selected=list(dict.fromkeys(a for a,_ in discoveries))
    # Mix positive and negative period leaders before using remaining candidates.
    mixed=[]
    for window in ('day','week','month','allTime'):
        pos=[a for a,s in discoveries if s['window']==window and s['side']=='profit']
        neg=[a for a,s in discoveries if s['window']==window and s['side']=='loss']
        for pair in zip(pos,neg):
            mixed.extend(pair)
    # Existing reporting is only an address discovery seed, never proof of a rule.
    import re
    reported=[]
    with connect_sqlite(store.path) as conn:
        for row in conn.execute("SELECT title,content FROM tasks WHERE source!='hyperliquid_news' AND (lower(content) LIKE '%hyperliquid%' OR lower(title) LIKE '%hyperliquid%') ORDER BY id DESC LIMIT 1500"):
            reported.extend(re.findall(r'0x[0-9a-fA-F]{40}', (row['title'] or '')+' '+row['content']))
    from .client import address_key
    selected=list(dict.fromkeys([address_key(a) for a in addresses] if addresses else [a.lower() for a in reported]+mixed+selected))[:address_limit]
    report={'mode':'prelaunch_case_mining','addresses':len(selected),'processed':0,'failed':{},'case_count':0,'coverage':{}}
    for address in selected:
        cache=store.metadata('test_fills:'+address,{})
        try:
            if cache.get('fetched_ms',0) > now-3600000 and cache.get('source') == 'userFillsByTime_paginated':
                fills=cache['fills']
            else:
                fills,coverage=client.history(address,now-90*86400000,now)
                store.save_global('test_fills:'+address,{'fills':fills,'fetched_ms':now,
                    'source':'userFillsByTime_paginated','coverage':coverage},[])
            cases=representative_cases(replay_fills(address,fills)+replay_execution_prices(address,fills)+replay_test_startup(address,fills))
            save_cases(store,cases)
            prune_replayed_cases(store,address,cases)
            report['processed']+=1
            report['case_count']+=len(cases)
            store.save_global('test_mining_progress',report,[])
            print(encode({'address':address,'fills':len(fills),'cases':len(cases),'processed':report['processed']}),flush=True)
        except Exception as exc:
            report['failed'][address]=str(exc)
    with connect_sqlite(store.path) as conn:
        all_cases=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM hl_news_test_cases')]
    eligible_coverage={s[0]:sum(s[0] in case_scenarios(e) for e in all_cases) for s in SCENARIOS}
    capped=prune_tested_cases(store)
    report['statistics_enriched']=enrich_cached_case_records(store)
    with connect_sqlite(store.path) as conn:
        all_cases=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM hl_news_test_cases')]
    report['eligible_coverage']=eligible_coverage
    report['coverage']={s[0]:sum(s[0] in case_scenarios(e) for e in all_cases) for s in SCENARIOS}
    report['cases_retained']=capped['retained']
    report['reviewed_cases_removed']=capped['removed']
    store.save_global('test_mining_progress',report,[])
    return report


def preview_cases(store: NewsStore, *, limit: int = 30, repository_template: bool = False) -> dict[str, Any]:
    """Choose representative real cases and write review copy without tasks/push."""
    from packages.common.config import load_x_processing_settings
    from packages.common.paths import get_paths
    from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
    from packages.x_processing.worker import XProcessingWorker
    from packages.x_processing.models import TaskRecord, HYPERLIQUID_SOURCE, PromptTemplateVersion
    from .copy import source_url
    from .preview import generate_preview
    if not 1 <= limit <= PREVIEWS_PER_SCENARIO * len(PREVIEW_SCENARIOS):
        raise ValueError(f'Preview limit must be 1..{PREVIEWS_PER_SCENARIO * len(PREVIEW_SCENARIOS)}')
    initialize_cases(store)
    capped=prune_tested_cases(store)
    statistics_enriched=enrich_cached_case_records(store)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute('SELECT payload FROM hl_news_test_cases ORDER BY observed_ms DESC').fetchall()
    events=[json.loads(r[0]) for r in rows]
    settings=load_x_processing_settings()
    worker=XProcessingWorker(stage='write',repository=SQLiteXProcessingRepository(store.path),settings=settings)
    if repository_template:
        template_path=get_paths().root_dir/'docs/OdAIly代码与整体设计/Hyperliquid快讯模板.txt'
        prompt=PromptTemplateVersion(id=0,template_key='hyperliquid_writer',version_number=0,
                                     content=template_path.read_text(encoding='utf-8'))
        prompt_source='repository_template'
    else:
        prompt=worker.repository.get_active_prompt('hyperliquid_writer')
        prompt_source='active_prompt'
    selected=select_preview_cases(events)[:limit]
    selected_scenarios={e['key']:set(case_scenarios(e)).intersection(PREVIEW_SCENARIOS) for e in selected}
    counts={scenario:sum(scenario in covered for covered in selected_scenarios.values())
            for scenario in PREVIEW_SCENARIOS}
    report={'selected':len(selected),'generated':0,'failed':{},'cases_retained':capped['retained'],
            'statistics_enriched':statistics_enriched,
            'coverage':sorted(key for key,value in counts.items() if value), 'scenario_counts':counts,
            'missing_scenarios':[key for key,value in counts.items() if not value]}
    for event in selected:
        try:
            task=TaskRecord(id=0,source=HYPERLIQUID_SOURCE,source_item_id=event['key'],source_url=source_url(event),
                            title=None,content='',raw_payload=store.with_address_notes(event))
            result=generate_preview(task=task,prompt=prompt,settings=settings,ai_client=worker._get_ai_client(),
                                    prompt_source=prompt_source)
            store.save_global('editorial_preview:'+event['key'],result,[])
            report['generated']+=1
            print(encode({'event_key':event['key'],'scenarios':case_scenarios(event),'title':result['title']}),flush=True)
        except Exception as exc:report['failed'][event['key']]=str(exc)
    store.save_global('test_preview_progress',report,[])
    return report


def replay_execution_prices(address: str, fills: list[dict[str, Any]], *, rules: Rules | None = None) -> list[dict[str, Any]]:
    """Test-only cost reconstruction using closedPnl and actual fill prices.

    Before a closing fill, entry = execution price minus closed PnL / signed
    closed quantity. Walk backwards through preceding additions to establish
    the first sampled cost. Reject gaps/ambiguous order or inconsistent costs.
    This is price-referenced floating PnL, not a saved historical mark snapshot.
    """
    rules=rules or Rules()
    groups={}
    for f in {fill_key(f):f for f in fills if valid_test_fill(f)}.values():groups.setdefault(f['coin'],[]).append(f)
    result=[]
    for coin,items in groups.items():
        ordered=_ordered_by_position(items)
        # Partition into position-continuous paths.
        paths=[];path=[];expected=None
        for f in ordered:
            before=number(f['startPosition'])
            if f.get('_ambiguous_order') or expected is not None and before!=expected:
                if path:paths.append(path)
                path=[]
            if f.get('_ambiguous_order'):
                expected=None;continue
            path.append(f);expected=before+(number(f['sz']) if f['side']=='B' else -number(f['sz']))
        if path:paths.append(path)
        for path in paths:
            cost=None
            if number(path[0]['startPosition'])==0:cost=ZERO
            else:
                # First close establishes the cost at that point; preceding
                # fills must be additions of the same side to walk backwards.
                seed_index=next((i for i,f in enumerate(path) if number(f['startPosition'])*(number(f['sz']) if f['side']=='B' else -number(f['sz']))<0),None)
                if seed_index is not None:
                    f=path[seed_index];b=number(f['startPosition']);q=min(abs(b),number(f['sz']))
                    cost=number(f['px'])-number(f.get('closedPnl','0'))/q*(1 if b>0 else -1)
                    for earlier in reversed(path[:seed_index]):
                        b=number(earlier['startPosition']);q=number(earlier['sz'])
                        if not b:cost=ZERO;break
                        cost=((abs(b)+q)*cost-q*number(earlier['px']))/abs(b)
            if cost is None or cost<0:continue
            state=None;preceding=[];path_result=[];consistent=True;action_batch=[];batch_previous=None;batch_cost=None
            for f in path:
                t=int(f['time']);b=number(f['startPosition']);q=number(f['sz']);px=number(f['px'])
                delta=q if f['side']=='B' else -q;a=b+delta
                if b and cost<=0:break
                if action_batch and t-int(action_batch[0]['time']) > 3600000:
                    action_batch=[];batch_previous=None;batch_cost=None
                floating=b*(px-cost) if b else ZERO
                p=Position(coin,b,abs(b)*px,px,floating,None) if b else None
                if not action_batch:
                    batch_previous={'observed_ms':t-1,'positions':{coin:p.facts()} if p else {}}
                    batch_cost=cost
                hist={}  # Price/size rules do not require expensive history statistics.
                # Reprice unchanged exposure immediately before the next fill.
                events,state=evaluate_address(address=address,observed_ms=t,first_seen_ms=0,
                    positions={coin:p} if p else {},previous=state,markets={},history=hist,new_fills=[],rules=rules)
                path_result.extend(_price_cases(events,address,[f],cost,'before_fill_price',rules))
                close=min(abs(b),q) if b*delta<0 else ZERO
                if close:
                    expected_pnl=close*(px-cost)*(1 if b>0 else -1)
                    if abs(expected_pnl-number(f.get('closedPnl','0'))) > Decimal('0.10'):
                        # Fee is separate; an inconsistent closedPnl proves a
                        # missing cost-changing event. Do not continue that path.
                        consistent=False
                        break
                if b==0 or b*a<0:next_cost=px
                elif close:next_cost=cost
                else:next_cost=(abs(b)*cost+q*px)/abs(a)
                current=Position(coin,a,abs(a)*px,px,a*(px-next_cost),None) if a else None
                if state:state['observed_ms']=max(0,t-1)
                events,state=evaluate_address(address=address,observed_ms=t,first_seen_ms=0,
                    positions={coin:current} if current else {},previous=state,markets={},history=hist,new_fills=[f],rules=rules)
                path_result.extend(_price_cases(events,address,[f],cost,'after_fill_price',rules))
                action_batch.append(f)
                if batch_previous and len(action_batch)>1:
                    batch_events,_=evaluate_address(address=address,observed_ms=t,first_seen_ms=0,
                        positions={coin:current} if current else {},previous=batch_previous,
                        markets={},history={},new_fills=action_batch,rules=rules)
                    batch_events=[e for e in batch_events if 'loss_addition' in e.kinds]
                    path_result.extend(_price_cases(batch_events,address,action_batch,batch_cost,'hourly_loss_addition',rules))
                cost=next_cost if a else ZERO;preceding.append(f)
            if consistent:result.extend(path_result)
    return result


def _price_cases(events: list[Candidate], address: str, fills: list[dict], cost: Decimal, phase: str, rules: Rules) -> list[dict]:
    result=[]
    for candidate in events:
        event=asdict(candidate)
        event['kinds']=[k for k in event['kinds'] if k in {'large_profit','large_loss','profit_giveback','profit_to_loss','pnl_sign_flip','loss_addition'}]
        if not event['kinds']:continue
        event['evidence']['test_replay']={'mode':'execution_price_cost_reconstruction',
            'valuation_basis':'actual_execution_price_with_reconstructed_entry_cost',
            'cost_basis_usd':str(cost),'reference_execution_price':fills[-1]['px'],
            'start_ms':event['evidence'].get('news_change',{}).get('previous_observed_ms',int(fills[0]['time'])-1),
            'end_ms':int(fills[-1]['time']), 'phase':phase,'real_address':True,
            'purpose':'prelaunch_case_review_only','not_historical_mark_snapshot':True}
        event['key']='test-price:'+hashlib.sha256(encode({'address':address,'coin':event['coin'],'tid':fill_key(fills[-1]),'phase':phase,'kinds':event['kinds']}).encode()).hexdigest()
        event['test_scenarios']=case_scenarios(event);result.append(event)
    return result


def replay_cached_cases(store: NewsStore, *, reset: bool = False) -> dict[str, Any]:
    initialize_cases(store)
    reset_result = clear_test_cases(store) if reset else None
    if not reset:
        revalidate_test_cases(store)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute("SELECT key,payload FROM hl_news_metadata WHERE key LIKE 'test_fills:%'").fetchall()
    report={'addresses':len(rows),'cases':0,'failed':{},'reset':reset_result}
    for row in rows:
        address=row['key'].split(':',1)[1]
        try:
            fills=json.loads(row['payload'])['fills']
            cases=representative_cases(replay_fills(address,fills)+replay_execution_prices(address,fills)+replay_test_startup(address,fills))
            save_cases(store,cases);report['cases']+=len(cases)
            prune_replayed_cases(store,address,cases)
            print(encode({'address':address,'cases':len(cases),'mode':'cached_test_replay'}),flush=True)
        except Exception as exc:report['failed'][address]=str(exc)
    with connect_sqlite(store.path) as conn:
        all_cases=[json.loads(row[0]) for row in conn.execute('SELECT payload FROM hl_news_test_cases')]
    report['eligible_scenario_counts']={key:sum(key in case_scenarios(event) for event in all_cases)
                                        for key in PREVIEW_SCENARIOS}
    capped=prune_tested_cases(store)
    report['statistics_enriched']=enrich_cached_case_records(store)
    with connect_sqlite(store.path) as conn:
        all_cases=[json.loads(row[0]) for row in conn.execute('SELECT payload FROM hl_news_test_cases')]
    report['scenario_counts']={key:sum(key in case_scenarios(event) for event in all_cases)
                               for key in PREVIEW_SCENARIOS}
    report['cases_retained']=capped['retained']
    report['reviewed_cases_removed']=capped['removed']
    report['missing_scenarios']=[key for key,count in report['scenario_counts'].items() if not count]
    return report


def replay_test_startup(address: str, fills: list[dict], *, rules: Rules | None = None) -> list[dict]:
    """Real opening actions with an explicitly simulated historical monitor start.

    This tests startup behavior, not wallet creation age or our actual discovery.
    Only a first observed zero-position fill can establish the empty test baseline.
    """
    rules=rules or Rules()
    valid=sorted([f for f in fills if valid_test_fill(f)],key=lambda f:int(f['time']))
    if not valid:return []
    start=int(valid[0]['time'])
    grouped={}
    for f in valid:
        if int(f['time'])-start<=rules.new_address_window_ms:grouped.setdefault(f['coin'],[]).append(f)
    results=[]
    for coin,raw in grouped.items():
        ordered=_ordered_by_position(raw)
        if not ordered or number(ordered[0]['startPosition'])!=0 or any(f.get('_ambiguous_order') for f in ordered):continue
        expected=ZERO
        for f in ordered:
            if number(f['startPosition'])!=expected:break
            expected+=number(f['sz']) if f['side']=='B' else -number(f['sz'])
        else:
            last=ordered[-1];px=number(last['px']);t=int(last['time'])
            if not expected:continue
            p=Position(coin,expected,abs(expected)*px,px,ZERO,None)
            events,_=evaluate_address(address=address,observed_ms=t,first_seen_ms=start,
                positions={coin:p},previous={'observed_ms':start-1,'positions':{}},markets={},history={},new_fills=ordered,rules=rules)
            for candidate in events:
                if 'newly_observed_address_position' not in candidate.kinds:continue
                event=asdict(candidate);event['kinds']=['newly_observed_address_position']
                event['evidence']['position']['unrealized_pnl']=None
                event['evidence']['position']['mark']=None
                event['evidence']['test_replay']={'mode':'historical_monitor_start_simulation',
                    'simulated_monitor_start_ms':start,'simulated_monitor_start':True,
                    'not_wallet_creation_time':True,'not_actual_discovery_time':True,
                    'start_ms':start,'end_ms':t,'real_address':True,'real_fills':True,
                    'valuation_basis':'quantity_times_window_last_execution_price',
                    'purpose':'prelaunch_startup_rule_review_only'}
                event['key']='test-startup:'+hashlib.sha256(encode({'address':address,'coin':coin,'fills':[fill_key(f) for f in ordered]}).encode()).hexdigest()
                results.append(event)
    return results


def enrich_account_cases(store: NewsStore, *, limit: int = 20) -> dict[str, Any]:
    """Historical official portfolio PnL before the action; no future leaderboard."""
    rules=Rules();client=NewsClient();now=int(time.time()*1000)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute("SELECT payload FROM hl_news_test_cases WHERE json_extract(payload,'$.evidence.actions') IS NOT NULL ORDER BY observed_ms DESC").fetchall()
    groups={}
    for r in rows:
        e=json.loads(r[0])
        if e['evidence']['test_replay'].get('simulated_monitor_start'):continue
        action=e['evidence']['actions']
        if max(number(action['open_notional']),number(action['close_notional']))>=rules.action:
            groups.setdefault(e['addresses'][0],[]).append(e)
    report={'processed':0,'added':0,'failed':{}}
    for address,events in list(groups.items())[:limit]:
        try:
            cached=store.metadata('test_portfolio:'+address,{})
            data=cached.get('data')
            if not data:
                data=client._post_info({'type':'portfolio','user':address})
                store.save_global('test_portfolio:'+address,{'data':data,'fetched_ms':now},[])
            for event in events:
                cutoff=int(event['evidence']['actions']['first_fill_ms'])-1
                windows={}
                for window,values in data:
                    if window not in ('perpDay','perpWeek','perpMonth','perpAllTime'):continue
                    points=[p for p in values.get('pnlHistory',[]) if int(p[0])<=cutoff]
                    if not points:continue
                    stamp,pnl=max(points,key=lambda p:int(p[0]))
                    if number(pnl)>=rules.account_profit:
                        name={'perpDay':'day','perpWeek':'week','perpMonth':'month','perpAllTime':'allTime'}[window]
                        windows[name]={'pnl':str(pnl),'observed_ms':int(stamp),'rank':None,
                            'scope':'official_portfolio_window_change','history_sample_ms':int(stamp)}
                if not windows:continue
                event['evidence'].setdefault('history',{})['official_account_performance']={
                    'source':'hyperliquid_portfolio_historical','windows':windows}
                labels=event['evidence'].setdefault('highlights',[])
                if 'official_account_high_profit' not in labels:labels.append('official_account_high_profit')
                if 'highlighted_address_action' not in event['kinds']:event['kinds'].append('highlighted_address_action')
                event['evidence']['test_replay']['official_pnl_cutoff_ms']=cutoff
                event['test_scenarios']=case_scenarios(event)
                with connect_sqlite(store.path) as conn:
                    conn.execute('UPDATE hl_news_test_cases SET payload=? WHERE event_key=?',(encode(event),event['key']))
                report['added']+=1
            report['processed']+=1
            print(encode({'address':address,**report}),flush=True)
        except Exception as exc:report['failed'][address]=str(exc)
    return report
