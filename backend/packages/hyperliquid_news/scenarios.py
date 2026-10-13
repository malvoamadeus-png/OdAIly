"""Stable editorial directions, independent of whether any records exist."""
from __future__ import annotations

SCENARIOS = [
    ('large_position', '巨大持仓', '持仓达到金额、市值占比或未平仓量占比门槛，并有重要建仓、加减仓变化。', ['large_position', 'large_position_action', 'oi_share', 'market_cap_share'], [], ['large_position', 'action', 'market_cap_share', 'oi_share']),
    ('large_pnl', '巨额单资产浮盈 / 浮亏', '单资产浮盈亏达到门槛，并出现可核对的前后变化；首次快照仅建立基线。', ['large_profit', 'large_loss'], [], ['major_pnl', 'other_pnl']),
    ('asset_win_rate', '单资产高胜率地址操作', '该资产胜率达到门槛，且发生至少100万美元的开仓、加仓、减仓、清仓或反手。', ['highlighted_address_action'], ['asset_high_win_rate'], ['win_rate', 'min_rounds', 'win_rate_action']),
    ('asset_profit', '单资产高盈利地址操作', '该资产历史已实现盈利达到门槛，且发生重要操作。', ['highlighted_address_action'], ['asset_high_profit'], ['asset_profit', 'action']),
    ('account_profit', '账户高盈利地址操作', '账户历史盈利或官方周期收益达到门槛，且发生重要操作；两种收益口径分开展示。', ['highlighted_address_action'], ['account_high_profit', 'official_account_high_profit'], ['account_profit', 'action']),
    ('historical_loss', '历史巨亏 / 连亏地址操作', '单资产历史亏损或连续亏损轮数达到门槛，且发生重要操作。', ['highlighted_address_action'], ['historical_loss'], ['historical_loss', 'losing_rounds', 'action']),
    ('reversal', '大仓反手', '前后持仓方向相反，且反手前后仓位均达到重要金额门槛。', ['large_reversal'], [], ['action']),
    ('giveback', '浮盈回吐 / 由盈转亏', '同一仓位的浮盈从峰值显著回撤，或从盈利变为亏损。', ['profit_giveback', 'profit_to_loss'], [], ['giveback_fraction', 'major_pnl', 'other_pnl']),
    ('loss_addition', '巨亏后加仓', '原有仓位浮亏达到门槛后，继续同方向重要加仓。', ['loss_addition'], [], ['major_pnl', 'other_pnl', 'action']),
    ('realized', '大额平仓盈亏兑现', '重要平仓成交带来达到门槛的已实现盈利或亏损。', ['large_realized_result'], [], ['action', 'major_pnl', 'other_pnl']),
    ('liquidation', '接近清算 / 实际清算', '重要仓位新进入清算价附近，或取得目标地址的实际大额清算成交。', ['near_liquidation', 'actual_liquidation'], [], ['action', 'liquidation_distance']),
    ('divergence', '同资产多空对决', '有历史亮点的多个地址，在同一资产持有方向相反的重要仓位；允许静态对照。', ['address_divergence'], [], ['action']),
    ('new_address', '新观察地址快速建仓', '新纳入观察的地址，在规定窗口内从无仓位到重要建仓，且有成交证据。', ['newly_observed_address_position'], [], ['new_address_window_ms', 'action']),
]


def scenario_condition(key: str) -> str:
    scenario = next((s for s in SCENARIOS if s[0] == key), None)
    if scenario is None:
        raise ValueError('Unknown Hyperliquid scenario')
    # Values below are fixed constants, never request values.
    def matches(path: str, values: list[str]) -> str:
        literals = ','.join("'" + value + "'" for value in values)
        return f"EXISTS (SELECT 1 FROM json_each(c.payload, '{path}') WHERE value IN ({literals}))"
    clause = matches('$.kinds', scenario[3])
    if scenario[4]:
        clause += ' AND ' + matches('$.evidence.highlights', scenario[4])
    return '(' + clause + ')'


def scenario_cards(counts: dict[str, int] | None = None) -> list[dict]:
    return [dict(key=s[0], name=s[1], description=s[2], rule_keys=s[5], count=(counts or {}).get(s[0], 0))
            for s in SCENARIOS]
