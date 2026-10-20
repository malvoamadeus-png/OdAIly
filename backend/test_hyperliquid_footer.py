"""The same factual address/PnL footer is required across editorial scenes."""
import pytest

from packages.hyperliquid_news.copy import footer, format_news
from packages.hyperliquid_news.scenarios import SCENARIOS
from packages.x_processing.models import DraftBrief

A = '0x' + '1' * 40
B = '0x' + '2' * 40


def total(pnl='6150000'):
    return {'source': 'hyperliquid_portfolio', 'scope': 'perpAllTime_including_unrealized',
            'pnl': pnl, 'observed_ms': 1000}


@pytest.mark.parametrize('scene', SCENARIOS, ids=lambda scene: scene[0])
def test_every_scene_has_inline_address_and_total(scene):
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [],
             'evidence': {'history': {'official_account_total_pnl': total()}}}
    event['kinds'] = scene[3][:1]
    event['evidence']['highlights'] = scene[4]
    text = footer(event)
    assert text.startswith(f'地址：{A}；总盈亏：615.00 万美元')
    assert '样本截至' in text


def test_multiple_addresses_keep_their_own_total():
    event = {'addresses': [A, B], 'coin': 'BTC', 'kinds': [], 'evidence': {
        'histories': {A: {'official_account_total_pnl': total()},
                      B: {'official_account_total_pnl': total('-2629700')}}}}
    text = footer(event)
    assert f'地址：{A}；总盈亏：615.00 万美元' in text
    assert f'地址：{B}；总盈亏：-262.97 万美元' in text


@pytest.mark.parametrize('source,scope', [
    ('hyperliquid_portfolio', 'official_portfolio_including_unrealized'),
    ('hyperliquid_perpetual_portfolio', 'official_account_including_unrealized'),
])
def test_both_official_loss_producers_supply_total(source, scope):
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'evidence': {'history': {
        'official_account_loss': {'source': source, 'pnl_scope': scope,
                                 'windows': {'allTime': {'pnl': '-2629700', 'observed_ms': 1000}}}}}}
    assert f'地址：{A}；总盈亏：-262.97 万美元' in footer(event)


def test_unknown_or_leaderboard_pnl_is_explicitly_unavailable():
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'evidence': {'history': {
        'official_account_performance': {'source': 'hyperliquid_public_leaderboard',
                                        'windows': {'allTime': {'pnl': '99999999'}}}}}}
    assert footer(event) == f'地址：{A}；总盈亏：未取得'


@pytest.mark.parametrize('before,verb', [('0', '开仓'), ('1', '加仓')])
def test_contract_buy_word_uses_position_action(before, verb):
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'evidence': {
        'position': {'size': '2'}, 'previous_position': {'size': before},
        'actions': {'open_long_notional': '23600000'},
        'history': {'official_account_total_pnl': total()}}}
    draft = DraftBrief(title='某地址买入约2360万美元BTC多单', content='该地址近期买入约2360万美元BTC多单，操作金额来自实际成交。')
    result = format_news(draft, event)
    assert '买入' not in result.title + result.content
    assert verb in result.title and verb in result.content
