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
    scaled = (amount / Decimal(10000)).quantize(Decimal('0.01'))
    if scaled == 0:
        scaled = abs(scaled)
    return f'{scaled:,.2f} 万美元'


def timestamp(value: Any) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(value) / 1000, UTC).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')


def source_url(event: dict[str, Any]) -> str:
    addresses = event.get('addresses', [])
    if not addresses:
        raise ValueError('Hyperliquid event has no target address')
    return f'https://hyperbot.network/trader/{address_key(addresses[0])}'


def footer(event: dict[str, Any]) -> str:
    """Only supplemental statistics; narrative position facts belong in the body."""
    lines = ['地址：' + '；'.join(address_key(a) for a in event['addresses'])]
    if 'highlighted_address_action' in event['kinds']:
        history = event['evidence'].get('history', {})
        asset = history.get('assets', {}).get(event['coin'], {})
        if 'asset_high_win_rate' in event['evidence'].get('highlights', []) and asset.get('rounds', 0) > 0 and asset.get('win_rate') is not None:
            lines.append(f'{event["coin"]} 交易轮数：{asset["rounds"]}；胜率：{number(asset["win_rate"]) * 100:.2f}%')
            start = asset.get('first_observed_fill_ms', history.get('first_observed_fill_ms'))
            end = asset.get('last_observed_fill_ms', history.get('last_observed_fill_ms'))
            if start is not None and end is not None:
                lines.append(f'统计时间：{timestamp(start)[:10]} 至 {timestamp(end)[:10]}')
    return '\n'.join(lines)


def format_news(draft: DraftBrief, event: dict[str, Any]) -> DraftBrief:
    content = draft.content.strip()
    for prefix in (ODAILY_PREFIX, 'Odaily星球日报讯', PREFIX, '据 Odaily 监测，'):
        if content.startswith(prefix):
            content = content.removeprefix(prefix).lstrip()
    normalized = format_brief(DraftBrief(title=draft.title, content=content))
    body = normalized.content.removeprefix(ODAILY_PREFIX).lstrip()
    return DraftBrief(title=normalized.title, content=PREFIX + body + '\n' + footer(event))


def _position_material(raw: dict[str, Any] | None, *, include_pnl: bool = False) -> dict[str, Any] | None:
    if not raw:
        return None
    facts = {'direction': '多单' if number(raw['size']) > 0 else '空单',
             'quantity': str(abs(number(raw['size']))), 'position_value_usd': raw['notional']}
    if include_pnl:
        facts['floating_pnl_usd'] = raw['unrealized_pnl']
    return facts


def _highlight_material(evidence: dict[str, Any], coin: str) -> dict[str, Any]:
    history = evidence.get('history', {})
    asset = history.get('assets', {}).get(coin, {})
    labels = evidence.get('highlights', [])
    facts: dict[str, Any] = {}
    if 'asset_high_win_rate' in labels and asset.get('rounds', 0) > 0 and asset.get('win_rate') is not None:
        facts['asset_win_rate'] = asset['win_rate']
        facts['asset_trade_rounds'] = asset['rounds']
    if any(label in labels for label in ('asset_high_profit', 'historical_loss')) and asset.get('closed_pnl_less_fees') is not None:
        facts['asset_trading_pnl_usd'] = asset['closed_pnl_less_fees']
        if asset.get('consecutive_losing_rounds', 0):
            facts['asset_consecutive_losing_rounds'] = asset['consecutive_losing_rounds']
    if 'account_high_profit' in labels and history.get('fills', 0) > 0 and history.get('account_closed_pnl_less_fees') is not None:
        facts['account_trading_pnl_usd'] = history['account_closed_pnl_less_fees']
    if 'official_account_high_profit' in labels:
        # Positive performance is eligible background only for an actual action.
        threshold = number(evidence.get('threshold_rules', {}).get('account_profit', '20000000'))
        facts['account_period_performance'] = [
            {'period': {'day': '近1天', 'week': '近7天', 'month': '近30天', 'allTime': '累计'}.get(window, window),
             'profit_usd': data['pnl']}
            for window, data in history.get('official_account_performance', {}).get('windows', {}).items()
            if number(data['pnl']) >= threshold]
    asset_start = asset.get('first_observed_fill_ms', history.get('first_observed_fill_ms'))
    asset_end = asset.get('last_observed_fill_ms', history.get('last_observed_fill_ms'))
    if asset_start is not None and asset_end is not None and any(k.startswith('asset_') for k in facts):
        facts['asset_trading_period'] = [timestamp(asset_start)[:10], timestamp(asset_end)[:10]]
    if 'account_trading_pnl_usd' in facts and history.get('first_observed_fill_ms') is not None and history.get('last_observed_fill_ms') is not None:
        facts['account_trading_period'] = [timestamp(history['first_observed_fill_ms'])[:10], timestamp(history['last_observed_fill_ms'])[:10]]
    return facts


def writer_material(event: dict[str, Any]) -> dict[str, Any]:
    """Allowlist editorial facts. Full provenance remains in the admin evidence."""
    from .dashboard import KIND_LABELS
    e, kinds = event['evidence'], event['kinds']
    show_pnl = any(k in kinds for k in ('large_profit', 'large_loss', 'profit_giveback',
                                        'profit_to_loss', 'loss_addition', 'near_liquidation'))
    facts: dict[str, Any] = {'position': _position_material(e.get('position'), include_pnl=show_pnl)}
    if e.get('previous_position'):
        facts['previous_position'] = _position_material(e['previous_position'], include_pnl=show_pnl)
    change = e.get('news_change')
    if change:
        facts['change_period'] = [timestamp(change['previous_observed_ms']), timestamp(change['observed_ms'])]
        if show_pnl and change.get('previous_floating_pnl') is not None:
            facts['previous_floating_pnl_usd'] = change['previous_floating_pnl']
    for kind in ('oi_share', 'market_cap_share'):
        ratio = e.get('ratios', {}).get(kind)
        if kind in kinds and ratio is not None:
            facts[kind + '_percent'] = str(number(ratio) * 100)
            old_ratio = e.get('previous_ratios', {}).get(kind)
            if old_ratio is not None:
                facts['previous_'+kind+'_percent'] = str(number(old_ratio)*100)
            if kind == 'market_cap_share':
                facts['token_market_cap_usd'] = e.get('market', {}).get('market_cap')
    if any(k in kinds for k in ('profit_giveback', 'profit_to_loss')):
        facts['peak_floating_profit_usd'] = e.get('episode_peak_unrealized_pnl')
        if e.get('episode_peak_ms') is not None:
            facts['peak_time'] = timestamp(e['episode_peak_ms'])
    if e.get('net_position_change'):
        facts['net_position_change'] = e['net_position_change']
    if e.get('previous_reported_floating_pnl') is not None:
        facts['previous_reported_floating_pnl_usd'] = e['previous_reported_floating_pnl']
        if e.get('previous_reported_floating_pnl_ms') is not None:
            facts['pnl_change_period'] = [timestamp(e['previous_reported_floating_pnl_ms']), timestamp(event['observed_ms'])]
    if e.get('actions'):
        facts['actions'] = {k: v for k, v in e['actions'].items() if k not in ('fill_keys','first_fill_ms','last_fill_ms')}
        if e['actions'].get('first_fill_ms') is not None:
            facts['action_period'] = [timestamp(e['actions']['first_fill_ms']), timestamp(e['actions']['last_fill_ms'])]
    if e.get('completed_round_results'):
        facts['completed_round_results'] = e['completed_round_results']
    if 'highlighted_address_action' in kinds:
        facts['historical_highlights'] = _highlight_material(e, event['coin'])
    p = e.get('position')
    if p and p.get('liquidation') is not None and any(k in kinds for k in ('large_loss', 'near_liquidation')):
        mark, liq = number(p['mark']), number(p['liquidation'])
        short = number(p['size']) < 0
        if liq > 0 and ((short and liq > mark) or (not short and liq < mark)):
            facts['liquidation_price_usd'] = p['liquidation']
            old = e.get('previous_position')
            if 'near_liquidation' in kinds and old and old.get('liquidation') is not None:
                old_mark, old_liq = number(old['mark']), number(old['liquidation'])
                facts['previous_price_move_to_liquidation_percent'] = str(abs(old_mark-old_liq)/old_mark*100)
            facts['price_move_to_liquidation'] = '上涨' if short else '下跌'
            facts['price_move_to_liquidation_percent'] = str(abs(mark-liq) / mark * 100)
    if 'address_divergence' in kinds:
        facts['sides'] = e['sides']
        facts['side_totals_usd'] = {side: str(sum((number(e['positions'][a]['notional']) for a in addresses), Decimal(0)))
                                    for side, addresses in e['sides'].items()}
        facts['positions'] = {a: _position_material(p) for a, p in e['positions'].items()}
        from .detector import highlights
        from .models import Rules
        defaults = Rules()
        rules = Rules(**{k: number(v) if isinstance(getattr(defaults, k), Decimal) else int(v)
                         for k, v in e.get('threshold_rules', {}).items()})
        facts['address_highlights'] = {a: _highlight_material({'history': h,
            'highlights': highlights(h, event['coin'], rules),
            'threshold_rules': e.get('threshold_rules', {})}, event['coin'])
            for a, h in e.get('histories', {}).items()}
    return {'coin': event['coin'], 'trigger_rules': [KIND_LABELS.get(k, k) for k in kinds], 'facts': facts}
