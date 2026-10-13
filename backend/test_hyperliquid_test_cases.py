from packages.hyperliquid_news.test_cases import replay_fills, save_cases
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
    assert next(s['count'] for s in test['scenarios'] if s['key']=='reversal')==1


def test_gap_and_future_history_do_not_create_high_win_rate():
    fills=[fill(0,2000),fill(3000,2000,'A',200,pnl='6000000'),fill(0,2000,'B',300)]
    events=replay_fills(A,fills)
    assert not any('asset_high_win_rate' in e['evidence']['highlights'] for e in events)


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
