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
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'observed_ms': 2000,
             'evidence': {'history': {'official_account_total_pnl': total()}}}
    event['kinds'] = scene[3][:1]
    event['evidence']['highlights'] = scene[4]
    text = footer(event)
    assert text == f'地址：{A}；总盈亏：盈利615万美元'
    assert '样本截至' not in text
    assert '交易轮数' not in text and '统计时间' not in text


def test_multiple_addresses_keep_their_own_total():
    event = {'addresses': [A, B], 'coin': 'BTC', 'kinds': [], 'observed_ms': 2000, 'evidence': {
        'histories': {A: {'official_account_total_pnl': total()},
                      B: {'official_account_total_pnl': total('-2629700')}}}}
    text = footer(event)
    assert f'地址：{A}；总盈亏：盈利615万美元' in text
    assert f'地址：{B}；总盈亏：亏损约263万美元' in text


def test_stale_account_pnl_is_omitted_instead_of_misleading_readers():
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'observed_ms': 16 * 3600000,
             'evidence': {'history': {'official_account_total_pnl': total('476296.79')}}}
    assert footer(event) == f'地址：{A}'


@pytest.mark.parametrize('source,scope', [
    ('hyperliquid_portfolio', 'official_portfolio_including_unrealized'),
    ('hyperliquid_perpetual_portfolio', 'official_account_including_unrealized'),
])
def test_both_official_loss_producers_supply_total(source, scope):
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'observed_ms': 2000, 'evidence': {'history': {
        'official_account_loss': {'source': source, 'pnl_scope': scope,
                                 'windows': {'allTime': {'pnl': '-2629700', 'observed_ms': 1000}}}}}}
    assert f'地址：{A}；总盈亏：亏损约263万美元' in footer(event)


def test_unknown_or_leaderboard_pnl_is_explicitly_unavailable():
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'evidence': {'history': {
        'official_account_performance': {'source': 'hyperliquid_public_leaderboard',
                                        'windows': {'allTime': {'pnl': '99999999'}}}}}}
    assert footer(event) == f'地址：{A}'


@pytest.mark.parametrize('before,verb', [('0', '开仓'), ('1', '加仓')])
def test_contract_buy_word_uses_position_action(before, verb):
    event = {'addresses': [A], 'coin': 'BTC', 'kinds': [], 'evidence': {
        'position': {'size': '2'}, 'previous_position': {'size': before},
        'actions': {'open_long_notional': '23600000'},
        'observed_ms': 2000, 'history': {'official_account_total_pnl': total()}}}
    draft = DraftBrief(title='某地址买入约2360万美元BTC多单', content='该地址近期买入约2360万美元BTC多单，操作金额来自实际成交。')
    result = format_news(draft, event)
    assert '买入' not in result.title + result.content
    assert verb in result.title and verb in result.content


def test_high_win_rate_title_matches_flat_final_position_and_compacts_precision():
    event = {'addresses': [A], 'coin': 'BTC', 'observed_ms': 1000,
             'kinds': ['highlighted_address_action'], 'evidence': {
                 'highlights': ['asset_high_win_rate'],
                 'previous_position': {'size': '104.81036', 'notional': '8699050'},
                 'actions': {'open_long_notional': '1595888', 'close_long_notional': '10303041',
                             'close_long_average_execution_price_usd': '83068.299',
                             'open_long_average_execution_price_usd': '82971.69'},
                 'history': {'assets': {'BTC': {'rounds': 37, 'win_rate': '1'}}},
             }}
    result = format_news(DraftBrief(title='BTC交易胜率达100.00%的地址加仓约159.59万美元BTC多单',
                                    content='某地址交易BTC共37轮，胜率为100.00%。其平仓104.81036 BTC多单。'), event)
    assert result.title == '近期BTC胜率达100%的某地址平仓约1,030万美元BTC多单'
    assert result.content.startswith('Odaily星球日报讯 据Odaily监测，')
    assert '104.81 BTC' in result.content
    assert '100.00%' not in result.content
    assert '交易轮数' not in result.content and '统计时间' not in result.content
    assert '83,068美元' in result.content
    assert '82,972美元' in result.content
