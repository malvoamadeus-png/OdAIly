import pytest

from packages.hyperliquid_news.compact_history import summarize_compact
from packages.hyperliquid_news.store import NewsStore


ADDRESS = '0x' + '1' * 40


def fill(tid, stamp, before, size, side):
    return {'tid': tid, 'time': stamp, 'coin': 'BTC', 'startPosition': before,
            'sz': size, 'side': side, 'px': '100', 'closedPnl': '0',
            'fee': '0', 'feeToken': 'USDC'}


def setup_store(tmp_path, fills):
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    store.commit_observation(address=ADDRESS, now_ms=500000,
        state={'observed_ms': 500000, 'history_last_queried_ms': 500000},
        history={}, coverage={'pagination_complete': True}, fills=fills, candidates=[],
        state_table='hl_news_normal_state')
    return store


def test_transactional_batches_resume_and_preserve_complete_position(tmp_path):
    rows = [fill(1, 10, '0', '1', 'B'), fill(2, 20, '1', '1', 'B'),
            fill(3, 30, '2', '2', 'A')]
    store = setup_store(tmp_path, rows)
    assert store.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=2)['fills'] == 2
    assert len(store.fills(ADDRESS, 0, 500000)) == 3
    assert store.compact_history(ADDRESS) is None
    restarted = NewsStore(store.path)
    assert restarted.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=2)['fills'] == 1
    assert restarted.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'activated'
    assert restarted.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=2)['fills'] == 2
    assert restarted.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=2)['fills'] == 1
    assert restarted.fills(ADDRESS, 0, 500000) == []
    through, data = restarted.compact_history(ADDRESS)
    result = summarize_compact(data, start_ms=0, end_ms=500000)
    assert through == 30 and result['fills'] == 3
    assert result['assets']['BTC']['rounds'] == 1


def test_failed_verification_rolls_back_without_deleting_fills(tmp_path, monkeypatch):
    store = setup_store(tmp_path, [fill(1, 10, '0', '1', 'B')])
    def fail(*args, **kwargs):
        raise ValueError('verification failed')
    monkeypatch.setattr('packages.hyperliquid_news.compact_history.verify_compact_history', fail)
    with pytest.raises(ValueError, match='verification failed'):
        store.compact_fills(ADDRESS, cutoff_ms=500000)
    assert len(store.fills(ADDRESS, 0, 500000)) == 1
    assert store.compact_history(ADDRESS) is None


def test_compaction_does_not_hold_writer_lock_while_calculating(tmp_path, monkeypatch):
    from packages.hyperliquid_news import compact_history
    store = setup_store(tmp_path, [fill(1, 10, '0', '1', 'B')])
    original = compact_history.extend_compact
    calls = 0

    def concurrent_write(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            store.save_global('concurrent_test', True, [])
        return original(*args, **kwargs)

    monkeypatch.setattr(compact_history, 'extend_compact', concurrent_write)
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'retry'
    assert len(store.fills(ADDRESS, 0, 500000)) == 1
    assert store.compact_history(ADDRESS) is None
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'building'


def test_late_source_fill_prevents_shadow_activation(tmp_path):
    store = setup_store(tmp_path, [fill(1, 100, '0', '1', 'B')])
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'building'
    store.commit_observation(address=ADDRESS, now_ms=600000,
        state={'account_history_checked_ms': 600000}, history={}, coverage={'pagination_complete': True},
        fills=[fill(2, 50, '0', '1', 'B')], candidates=[])
    assert store.compact_fills(ADDRESS, cutoff_ms=600000)['phase'] == 'rebuild'
    assert store.compact_history(ADDRESS) is None
    assert len(store.fills(ADDRESS, 0, 600000)) == 2
    assert store.compact_fills(ADDRESS, cutoff_ms=600000)['fills'] == 2


def test_pending_history_and_account_cursor_limit_compaction(tmp_path):
    rows = [fill(1, 100000, '0', '1', 'B'), fill(2, 300000, '1', '1', 'B')]
    store = setup_store(tmp_path, rows)
    store.commit_observation(address=ADDRESS, now_ms=400000,
        state={'account_history_checked_ms': 200000}, history={},
        coverage={'pagination_complete': True}, fills=[], candidates=[])
    store.save_global('account_pending_fills:' + ADDRESS, {'fills': rows[:1]}, [])
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['reason'] == 'pending_history'
    store.save_global('account_pending_fills:' + ADDRESS, {}, [])
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['fills'] == 1
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'activated'
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['fills'] == 1
    assert store.fills(ADDRESS, 0, 500000) == rows[1:]


def test_timestamp_saturation_and_late_fills_fail_closed(tmp_path):
    rows = [fill(1, 10, '0', '1', 'B'), fill(2, 10, '1', '1', 'B')]
    store = setup_store(tmp_path, rows)
    with pytest.raises(ValueError, match='saturated'):
        store.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=1)
    assert len(store.fills(ADDRESS, 0, 500000)) == 2
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['fills'] == 2
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'activated'
    with pytest.raises(ValueError, match='Late fill'):
        store.commit_observation(address=ADDRESS, now_ms=600000,
            state={'account_history_checked_ms': 600000}, history={},
            coverage={'pagination_complete': True}, fills=[rows[0]], candidates=[])


def test_monitor_dedup_keys_are_independent_and_commit_atomically(tmp_path):
    from packages.hyperliquid_news.history import fill_key
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    row = fill(1, 100000, '0', '1', 'B')
    state = {'separate_fill_dedup': True, 'account_history_checked_ms': 120000}
    store.commit_observation(address=ADDRESS, now_ms=120000, state=state,
                             history={}, coverage={}, fills=[row], candidates=[])
    assert store.monitor_fill_keys(ADDRESS, 'account', 0, 200000) == {fill_key(row)}
    assert store.monitor_fill_keys(ADDRESS, 'normal', 0, 200000) == set()
    normal = {'separate_fill_dedup': True, 'history_last_queried_ms': 120000}
    store.commit_observation(address=ADDRESS, now_ms=120000, state=normal, history={},
                             coverage={}, fills=[row], candidates=[], state_table='hl_news_normal_state')
    assert store.monitor_fill_keys(ADDRESS, 'normal', 0, 200000) == {fill_key(row)}
    with pytest.raises(ValueError, match='monotonically'):
        store.commit_observation(address=ADDRESS, now_ms=120000, state=state,
                                 history={}, coverage={}, fills=[fill(2, 110000, '1', '1', 'B')], candidates=[])
    assert store.monitor_fill_keys(ADDRESS, 'account', 0, 200000) == {fill_key(row)}


def test_stale_observation_cannot_commit_after_concurrent_compaction(tmp_path):
    store = setup_store(tmp_path, [fill(1, 10, '0', '1', 'B')])
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['fills'] == 1
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'activated'
    with pytest.raises(ValueError, match='changed during observation'):
        store.commit_observation(address=ADDRESS, now_ms=600000, state={}, history={},
            coverage={}, fills=[], candidates=[], state_table='hl_news_normal_state',
            check_compact_boundary=True, expected_compact_through=None)
    assert store.load(ADDRESS, state_table='hl_news_normal_state')['state']['observed_ms'] == 500000


def test_segment_summaries_match_exact_windows_across_batches(tmp_path):
    from packages.hyperliquid_news.compact_history import verify_compact_history
    rows = [fill(i, i * 10, str((i - 1) % 4), '1', 'B') if (i - 1) % 4 < 3
            else fill(i, i * 10, '3', '3', 'A') for i in range(1, 41)]
    store = setup_store(tmp_path, rows)
    while True:
        result = store.compact_fills(ADDRESS, cutoff_ms=500000, batch_size=5)
        if not result['fills'] and result.get('phase') != 'activated':
            break
    windows = [(0, 400), (15, 390), (55, 305), (115, 125), (201, 379)]
    cuts = [v for start, end in windows for v in (start, end + 1)]
    archive = store.compact_history(ADDRESS, min(cuts), cuts=cuts)
    verify_compact_history(rows, archive[1], windows)
    reduced = store.compact_history(ADDRESS, min(cuts), cuts=cuts, aggregate_rounds=True)
    for start, end in windows:
        exact = summarize_compact(archive[1], start_ms=start, end_ms=end)
        aggregate = summarize_compact(reduced[1], start_ms=start, end_ms=end)
        for asset in exact['assets'].values():
            asset.pop('completed_rounds')
            asset.pop('completed_round_pnl')
        for asset in aggregate['assets'].values():
            asset.pop('completed_rounds')
            asset.pop('completed_round_pnl')
        assert aggregate == exact


def test_normal_poll_schedule_is_independent_of_account_polling(tmp_path):
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    other = '0x' + '2' * 40
    for address in (ADDRESS, other):
        store.discover(address, source={'kind': 'test'}, now_ms=1)
    store.record_error(ADDRESS, 100, 'normal error', normal_schedule=True)
    store.commit_observation(address=other, now_ms=200, state={}, history={},
                             coverage={}, fills=[], candidates=[])
    assert store.addresses(1, normal_schedule=True)[0]['address'] == other
    store.record_error(other, 300, 'normal error', normal_schedule=True)
    assert store.addresses(1, normal_schedule=True)[0]['address'] == ADDRESS


def test_account_history_can_compact_before_normal_baseline_exists(tmp_path):
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    row = fill(1, 100000, '0', '1', 'B')
    store.commit_observation(address=ADDRESS, now_ms=500000,
        state={'account_history_checked_ms': 500000}, history={},
        coverage={'pagination_complete': True}, fills=[row], candidates=[])
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'building'
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'activated'
    assert store.compact_fills(ADDRESS, cutoff_ms=500000)['phase'] == 'cleanup'
    assert store.compact_boundary(ADDRESS) == 100000


def test_normal_monitor_can_start_after_account_history_was_compacted(tmp_path):
    from packages.hyperliquid_news.service import NewsMonitor
    from test_hyperliquid_news import FakeClient
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    store.discover(ADDRESS, source={'kind': 'manual'}, now_ms=1)
    store.commit_observation(address=ADDRESS, now_ms=500000,
        state={'account_history_checked_ms': 500000}, history={},
        coverage={'pagination_complete': True},
        fills=[fill(1, 100000, '0', '1', 'B')], candidates=[])
    store.compact_fills(ADDRESS, cutoff_ms=500000)
    store.compact_fills(ADDRESS, cutoff_ms=500000)
    store.compact_fills(ADDRESS, cutoff_ms=500000)
    store.save_global('compact_history_reset_start_ms', 50000, [])

    class Client(FakeClient):
        def __init__(self): self.starts = []
        def history(self, address, start_ms, end_ms):
            self.starts.append(start_ms)
            return [], {'pagination_complete': True, 'next_start_ms': start_ms,
                        'coverage': 'retained_api_history'}

    client = Client()
    report = NewsMonitor(store=store, client=client, leaderboard_seeds=0,
                         addresses_per_pass=1).run_once(now_ms=600000)
    assert not report['failed']
    assert client.starts == [100001]
    assert store.load(ADDRESS, state_table='hl_news_normal_state') is not None


def test_online_monitor_rebuilds_and_compacts_after_reset(tmp_path):
    from packages.hyperliquid_news.service import NewsMonitor
    from scripts.reset_hyperliquid_history import reset_history
    from test_hyperliquid_news import FakeClient
    store = NewsStore(tmp_path / 'offline.sqlite')
    store.initialize()
    store.discover(ADDRESS, source={'kind': 'manual'}, now_ms=1)
    reset_history(store.path, now_ms=300000, lookback_ms=250000)
    rows = [fill(1, 100000, '0', '1', 'B'), fill(2, 200000, '1', '1', 'A')]

    class Client(FakeClient):
        def history(self, address, start_ms, end_ms):
            return [row for row in rows if start_ms <= row['time'] <= end_ms], {
                'pagination_complete': True, 'next_start_ms': start_ms, 'coverage': 'retained_api_history'}

    monitor = NewsMonitor(store=store, client=Client(), leaderboard_seeds=0, addresses_per_pass=10)
    for stamp in (400000, 500000, 600000, 700000):
        report = monitor.run_once(now_ms=stamp)
        assert not report['failed'] and not report.get('compaction_failed')
    assert store.compact_boundary(ADDRESS) == 200000
    assert store.fills(ADDRESS, 0, 700000) == []
    history = store.load(ADDRESS, state_table='hl_news_normal_state')['history']
    assert history['fills'] == 2 and history['assets']['BTC']['rounds'] == 1


def test_reset_discards_only_history_and_monitor_baselines(tmp_path):
    from scripts.reset_hyperliquid_history import reset_history
    store = setup_store(tmp_path, [fill(1, 10, '0', '1', 'B')])
    store.discover(ADDRESS, source={'kind': 'manual'}, now_ms=1)
    store.save_global('account_pending_fills:' + ADDRESS, {'fills': [1]}, [])
    store.save_global('production_account_profit', {'enabled': True}, [])
    result = reset_history(store.path, now_ms=500000, lookback_ms=60000)
    assert result['history_start_ms'] == 440000
    assert store.fills(ADDRESS, 0, 500000) == []
    assert store.load(ADDRESS, state_table='hl_news_normal_state') is None
    assert store.addresses(10)[0]['address'] == ADDRESS
    assert store.metadata('production_account_profit', {}) == {'enabled': True}
    assert store.metadata('account_pending_fills:' + ADDRESS, {}) == {}
    assert store.metadata('compact_history_enabled', False) is True
