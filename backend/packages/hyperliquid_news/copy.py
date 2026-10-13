"""Deterministic attribution and factual footer, after shared writer2."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from packages.x_processing.formatter import ODAILY_PREFIX, format_brief
from packages.x_processing.models import DraftBrief
from .client import address_key
from .models import number

PREFIX = '据Odaily监测，'


def money(value: Any) -> str:
    amount = number(value)
    return f'{amount / Decimal(10000):,.2f} 万美元'


def timestamp(value: Any) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(value) / 1000, UTC).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')


def source_url(event: dict[str, Any]) -> str:
    addresses = event.get('addresses', [])
    if not addresses:
        raise ValueError('Hyperliquid event has no target address')
    return f'https://hyperbot.network/trader/{address_key(addresses[0])}'


def footer(event: dict[str, Any]) -> str:
    evidence = event['evidence']
    coin = event['coin']
    lines = ['地址：' + '；'.join(address_key(a) for a in event['addresses']), f'合约：{coin}']
    p = evidence.get('position')
    if p:
        direction = '多仓' if number(p['size']) > 0 else '空仓'
        lines += [f'当前持仓：{direction}，名义价值 {money(p["notional"])}',
                  f'当前浮盈亏：{money(p["unrealized_pnl"])}']
    market = evidence.get('market', {})
    ratios = evidence.get('ratios', {})
    if 'market_cap_share' in event['kinds'] and market.get('market_cap'):
        lines += [f'代币市值：{money(market["market_cap"])}（{market.get("market_cap_source", "外部市值来源")}）',
                  f'持仓相当于市值：{number(ratios["market_cap_share"]) * 100:.2f}%']
    if 'oi_share' in event['kinds'] and ratios.get('oi_share'):
        lines.append(f'占本合约未平仓名义金额：{number(ratios["oi_share"]) * 100:.2f}%')
    peak = evidence.get('episode_peak_unrealized_pnl')
    if peak is not None and p:
        lines += [f'监测期间浮盈峰值：{money(peak)}',
                  f'浮盈回吐：{money(number(peak) - number(p["unrealized_pnl"]))}']
        if evidence.get('episode_peak_ms'):
            lines.append(f'峰值观察时间：{timestamp(evidence["episode_peak_ms"])}（北京时间）')
    history = evidence.get('history', {})
    asset = history.get('assets', {}).get(coin)
    if asset:
        start = history.get('first_observed_fill_ms')
        end = history.get('last_observed_fill_ms')
        if start is not None and end is not None:
            lines.append(f'成交样本时间：{timestamp(start)} 至 {timestamp(end)}（北京时间）')
        lines.append(f'{coin} 已观察成交已实现盈亏：{money(asset["closed_pnl_less_fees"])}（扣可计价手续费，不含资金费）')
        lines.append(f'{coin} 完整交易轮数：{asset["rounds"]}')
        if asset.get('win_rate') is not None:
            lines.append(f'{coin} 胜率：{number(asset["win_rate"]) * 100:.2f}%')
        lines.append('统计范围：已取得的成交样本，非保证完整历史')
    official = history.get('official_account_performance', {})
    for window, data in official.get('windows', {}).items():
        label = {'day': '近1天', 'week': '近7天', 'month': '近30天', 'allTime': '全部时间'}.get(window, window)
        lines.append(f'官方榜单账户 PnL（{label}）：{money(data["pnl"])}')
    if history.get('fee_currency_complete') and history.get('account_closed_pnl_less_fees') is not None:
        lines.append(f'账户已观察合约成交已实现盈亏：{money(history["account_closed_pnl_less_fees"])}（不含资金费）')
    action = evidence.get('actions', {})
    if action:
        lines += [f'本次取得成交开仓金额：{money(action["open_notional"])}',
                  f'本次取得成交平仓金额：{money(action["close_notional"])}']
    for result in evidence.get('completed_round_results', []):
        lines.append(f'本轮平仓累计已实现盈亏：{money(result["pnl"])}')
    if evidence.get('liquidation_distance') is not None:
        lines.append(f'观察时点距清算价：{number(evidence["liquidation_distance"]) * 100:.2f}%（非清算预测）')
    if evidence.get('sides'):
        for side, addresses in evidence['sides'].items():
            for address in addresses:
                pos = evidence['positions'][address]
                lines.append(f'{"多方" if side == "long" else "空方"}地址 {address}：持仓 {money(pos["notional"])}')
    return '\n'.join(lines)


def format_news(draft: DraftBrief, event: dict[str, Any]) -> DraftBrief:
    content = draft.content.strip()
    for prefix in (ODAILY_PREFIX, 'Odaily星球日报讯', PREFIX, '据 Odaily 监测，'):
        if content.startswith(prefix):
            content = content.removeprefix(prefix).lstrip()
    normalized = format_brief(DraftBrief(title=draft.title, content=content))
    body = normalized.content.removeprefix(ODAILY_PREFIX).lstrip()
    return DraftBrief(title=normalized.title, content=PREFIX + body + '\n' + footer(event))


def writer_material(event: dict[str, Any]) -> dict[str, Any]:
    """Keep relevant facts while avoiding thousands of historical round arrays."""
    from copy import deepcopy
    result = deepcopy(event)
    evidence = result['evidence']
    history = evidence.get('history', {})
    assets = history.get('assets', {})
    selected = assets.get(event['coin'])
    if selected is not None:
        selected.pop('completed_round_pnl', None)
        selected.pop('completed_rounds', None)
    if 'assets' in history:
        history['assets'] = {event['coin']: selected} if selected is not None else {}
    for h in evidence.get('histories', {}).values():
        asset = h.get('assets', {}).get(event['coin'])
        h['assets'] = {event['coin']: {k: v for k, v in asset.items() if k not in ('completed_round_pnl', 'completed_rounds')}} if asset else {}
    return result
