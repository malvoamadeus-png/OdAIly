"""Stable editorial directions, independent of whether any records exist."""
from __future__ import annotations

SCENARIOS = [
    ('large_position', '巨大持仓', '持仓达到金额、市值占比或未平仓量占比门槛，并有重要建仓、加减仓变化。', ['large_position', 'large_position_action', 'oi_share', 'market_cap_share'], [], ['large_position', 'action', 'market_cap_share', 'oi_share']),
    ('large_pnl', '巨额单资产浮盈 / 浮亏', '同一仓位的浮盈或浮亏扩大达到变化额门槛；收窄和仓位增减导致的盈亏变化不计入。', ['large_profit', 'large_loss'], [], ['major_pnl', 'other_pnl']),
    ('asset_win_rate', '单资产高胜率地址操作', '该资产胜率达到门槛，且净仓位变化至少100万美元；往返成交不计为净操作。', ['highlighted_address_action'], ['asset_high_win_rate'], ['win_rate', 'min_rounds', 'win_rate_action']),
    ('asset_profit', '单资产高盈利地址操作', 'BTC / ETH 近30天交易净盈利至少300万美元，其他资产至少100万美元，且发生至少200万美元的重要操作。', ['highlighted_address_action'], ['asset_high_profit'], ['asset_profit', 'other_asset_profit', 'asset_profit_action']),
    ('account_profit', '账户高盈利地址操作', '账户历史盈利或官方周期收益达到门槛，且净仓位变化至少300万美元；两种收益口径分开展示。', ['highlighted_address_action'], ['account_high_profit', 'official_account_high_profit'], ['account_profit', 'action']),
    ('historical_loss', '账户总亏损高地址操作', '官方全时段合约账户总亏损至少2000万美元，并发生至少300万美元的重要净操作。', ['highlighted_address_action'], ['historical_loss'], ['historical_loss', 'action']),
    ('reversal', '大仓反手', '前后持仓方向相反，且反手前后仓位均达到重要金额门槛。', ['large_reversal'], [], ['action']),
    ('giveback', '浮盈回吐 / 双向盈亏转换', '同一仓位的浮盈从峰值显著回撤，或浮盈与浮亏之间发生转换。', ['profit_giveback', 'profit_to_loss', 'pnl_sign_flip'], [], ['giveback_fraction', 'giveback_major_pnl', 'giveback_other_pnl']),
    ('loss_addition', '巨亏后加仓', '原有仓位浮亏达到门槛后，继续同方向重要加仓。', ['loss_addition'], [], ['major_pnl', 'other_pnl', 'action']),
    ('realized', '大额主动平仓盈亏兑现', '非清算平仓成交带来达到门槛的已实现盈利或亏损。', ['large_realized_result'], [], ['action', 'major_pnl', 'other_pnl']),
    ('liquidation', '接近清算 / 实际清算', '重要仓位新进入清算价附近，或取得目标地址的实际大额清算成交。', ['near_liquidation', 'actual_liquidation'], [], ['action', 'liquidation_distance']),
    ('divergence', '同资产多空对决', '有历史亮点的多个地址，在同一资产持有方向相反的重要仓位；允许静态对照。', ['address_divergence'], [], ['action']),
    ('new_address', '新观察地址快速建仓', '新纳入观察的地址，在规定窗口内从无仓位到重要建仓，且有成交证据。', ['newly_observed_address_position'], [], ['new_address_window_ms', 'action']),
]

PRODUCTION_SCENARIOS = (
    'large_pnl', 'asset_win_rate', 'reversal', 'loss_addition', 'realized',
    'account_profit', 'historical_loss', 'asset_profit', 'giveback', 'liquidation',
)
UNRESEARCHED_SCENARIOS = tuple(s[0] for s in SCENARIOS if s[0] not in PRODUCTION_SCENARIOS)
EXPERIMENTAL_SCENARIOS = ()
TEST_SCENARIOS = tuple(dict.fromkeys((*UNRESEARCHED_SCENARIOS, *EXPERIMENTAL_SCENARIOS)))

PROMPT_GROUPS = {
    'hyperliquid_position_writer': '大仓持仓变化',
    'hyperliquid_large_pnl_writer': '单资产浮盈亏变化',
    'hyperliquid_asset_performance_writer': '单资产交易表现',
    'hyperliquid_account_result_writer': '账户盈亏与操作',
    'hyperliquid_reversal_writer': '大仓反手',
    'hyperliquid_giveback_writer': '浮盈回吐与盈亏转换',
    'hyperliquid_loss_addition_writer': '浮亏中继续加仓',
    'hyperliquid_realized_writer': '大额平仓盈亏兑现',
    'hyperliquid_liquidation_writer': '接近清算与实际清算',
    'hyperliquid_divergence_writer': '同资产多空分歧',
    'hyperliquid_new_address_writer': '新观察地址建仓',
    'hyperliquid_writer': '通用兜底',
}

PROMPT_KEY_BY_SCENARIO = {
    'large_position': 'hyperliquid_position_writer',
    'large_pnl': 'hyperliquid_large_pnl_writer',
    'asset_win_rate': 'hyperliquid_asset_performance_writer',
    'asset_profit': 'hyperliquid_asset_performance_writer',
    'account_profit': 'hyperliquid_account_result_writer',
    'historical_loss': 'hyperliquid_account_result_writer',
    'reversal': 'hyperliquid_reversal_writer',
    'giveback': 'hyperliquid_giveback_writer',
    'loss_addition': 'hyperliquid_loss_addition_writer',
    'realized': 'hyperliquid_realized_writer',
    'liquidation': 'hyperliquid_liquidation_writer',
    'divergence': 'hyperliquid_divergence_writer',
    'new_address': 'hyperliquid_new_address_writer',
}

PROMPT_PRIORITY = (
    'liquidation', 'giveback', 'large_pnl', 'realized', 'reversal', 'loss_addition',
    'historical_loss', 'account_profit', 'asset_win_rate', 'asset_profit',
    'large_position', 'divergence', 'new_address',
)


def scenario_condition(key: str, *, dataset: str = 'live') -> str:
    scenario = next((s for s in SCENARIOS if s[0] == key), None)
    if scenario is None:
        raise ValueError('Unknown Hyperliquid scenario')
    # Values below are fixed constants, never request values.
    if dataset == 'test':
        return f"EXISTS (SELECT 1 FROM json_each(c.payload, '$.test_scenarios') WHERE value='{key}')"
    def matches(path: str, values: list[str]) -> str:
        literals = ','.join("'" + value + "'" for value in values)
        return f"EXISTS (SELECT 1 FROM json_each(c.payload, '{path}') WHERE value IN ({literals}))"
    clause = matches('$.kinds', scenario[3])
    if scenario[4]:
        clause += ' AND ' + matches('$.evidence.highlights', scenario[4])
    return '(' + clause + ')'


def indexed_scenario_condition(key: str) -> str:
    if key not in {scenario[0] for scenario in SCENARIOS}:
        raise ValueError('Unknown Hyperliquid scenario')
    return ("c.event_key IN (SELECT event_key FROM hl_news_candidate_scenarios "
            f"WHERE scenario_key='{key}')")


def scenario_cards(counts: dict[str, int] | None = None, *, dataset: str = 'live') -> list[dict]:
    visible = PRODUCTION_SCENARIOS if dataset == 'live' else TEST_SCENARIOS
    return [dict(key=s[0], name=s[1], description=s[2], rule_keys=s[5], count=(counts or {}).get(s[0], 0),
                 prompt_key=PROMPT_KEY_BY_SCENARIO[s[0]],
                 prompt_name=PROMPT_GROUPS[PROMPT_KEY_BY_SCENARIO[s[0]]])
            for s in SCENARIOS if s[0] in visible]


def event_scenarios(event: dict) -> list[str]:
    kinds = set(event.get('kinds', []))
    labels = set(event.get('evidence', {}).get('highlights', []))
    return [s[0] for s in SCENARIOS if kinds.intersection(s[3]) and
            (not s[4] or labels.intersection(s[4]))]


def prompt_key_for_event(event: dict) -> str:
    matched = set(event_scenarios(event))
    for scenario in PROMPT_PRIORITY:
        if scenario in matched:
            return PROMPT_KEY_BY_SCENARIO[scenario]
    return 'hyperliquid_writer'
