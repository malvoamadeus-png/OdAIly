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
