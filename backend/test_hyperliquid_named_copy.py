from packages.hyperliquid_news.copy import format_news, naming_explanation, normalize_display_numbers
from packages.x_processing.models import DraftBrief

A = '0x1f250df59a777d61cb8bd043c12970f3afe4f925'
NOW = 1794982635374


def named_event():
    return {'addresses': [A], 'coin': 'BTC', 'kinds': ['highlighted_address_action'], 'observed_ms': NOW,
        'evidence': {'highlights': ['historical_loss'],
            'previous_position': {'size': '1838.94197', 'mark': '64735', 'notional': '119043908'},
            'position': {'size': '1915.98618', 'mark': '64640', 'notional': '123849346'},
            'history': {'official_account_loss': {'windows': {'allTime': {'pnl': '-35435743'}}},
                        'official_account_total_pnl': {'pnl': '-35435743', 'observed_ms': NOW-200,
                            'source': 'hyperliquid_portfolio', 'scope': 'perpAllTime_including_unrealized'}},
            'address_names': {A: 'BTC爆赚交易员0x1f'},
            'address_name_evidence': {A: {'current_name': 'BTC爆赚交易员0x1f', 'kind': 'stable',
                'checked_ms': NOW-1000, 'evidence': {'selected_dimension': 'asset_profit'},
                'coverage': {'dimensions': [{'evidence_id': 'asset_profit', 'available': True,
                    'data': {'BTC': {'closed_pnl_less_fees': '6290598.58',
                        'first_observed_fill_ms': NOW-86400000, 'last_observed_fill_ms': NOW-2000}}}]}}}}}


def test_named_account_event_has_one_action_and_separate_scoped_evidence():
    draft = DraftBrief(title='总亏损约3544万美元，某地址增持约498万美元BTC多单',
        content='2026年11月18日13:42至14:17，某地址增持77.04枚BTC多单，净增持价值约498万美元；当前持有1915.99枚BTC多单，持仓价值约1.24亿美元。其合约账户累计亏损约3544万美元。')
    result = format_news(draft, named_event())
    assert result.title == 'BTC爆赚交易员0x1f增持约498万美元BTC多单'
    assert '77.04 枚 BTC' in result.content
    assert result.content.count('增持') == 2  # action plus its net value in the same sentence
    assert 'BTC交易净盈利约629万美元（扣手续费，不含资金费）' in result.content
    assert result.content.count('亏损') == 1
    assert result.content.endswith('地址：'+A+'；总盈亏：亏损约3,544万美元')


def test_future_and_other_asset_name_evidence_not_used():
    event = named_event()
    row = event['evidence']['address_name_evidence'][A]
    row['checked_ms'] = NOW + 1
    assert naming_explanation(row, event) is None
    row['checked_ms'] = NOW - 1
    row['current_name'] = 'ETH爆赚交易员0x1f'
    assert naming_explanation(row, event) is None


def test_quantity_precision_does_not_drop_decimal_zero():
    assert normalize_display_numbers('77.04 枚 BTC，1915.99 枚 BTC，1.234 枚 ETH') == '77.04 枚 BTC，1,915.99 枚 BTC，1.23 枚 ETH'


def test_named_asset_win_rate_title_does_not_repeat_feature():
    event = named_event()
    e = event['evidence']
    e['highlights'] = ['asset_high_win_rate']
    e['history']['assets'] = {'BTC': {'win_rate': '0.8', 'rounds': 10}}
    e['address_names'][A] = '近期BTC高胜率交易员0x1f'
    row = e['address_name_evidence'][A]
    row['current_name'] = e['address_names'][A]
    row['evidence']['selected_dimension'] = 'asset_win_rate'
    row['coverage']['dimensions'] = [{'evidence_id': 'asset_win_rate', 'available': True,
        'data': {'BTC': {'win_rate': '0.8', 'rounds': 10, 'window_start_ms': NOW-86400000, 'window_end_ms': NOW-2000}}}]
    result = format_news(DraftBrief(title='某地址增持BTC多单', content='某地址增持约498万美元BTC多单，近期BTC胜率80%。'), event)
    assert result.title.startswith('近期BTC高胜率交易员0x1f增持')
    assert result.content.count('胜率80%') == 1
