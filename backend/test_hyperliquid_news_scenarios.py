"""End-to-end evidence coverage for every agreed news direction, fictional addresses."""
from dataclasses import asdict, replace
from decimal import Decimal as D

import pytest

from packages.hyperliquid_news.copy import format_news, writer_material
from packages.hyperliquid_news.detector import evaluate_divergence
from packages.hyperliquid_news.dispatch import EventDispatcher, fingerprint
from packages.hyperliquid_news.history import summarize
from packages.hyperliquid_news.models import Candidate, Rules
from packages.hyperliquid_news.search import search_material
from packages.hyperliquid_news.store import NewsStore
from packages.x_processing.models import DraftBrief
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
from test_hyperliquid_news import A, B, COIN, baseline, evaluate, fill, position, stats
from test_hyperliquid_news_pipeline import Pipeline

SCENARIOS = [
    'large_position', 'oi_share', 'market_cap_share', 'large_profit', 'large_loss',
    'win_open', 'win_add', 'win_reduce', 'win_close', 'win_reverse',
    'asset_profit_action', 'account_profit_action', 'official_profit_action',
    'historical_loss_action', 'losing_streak_action', 'large_reversal',
    'profit_giveback', 'profit_to_loss', 'loss_addition', 'realize_profit', 'realize_loss',
    'near_liquidation', 'actual_liquidation', 'address_divergence', 'new_address_position',
]


def scenario(name):
    p=position(); old=baseline(p); new=p; fills=[]; history={}; market={}; expected=name
    if name in ('large_position','oi_share','market_cap_share'):
        old=baseline(position(size='50',notional='5000000'))
        new=position(size='150',notional='15000000')
        market={COIN:{'oi_notional':'100000000','market_cap':'100000000'}}
    elif name in ('large_profit','large_loss'):
        old=baseline(position(pnl='1000000' if name=='large_profit' else '-1000000'))
        new=position(pnl='3000000' if name=='large_profit' else '-3000000')
    elif name.startswith('win_'):
        expected='highlighted_address_action';history=stats(profit='0',account='0')
        if name=='win_open':
            _,old=evaluate(None);new=position(size='30',notional='3000000');fills=[fill(size='30',stamp=200)]
        elif name=='win_add':
            new=position(size='140',notional='14000000');fills=[fill(before='100',size='40',stamp=200)]
        elif name=='win_reduce':
            new=position(size='60',notional='6000000');fills=[fill(before='100',size='40',side='A',stamp=200)]
        elif name=='win_close':
            new=None;fills=[fill(before='100',size='100',side='A',stamp=200)]
        else:
            new=position(size='-100');fills=[fill(before='100',size='200',side='A',stamp=200)]
    elif name.endswith('_action'):
        expected='highlighted_address_action';new=position(size='140',notional='14000000')
        fills=[fill(before='100',size='40',stamp=200)]
        if name=='asset_profit_action':history=stats(win='0.3',account='0')
        elif name=='account_profit_action':history=stats(win='0.3',profit='0')
        elif name=='official_profit_action':history={'official_account_performance':{'windows':{'week':{'pnl':'21000000','rank':1}}}}
        elif name=='historical_loss_action':history=stats(win='0.3',profit='-3000000',account='0')
        else:history=stats(win='0.3',profit='-1000',account='0',loss_streak=3)
    elif name=='large_reversal':
        new=position(size='-100');fills=[fill(before='100',size='200',side='A',stamp=200)]
    elif name in ('profit_giveback','profit_to_loss'):
        old=baseline(position(pnl='8000000'));new=position(pnl='2000000' if name=='profit_giveback' else '-100000')
    elif name=='loss_addition':
        old=baseline(position(pnl='-3000000'));new=position(size='140',notional='14000000',pnl='-4000000')
        fills=[fill(before='100',size='40',stamp=200)]
    elif name in ('realize_profit','realize_loss'):
        expected='large_realized_result';new=None
        fills=[fill(before='100',side='A',pnl='3000000' if name=='realize_profit' else '-3000000',stamp=200)]
    elif name=='near_liquidation':
        old=baseline(position(liquidation='80000'));new=position(liquidation='98000')
    elif name=='actual_liquidation':
        new=None;fills=[fill(before='100',side='A',stamp=200,liquidation={'liquidatedUser':A})]
    elif name=='address_divergence':
        events,_=evaluate_divergence(observed_ms=200,states={A:old,B:baseline(position(size='-100'))},
            histories={A:stats(),B:stats()},previous={},rules=Rules())
        return asdict(events[0]),old,expected
    else:
        expected='newly_observed_address_position';_,old=evaluate(None)
        new=position(size='30',notional='3000000');fills=[fill(size='30',stamp=200)]
    events,state=evaluate(new,previous=old,fills=fills,history=history,markets=market)
    assert len(events)==1
    return asdict(events[0]),state,expected


@pytest.mark.parametrize('name', SCENARIOS)
def test_every_direction_reaches_searchable_task_and_reader_material(tmp_path,name):
    event,state,expected=scenario(name)
    assert expected in event['kinds']
    repo=SQLiteXProcessingRepository(tmp_path/'db.sqlite');repo.init_schema()
    store=NewsStore(repo.path);store.initialize();store.discover(A,source={'kind':'fixture'},now_ms=200)
    store.commit_observation(address=A,now_ms=200,state=state,history=event['evidence'].get('history',{}),
                             coverage={},fills=[],candidates=[Candidate(**event)])
    client=Pipeline();result=EventDispatcher(store,pipeline_client=client).dispatch(limit=1)[0]
    assert result['status']=='queued'
    task=repo.get_task(result['task_id'])
    assert task.status=='judged' and task.source=='hyperliquid_news'
    assert task.content==search_material(event)[1] and A in task.content
    assert 'threshold_rules' not in task.content and 'first_observation' not in task.content
    assert repo.claim_task_by_id('search',task_id=task.id,worker_id='test') is not None
    facts=writer_material(event)['facts']
    assert facts.get('change_period') or name=='address_divergence'
    if name.startswith('win_'):
        assert facts['historical_highlights']['asset_win_rate']=='0.7'
        assert any(D(facts['actions'].get(k,'0'))>0 for k in ('open_long_notional','open_short_notional','close_long_notional','close_short_notional'))
    if name in ('profit_giveback','profit_to_loss'):
        assert facts['peak_floating_profit_usd']=='8000000'
    if name=='actual_liquidation':assert facts['actions']['liquidated_notional']=='10000000'
    if name=='address_divergence':assert facts['side_totals_usd']=={'long':'10000000','short':'10000000'}
    if name=='near_liquidation':assert facts['previous_price_move_to_liquidation_percent']=='20.0'
    final=format_news(DraftBrief(title='事件报道',content='某地址发生重要仓位变化。'),event)
    assert final.content.startswith('据Odaily监测，') and A in final.content


def test_pnl_changes_above_threshold_without_doubling_and_internal_dedup():
    old=baseline(position(pnl='6000000'))
    first,state=evaluate(position(pnl='8000000'),previous=old,stamp=300)
    second,state=evaluate(position(pnl='10000000'),previous=state,stamp=400)
    assert 'large_profit' in first[0].kinds and 'large_profit' in second[0].kinds
    assert fingerprint(asdict(first[0]))!=fingerprint(asdict(second[0]))
    assert evaluate(position(pnl='10000000'),previous=state,stamp=500)[0]==[]


def test_partial_profit_realization_is_not_passive_giveback():
    old=baseline(position(pnl='8000000'))
    events,_=evaluate(position(size='50',notional='5000000',pnl='4000000'),previous=old,
                      fills=[fill(before='100',size='50',side='A',pnl='4000000',stamp=200)])
    assert 'large_realized_result' in events[0].kinds
    assert 'profit_giveback' not in events[0].kinds


def test_snapshot_only_important_reduction_is_provided_to_writer():
    old=baseline(position(pnl='6000000'))
    events,_=evaluate(position(size='50',notional='5000000',pnl='3000000'),previous=old)
    assert 'large_position_action' in events[0].kinds
    facts=writer_material(asdict(events[0]))['facts']
    assert facts['net_position_change']['direction']=='减仓'
    assert 'profit_giveback' not in events[0].kinds
