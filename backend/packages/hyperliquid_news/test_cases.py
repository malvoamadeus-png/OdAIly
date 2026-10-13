"""Explicit pre-launch case mining. Never called by the normal monitor/dispatcher."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from packages.common.storage import connect_sqlite
from .client import NewsClient
from .detector import evaluate_address
from .history import _ordered_by_position, fill_key, perp_fill, summarize
from .models import Candidate, Position, Rules, ZERO, number
from .scenarios import SCENARIOS
from .store import NewsStore, encode


def case_scenarios(event: dict[str, Any]) -> list[str]:
    kinds = set(event['kinds'])
    labels = set(event['evidence'].get('highlights', []))
    return [s[0] for s in SCENARIOS if kinds.intersection(s[3]) and (not s[4] or labels.intersection(s[4]))]


def initialize_cases(store: NewsStore) -> None:
    with connect_sqlite(store.path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS hl_news_test_cases (
            event_key TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL, payload TEXT NOT NULL,
            preview TEXT, created_ms INTEGER NOT NULL)''')


def save_cases(store: NewsStore, events: list[dict[str, Any]]) -> None:
    initialize_cases(store)
    now = int(time.time()*1000)
    with connect_sqlite(store.path) as conn:
        for event in events:
            conn.execute('INSERT OR IGNORE INTO hl_news_test_cases VALUES(?,?,?,NULL,?)',
                         (event['key'], event['observed_ms'], encode(event), now))


def replay_fills(address: str, fills: list[dict[str, Any]], *, rules: Rules | None = None) -> list[dict[str, Any]]:
    """Reconstruct quantity/action facts only; no invented mark, PnL, OI or liquidation.

    Fill prices value quantities at the actual execution time. Unknown historical
    floating PnL remains omitted. Attributes are computed strictly before actions.
    One-hour action windows allow split orders; discontinuities break replay state.
    """
    rules = rules or Rules()
    unique = {fill_key(f): f for f in fills if perp_fill(f)}
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
        event['evidence']['history'] = history
        from .detector import highlights
        labels = highlights(history, event['coin'], rules)
        event['evidence']['highlights'] = labels
        if labels and max(number(event['evidence']['actions']['open_notional']),
                          number(event['evidence']['actions']['close_notional'])) >= rules.action:
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


def mine_cases(store: NewsStore, *, address_limit: int = 40, per_side: int = 12) -> dict[str, Any]:
    """Bounded sequential test scan; resumable cache separate from live history."""
    if not 1 <= address_limit <= 200 or not 1 <= per_side <= 50:
        raise ValueError('Invalid test mining budget')
    initialize_cases(store)
    client=NewsClient()
    now=int(time.time()*1000)
    from .discovery import leaderboard_candidates
    rows=client.leaderboard()
    discoveries=list(leaderboard_candidates(rows,per_side,now))
    selected=list(dict.fromkeys(a for a,_ in discoveries))
    # Mix positive and negative period leaders before using remaining candidates.
    mixed=[]
    for window in ('day','week','month','allTime'):
        pos=[a for a,s in discoveries if s['window']==window and s['side']=='profit']
        neg=[a for a,s in discoveries if s['window']==window and s['side']=='loss']
        for pair in zip(pos,neg):
            mixed.extend(pair)
    selected=list(dict.fromkeys(mixed+selected))[:address_limit]
    report={'mode':'prelaunch_case_mining','addresses':len(selected),'processed':0,'failed':{},'case_count':0,'coverage':{}}
    for address in selected:
        cache=store.metadata('test_fills:'+address,{})
        try:
            if cache.get('fetched_ms',0) > now-3600000:
                fills=cache['fills']
            else:
                fills=client._post_info({'type':'userFills','user':address,'aggregateByTime':False})
                if not isinstance(fills,list):raise ValueError('Unexpected fills')
                store.save_global('test_fills:'+address,{'fills':fills,'fetched_ms':now,'source':'userFills_latest_retained_sample'},[])
            cases=replay_fills(address,fills)
            save_cases(store,cases)
            report['processed']+=1
            report['case_count']+=len(cases)
            store.save_global('test_mining_progress',report,[])
            print(encode({'address':address,'fills':len(fills),'cases':len(cases),'processed':report['processed']}),flush=True)
        except Exception as exc:
            report['failed'][address]=str(exc)
    with connect_sqlite(store.path) as conn:
        all_cases=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM hl_news_test_cases')]
    report['coverage']={s[0]:sum(s[0] in case_scenarios(e) for e in all_cases) for s in SCENARIOS}
    store.save_global('test_mining_progress',report,[])
    return report


def preview_cases(store: NewsStore, *, limit: int = 15) -> dict[str, Any]:
    """Choose representative real cases and write review copy without tasks/push."""
    from packages.common.config import load_x_processing_settings
    from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
    from packages.x_processing.worker import XProcessingWorker
    from packages.x_processing.models import TaskRecord, HYPERLIQUID_SOURCE
    from .copy import source_url
    from .preview import generate_preview
    if not 1 <= limit <= 30:
        raise ValueError('Preview limit must be 1..30')
    initialize_cases(store)
    with connect_sqlite(store.path) as conn:
        rows=conn.execute('SELECT payload FROM hl_news_test_cases ORDER BY observed_ms DESC').fetchall()
    events=[json.loads(r[0]) for r in rows]
    settings=load_x_processing_settings()
    worker=XProcessingWorker(stage='write',repository=SQLiteXProcessingRepository(store.path),settings=settings)
    prompt=worker.repository.get_active_prompt('hyperliquid_writer')
    selected=[]; covered=set()
    for scenario in SCENARIOS:
        if scenario[0] in covered:continue
        match=next((e for e in events if scenario[0] in case_scenarios(e)),None)
        if match and match['key'] not in {e['key'] for e in selected}:
            selected.append(match);covered.update(case_scenarios(match))
    selected=selected[:limit]
    report={'selected':len(selected),'generated':0,'failed':{},'coverage':sorted(covered)}
    for event in selected:
        try:
            task=TaskRecord(id=0,source=HYPERLIQUID_SOURCE,source_item_id=event['key'],source_url=source_url(event),
                            title=None,content='',raw_payload=event)
            result=generate_preview(task=task,prompt=prompt,settings=settings,ai_client=worker._get_ai_client())
            store.save_global('editorial_preview:'+event['key'],result,[])
            report['generated']+=1
            print(encode({'event_key':event['key'],'scenarios':case_scenarios(event),'title':result['title']}),flush=True)
        except Exception as exc:report['failed'][event['key']]=str(exc)
    store.save_global('test_preview_progress',report,[])
    return report
