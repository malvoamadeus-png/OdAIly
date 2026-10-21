"""Readable deterministic search facts; no value judging or final copy generation."""
from __future__ import annotations

from typing import Any

from .copy import money, rounded_percent, timestamp, writer_material
from .dashboard import KIND_LABELS
from .models import number


def search_material(event: dict[str, Any]) -> tuple[str, str]:
    e, coin = event['evidence'], event['coin']
    kinds = event['kinds']
    labels = '、'.join(KIND_LABELS[k] for k in kinds)
    lines = [f'Hyperliquid {coin}：{labels}。', '地址：'+'；'.join(event['addresses'])]
    change = e.get('news_change')
    if change:
        lines.append(f'变化时间：{timestamp(change["previous_observed_ms"])} 至 {timestamp(change["observed_ms"])}。')
    for key, label in (('previous_position', '此前'), ('position', '当前')):
        p = e.get(key)
        if p:
            side = '多单' if number(p['size'])>0 else '空单'
            lines.append(f'{label}持有 {abs(number(p["size"]))} 枚 {coin} {side}，价值 {money(p["notional"])}。')
            if any(k in kinds for k in ('large_profit','large_loss','profit_giveback','profit_to_loss','pnl_sign_flip','loss_addition','near_liquidation')):
                pnl=number(p['unrealized_pnl'])
                lines.append(f'{label}{"浮盈" if pnl>=0 else "浮亏"} {money(abs(pnl))}。')
        elif key=='position' and e.get('previous_position'):
            lines.append('当前已无该资产持仓。')
    action=e.get('actions',{})
    for key,label in (('open_long_notional','开仓或加仓多单'),('open_short_notional','开仓或加仓空单'),
                      ('close_long_notional','减仓或平仓多单'),('close_short_notional','减仓或平仓空单'),
                      ('liquidated_notional','实际被清算')):
        if number(action.get(key,'0'))>0:
            lines.append(f'{label} {money(action[key])}。')
    if 'large_realized_result' in kinds:
        if e.get('completed_round_results'):
            for r in e['completed_round_results']:
                lines.append(f'完整交易轮平仓盈亏 {money(r["pnl"])}。')
        elif action:
            pnl = action.get('ordinary_closed_pnl', action['closed_pnl'])
            lines.append(f'本次主动平仓盈亏 {money(pnl)}。')
    if any(k in kinds for k in ('profit_giveback','profit_to_loss')):
        lines.append(f'此前浮盈一度达到 {money(e["episode_peak_unrealized_pnl"])}。')
    elif 'pnl_sign_flip' in kinds:
        previous_pnl = e.get('news_change', {}).get('previous_floating_pnl')
        if previous_pnl is not None:
            lines.append(f'盈亏由浮{"亏" if number(previous_pnl)<0 else "盈"}{money(abs(number(previous_pnl)))}转为当前浮{"亏" if number((e.get("position") or {}).get("unrealized_pnl", "0"))<0 else "盈"}{money(abs(number((e.get("position") or {}).get("unrealized_pnl", "0"))))}。')
    facts=writer_material(event)['facts']
    for key,label in (('oi_share_percent','占合约未平仓量'),('market_cap_share_percent','持仓价值相当于代币市值')):
        if key in facts:
            value, approximate = rounded_percent(facts[key])
            lines.append(f'{label} {"约" if approximate else ""}{value}%。')
    if 'liquidation_price_usd' in facts:
        value, approximate = rounded_percent(facts['price_move_to_liquidation_percent'])
        lines.append(f'清算价 {facts["liquidation_price_usd"]} 美元，距离 {"约" if approximate else ""}{value}%。')
    if 'historical_highlights' in facts:
        h=facts['historical_highlights']
        if 'asset_win_rate' in h:
            value, approximate = rounded_percent(h['asset_win_rate'], fraction=True)
            lines.append(f'{coin} 胜率 {"约" if approximate else ""}{value}%，{h["asset_trade_rounds"]}个完整交易轮。')
        for key,label in (('asset_trading_pnl_usd',coin+'近30天交易盈亏' if h.get('asset_trading_window') else coin+'交易盈亏'),('account_trading_pnl_usd','账户交易盈亏')):
            if key in h:
                lines.append(f'{label} {money(h[key])}。')
        for period in h.get('account_period_performance',[]):
            lines.append(f'账户{period["period"]}收益 {money(period["profit_usd"])}。')
        for period in h.get('account_period_loss', []):
            lines.append(f'合约账户{period["period"]}亏损 {money(period["loss_usd"])}。')
    if 'address_divergence' in kinds:
        for side,addresses in e['sides'].items():
            for address in addresses:
                lines.append(f'{address} 持有 {coin} {"多单" if side=="long" else "空单"}，价值 {money(e["positions"][address]["notional"])}。')
    return f'{coin} {labels}', '\n'.join(lines)
