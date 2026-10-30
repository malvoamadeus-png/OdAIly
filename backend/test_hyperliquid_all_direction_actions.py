"""Regressions for net action and gross execution facts across live directions."""
from dataclasses import asdict

import pytest

from packages.hyperliquid_news.copy import format_news
from packages.x_processing.models import DraftBrief
from test_hyperliquid_news import A, COIN, baseline, evaluate, fill, position, stats


@pytest.mark.parametrize('label', ['asset_high_win_rate', 'asset_high_profit'])
def test_asset_action_roundtrip_does_not_qualify_from_turnover(label):
    history = stats(account='0', win='0.9' if label == 'asset_high_win_rate' else '0',
                    profit='8000000' if label == 'asset_high_profit' else '0')
    p = position()
    events, _ = evaluate(p, previous=baseline(p), history=history, fills=[
        fill(1, before='100', size='40', side='A', stamp=110),
        fill(2, before='60', size='40', side='B', stamp=120)])
    assert not any(label in e.evidence.get('highlights', []) for e in events)


def test_loss_addition_uses_net_increase_not_gross_opened_quantity():
    old = position(pnl='-3000000')
    events, _ = evaluate(position(size='110', notional='11000000', pnl='-4000000'),
        previous=baseline(old), fills=[
            fill(1, before='100', size='90', side='A', stamp=110),
            fill(2, before='10', size='100', side='B', stamp=120)])
    assert not any('loss_addition' in e.kinds for e in events)


def test_repeated_reversals_do_not_present_window_turnover_as_one_reversal():
    old = position()
    events, _ = evaluate(position(size='-100'), previous=baseline(old), fills=[
        fill(1, before='100', size='200', side='A', stamp=110),
        fill(2, before='-100', size='200', side='B', stamp=120),
        fill(3, before='100', size='200', side='A', stamp=130)])
    result = format_news(DraftBrief(title='反手操作', content='某地址反手操作。'), asdict(events[0]))
    assert '2,000万美元' not in result.title
    assert '持有' in result.title


def test_realized_title_preserves_realized_result_on_mixed_kind_event():
    old = position(pnl='6000000')
    events, _ = evaluate(position(size='60', notional='6000000'), previous=baseline(old), fills=[
        fill(1, before='100', size='40', side='A', pnl='3000000', stamp=110)])
    result = format_news(DraftBrief(title='平仓盈利300万美元', content='某地址平仓盈利300万美元。'), asdict(events[0]))
    assert '盈利300万美元' in result.title


def test_flat_roundtrip_realization_still_qualifies_as_realized_event():
    events, _ = evaluate(None, previous={'observed_ms': 0, 'positions': {}}, fills=[
        fill(1, before='0', size='100', side='B', stamp=110),
        fill(2, before='100', size='100', side='A', pnl='3000000', stamp=120)])
    assert any('large_realized_result' in e.kinds for e in events)


def test_all_directions_reject_unproven_close_then_reopen_chronology():
    from test_hyperliquid_news_scenarios import scenario
    event, _, _ = scenario('win_add')
    event['evidence']['actions'].update(open_notional='4000000', close_notional='4000000',
        close_long_notional='4000000', execution_sequence=None)
    with pytest.raises(ValueError, match='chronology'):
        format_news(DraftBrief(title='高胜率操作', content='某地址平仓400万美元多单后再开仓400万美元多单。'), event)


def test_verified_multistep_body_can_describe_a_subsequence_of_the_window():
    from packages.hyperliquid_news.copy import normalize_account_copy
    event = {'kinds': [], 'evidence': {'actions': {'open_notional': '8000000',
        'close_notional': '4000000', 'execution_sequence': [
            {'action': 'open_long'}, {'action': 'close_long'}, {'action': 'open_long'}]}}}
    draft = DraftBrief(title='操作情况', content='某地址开仓多单，然后平仓；之后再次开仓多单。')
    assert normalize_account_copy(draft, event) == draft


def test_legacy_liquidation_without_direction_does_not_infer_side_from_new_position():
    from packages.hyperliquid_news.copy import scenario_title
    event = {'coin': COIN, 'kinds': ['actual_liquidation'], 'evidence': {
        'position': {'size': '-100'}, 'previous_position': {'size': '100'},
        'actions': {'liquidated_notional': '10000000'}}}
    title = scenario_title(event)
    assert '清算' in title and '多单' not in title and '空单' not in title


def test_normal_monitor_preserves_baseline_until_fill_pagination_finishes(tmp_path):
    from packages.hyperliquid_news.service import NewsMonitor
    from packages.hyperliquid_news.store import NewsStore
    from test_hyperliquid_news import FakeClient
    store = NewsStore(tmp_path/'offline.sqlite')
    client = FakeClient()
    monitor = NewsMonitor(store=store, client=client, leaderboard_seeds=0, addresses_per_pass=1)
    monitor.run_once(now_ms=100000)
    old = store.load(A, state_table='hl_news_normal_state')['state']
    def partial(address, start_ms, end_ms):
        return [fill(before='100', size='40', side='A', stamp=150000)], {
            'pagination_complete': False, 'next_start_ms': 150000, 'coverage': 'retained_api_history'}
    client.history = partial
    monitor.run_once(now_ms=200000)
    # The scheduler also polls the second discovered address, so poll both once.
    monitor.addresses_per_pass = 2
    monitor.run_once(now_ms=210000)
    assert store.load(A, state_table='hl_news_normal_state')['state'] == old
    assert store.candidates() == []
    def complete(address, start_ms, end_ms):
        return [fill(2, before='60', size='40', side='B', stamp=160000)], {
            'pagination_complete': True, 'next_start_ms': start_ms, 'coverage': 'retained_api_history'}
    client.history = complete
    monitor.run_once(now_ms=220000)
    assert len(store.fills(A, 100000, 220000)) == 2
    assert store.load(A, state_table='hl_news_normal_state')['state']['observed_ms'] == 220000


def test_closing_losing_position_then_reopening_is_not_adding_to_that_loss():
    old = position(pnl='-3000000')
    events, _ = evaluate(position(size='140', notional='14000000', pnl='0'),
        previous=baseline(old), fills=[
            fill(1, before='100', size='100', side='A', stamp=110),
            fill(2, before='0', size='140', side='B', stamp=120)])
    assert not any('loss_addition' in e.kinds for e in events)


def test_actual_liquidation_side_is_execution_side_even_if_current_position_reversed():
    old = position()
    events, _ = evaluate(position(size='-100'), previous=baseline(old), fills=[
        fill(1, before='100', size='100', side='A', stamp=110, liquidation={'liquidatedUser': A}),
        fill(2, before='0', size='100', side='A', stamp=120)])
    result = format_news(DraftBrief(title='发生清算', content='某地址多单发生清算。'), asdict(events[0]))
    assert 'CL多单发生' in result.title
    assert 'CL空单发生' not in result.title


def production_sample(task_id):
    import json
    from pathlib import Path
    rows = json.loads((Path(__file__).parent/'fixtures/hyperliquid_ten_direction_audit.json').read_text())
    return next(r for r in rows if r['task_id'] == task_id)['event']


@pytest.mark.parametrize('task_id', [881700, 881849])
def test_production_asset_samples_below_net_line_are_blocked(task_id):
    from packages.hyperliquid_news.production import production_allowed
    from packages.hyperliquid_news.scenarios import PRODUCTION_SCENARIOS
    policy = {'enabled': True, 'started_ms': 1, 'enabled_scenarios': PRODUCTION_SCENARIOS}
    assert not production_allowed(production_sample(task_id), policy)


def test_production_flat_btc_title_and_eth_realized_title_are_corrected():
    from packages.hyperliquid_news.copy import scenario_title
    assert '清仓BTC多单' in scenario_title(production_sample(878214))
    assert '已实现亏损约587 万美元' in scenario_title(production_sample(936038))


@pytest.mark.parametrize('task_id', [886953, 887464, 934276])
def test_passive_production_events_keep_their_pnl_or_risk_meaning(task_id):
    from packages.hyperliquid_news.copy import scenario_title
    title = scenario_title(production_sample(task_id))
    assert title and ('浮盈' in title or '清算价' in title)
    assert not any(word in title for word in ['开仓', '平仓', '增持', '减仓'])


def test_realized_prompt_wins_over_address_highlights_and_position_action():
    from packages.hyperliquid_news.scenarios import prompt_key_for_event
    old = position(pnl='6000000')
    events, _ = evaluate(position(size='60', notional='6000000'), previous=baseline(old),
        history=stats(), fills=[fill(before='100', size='40', side='A', pnl='3000000')])
    assert prompt_key_for_event(asdict(events[0])) == 'hyperliquid_realized_writer'


def test_partial_history_legacy_candidates_are_blocked_before_dispatch():
    from packages.hyperliquid_news.production import production_allowed
    event = production_sample(934276)
    event['evidence'].setdefault('history', {})['api_coverage'] = {'pagination_complete': False}
    assert not production_allowed(event, {'enabled': True, 'started_ms': 1,
        'enabled_scenarios': ['large_pnl'], 'scenario_started_ms': 1})
