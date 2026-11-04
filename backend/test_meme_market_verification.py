from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from packages.meme_scanner import scanner
from packages.meme_scanner.market_verification import verify_market_cap, VerificationClient


def token(chain='solana'):
    return SimpleNamespace(chain=chain, address='CaseSensitiveMint', market_cap=1_000_000, raw={})


@pytest.mark.parametrize('caps,status', [((850000, None), 'passed'), ((1150000, None), 'passed'),
    ((1150001, None), 'suspect'), ((849999, None), 'suspect'), ((None, 1000000), 'passed'),
    ((1000000, 1500000), 'suspect'), ((None, None), 'unavailable'), ((float('nan'), float('inf')), 'unavailable')])
def test_threshold_and_partial_results(caps, status):
    client = SimpleNamespace(gecko=lambda *a: {'market_cap': caps[0]}, jupiter=lambda *a: {'market_cap': caps[1]})
    assert verify_market_cap(token(), client)['status'] == status


def test_one_provider_failure_does_not_hide_other_result():
    client = SimpleNamespace(gecko=Mock(side_effect=RuntimeError()), jupiter=lambda *a: {'market_cap': 1000000})
    assert verify_market_cap(token(), client)['status'] == 'passed'


def test_other_chains_never_request_verification():
    client = Mock()
    assert verify_market_cap(token('bsc'), client)['status'] == 'not_applicable'
    client.gecko.assert_not_called()
    client.jupiter.assert_not_called()


@pytest.mark.parametrize('status,expected,reason', [('suspect', 'market_cap_suspect', 'market_cap_suspect'),
                                                    ('unavailable', 'retry_wait', 'market_verification_unavailable')])
def test_gate_blocks_grok_narrative_and_publish(tmp_path, monkeypatch, status, expected, reason):
    store = scanner.Store(tmp_path / 'meme.sqlite3')
    raw = {'address': 'CaseSensitiveMint', 'chain': 'solana', 'name': 'Test', 'symbol': 'TEST',
           'usd_market_cap': 1000000, 'volume_24h': 900000, 'launchpad_platform': 'pumpfun'}
    t = scanner.token_from_row(raw)
    assert t is not None
    store.add_job(t, 'daily_hot', 'queued')
    monkeypatch.setattr(scanner, 'verify_market_cap', lambda *a: {'status': status, 'providers': {}})
    monkeypatch.setattr(scanner, '_ensure_gmgn_kol_profile', lambda *a: {'kol_count': 1, 'checked_at': scanner.now_iso()})
    narrative = Mock(side_effect=AssertionError('must not write'))
    monkeypatch.setattr(scanner, 'collect_narrative', narrative)
    monkeypatch.setattr(scanner, 'push_pending', narrative)
    from packages.meme_scanner import grok_jobs
    monkeypatch.setattr(grok_jobs, 'prepare', narrative)
    assert scanner.process_one(store, SimpleNamespace()) == expected
    row = store.conn.execute('SELECT * FROM jobs').fetchone()
    assert row['reason'] == reason
    assert row['status'] == ('discarded' if status == 'suspect' else 'retry_wait')
    narrative.assert_not_called()
    store.close()


@pytest.mark.parametrize('trigger', ['daily_hot', 'market_cap_milestone', 'tg_burst'])
def test_successful_gate_is_reused_during_grok_wait(tmp_path, monkeypatch, trigger):
    store = scanner.Store(tmp_path / 'meme.sqlite3')
    t = scanner.token_from_row({'address': 'CaseSensitiveMint', 'chain': 'solana', 'name': 'Test',
        'symbol': 'TEST', 'usd_market_cap': 1000000, 'volume_24h': 900000, 'launchpad_platform': 'pumpfun'})
    store.add_job(t, trigger, 'queued', trigger_key=f'{trigger}:test')
    verification = Mock(return_value={'status': 'passed', 'address': t.address,
                                      'dexscreener_market_cap': t.market_cap, 'providers': {}})
    monkeypatch.setattr(scanner, 'verify_market_cap', verification)
    monkeypatch.setattr(scanner, '_ensure_gmgn_kol_profile', lambda *a: {'kol_count': 1, 'checked_at': scanner.now_iso()})
    from packages.meme_scanner import grok_jobs
    def wait(store, job, token):
        store.conn.execute("UPDATE jobs SET status='retry_wait',reason='grok_pending',next_attempt_at='' WHERE id=?", (job['id'],))
        store.conn.commit()
        return False, {}
    monkeypatch.setattr(grok_jobs, 'prepare', wait)
    assert scanner.process_one(store, SimpleNamespace()) == 'grok_pending'
    assert scanner.process_one(store, SimpleNamespace()) == 'grok_pending'
    assert verification.call_count == 1
    from packages.meme_dashboard import MemeDashboardStore
    dashboard = MemeDashboardStore(tmp_path / 'meme.sqlite3').dashboard()
    assert dashboard['items'][0]['market_verification']['status'] == 'passed'
    store.close()


def test_legacy_worker_cannot_create_database_or_call_providers(tmp_path, monkeypatch):
    from packages.meme_scanner import quote_audit
    monkeypatch.setenv('MEME_QUOTE_AUDIT_ENABLED', 'true')
    client = Mock(side_effect=AssertionError('retired worker must not run'))
    monkeypatch.setattr(quote_audit, 'AuditStore', client)
    assert quote_audit.run(SimpleNamespace(once=True, audit_db=str(tmp_path / 'old.sqlite3'))) == 0
    client.assert_not_called()
    assert not (tmp_path / 'old.sqlite3').exists()


def test_live_provider_parsing_uses_exact_mint_and_chain_supply(monkeypatch):
    from packages.meme_scanner import market_verification as mv
    address = 'CaseSensitiveMint'
    def pool(mint, fdv, reserve):
        return {'attributes': {'fdv_usd': fdv, 'reserve_in_usd': reserve, 'address': 'Pool'},
                'relationships': {'base_token': {'data': {'id': f'solana_{mint}'}}}}
    responses = [SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'data': [
        pool(address.lower(), 9000000, 1000000), pool(address, 1100000, 10)]}),
        SimpleNamespace(raise_for_status=lambda: None, json=lambda: {address: {'usdPrice': .001}})]
    monkeypatch.setattr(mv.requests, 'get', Mock(side_effect=responses))
    post = Mock(return_value=SimpleNamespace(raise_for_status=lambda: None,
                json=lambda: {'result': {'value': {'amount': '1000000000000000', 'decimals': 6}}}))
    monkeypatch.setattr(mv.requests, 'post', post)
    client = VerificationClient()
    assert client.gecko(address, 'Pool')['market_cap'] == 1100000
    assert client.jupiter(address)['market_cap'] == 1000000
    assert post.call_args.kwargs['json']['params'] == [address]
