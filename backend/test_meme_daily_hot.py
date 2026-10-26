from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from packages.meme_scanner import daily_hot, grok_jobs, grok_callback, scanner, fast_narrative
from packages.meme_dashboard import MemeDashboardStore

CA = 'AhBidGAgv6QbKSWfrSCegGrzFLcBx4E5FNYTKHNQQj6C'


def rows(address=CA, change=.30, rank=20):
    return [{'address': address, 'symbol': 'LAUNCH', 'rank': rank, 'price_change_percent': change}]


def market_pair(cap=1_000_000, volume=500_000):
    return {'marketCap': cap, 'volume': {'h24': volume}, 'baseToken': {'address': CA}, 'liquidity': {'usd': 1}}


@pytest.mark.parametrize('interval,change,rank,expected', [
    ('1h', .20, 20, True), ('1h', .199, 20, False), ('1h', 20, 21, False),
    ('6h', .30, 20, True), ('6h', .299, 20, False), ('24h', -1, 20, True),
])
def test_rule_boundaries(interval, change, rank, expected):
    assert bool(daily_hot.ranked_candidates('solana', {interval: rows(change=change, rank=rank)})) == expected


def test_baseline_then_new_full_qualification_is_permanent(tmp_path):
    store = scanner.Store(tmp_path / 'scanner.sqlite3')
    current = [CA]
    volume = [500_000]
    def fetch(chain, interval, order):
        return rows(current[0]) if chain == 'solana' else []
    market = Mock()
    market.price_info.side_effect = lambda chain, addresses: {a: market_pair(volume=volume[0]) for a in addresses}
    assert daily_hot.scan(store, fetch=fetch, market=market, force=True)['baseline'] == 1
    assert store.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    current[0] = CA + 'A'
    volume[0] = 10_000
    assert daily_hot.scan(store, fetch=fetch, market=market, force=True)['queued'] == 0
    volume[0] = 500_000
    assert daily_hot.scan(store, fetch=fetch, market=market, force=True)['queued'] == 1
    store.close()
    store = scanner.Store(tmp_path / 'scanner.sqlite3')
    assert daily_hot.scan(store, fetch=fetch, market=market, force=True)['queued'] == 0
    job = store.conn.execute('SELECT * FROM jobs').fetchone()
    assert job['address'] == CA + 'A'
    assert len(json.loads(job['evidence_json'])['matches']) == 3
    assert store.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0
    store.close()


def test_failed_query_does_not_initialize_chain(tmp_path):
    store = scanner.Store(tmp_path / 'scanner.sqlite3')
    def fetch(chain, interval, order):
        if interval == '6h':
            raise RuntimeError('upstream failed')
        return []
    result = daily_hot.scan(store, fetch=fetch, market=Mock(), force=True)
    assert len(result['errors']) == 5
    assert store.conn.execute('SELECT COUNT(*) FROM daily_hot_chains').fetchone()[0] == 0
    store.close()


def make_job(tmp_path):
    store = scanner.Store(tmp_path / 'scanner.sqlite3')
    token = scanner.token_from_row({'address': CA, 'chain': 'solana', 'symbol': 'LAUNCH',
                                   'usd_market_cap': 1_000_000, 'volume_24h': 500_000})
    store.add_job(token, 'daily_hot', 'queued')
    return store, token, store.claim_job()


def test_grok_roundtrip_is_nonblocking_and_durable(tmp_path, monkeypatch):
    store, token, job = make_job(tmp_path)
    paths = SimpleNamespace(runtime_dir=tmp_path / 'runtime', config_dir=tmp_path)
    credentials = tmp_path / 'grok.txt'
    credentials.write_text('url=https://example.org/webhook\nkey=private-test\nheader=Authorization: Bearer\n')
    monkeypatch.setenv('GROK_ROUTINE_CREDENTIALS_FILE', str(credentials))
    with patch.object(grok_jobs, 'get_paths', return_value=paths), patch.object(grok_jobs.requests, 'post') as post:
        post.return_value = SimpleNamespace(status_code=202)
        ready, audit = grok_jobs.prepare(store, job, token)
        assert not ready and audit['state'] == 'pending'
        assert store.conn.execute('SELECT attempts FROM jobs').fetchone()[0] == 0
        ready, _ = grok_jobs.prepare(store, job, token)
        assert not ready and post.call_count == 1
        callback = post.call_args.kwargs['json']['callback_url']
        result = {'ok': True, 'type': 'meme_narrative', 'request_id': audit['request_id'], 'ca': CA,
                  'primary_narrative': '反Pump发行平台'}
        assert grok_callback.store_callback_result(paths.runtime_dir / 'grok_narrative_callbacks',
                   audit['request_id'], callback.rsplit('/', 1)[1], json.dumps(result).encode())
        ready, final = grok_jobs.prepare(store, job, token)
        assert ready and final['result'] == result and post.call_count == 1
    store.close()


def test_grok_timeout_is_ready_for_other_sources(tmp_path):
    store, token, job = make_job(tmp_path)
    grok_jobs.setup(store.conn)
    store.conn.execute('INSERT INTO meme_grok_jobs VALUES (?,?,?,?,NULL,NULL)',
        (job['trigger_key'], 'test-timeout', (datetime.now(UTC) - timedelta(minutes=16)).isoformat(), 'pending'))
    store.conn.commit()
    with patch.object(grok_jobs, 'get_paths', return_value=SimpleNamespace(runtime_dir=tmp_path)):
        ready, audit = grok_jobs.prepare(store, job, token)
    assert ready and audit['state'] == 'timeout'
    store.close()


def test_dashboard_filters_before_limit_and_titles(tmp_path):
    store, token, job = make_job(tmp_path)
    for i in range(105):
        store.add_job(token, 'tg_burst', 'queued', trigger_key=f'tg_burst:{i}')
    dashboard = MemeDashboardStore(tmp_path / 'scanner.sqlite3')
    assert len(dashboard.dashboard(column='daily_hot')['items']) == 1
    assert len(dashboard.dashboard(column='digest')['items']) == 100
    for chain, label in [('base', 'Base'), ('eth', 'Ethereum'), ('solana', 'Solana')]:
        current = scanner.token_from_row({**token.raw, 'chain': chain})
        title, content = scanner.format_text(current, '叙事\n\n' + fast_narrative.narrative_v2.READER_DISCLAIMER, datetime.now(UTC), 'daily_hot')
        assert title == f'今日热门Meme：{label}上LAUNCH市值突破100万美元'
        assert '据Odaily今日热门Meme监测，' in content
        assert '「今日热门Meme」' in content
    store.close()


def test_grok_is_usable_when_local_sources_empty(tmp_path):
    args = SimpleNamespace(chain='solana', contract=CA, symbol='LAUNCH', output=str(tmp_path / 'audit.json'),
        gpt_model='test', gpt_timeout=1, writer_base_url='https://invalid', writer_api_key='test',
        grok_routine={'state': 'complete', 'result': {'ok': True, 'primary_narrative': '反Pump发行平台'}})
    provider = fast_narrative.InMemoryFastEvidenceAdapter({'version': '2026-09-01', 'status': 'empty', 'evidence': []})
    final = {'primary_type': 'app_linked', 'supplemental_information_ids': ['grok:routine:primary_narrative'],
             'reader_text': 'Grok补充资料显示，Launch 是反 Pump.fun 的发行平台。'}
    with patch.object(fast_narrative, 'write_json_with_metrics', return_value=(final, {})):
        result = fast_narrative.run(args, provider=provider)
    assert result['status'] == 'success'
    assert result['primary_type'] == 'app_linked'
    assert result['grok_research']['ok'] is True


def test_daily_hot_publish_uses_shared_draft_and_does_not_touch_milestones(tmp_path):
    store, token, job = make_job(tmp_path)
    store.conn.execute("UPDATE jobs SET status='queued' WHERE id=?", (job['id'],))
    store.conn.commit()
    args = SimpleNamespace(db=str(tmp_path / 'scanner.sqlite3'), audit_dir=str(tmp_path / 'audit'),
                           narrative_command=None, narrative_timeout=1, push_timeout=1, send=True)
    audit = {'state': 'failed', 'request_id': 'failed-test', 'result': None}
    narrative = {'status': 'success', 'reader_text': 'Launch 是发行平台。', 'primary_type': 'app_linked'}
    with patch.object(grok_jobs, 'prepare', return_value=(True, audit)), \
         patch.object(scanner, 'collect_narrative', return_value=narrative), \
         patch.object(scanner, 'push_pending', return_value={'ok': True}) as push, \
         patch.object(scanner, 'LocalEditorPluginFeedWriter') as feed:
        assert scanner.process_one(store, args) == 'publisher_pending'
        assert scanner.process_one(store, args) is None
        assert push.call_count == 1
        assert push.call_args.kwargs['send'] is True
        assert 'daily_hot:solana:' in push.call_args.kwargs['idempotency_key']
        assert feed.return_value.upsert_meme_digest.call_args.kwargs['trigger_kind'] == 'daily_hot'
    assert store.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 0
    assert store.conn.execute('SELECT COUNT(*) FROM market_cap_milestones').fetchone()[0] == 0
    assert store.conn.execute('SELECT status FROM jobs').fetchone()[0] == 'publisher_pending'
    store.close()
