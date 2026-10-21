from packages.hyperliquid_news.test_cases import replay_fills, save_cases, prune_replayed_cases
from packages.hyperliquid_news.dashboard import dashboard
from packages.hyperliquid_news.store import NewsStore
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository

A='0x'+'1'*40

def fill(before, size, side='B', stamp=100, pnl='0'):
    return dict(coin='ETH',startPosition=str(before),sz=str(size),side=side,time=stamp,tid=stamp,px='3000',closedPnl=pnl,fee='0',feeToken='USDC')


def test_replay_real_actions_does_not_invent_pnl_or_first_seen():
    events=replay_fills(A,[fill(2000,4000,'A')])
    assert any('large_reversal' in e['kinds'] for e in events)
    assert all(e['evidence']['position']['unrealized_pnl'] is None for e in events)
    assert all('newly_observed_address_position' not in e['kinds'] for e in events)
    assert all(not set(e['kinds']) & {'large_loss','large_profit','near_liquidation'} for e in events)


def test_test_cases_never_enter_candidates_or_live_state(tmp_path):
    path=tmp_path/'test.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    events=replay_fills(A,[fill(2000,4000,'A')]);save_cases(store,events);save_cases(store,events)
    assert not store.candidates() and store.load(A) is None
    live=dashboard(store,scenario='reversal');test=dashboard(store,scenario='reversal',dataset='test')
    assert live['events']==[] and len(test['events'])==1
    assert test['events'][0]['pipeline']['task_id'] is None
    assert not any(s['key']=='reversal' for s in test['scenarios'])


def test_reset_cases_only_clears_test_rows_and_previews(tmp_path):
    from packages.hyperliquid_news.test_cases import clear_test_cases

    path=tmp_path/'reset.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    event=replay_fills(A,[fill(2000,4000,'A')])[0]
    save_cases(store,[event])
    cache={'fills':[fill(0,1000)]}
    store.save_global('test_fills:'+A,cache,[])
    store.save_global('editorial_preview:'+event['key'],{'title':'old'},[])
    store.save_global('editorial_preview:live-event',{'title':'live'},[])
    from packages.hyperliquid_news.models import Candidate
    live=Candidate('live-event',1,(A,),'ETH',('large_position',),{})
    store.commit_observation(address=A,now_ms=1,state={},history={},coverage={},fills=[],candidates=[live])
    removed=clear_test_cases(store)
    assert removed=={'cases_removed':1,'previews_removed':1}
    assert dashboard(store,dataset='test')['events']==[]
    assert store.metadata('test_fills:'+A,{})==cache
    assert store.metadata('editorial_preview:live-event',{})=={'title':'live'}
    assert len(store.candidates())==1 and store.candidates()[0]['key']=='live-event'


def test_cached_replay_removes_reviewed_cases_and_keeps_unresearched(tmp_path, monkeypatch):
    from packages.common.storage import connect_sqlite
    from packages.hyperliquid_news import test_cases

    path=tmp_path/'replay-cap.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    events={}
    for index in range(6):
        address='0x'+f'{index+2:040x}'
        store.save_global('test_fills:'+address,{'fills':[]},[])
        events[address]={
            'key':f'test:large-loss-{index}','observed_ms':index,'addresses':[address],
            'coin':'ETH','kinds':['newly_observed_address_position'],'evidence':{
                'position':{'size':'-10','notional':'10000000','unrealized_pnl':'-10000000'},
                'previous_reported_floating_pnl':'0','actions':{},'highlights':[],
                'test_replay':{'real_address':True}},
        }
    monkeypatch.setattr(test_cases,'replay_fills',lambda address,fills:[events[address]])
    monkeypatch.setattr(test_cases,'replay_execution_prices',lambda address,fills:[])
    monkeypatch.setattr(test_cases,'replay_test_startup',lambda address,fills:[])
    for event in events.values():
        store.save_global('editorial_preview:'+event['key'],{'title':'stale'},[])

    report=test_cases.replay_cached_cases(store,reset=True)

    with connect_sqlite(path) as conn:
        rows=conn.execute('SELECT event_key FROM hl_news_test_cases').fetchall()
        previews=conn.execute("SELECT key FROM hl_news_metadata WHERE key LIKE 'editorial_preview:%'").fetchall()
    assert len(rows)==6
    assert len(previews)==0
    assert report['cases_retained']==6
    assert report['reviewed_cases_removed']==0
    assert report['scenario_counts']['new_address']==6

    weaker={
        'key':'test:large-loss-weaker','observed_ms':10,'addresses':['0x'+f'{8:040x}'],
        'coin':'ETH','kinds':['large_loss'],'evidence':{
            'position':{'size':'-10','notional':'10000000','unrealized_pnl':'-6000000'},
            'previous_reported_floating_pnl':'0','actions':{},'highlights':[],
            'test_replay':{'real_address':True}},
    }
    test_cases.save_cases(store,[weaker])
    store.save_global('editorial_preview:'+weaker['key'],{'title':'unselected'},[])
    assert test_cases.prune_tested_cases(store)=={'retained':6,'removed':1,'previews_removed':1}


def test_cached_case_record_uses_only_fills_before_event(tmp_path):
    import json
    from packages.common.storage import connect_sqlite
    from packages.hyperliquid_news.copy import footer
    from packages.hyperliquid_news.test_cases import enrich_cached_case_records

    path=tmp_path/'record-enrichment.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    fills=[fill(0,1000,stamp=100),fill(1000,1000,'A',stamp=200,pnl='500')]
    store.save_global('test_fills:'+A,{'fills':fills},[])
    event={
        'key':'test-price:record','observed_ms':300,'addresses':[A],'coin':'ETH',
        'kinds':['large_loss'],'evidence':{
            'position':{'size':'-1000','notional':'3000000','unrealized_pnl':'-6000000'},
            'previous_reported_floating_pnl':'-1000000','history':{},'actions':{},
            'test_replay':{'real_address':True,'phase':'after_fill_price'}},
    }
    save_cases(store,[event])

    assert enrich_cached_case_records(store)==1
    with connect_sqlite(path) as conn:
        stored=json.loads(conn.execute('SELECT payload FROM hl_news_test_cases').fetchone()[0])
    history=stored['evidence']['history']
    assert history['account_closed_pnl_less_fees']=='500'
    assert history['account_rounds']==1 and history['account_win_rate']=='1'
    assert '地址统计期合约交易盈亏' not in footer(stored)


def test_gap_and_future_history_do_not_create_high_win_rate():
    fills=[fill(0,2000),fill(3000,2000,'A',200,pnl='6000000'),fill(0,2000,'B',300)]
    events=replay_fills(A,fills)
    assert not any('asset_high_win_rate' in e['evidence']['highlights'] for e in events)


def test_asset_high_profit_requires_pre_action_30_day_pnl():
    from packages.hyperliquid_news.detector import highlights
    from packages.hyperliquid_news.models import Rules

    history={'assets':{'ETH':{'closed_pnl_less_fees':'9000000'}}}
    assert 'asset_high_profit' not in highlights(history,'ETH',Rules())
    history['assets']['ETH']['last_30d_closed_pnl_less_fees']='5000001'
    assert 'asset_high_profit' in highlights(history,'ETH',Rules())


def test_asset_high_profit_thresholds_and_operation_amount_are_asset_specific():
    from decimal import Decimal
    from packages.hyperliquid_news.detector import action_highlights, highlights
    from packages.hyperliquid_news.models import Rules

    rules = Rules()
    assert rules.asset_profit_line('BTC') == Decimal('3000000')
    assert rules.asset_profit_line('ETH') == Decimal('3000000')
    assert rules.asset_profit_line('SOL') == Decimal('1000000')
    assert 'asset_high_profit' not in highlights(
        {'assets': {'SOL': {'last_30d_closed_pnl_less_fees': '999999'}}}, 'SOL', rules)
    assert 'asset_high_profit' in highlights(
        {'assets': {'SOL': {'last_30d_closed_pnl_less_fees': '1000000'}}}, 'SOL', rules)
    assert action_highlights(['asset_high_profit'], Decimal('1999999'), rules) == []
    assert action_highlights(['asset_high_profit'], Decimal('2000000'), rules) == ['asset_high_profit']


def test_requested_asset_pnl_reversal_and_sign_flip_titles():
    from packages.hyperliquid_news.copy import scenario_title

    loss={
        'coin':'NEAR','kinds':['large_loss'],'evidence':{
            'position':{'size':'-100','notional':'17410000','unrealized_pnl':'-4000000'},
            'previous_reported_floating_pnl':'-1000000'}
    }
    assert scenario_title(loss)=='某地址持有的NEAR空单浮亏扩大至400 万美元'
    reversal={
        'coin':'BTC','kinds':['large_reversal'],'evidence':{
            'previous_position':{'size':'1'},'position':{'size':'-1'},
            'actions':{'close_long_notional':'5000000','open_short_notional':'6000000'}}
    }
    assert scenario_title(reversal)=='某地址平仓500 万美元BTC多单并开仓600 万美元BTC空单'
    flip={
        'coin':'SOL','kinds':['pnl_sign_flip'],'evidence':{
            'previous_position':{'unrealized_pnl':'-2300000'},
            'position':{'size':'1','notional':'10000000','unrealized_pnl':'3400000'}}
    }
    assert scenario_title(flip)=='浮亏230 万美元转为浮盈340 万美元，某地址当前持有价值1,000 万美元SOL多单'


def test_all_news_bodies_start_with_some_address_not_that_address():
    from packages.hyperliquid_news.copy import format_news
    from packages.x_processing.models import DraftBrief

    event = {
        'key': 'test-reversal', 'observed_ms': 1, 'addresses': [A], 'coin': 'BTC',
        'kinds': ['large_reversal'], 'evidence': {
            'previous_position': {'size': '1'}, 'position': {'size': '-1'},
            'actions': {'close_long_notional': '5000000', 'open_short_notional': '6000000'},
        },
    }
    draft = DraftBrief(title='反手', content='该地址平仓500万美元BTC多单并开仓600万美元BTC空单。')
    result = format_news(draft, event)
    assert result.content.startswith('Odaily星球日报讯 据Odaily监测，某地址')
    assert '该地址' not in result.content


def test_representative_case_collapses_same_address_asset_scenario():
    from packages.hyperliquid_news.test_cases import representative_cases

    def event(key, pnl):
        return {
            'key':key,'observed_ms':int(key),'addresses':[A],'coin':'ETH',
            'kinds':['loss_addition'],
            'evidence':{'highlights':[], 'previous_position':{'unrealized_pnl':str(pnl)},
                        'actions':{'open_notional':'3000000'},'history':{}},
        }
    selected=representative_cases([event('1',-1000000),event('2',-9000000)])
    assert [e['key'] for e in selected]==['2']


def test_preview_selection_caps_each_unresearched_scenario_at_five():
    from packages.hyperliquid_news.test_cases import PREVIEW_SCENARIOS, case_scenarios, select_preview_cases

    events=[]
    for index in range(9):
        current_pnl = -5000000 - index * 1000000
        events.append({
        'key':str(index),'observed_ms':index,'addresses':[f'0x{index:040x}'],'coin':'ETH',
            'kinds':['newly_observed_address_position'],'evidence':{
                'position':{'unrealized_pnl':str(current_pnl)},
                'previous_reported_floating_pnl':'-1000000','actions':{},'highlights':[]}})
    selected=select_preview_cases(events)
    assert len(selected)==5
    assert all('new_address' in case_scenarios(event) for event in selected)
    assert all(not set(case_scenarios(event)).intersection({'large_pnl','loss_addition'}) for event in selected)
    assert 'large_pnl' not in PREVIEW_SCENARIOS


def test_large_pnl_case_filter_rejects_contraction_but_keeps_loss_addition():
    from packages.hyperliquid_news.test_cases import case_scenarios

    event={'coin':'ZEC','kinds':['large_loss','loss_addition'],'evidence':{
        'position':{'unrealized_pnl':'-24500000'},
        'previous_reported_floating_pnl':'-24840000',
        'actions':{'open_notional':'8340000'},'highlights':[]}}
    assert 'large_pnl' not in case_scenarios(event)
    assert 'loss_addition' in case_scenarios(event)


def test_reversal_writer_material_omits_interval_account_trade_record():
    from packages.hyperliquid_news.copy import writer_material, footer

    event={'key':'reversal','observed_ms':200,'addresses':[A],'coin':'BTC','kinds':['large_reversal'],
        'evidence':{'previous_position':{'size':'1','notional':'1000000'},
            'position':{'size':'-1','notional':'1000000'},'actions':{},
            'history':{'fills':8,'fee_currency_complete':True,'account_closed_pnl_less_fees':'1200000',
                'account_rounds':4,'account_wins':3,'account_win_rate':'0.75',
                'first_observed_fill_ms':100,'last_observed_fill_ms':150}}}
    assert 'address_trading_record' not in writer_material(event)['facts']
    assert '地址统计期合约交易盈亏' not in footer(event)


def test_preview_selection_excludes_approved_directions():
    from packages.hyperliquid_news.test_cases import select_preview_cases

    events=[]
    for key, amount, pnl in [('first',3000000,-24600000),('second',8000000,-24500000)]:
        events.append({
            'key':key,'observed_ms':1 if key=='first' else 2,'addresses':[A],'coin':'ZEC',
            'kinds':['large_loss','loss_addition'],
            'evidence':{'position':{'unrealized_pnl':str(pnl)},
                        'previous_position':{'unrealized_pnl':'-24800000'},
                        'actions':{'open_notional':str(amount)},'highlights':[]},
        })
    selected=select_preview_cases(events)
    assert selected==[]


def test_replay_prunes_stale_per_fill_cases_for_address(tmp_path):
    path=tmp_path/'prune.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    from packages.hyperliquid_news.test_cases import representative_cases
    base=replay_fills(A,[fill(2000,4000,'A',stamp=1)])
    duplicate={**base[0],'key':base[0]['key']+':old-fill-row','observed_ms':2}
    old=[base[0],duplicate]
    save_cases(store,old)
    keep=representative_cases(old)
    assert len(old)>len(keep)==1
    removed=prune_replayed_cases(store,A,keep)
    assert removed==len(old)-len(keep)
    assert len(dashboard(store,dataset='test')['events'])==len(keep)


def test_split_reverse_within_one_hour():
    events=replay_fills(A,[fill(2000,2000,'A'),fill(0,2000,'A',101)])
    assert any('large_reversal' in e['kinds'] for e in events)


def test_price_replay_giveback_from_continuous_cost_and_real_prices():
    from packages.hyperliquid_news.test_cases import replay_execution_prices
    fills=[fill(0,5000,stamp=1),{**fill(5000,1,stamp=2),'px':'5000'},
           {**fill(5001,1,stamp=3),'px':'3500'}]
    events=replay_execution_prices(A,fills)
    assert any('profit_giveback' in e['kinds'] for e in events)
    assert all(e['evidence']['test_replay']['not_historical_mark_snapshot'] for e in events)


def test_price_replay_inconsistent_closed_pnl_breaks_path():
    from packages.hyperliquid_news.test_cases import replay_execution_prices
    fills=[fill(0,5000,stamp=1),fill(5000,1,'A',stamp=2,pnl='999'),
           {**fill(4999,1,stamp=3),'px':'10000'}]
    assert replay_execution_prices(A,fills)==[]


def test_startup_simulation_is_explicit_not_wallet_age():
    from packages.hyperliquid_news.test_cases import replay_test_startup
    cases=replay_test_startup(A,[fill(0,2000)])
    assert len(cases)==1
    proof=cases[0]['evidence']['test_replay']
    assert proof['simulated_monitor_start'] and proof['not_wallet_creation_time'] and proof['not_actual_discovery_time']
    assert replay_test_startup(A,[fill(1000,2000)])==[]


def test_split_loss_addition_uses_real_cost_and_original_threshold():
    from packages.hyperliquid_news.test_cases import replay_execution_prices
    fills=[fill(0,10000,stamp=1),{**fill(10000,1000,stamp=4000000),'px':'2000'},
           {**fill(11000,1000,stamp=4000100),'px':'2000'}]
    cases=replay_execution_prices(A,fills)
    assert any('loss_addition' in e['kinds'] for e in cases)
    e=next(e for e in cases if 'loss_addition' in e['kinds'])
    assert e['evidence']['previous_position']['unrealized_pnl']=='-10000000'


def test_official_account_attribute_uses_only_pre_action_samples(tmp_path, monkeypatch):
    from packages.hyperliquid_news.test_cases import enrich_account_cases
    path=tmp_path/'account.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    events=replay_fills(A,[fill(2000,4000,'A',stamp=100)])
    save_cases(store,events)
    store.save_global('test_portfolio:'+A,{'data':[['perpAllTime',{'pnlHistory':[[50,'21000000'],[200,'99000000']]}]]},[])
    assert enrich_account_cases(store,limit=1)['added']==1
    data=dashboard(store,scenario='account_profit',dataset='test')
    windows=data['events'][0]['evidence']['history']['official_account_performance']['windows']
    assert windows['allTime']['pnl']=='21000000' and windows['allTime']['observed_ms']==50
    assert data['events'][0]['evidence']['test_replay']['official_pnl_cutoff_ms']==99
    # The same positive result occurring after an action must never qualify it.
    other=NewsStore(tmp_path/'future.sqlite');SQLiteXProcessingRepository(other.path).init_schema();other.initialize()
    save_cases(other,events)
    other.save_global('test_portfolio:'+A,{'data':[['perpAllTime',{'pnlHistory':[[200,'99000000']]}]]},[])
    assert enrich_account_cases(other,limit=1)['added']==0
    assert dashboard(other,scenario='account_profit',dataset='test')['events']==[]


def test_outcome_settlements_do_not_abort_perp_case_replay():
    cases=replay_fills(A,[fill(2000,4000,'A'),{**fill(0,1,stamp=200),'coin':'#123','px':'0'}])
    assert any('large_reversal' in e['kinds'] for e in cases)


def test_refresh_test_case_replaces_truncated_history_and_invalidates_preview(tmp_path):
    from packages.common.storage import connect_sqlite
    from packages.hyperliquid_news.test_cases import revalidate_test_cases
    path=tmp_path/'refresh.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    event=replay_fills(A,[fill(0,4000)])[0]
    event['kinds'].append('highlighted_address_action')
    event['evidence']['highlights']=['asset_high_win_rate']
    event['evidence']['history']={'assets':{'ETH':{'rounds':1,'win_rate':'1'}}}
    save_cases(store,[event])
    store.save_global('editorial_preview:'+event['key'],{'title':'old'},[])
    assert revalidate_test_cases(store)['updated']==1
    data=dashboard(store,dataset='test')['events'][0]
    assert 'asset_high_win_rate' not in data['evidence']['highlights']
    assert 'highlighted_address_action' not in data['kinds']
    assert 'editorial_preview' not in data
    event['evidence']['history']['assets']['ETH']['rounds']=2
    event['kinds'].remove('highlighted_address_action');event['evidence']['highlights']=[]
    save_cases(store,[event])
    assert dashboard(store,dataset='test')['events'][0]['evidence']['history']['assets']['ETH']['rounds']==2


def test_revalidate_removes_asset_profit_without_qualifying_30_day_value(tmp_path):
    from packages.common.storage import connect_sqlite
    from packages.hyperliquid_news.test_cases import revalidate_test_cases
    path=tmp_path/'monthly-profit.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    event=replay_fills(A,[fill(2000,4000,'A',stamp=100)])[0]
    event['kinds'].append('highlighted_address_action')
    event['evidence']['highlights']=['asset_high_profit']
    event['evidence']['history']={'assets':{'ETH':{'closed_pnl_less_fees':'9000000'}}}
    save_cases(store,[event])
    store.save_global('editorial_preview:'+event['key'],{'title':'old'},[])
    assert revalidate_test_cases(store)['updated']==1
    data=dashboard(store,dataset='test')['events'][0]
    assert 'asset_high_profit' not in data['evidence']['highlights']
    assert 'highlighted_address_action' not in data['kinds']
    with connect_sqlite(path) as conn:
        assert conn.execute('SELECT 1 FROM hl_news_metadata WHERE key=?',('editorial_preview:'+event['key'],)).fetchone() is None


def test_mining_uses_paginated_history_not_latest_fill_slice(tmp_path, monkeypatch):
    from packages.hyperliquid_news import test_cases
    path=tmp_path/'mining.sqlite';SQLiteXProcessingRepository(path).init_schema()
    store=NewsStore(path);store.initialize()
    fills=[fill(0,4000,stamp=100),fill(4000,4000,'A',stamp=4000000,pnl='100'),
           fill(0,4000,stamp=8000000)]
    class Client:
        def history(self,address,start,end):
            assert address==A
            return fills,{'pages':2,'pagination_complete':True}
        def _post_info(self,payload):
            return fills[1:]
    monkeypatch.setattr(test_cases,'NewsClient',Client)
    result=test_cases.mine_cases(store,addresses=[A])
    assert result['processed']==1
    cache=store.metadata('test_fills:'+A,{})
    assert cache['fills'][0]['startPosition']=='0'
    assert cache['coverage']['pages']==2
