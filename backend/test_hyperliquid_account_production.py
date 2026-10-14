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
