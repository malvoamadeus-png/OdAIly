"""Offline rollout tests: discovery, scheduling, restart and direction isolation."""
import json

import pytest

from packages.common.storage import connect_sqlite
from packages.hyperliquid_news.account_monitor import (
    AccountProfitMonitor, DAY, HOUR, portfolio_performance, qualifying_leaderboard)
from packages.hyperliquid_news.dispatch import EventDispatcher
from packages.hyperliquid_news.models import Rules
from packages.hyperliquid_news.production import production_allowed
from packages.hyperliquid_news.store import NewsStore
from packages.hyperliquid_news.copy import writer_material
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository

A = '0x' + '1'*40
T = 2*DAY


def board(address=A, pnl='21000000'):
    return {'ethAddress': address, 'windowPerformances': [['allTime', {'pnl': pnl}]]}


class Client:
    now = T
    size = '100'
    pnl = '21000000'
    fills = []
    fail_history = False
    portfolio_calls = 0
    history_calls = 0

    def leaderboard(self): return [board()]
    def dex_names(self): return ['']
    def markets(self, dex): return {'ETH': {'dex': '', 'mark': '100000'}}
    def recent_trades(self, coin): return []
    def portfolio(self, address):
        self.portfolio_calls += 1
        return [['perpAllTime', {'pnlHistory': [[self.now, self.pnl]]}]]
    def positions(self, address, dex):
        return {'assetPositions': [{'position': {'coin': 'ETH', 'szi': self.size,
            'positionValue': str(int(self.size)*100000), 'unrealizedPnl': '6000000',
            'liquidationPx': None}}] if self.size != '0' else []}
    def history(self, address, start_ms, end_ms, **kwargs):
        self.history_calls += 1
        if self.fail_history: raise RuntimeError('unavailable')
        return [f for f in self.fills if start_ms <= f['time'] <= end_ms], {
            'pagination_complete': True, 'next_start_ms': start_ms}


def setup(tmp_path):
    path = tmp_path/'offline.sqlite'
    repo = SQLiteXProcessingRepository(path); repo.init_schema()
    store = NewsStore(path); store.initialize()
    store.save_global('production_account_profit', {'enabled': True, 'started_ms': T,
        'enabled_scenarios': ['account_profit']}, [])
    client = Client()
    return store, client, AccountProfitMonitor(store, client)


def run(monitor, client, now):
    client.now = now
    return monitor.run_once(now_ms=now)


def close(client, now, pnl='1000'):
    client.size = '60'
    client.fills = [{'coin': 'ETH', 'tid': 1, 'time': now, 'startPosition': '100',
        'sz': '40', 'px': '100000', 'side': 'A', 'closedPnl': pnl, 'fee': '0', 'feeToken': 'USDC'}]


def test_all_returned_threshold_addresses_not_top_n():
    rows = [board('0x'+format(i, '040x')) for i in range(80)] + [board(pnl='19999999')]
    found = list(qualifying_leaderboard(rows, Rules(), T))
    assert len(found) == 80
    assert len({a for a, _ in found}) == 80


def test_only_fresh_perpetual_profit_windows_are_qualification():
    payload = [['allTime', {'pnlHistory': [[T, '999999999']]}],
               ['perpWeek', {'pnlHistory': [[T-DAY-1, '99999999']]}],
               ['perpAllTime', {'pnlHistory': [[T, '21000000']]}]]
    assert set(portfolio_performance(payload, T)['windows']) == {'allTime'}
    with pytest.raises(ValueError): portfolio_performance(payload[:2], T)


def test_pool_can_expand_and_archives_inactive_without_deleting_other_sources(tmp_path):
    store = NewsStore(tmp_path/'offline.sqlite'); store.initialize()
    assert store.discover(A, source={'kind': 'manual'}, now_ms=T, limit=1)
    b = '0x'+'2'*40
    assert not store.discover(b, source={'kind': 'manual'}, now_ms=T, limit=1)
    assert store.discover(b, source={'kind': 'large_observed_trade'}, now_ms=T, limit=None)
    store.schedule_account(b, now_ms=T)
    store.update_account_schedule(b, now_ms=T, qualified=False, tier='unqualified', interval_ms=DAY)
    assert store.account_due(T, 10) == []
    assert len(store.addresses(100)) == 2


def test_discovery_is_not_a_report_and_active_actions_are_account_only(tmp_path):
    store, client, monitor = setup(tmp_path)
    report = run(monitor, client, T)
    assert report['candidates'] == 0 and client.history_calls == 0
    assert report['pool']['tiers'] == {'active': 1}
    close(client, T+180000)
    report = run(monitor, client, T+180000)
    assert report['candidates'] == 1
    event = store.candidates()[0]
    assert event['kinds'] == ['highlighted_address_action']
    assert event['evidence']['highlights'] == ['official_account_high_profit']
    material = writer_material(event)
    assert 'historical_highlights' in material['facts']
    assert 'floating_pnl_usd' not in material['facts']['position']
    assert client.portfolio_calls == 2  # qualification plus pre-report confirmation
    assert production_allowed(event, store.metadata('production_account_profit', None))
    # Restart retains schedule, baseline and fill dedup.
    restarted = AccountProfitMonitor(NewsStore(store.path), client)
    run(restarted, client, T+360000)
    assert len(store.candidates()) == 1


def test_pre_report_recheck_below_threshold_suppresses_action_and_cools_pool(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    close(client, T+180000)
    client.pnl = '19000000'
    report = run(monitor, client, T+180000)
    assert report['candidates'] == 0
    assert report['pool']['tiers'] == {'unqualified': 1}


def test_requalification_does_not_report_changes_during_inactive_period(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    close(client, T+180000); client.pnl = '0'
    run(monitor, client, T+180000)
    client.pnl = '21000000'; client.size = '10'
    run(monitor, client, T+DAY+180000)
    assert store.candidates() == []
    assert store.load(A)['state']['positions']['ETH']['size'] == '10'


def test_failed_history_preserves_baseline_and_retries_without_losing_action(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    close(client, T+180000); client.fail_history = True
    report = run(monitor, client, T+180000)
    assert A in report['failed']
    assert store.load(A)['state']['observed_ms'] == T
    client.fail_history = False
    run(monitor, client, T+480000)
    assert len(store.candidates()) == 1


def test_probe_slot_and_due_order_prevent_hot_and_new_candidates_starving_each_other(tmp_path):
    store = NewsStore(tmp_path/'offline.sqlite'); store.initialize()
    for i in range(12):
        a = '0x'+format(i, '040x')
        store.discover(a, source={'kind': 'manual'}, now_ms=T, limit=None)
        store.schedule_account(a, now_ms=T)
        if i < 10:
            store.update_account_schedule(a, now_ms=T-1000-i, qualified=True, tier='active', interval_ms=1)
    due = store.account_due(T, 3)
    assert [r['qualified'] for r in due] == [1, 1, 0]
    assert due[0]['address'].endswith('9')


def test_dispatch_sql_isolates_launch_epoch_and_direction_before_batch_limit(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T); close(client, T+180000); run(monitor, client, T+180000)
    event = store.candidates()[0]
    with connect_sqlite(store.path) as conn:
        for i in range(510):
            other = {**event, 'key': 'other-'+str(i), 'observed_ms': T-1}
            conn.execute('INSERT INTO hl_news_candidates VALUES(?,?,?,?)',
                (other['key'], other['observed_ms'], 'ETH', json.dumps(other)))
    class Pipeline:
        def __init__(self): self.jobs = []
        def submit_job(self, **kwargs): self.jobs.append(kwargs)
    pipeline = Pipeline()
    result = EventDispatcher(store, pipeline_client=pipeline).dispatch(limit=10)
    assert len(result) == 1 and result[0]['status'] == 'queued'
    assert len(pipeline.jobs) == 1
    assert EventDispatcher(store, pipeline_client=pipeline).dispatch(limit=10) == []
    store.save_global('production_account_profit', {'enabled': False, 'started_ms': T}, [])
    assert EventDispatcher(store, pipeline_client=pipeline)._create_task(event)['status'] == 'direction_disabled'


def test_production_gate_rejects_tests_other_directions_and_stale_profit(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T); close(client, T+180000); run(monitor, client, T+180000)
    e = store.candidates()[0]; policy = store.metadata('production_account_profit', None)
    assert not production_allowed({**e, 'key': 'test:abc'}, policy)
    assert not production_allowed({**e, 'kinds': ['large_position_action']}, policy)
    e['evidence']['history']['official_account_performance']['fetched_ms'] -= HOUR+1
    assert not production_allowed(e, policy)


def test_worker_requires_explicit_rollout(tmp_path):
    store = NewsStore(tmp_path/'offline.sqlite')
    with pytest.raises(ValueError, match='Enable'):
        AccountProfitMonitor(store, Client()).run_once(now_ms=T)


def test_equal_sized_consecutive_reductions_are_distinct_news(tmp_path):
    from packages.hyperliquid_news.dispatch import fingerprint
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    close(client, T+180000); run(monitor, client, T+180000)
    first = store.candidates()[0]
    client.size = '20'
    client.fills = [{**client.fills[0], 'tid': 2, 'time': T+360000, 'startPosition': '60'}]
    run(monitor, client, T+360000)
    second = store.candidates()[1]
    assert fingerprint(first) != fingerprint(second)


def test_partial_pages_resume_before_any_action_is_published(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    close(client, T+180000)
    original = client.history
    def partial(address, start_ms, end_ms, **kwargs):
        rows, coverage = original(address, start_ms, end_ms, **kwargs)
        return rows, {**coverage, 'pagination_complete': False, 'next_start_ms': rows[-1]['time']}
    client.history = partial
    report = run(monitor, client, T+180000)
    assert report['incomplete_history'] == [A]
    assert store.load(A)['state']['observed_ms'] == T
    assert not store.candidates()
    client.history = original
    run(monitor, client, T+210000)
    assert len(store.candidates()) == 1
    assert store.candidates()[0]['evidence']['actions']['close_notional'] == '4000000'


def test_unchanged_position_uses_cheap_checks_but_periodic_fills_catch_roundtrip(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    run(monitor, client, T+180000)
    assert client.history_calls == 0
    client.fills = [
        {'coin': 'ETH', 'tid': 1, 'time': T+200000, 'startPosition': '100',
         'sz': '40', 'px': '100000', 'side': 'A', 'closedPnl': '1000', 'fee': '0', 'feeToken': 'USDC'},
        {'coin': 'ETH', 'tid': 2, 'time': T+210000, 'startPosition': '60',
         'sz': '40', 'px': '100000', 'side': 'B', 'closedPnl': '0', 'fee': '0', 'feeToken': 'USDC'}]
    run(monitor, client, T+900000)
    assert client.history_calls == 1
    assert len(store.candidates()) == 1
    assert store.candidates()[0]['evidence']['actions']['close_notional'] == '4000000'


def enable_loss(store):
    policy = store.metadata('production_account_profit', {})
    policy.update(enabled_scenarios=['account_profit','historical_loss'], loss_started_ms=T)
    store.save_global('production_account_profit',policy,[])
    return policy


def test_shared_monitor_discovers_total_loss_and_preserves_profit(tmp_path):
    store,client,monitor=setup(tmp_path)
    policy=enable_loss(store)
    client.pnl='-25000000'
    client.leaderboard=lambda:[board(pnl='-25000000')]
    run(monitor,client,T)
    assert not store.candidates()
    close(client,T+180000)
    run(monitor,client,T+180000)
    event=store.candidates()[0]
    assert event['evidence']['production_direction']=='historical_loss'
    assert event['evidence']['highlights']==['historical_loss']
    assert production_allowed(event,policy)
    assert writer_material(event)['facts']['historical_highlights']['account_period_loss']==[
        {'period':'累计','loss_usd':'25000000'}]
    assert not production_allowed(event,{**policy,'enabled_scenarios':['account_profit']})
    assert not production_allowed(event,{**policy,'loss_started_ms':T+180001})
    assert not production_allowed({**event,'evidence':{**event['evidence'],'net_action_notional_usd':'2999999'}},policy)


def test_total_loss_excludes_finite_period_only_and_curve_declines(tmp_path):
    from packages.hyperliquid_news.account_monitor import qualified
    from packages.hyperliquid_news.account_loss import loss_qualified
    assert not loss_qualified({'windows':{'month':{'pnl':'-99999999'}}},Rules())
    assert not qualified({'windows':{'allTime':{'pnl':'-19999999'}}},Rules(),include_loss=True)
    assert not loss_qualified({'continuous_decline':{'loss_usd':'999999999'}},Rules())
    rows=[{'ethAddress':A,'windowPerformances':[['month',{'pnl':'-99999999'}],['allTime',{'pnl':'-20000000'}]]}]
    found=list(qualifying_leaderboard(rows,Rules(),T,include_loss=True))
    assert len(found)==1 and found[0][1]['window']=='allTime' and found[0][1]['side']=='loss'


def test_loss_recheck_and_net_offset_prevent_reporting(tmp_path):
    store,client,monitor=setup(tmp_path);enable_loss(store)
    client.pnl='-25000000';client.leaderboard=lambda:[board(pnl=client.pnl)]
    run(monitor,client,T)
    client.size='105'
    client.fills=[
        {'coin':'ETH','tid':1,'time':T+180000,'startPosition':'100','sz':'40','px':'100000','side':'A','closedPnl':'0'},
        {'coin':'ETH','tid':2,'time':T+180001,'startPosition':'60','sz':'45','px':'100000','side':'B','closedPnl':'0'}]
    run(monitor,client,T+180001)
    assert not store.candidates()  # $4m / $4.5m turnover, only $500k net.
    close(client,T+360001);client.pnl='-19000000'
    run(monitor,client,T+360001)
    assert not store.candidates()


def test_loss_dispatch_uses_shared_queue_and_launch_boundary(tmp_path):
    from test_hyperliquid_news_pipeline import Pipeline
    store,client,monitor=setup(tmp_path);enable_loss(store)
    client.pnl='-25000000';client.leaderboard=lambda:[board(pnl=client.pnl)]
    run(monitor,client,T);close(client,T+180000);run(monitor,client,T+180000)
    result=EventDispatcher(store,pipeline_client=Pipeline()).dispatch(limit=10)
    assert len(result)==1 and result[0]['status']=='queued'
    task=SQLiteXProcessingRepository(store.path).get_task(result[0]['task_id'])
    assert task.metadata['force_backend_only'] and task.metadata['production_direction']=='historical_loss'


def test_loss_rollout_does_not_reuse_prelaunch_profit_baseline(tmp_path):
    store, client, monitor = setup(tmp_path)
    run(monitor, client, T)
    policy = enable_loss(store)
    policy['loss_started_ms'] = T + 100000
    store.save_global('production_account_profit', policy, [])
    client.pnl = '-25000000'
    store.save_global('account_performance:' + A, {}, [])
    close(client, T + 180000)
    run(monitor, client, T + 180000)
    assert not store.candidates()
    assert store.load(A)['state']['account_monitoring_since_ms'] == T + 180000


def test_loss_threshold_is_rechecked_before_candidate(tmp_path):
    store, client, monitor = setup(tmp_path)
    enable_loss(store)
    client.pnl = '-25000000'
    run(monitor, client, T)
    close(client, T + 180000)
    client.pnl = '-19000000'
    run(monitor, client, T + 180000)
    assert not store.candidates()


def test_enable_loss_is_idempotent_and_preserves_profit_cutoff(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from packages.hyperliquid_news import commands
    store, client, monitor = setup(tmp_path)
    monkeypatch.setattr(commands, 'load_storage_settings', lambda: SimpleNamespace(sqlite_path=store.path))
    args = SimpleNamespace(hyperliquid_action='enable-account-loss')
    commands.run_command(args)
    first = store.metadata('production_account_profit', {})
    commands.run_command(args)
    assert store.metadata('production_account_profit', {}) == first
    assert first['started_ms'] == T
    assert first['enabled_scenarios'] == ['account_profit', 'historical_loss']
