from dataclasses import replace
from decimal import Decimal as D

import pytest

from packages.hyperliquid_news.__main__ import replay
from packages.hyperliquid_news.client import NewsClient
from packages.hyperliquid_news.detector import evaluate_address, evaluate_divergence
from packages.hyperliquid_news.history import summarize
from packages.hyperliquid_news.models import Position, Rules
from packages.hyperliquid_news.service import NewsMonitor
from packages.hyperliquid_news.store import NewsStore

A = '0x' + '1' * 40
B = '0x' + '2' * 40
COIN = 'xyz:CL'


def position(size='100', notional='10000000', pnl='0', liquidation=None, coin=COIN):
    return Position(coin, D(size), D(notional), D(notional) / abs(D(size)), D(pnl), D(liquidation) if liquidation else None)


def fill(tid=1, before='0', size='100', side='B', pnl='0', stamp=100, coin=COIN, px='100000', **extras):
    return {'tid': tid, 'coin': coin, 'startPosition': before, 'sz': size, 'px': px,
            'side': side, 'closedPnl': pnl, 'fee': '0', 'feeToken': 'USDC', 'time': stamp, **extras}


def stats(win='0.7', profit='8000000', loss_streak=0, account='20000000'):
    return {'assets': {COIN: {'rounds': 10, 'win_rate': win, 'closed_pnl_less_fees': profit,
                             'last_30d_closed_pnl_less_fees': profit,
                             'consecutive_losing_rounds': loss_streak}},
            'account_closed_pnl_less_fees': account, 'fee_currency_complete': True}


def evaluate(p=None, previous=None, fills=None, history=None, markets=None, stamp=200, rules=None,
             history_before_actions=None):
    return evaluate_address(address=A, observed_ms=stamp, first_seen_ms=100,
        positions={p.coin: p} if p else {}, previous=previous, markets=markets or {},
        history=history or {}, new_fills=fills or [], rules=rules or Rules(),
        history_before_actions=history_before_actions)


def kinds(events):
    return {kind for e in events for kind in e.kinds}


def baseline(p):
    return evaluate(p)[1]


def test_large_position_oi_cap_and_no_repeat_or_fake_action():
    market = {COIN: {'oi_notional': '50000000', 'market_cap': '100000000'}}
    events, state = evaluate(position(), markets=market)
    assert events == []
    assert state['signals'][COIN]['large_position'] == 1
    assert not evaluate(position(), previous=state, markets=market, stamp=300)[0]
    assert not evaluate(position(), previous=state,
        markets={COIN: {'oi_notional': '10000000', 'market_cap': '100000000'}}, stamp=300)[0]
    changed = evaluate(position(size='150',notional='15000000'),previous=state,
        markets={COIN:{'oi_notional':'10000000','market_cap':'100000000'}},stamp=400)[0]
    assert 'oi_share' in kinds(changed)


@pytest.mark.parametrize('coin,line', [('BTC', '5000000'), ('ETH', '5000000'), ('xyz:GOLD', '2000000'), ('xyz:BTC', '2000000')])
def test_pnl_thresholds_by_market(coin, line):
    p = position(notional='3000000', coin=coin, pnl=line)
    events, state = evaluate(p)
    assert events==[]
    assert not evaluate(p, previous=state, stamp=300)[0]
    assert 'large_profit' in kinds(evaluate(replace(p, unrealized_pnl=D(line) * 2), previous=state, stamp=300)[0])
    assert 'pnl_sign_flip' in kinds(evaluate(replace(p, unrealized_pnl=-D(line)), previous=state, stamp=300)[0])


def test_large_pnl_requires_directional_expansion_and_price_only_position_value_change_is_reportable():
    old = position(size='100', notional='24840000', pnl='-24840000', coin='ZEC')
    previous = {'observed_ms': 100, 'positions': {'ZEC': old.facts()}}
    narrowed = position(size='100', notional='24500000', pnl='-24500000', coin='ZEC')
    assert not evaluate(narrowed, previous=previous, stamp=200)[0]

    expanded = position(size='100', notional='27000000', pnl='-27000000', coin='ZEC')
    assert 'large_loss' in kinds(evaluate(expanded, previous=previous, stamp=200)[0])

    old_large = position(size='100', notional='9000000')
    current_large = position(size='100', notional='13000000')
    assert 'large_position' in kinds(evaluate(current_large, previous=baseline(old_large), stamp=300)[0])


def test_writer_material_has_execution_prices_but_omits_small_close_pnl():
    from dataclasses import asdict
    from packages.hyperliquid_news.copy import writer_material

    old = baseline(position(size='10', notional='10000000'))
    current = position(size='5', notional='5500000')
    history = {'assets': {COIN: {'rounds': 10, 'win_rate': '0.8'}}}
    events, _ = evaluate(current, previous=old, history=history, fills=[
        fill(1, before='10', size='10', side='A', pnl='910', stamp=201, px='1000000'),
        fill(2, before='0', size='5', side='B', stamp=202, px='1100000'),
    ])
    event = next(e for e in events if 'highlighted_address_action' in e.kinds)
    actions = writer_material(asdict(event))['facts']['actions']
    assert actions['close_long_average_execution_price_usd'] == '1000000'
    assert actions['open_long_average_execution_price_usd'] == '1100000'
    assert 'closed_pnl' not in actions and 'ordinary_closed_pnl' not in actions


def test_high_win_preview_adds_available_open_and_close_execution_prices():
    from packages.hyperliquid_news.copy import format_news
    from packages.x_processing.models import DraftBrief

    event={
        'key':'high-win','observed_ms':1,'addresses':[A],'coin':'ETH',
        'kinds':['highlighted_address_action'],'evidence':{
            'highlights':['asset_high_win_rate'],'previous_position':{'size':'2'},
            'position':{'size':'4'},'actions':{
                'close_long_notional':'670000','open_long_notional':'1160000',
                'close_long_average_execution_price_usd':'2437.2963',
                'open_long_average_execution_price_usd':'2442.5789'},
            'history':{'assets':{'ETH':{'rounds':22,'win_rate':'0.8181818'}}},
        },
    }
    result=format_news(DraftBrief(title='ETH交易胜率达81.8%',content='某地址平仓后加仓ETH多单。'),event)
    assert 'ETH多单平仓的加权成交价约为2,437.30美元' in result.content
    assert '开仓的加权成交价约为2,442.58美元' in result.content


def test_loss_addition_uses_pre_action_loss_without_reporting_small_pnl_change():
    from packages.hyperliquid_news.copy import scenario_title, writer_material

    event={
        'key':'loss-addition','observed_ms':1,'addresses':[A],'coin':'ZEC',
        'kinds':['loss_addition'],'evidence':{
            'previous_position':{'size':'-32759.57','notional':'39390000',
                                 'unrealized_pnl':'-24844732.254914'},
            'position':{'size':'-39759.57','notional':'47420000',
                        'unrealized_pnl':'-24503830.942914'},
            'previous_reported_floating_pnl':'-24844732.254914',
            'actions':{'open_notional':'8368057.526'},'highlights':[],
        },
    }
    facts=writer_material(event)['facts']
    assert facts['floating_loss_before_addition_usd']=='24844732.254914'
    assert 'floating_pnl_usd' not in facts['position']
    assert facts['previous_position']['floating_pnl_usd']=='-24844732.254914'
    assert 'previous_floating_pnl_usd' not in facts
    assert scenario_title(event)=='某地址持有的ZEC空单浮亏2,484.47 万美元时加仓836.81 万美元'


def test_footer_uses_only_official_account_total_and_keeps_asset_win_rate():
    from packages.hyperliquid_news.copy import footer

    event={'addresses':[A],'coin':'ETH','kinds':['highlighted_address_action'],'evidence':{
        'highlights':['asset_high_win_rate'],'history':{
            'fills':40,'fee_currency_complete':True,'account_closed_pnl_less_fees':'250000',
            'account_rounds':60,'account_wins':49,'account_win_rate':'0.8166667',
            'first_observed_fill_ms':100,'last_observed_fill_ms':200,
            'assets':{'ETH':{'rounds':22,'win_rate':'0.8181818',
                             'first_observed_fill_ms':100,'last_observed_fill_ms':200}},
        }},
    }
    text=footer(event)
    assert '地址统计期合约交易盈亏' not in text
    assert '合约账户总盈亏' not in text
    assert 'ETH 交易轮数：22；胜率：81.82%' in text


def test_summarize_exposes_account_level_rounds_and_win_rate():
    fills = [fill(1, size='10', stamp=100),
             fill(2, before='10', size='10', side='A', pnl='100', stamp=200),
             fill(3, size='10', stamp=300),
             fill(4, before='10', size='10', side='A', pnl='-100', stamp=400)]
    history = summarize(fills, start_ms=0, end_ms=500)
    assert history['account_rounds'] == 2
    assert history['account_wins'] == 1
    assert history['account_win_rate'] == '0.5'


def test_high_profit_and_reversal_record_use_history_before_current_action(tmp_path):
    opened=fill(1,before='0',size='100',stamp=1000)
    closed=fill(2,before='100',size='100',side='A',pnl='6000000',stamp=2000)
    monitor=NewsMonitor(store=NewsStore(tmp_path/'history.sqlite'),client=FakeClient(),
                        leaderboard_seeds=0,history_window_ms=90*86400000)
    current=summarize([opened,closed],start_ms=0,end_ms=3000)
    current['assets'][COIN]['last_30d_closed_pnl_less_fees']='6000000'
    prior=monitor._history_before_actions([opened,closed],[closed],current)[COIN]
    assert current['assets'][COIN]['last_30d_closed_pnl_less_fees']=='6000000'
    assert prior['assets'][COIN]['last_30d_closed_pnl_less_fees']=='0'
    events,_=evaluate(None,previous=baseline(position(size='100',notional='10000000')),
        fills=[closed],history=current,history_before_actions={COIN:prior})
    realized=next(e for e in events if 'large_realized_result' in e.kinds)
    assert 'asset_high_profit' not in realized.evidence['highlights']
    assert realized.evidence['history']['account_closed_pnl_less_fees']=='0'


def test_partial_closes_one_round_and_incomplete_start_excluded():
    fills = [fill(), fill(2, before='100', size='40', side='A', pnl='400', stamp=110),
             fill(3, before='60', size='60', side='A', pnl='600', stamp=120)]
    history = summarize(fills + [fills[-1]], start_ms=0, end_ms=200)
    asset = history['assets'][COIN]
    assert asset['rounds'] == 1 and asset['win_rate'] == '1'
    assert asset['closed_pnl_less_fees'] == '1000'
    assert asset['completed_rounds'][0]['close_notional'] == '10000000'
    assert summarize(fills[1:], start_ms=0, end_ms=200)['assets'][COIN]['rounds'] == 0


def test_reverse_splits_round_and_fee_instead_of_counting_each_fill():
    fills = [fill(fee='10'), fill(2, before='100', size='150', side='A', pnl='1000', stamp=110, fee='15'),
             fill(3, before='-50', size='50', side='B', pnl='-500', stamp=120, fee='5')]
    asset = summarize(fills, start_ms=0, end_ms=200)['assets'][COIN]
    assert asset['rounds'] == 2 and asset['win_rate'] == '0.5'
    assert asset['completed_round_pnl'] == ['980', '-510']
    assert asset['closed_pnl_less_fees'] == '470'


def test_missing_position_path_excludes_unreliable_round():
    asset = summarize([fill(), fill(2, before='80', size='80', side='A', pnl='100', stamp=110)],
                       start_ms=0, end_ms=200)['assets'][COIN]
    assert asset['rounds'] == 0 and asset['position_discontinuities'] == 1


@pytest.mark.parametrize('history,label', [(stats(), 'asset_high_win_rate'),
    (stats(win='0.3'), 'asset_high_profit'), (stats(profit='0'), 'account_high_profit'),
    ({'official_account_loss': {'windows': {'allTime': {'pnl': '-20000000'}}}}, 'historical_loss'),
    ({'official_account_loss': {'windows': {'allTime': {'pnl': '-21000000'}}}}, 'historical_loss')])
def test_highlighted_address_actions(history, label):
    _, old = evaluate(None)
    events, _ = evaluate(position(size='30', notional='3000000'), previous=old,
                         fills=[fill(size='30', stamp=200)], history=history)
    assert 'highlighted_address_action' in kinds(events)
    assert label in events[0].evidence['highlights']
    assert 'newly_observed_address_position' in kinds(events)


def test_small_reduction_not_important_based_on_remaining_position():
    old = baseline(position(size='300', notional='30000000'))
    events, _ = evaluate(position(size='299', notional='29900000'), previous=old,
                         fills=[fill(before='300', size='1', side='A', stamp=200)], history=stats())
    assert 'highlighted_address_action' not in kinds(events)
    assert 'large_position_action' not in kinds(events)


def test_price_move_not_an_addition():
    old = baseline(position(size='100', notional='9000000'))
    events, _ = evaluate(position(size='100', notional='12000000'), previous=old, history=stats())
    assert 'large_position' in kinds(events)
    assert 'large_position_action' not in kinds(events)


def test_large_position_action_without_doubling():
    old = baseline(position())
    events, _ = evaluate(position(size='130', notional='13000000'), previous=old,
                         fills=[fill(before='100', size='30', stamp=200)])
    assert 'large_position_action' in kinds(events)


def test_reversal_peak_reset_and_close():
    old = baseline(position(pnl='6000000'))
    events, state = evaluate(position(size='-100', pnl='-100000'), previous=old,
                             fills=[fill(before='100', size='200', side='A', stamp=200)])
    assert 'large_reversal' in kinds(events)
    assert 'profit_to_loss' not in kinds(events)
    assert state['peaks'][COIN] == '0'
    _, state = evaluate(None, previous=state, stamp=300)
    assert not state['positions'] and not state['peaks']


def test_giveback_and_turn_to_loss_once():
    old = baseline(position(pnl='6000000'))
    events, state = evaluate(position(pnl='2000000'), previous=old)
    assert 'profit_giveback' in kinds(events)
    events, state = evaluate(position(pnl='-100000'), previous=state, stamp=300)
    assert 'profit_to_loss' in kinds(events)
    assert not evaluate(position(pnl='-100000'), previous=state, stamp=400)[0]


def test_loss_addition_merge_highlights_and_big_loss():
    old = baseline(position(pnl='-2000000'))
    events, _ = evaluate(position(size='140', notional='14000000', pnl='-4000000'), previous=old,
                         fills=[fill(before='100', size='40', stamp=200)], history=stats())
    assert len(events) == 1
    assert {'loss_addition', 'highlighted_address_action'} <= kinds(events)
    assert 'large_loss' not in kinds(events)


def test_liquidation_distance_and_actual_fill_not_disappearance():
    p = position(size='100', notional='10000000', liquidation='98000')
    events, state = evaluate(p)
    assert events==[]
    old=baseline(replace(p,liquidation=D('80000')))
    assert 'near_liquidation' in kinds(evaluate(p,previous=old,stamp=300)[0])
    events, _ = evaluate(None, previous=state, fills=[fill(before='100', side='A', stamp=200,
                                pnl='-6000000', liquidation={'liquidatedUser': A, 'method': 'market'})])
    assert 'actual_liquidation' in kinds(events)
    assert 'large_realized_result' not in kinds(events)
    assert 'actual_liquidation' not in kinds(evaluate(None, previous=state)[0])


def test_ordinary_close_with_large_pnl_is_reported_as_realized_result():
    old=baseline(position())
    events,_=evaluate(None,previous=old,fills=[fill(before='100',side='A',pnl='6000000',stamp=200)])
    assert 'large_realized_result' in kinds(events)
    assert 'actual_liquidation' not in kinds(events)


def test_completed_round_realized_result_even_when_last_partial_exit_small():
    fills = [fill(stamp=50), fill(2, before='100', size='90', side='A', pnl='2500000', stamp=90),
             fill(3, before='10', size='10', side='A', pnl='500000', stamp=200)]
    history = summarize(fills, start_ms=0, end_ms=200)
    events, _ = evaluate(None, previous=baseline(position(size='10', notional='1000000')),
                         fills=[fills[-1]], history=history)
    assert 'large_realized_result' in kinds(events)
    assert events[0].evidence['completed_round_results'][0]['pnl'] == '3000000'


def test_divergence_repeat_direction_change_and_rearm():
    states = {A: baseline(position()), B: baseline(position(size='-100'))}
    histories = {A: stats(), B: stats()}
    events, active = evaluate_divergence(observed_ms=200, states=states, histories=histories, previous={}, rules=Rules())
    assert kinds(events) == {'address_divergence'}
    assert not evaluate_divergence(observed_ms=300, states=states, histories=histories, previous=active, rules=Rules())[0]
    states[B] = baseline(position())
    _, active = evaluate_divergence(observed_ms=400, states=states, histories=histories, previous=active, rules=Rules())
    assert not active
    states[B] = baseline(position(size='-100'))
    assert evaluate_divergence(observed_ms=500, states=states, histories=histories, previous=active, rules=Rules())[0]


class FakeClient:
    fail = False
    pnl = '0'
    def dex_names(self): return ['', 'xyz']
    def markets(self, dex): return {COIN: {'oi_notional': '1000000000'}} if dex else {}
    def recent_trades(self, coin): return [{'coin': coin, 'users': [A, B], 'time': 100, 'tid': 1, 'px': '100000', 'sz': '30'}]
    def positions(self, address, dex):
        if self.fail and dex == 'xyz': raise RuntimeError('upstream failure')
        return {'assetPositions': [{'position': {'coin': COIN, 'szi': '100', 'positionValue': '10000000',
                'unrealizedPnl': self.pnl, 'liquidationPx': None}}] if dex else [], 'time': 100}
    def history(self, address, start_ms, end_ms):
        return [], {'pagination_complete': True, 'next_start_ms': start_ms, 'coverage': 'retained_api_history'}


def test_service_discovery_restart_failure_preserves_baseline_and_dedup(tmp_path):
    store = NewsStore(tmp_path / 'test.sqlite')
    client = FakeClient()
    monitor = NewsMonitor(store=store, client=client, leaderboard_seeds=0, addresses_per_pass=1)
    first = monitor.run_once(now_ms=100000)
    assert first['processed'] == 1
    assert len(store.addresses(100)) == 2
    assert len(store.candidates()) == 0
    monitor = NewsMonitor(store=NewsStore(store.path), client=client, leaderboard_seeds=0, addresses_per_pass=2)
    monitor.run_once(now_ms=200000)
    assert len(store.candidates()) == 0
    client.fail = True
    report = monitor.run_once(now_ms=300000)
    assert len(report['failed']) == 2
    assert store.load(A, state_table='hl_news_normal_state')['state']['observed_ms'] == 200000
    assert len(store.candidates()) == 0
    client.fail = False
    client.pnl = '2000000'
    monitor.run_once(now_ms=400000)
    assert len(store.candidates()) == 2


def test_atomic_observation_rolls_back_invalid_fill(tmp_path):
    store = NewsStore(tmp_path / 'test.sqlite'); store.initialize()
    store.discover(A, source={'kind': 'test'}, now_ms=100)
    events, state = evaluate(position())
    with pytest.raises(ValueError):
        store.commit_observation(address=A, now_ms=200, state=state, history={}, coverage={},
                                 fills=[{'coin': COIN}], candidates=events)
    assert store.load(A) is None and store.candidates() == []


def test_api_pagination_inclusive_boundary_and_timestamp_saturation():
    class Client(NewsClient):
        def __init__(self): self.requests = []
        def _post_info(self, payload):
            self.requests.append(payload)
            return [fill(i, stamp=10) for i in range(2000)]
    client = Client()
    rows, status = client.history(A, 0, 100)
    assert len(rows) == 2000
    assert client.requests[1]['startTime'] == 10
    assert status['reason'] == 'timestamp_saturated' and not status['pagination_complete']


def test_offline_replay_runs_persistence_and_merges_candidates():
    def raw(p):
        return {'coin': p.coin, 'szi': str(p.size), 'positionValue': str(p.notional),
                'unrealizedPnl': str(p.unrealized_pnl), 'liquidationPx': None}
    payload = {'observations': [
        {'observed_ms': 100, 'addresses': {A: {'positions': [raw(position())]}}},
        {'observed_ms': 200, 'addresses': {A: {'positions': [raw(position(pnl='2000000'))]}}},
        {'observed_ms': 300, 'addresses': {A: {'positions': [raw(position(pnl='2000000'))]}}}]}
    result = replay(payload)
    assert len(result['candidates']) == 1
    assert result['candidates'][-1]['kinds'] == ['large_profit']


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-Infinity'])
def test_invalid_numbers_cannot_advance_evidence(value):
    from packages.hyperliquid_news.models import number
    with pytest.raises(ValueError): number(value)


def test_spot_fills_excluded_and_unconvertible_fee_prevents_labels():
    h = summarize([fill(coin='@1'), fill(fee='0.1', feeToken='HYPE')], start_ms=0, end_ms=200)
    assert '@1' not in h['assets'] and not h['fee_currency_complete']


def test_same_timestamp_tid_hash_not_execution_order():
    fills = [fill(90, stamp=100), fill(10, before='100', size='40', side='A', pnl='400', stamp=100),
             fill(50, before='60', size='60', side='A', pnl='600', stamp=100)]
    asset = summarize(list(reversed(fills)), start_ms=0, end_ms=200)['assets'][COIN]
    assert asset['rounds'] == 1 and asset['completed_round_pnl'] == ['1000']
    assert asset['ambiguous_order_fills'] == 0


def test_ambiguous_same_time_rounds_excluded_but_pnl_preserved():
    fills = [fill(1, stamp=100), fill(2, before='100', side='A', pnl='100', stamp=100),
             fill(3, stamp=100), fill(4, before='100', side='A', pnl='100', stamp=100)]
    asset = summarize(fills, start_ms=0, end_ms=200)['assets'][COIN]
    assert asset['rounds'] == 0 and asset['closed_pnl_less_fees'] == '200'
    assert asset['ambiguous_order_fills'] == 4


def test_single_asset_win_rate_never_inherited_from_another_asset_or_account():
    from packages.hyperliquid_news.detector import highlights
    h = stats()
    h['win_rate'] = '0.99'
    assert 'asset_high_win_rate' in highlights(h, COIN, Rules())
    assert 'asset_high_win_rate' not in highlights(h, 'BTC', Rules())
    assert 'asset_high_win_rate' not in highlights({'win_rate': '0.99', 'rounds': 100}, COIN, Rules())


def test_leaderboard_seeds_union_all_periods_and_keep_source_metrics():
    from packages.hyperliquid_news.discovery import leaderboard_candidates
    c = '0x' + '3' * 40
    rows = [
        {'ethAddress': A, 'windowPerformances': [['week', {'pnl': '30000000'}], ['allTime', {'pnl': '1'}]]},
        {'ethAddress': B, 'windowPerformances': [['month', {'pnl': '40000000'}]]},
        {'ethAddress': c, 'windowPerformances': [['week', {'pnl': '-4000000'}], ['allTime', {'pnl': '50000000'}]]},
    ]
    candidates = list(leaderboard_candidates(rows, 1, 100))
    assert (A, 'week', 'profit') in [(a, s['window'], s['side']) for a, s in candidates]
    assert (B, 'month', 'profit') in [(a, s['window'], s['side']) for a, s in candidates]
    assert (c, 'week', 'loss') in [(a, s['window'], s['side']) for a, s in candidates]
    assert all('pnl' in source and 'rank' in source for _, source in candidates)


def test_small_trades_do_not_fill_discovery_pool_but_split_large_trade_can():
    from packages.hyperliquid_news.discovery import trade_candidates
    small = {'coin': COIN, 'time': 100, 'tid': 1, 'px': '100', 'sz': '1', 'users': [A, B]}
    assert not list(trade_candidates([small], COIN, Rules(), 200))
    trades = [{**small, 'tid': i, 'sz': '10000'} for i in range(3)]
    selected = list(trade_candidates(trades + [trades[0]], COIN, Rules(), 200))
    assert len(selected) == 2
    assert all(s['sample_notional'] == '3000000' for _, s in selected)


def test_unqualified_address_skips_history_but_important_position_queries_it(tmp_path):
    class Client(FakeClient):
        history_queries = 0
        large = False
        def recent_trades(self, coin): return []
        def positions(self, address, dex):
            if self.large:
                return super().positions(address, dex)
            return {'assetPositions': [], 'time': 100}
        def history(self, *args):
            self.history_queries += 1
            return super().history(*args)
    client = Client()
    store = NewsStore(tmp_path / 'eligibility.sqlite'); store.initialize()
    store.discover(A, source={'kind': 'unqualified_observation'}, now_ms=100)
    monitor = NewsMonitor(store=store, client=client, leaderboard_seeds=0)
    monitor.run_once(now_ms=200)
    assert client.history_queries == 0
    assert store.load(A, state_table='hl_news_normal_state')['coverage']['coverage'] == 'not_queried_no_eligibility'
    client.large = True
    monitor.run_once(now_ms=300)
    assert client.history_queries == 1
    assert 'important_position' in store.load(A, state_table='hl_news_normal_state')['history']['enrichment_reasons']


def test_official_profit_label_does_not_require_recomputed_fill_profit():
    from packages.hyperliquid_news.detector import highlights
    h = {'official_account_performance': {'source': 'hyperliquid_public_leaderboard',
         'windows': {'week': {'pnl': '30000000'}}, 'pnl_scope': 'official_leaderboard_not_assumed_realized'}}
    labels = highlights(h, COIN, Rules())
    assert 'official_account_high_profit' in labels
    assert 'account_high_profit' not in labels
    assert 'asset_high_profit' not in labels and 'asset_high_win_rate' not in labels


def test_leaderboard_source_updates_in_place_instead_of_growing_forever(tmp_path):
    store = NewsStore(tmp_path / 'sources.sqlite'); store.initialize()
    for stamp in range(100, 110):
        store.discover(A, source={'kind': 'leaderboard', 'window': 'week', 'side': 'profit',
                                 'pnl': str(stamp), 'observed_ms': stamp}, now_ms=stamp)
    import json
    sources = json.loads(store.addresses(1)[0]['sources'])
    assert len(sources) == 1 and sources[0]['observed_ms'] == 109


def test_coingecko_explicit_ids_cache_unknown_and_expired_caps():
    from packages.hyperliquid_news.market_cap import CoinGeckoMarketCaps
    class Response:
        status_code = 200
        def json(self): return [
            {'id': 'bitcoin', 'symbol': 'btc', 'market_cap': 100000000, 'last_updated': '2026-10-13T00:00:00Z'},
            {'id': 'old', 'market_cap': 123, 'last_updated': '2020-01-01T00:00:00Z'},
            {'id': 'missing', 'market_cap': None}]
    class Session:
        calls = 0
        def get(self, url, **kwargs):
            self.calls += 1
            assert kwargs['params']['vs_currency'] == 'usd'
            return Response()
    from datetime import datetime
    now = int(datetime.fromisoformat('2026-10-13T00:00:10+00:00').timestamp() * 1000)
    session = Session()
    client = CoinGeckoMarketCaps(session=session)
    caps = client.fetch({'BTC': 'bitcoin', 'OLD': 'old', 'MISSING': 'missing'}, now_ms=now)
    assert list(caps) == ['BTC'] and caps['BTC']['coin_id'] == 'bitcoin'
    assert client.fetch({'BTC': 'bitcoin'}, now_ms=now+1000, cached={'bitcoin': caps['BTC']}) == caps
    assert session.calls == 1
    assert client.fetch({'BTC': 'bitcoin'}, now_ms=now+3600000, cached={'bitcoin': caps['BTC']}) == {}


def test_coingecko_failure_does_not_prevent_position_observation(tmp_path):
    class Caps:
        def fetch(self, *args, **kwargs): raise RuntimeError('HTTP 429')
    monitor = NewsMonitor(store=NewsStore(tmp_path / 'cap-failure.sqlite'), client=FakeClient(),
                          leaderboard_seeds=0, market_cap_client=Caps(), market_cap_mapping={COIN: 'explicit-test-id'})
    result = monitor.run_once(now_ms=100000)
    assert result['processed'] == 2 and result['failed']['market_caps:coingecko'] == 'HTTP 429'


def test_first_snapshot_is_baseline_even_with_huge_pnl_and_near_liquidation():
    events,state=evaluate(position(pnl='6000000',liquidation='98000'),
        markets={COIN:{'market_cap':'100000000','oi_notional':'50000000'}})
    assert events==[]
    assert 'large_profit' not in state['signals'][COIN]


def test_price_only_large_position_value_change_is_news_but_not_an_action():
    old=baseline(position(notional='9000000'))
    events=evaluate(position(notional='12000000'),previous=old)[0]
    assert 'large_position' in kinds(events)
    assert 'large_position_action' not in kinds(events)


def test_profit_crossing_contains_comparable_before_after_and_does_not_repeat():
    old=baseline(position(pnl='1000000'))
    events,state=evaluate(position(pnl='3000000'),previous=old)
    assert kinds(events)=={'large_profit'}
    assert events[0].evidence['news_change']['previous_observed_ms']==old['observed_ms']
    assert events[0].evidence['news_change']['previous_floating_pnl']=='1000000'
    assert not evaluate(position(pnl='3000000'),previous=state,stamp=300)[0]


def test_newly_visible_old_position_without_fills_is_baseline_not_new_action():
    _,old=evaluate(None)
    events,state=evaluate(position(pnl='6000000'),previous=old)
    assert events==[] and COIN in state['positions']


def test_repeated_daily_snapshots_of_large_position_never_emit():
    p=position(pnl='6000000')
    events,state=evaluate(p)
    assert events==[]
    for stamp in (86400000,172800000,259200000):
        events,state=evaluate(p,previous=state,stamp=stamp)
        assert events==[]


def test_single_asset_win_rate_requires_five_complete_rounds():
    from packages.hyperliquid_news.detector import highlights
    h = stats(profit='0', account='0', win='1')
    for rounds in (1, 2, 4):
        h['assets'][COIN]['rounds'] = rounds
        assert 'asset_high_win_rate' not in highlights(h, COIN, Rules())
    h['assets'][COIN]['rounds'] = 5
    assert 'asset_high_win_rate' in highlights(h, COIN, Rules())


def test_history_continues_at_500_time_blocks_even_below_2000_fills():
    class Client(NewsClient):
        def __init__(self): self.requests=[]
        def _post_info(self,payload):
            self.requests.append(payload)
            if len(self.requests)==1:
                return [fill(i,stamp=i) for i in range(1,501)]
            return [fill(500,stamp=500),fill(501,stamp=501)]
    client=Client();rows,status=client.history(A,0,1000)
    assert len(rows)==501 and status['pagination_complete']
    assert client.requests[1]['startTime']==500


@pytest.mark.parametrize('amount,qualifies', [('999999',False),('1000000',True),('2999999',True)])
def test_win_rate_action_has_own_one_million_threshold(amount,qualifies):
    h=stats()
    h['assets'][COIN].pop('last_30d_closed_pnl_less_fees')
    p=position(size='10',notional=amount)
    events,_=evaluate(p,previous={'positions':{},'observed_ms':100},
        fills=[fill(size='10',px=str(D(amount)/10))],history=h)
    assert ('highlighted_address_action' in kinds(events))==qualifies
    if qualifies:
        event=next(e for e in events if 'highlighted_address_action' in e.kinds)
        assert event.evidence['highlights']==['asset_high_win_rate']
    assert 'newly_observed_address_position' not in kinds(events)


def test_high_profit_only_action_uses_two_million_threshold():
    h=stats(win='0.2')
    h['assets'][COIN]['last_30d_closed_pnl_less_fees']='5000000'
    events,_=evaluate(position(size='20',notional='2000000'),previous={'positions':{},'observed_ms':100},
        fills=[fill(size='20',px='100000')],history=h)
    assert 'highlighted_address_action' in kinds(events)
    assert 'asset_high_profit' in events[0].evidence['highlights']


def test_high_profit_only_action_below_two_million_is_rejected():
    h=stats(win='0.2')
    h['assets'][COIN]['last_30d_closed_pnl_less_fees']='5000000'
    events,_=evaluate(position(size='10',notional='1000000'),previous={'positions':{},'observed_ms':100},
        fills=[fill(size='10',px='100000')],history=h)
    assert 'highlighted_address_action' not in kinds(events)
