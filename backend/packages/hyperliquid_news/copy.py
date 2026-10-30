"""Deterministic attribution and factual footer, after shared writer2."""
from __future__ import annotations

import re

from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any

from packages.x_processing.formatter import ODAILY_PREFIX, format_brief
from packages.x_processing.models import DraftBrief
from .client import address_key
from .models import number
from .actions import snapshot_net_change, single_reversal_legs

PREFIX = '据Odaily监测，'
ACCOUNT_PNL_MAX_AGE_MS = 60 * 60 * 1000
_SAMPLE_TIMESTAMP_PATTERN = re.compile(
    r'(?:[，,；;]\s*)?[（(]?\s*(?:(?:官方|账户|总盈亏)\s*)*'
    r'样本(?:截至|时间|日期)\s*[:：]?\s*'
    r'20\d{2}(?:[-/.年]\s*\d{1,2}(?:[-/.月]\s*\d{1,2}(?:日)?)?)?'
    r'(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*[）)]?'
)
_ACCOUNT_PNL_QUALIFIER_PATTERN = re.compile(
    r'[（(]\s*(?:(?:官方|合约账户|账户)?口径\s*)?(?:含|包含)\s*'
    r'(?:未实现的?)?(?:浮盈亏|浮动盈亏|未实现盈亏)\s*[）)]'
)


def money(value: Any) -> str:
    amount = number(value)
    if abs(amount) >= Decimal(100_000_000):
        scaled = (amount / Decimal(100_000_000)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        unit = '亿美元'
        rendered = f'{scaled:,.2f}'.rstrip('0').rstrip('.')
        approximate = scaled * Decimal(100_000_000) != amount
    else:
        scaled = (amount / Decimal(10_000)).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        unit = '万美元'
        rendered = f'{scaled:,.0f}'
        approximate = scaled * Decimal(10_000) != amount
    if scaled == 0:
        rendered = '0'
    return f'{"约" if approximate else ""}{rendered} {unit}'


def rounded_percent(value: Any, *, fraction: bool = False) -> tuple[str, bool]:
    percent = number(value) * (100 if fraction else 1)
    rounded = percent.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f'{rounded:.0f}', rounded != percent


def normalize_display_numbers(text: str) -> str:
    qualifier = r'(?P<qualifier>约(?:为)?|超过|超|逾|至少|不低于|高于|大于|达到|达|不超过|至多|不足|低于|不到|少于)?'
    amount_pattern = re.compile(
        qualifier + r'\s*(?P<sign>[-−]?)(?P<amount>[\d,]+(?:\.\d+)?)\s*(?P<unit>万美元|万美金|亿美元|亿美金)')
    floor_words = {'超过', '超', '逾', '至少', '不低于', '高于', '大于', '达到'}
    ceiling_words = {'不超过', '至多', '不足', '低于', '不到', '少于'}

    def normalize_amount(match: re.Match[str]) -> str:
        amount = number(match.group('sign') + match.group('amount').replace(',', ''))
        unit = match.group('unit')
        is_yi = unit.startswith('亿')
        dollars = amount * (Decimal(100_000_000) if is_yi else Decimal(10_000))
        prefix = match.group('qualifier') or ''
        rounding = (ROUND_FLOOR if prefix in floor_words else
                    ROUND_CEILING if prefix in ceiling_words else ROUND_HALF_UP)
        if abs(dollars) < Decimal(100_000_000):
            scaled = (dollars / Decimal(10_000)).quantize(Decimal('1'), rounding=rounding)
            rendered = f'{scaled:,.0f}'
            output_unit = '万美金' if unit.endswith('美金') else '万美元'
            changed = scaled * Decimal(10_000) != dollars
        else:
            scaled = (dollars / Decimal(100_000_000)).quantize(Decimal('0.01'), rounding=rounding)
            rendered = f'{scaled:,.2f}'.rstrip('0').rstrip('.')
            output_unit = '亿美金' if unit.endswith('美金') else '亿美元'
            changed = scaled * Decimal(100_000_000) != dollars
        if changed and prefix in ('达', '达到'):
            prefix = '约'
        elif changed and not prefix:
            prefix = '约'
        return f'{prefix}{rendered}{output_unit}'

    text = amount_pattern.sub(normalize_amount, text)
    quantity_pattern = re.compile(
        r'(?P<amount>[\d,]+(?:\.\d{3,})?)(?P<space>\s*)(?P<coin>BTC|ETH)\b')

    def normalize_quantity(match: re.Match[str]) -> str:
        quantity = number(match.group('amount').replace(',', ''))
        if abs(quantity) < Decimal('0.01'):
            return match.group(0)
        rounded = quantity.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        rendered = f'{rounded:,.2f}'.rstrip('0').rstrip('.')
        return f'{rendered}{match.group("space")}{match.group("coin")}'

    text = quantity_pattern.sub(normalize_quantity, text)
    percent_pattern = re.compile(qualifier + r'\s*(?P<sign>[-−]?)(?P<percent>[\d,]+(?:\.\d+)?)\s*[%％]')

    def normalize_percent(match: re.Match[str]) -> str:
        prefix = match.group('qualifier') or ''
        value = number(match.group('sign') + match.group('percent').replace(',', ''))
        rounding = (ROUND_FLOOR if prefix in floor_words else
                    ROUND_CEILING if prefix in ceiling_words else ROUND_HALF_UP)
        rounded = value.quantize(Decimal('1'), rounding=rounding)
        if rounded != value and prefix in ('达', '达到'):
            prefix = '约'
        elif rounded != value and not prefix:
            prefix = '约'
        return f'{prefix}{rounded:.0f}%'

    return percent_pattern.sub(normalize_percent, text)


def normalize_reader_terms(text: str) -> str:
    """Replace exchange terminology that adds no useful meaning for readers."""
    text = re.sub(r'(?P<action>开仓|加仓|平仓|减仓)?名义金额(?:为|是)?',
                  lambda match: (match.group('action') or '') + '金额', text)
    text = text.replace('名义价值', '持仓价值')
    text = re.sub(r'按持仓估值参考价格计价值', '持仓价值', text)
    text = re.sub(r'按\s*[\d,.]+\s*美元参考价(?:格)?计(?:持仓价值|仓位估值|价值)',
                  '持仓价值', text)
    text = re.sub(r'按标记价\s*[\d,.]+\s*美元(?:计算|折算)?[，,]?\s*', '', text)
    text = text.replace('仓位估值', '持仓价值')
    price_pattern = re.compile(
        r'(?P<label>加权成交价|成交均价|平均执行价|执行均价|执行价)'
        r'(?P<separator>\s*(?:约?为|[:：])?\s*)'
        r'(?P<price>[\d,]+(?:\.\d+)?)\s*美元')

    def normalize_price(match: re.Match[str]) -> str:
        label = '成交均价' if match.group('label') in {'平均执行价', '执行均价', '执行价'} else match.group('label')
        price = match.group('price').replace(',', '')
        return f'{label}{match.group("separator")}{_execution_price(price)}美元'

    text = price_pattern.sub(normalize_price, text)
    return text


def _remove_sample_timestamps(text: str) -> str:
    return _SAMPLE_TIMESTAMP_PATTERN.sub('', text)


def timestamp(value: Any) -> str:
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(value) / 1000, UTC).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')


def _execution_price(value: Any) -> str:
    amount=number(value)
    if abs(amount) >= 100:
        return f'{amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP):,.0f}'
    precision=2 if abs(amount) >= 1 else 12
    rendered=f'{amount:,.{precision}f}'.rstrip('0').rstrip('.')
    if '.' not in rendered:
        return rendered+'.00'
    if len(rendered.rsplit('.',1)[1]) == 1:
        return rendered+'0'
    return rendered


def _high_win_execution_prices(event: dict[str, Any]) -> str | None:
    evidence=event.get('evidence',{})
    if 'asset_high_win_rate' not in evidence.get('highlights',[]):
        return None
    actions=evidence.get('actions') or {}
    previous=evidence.get('previous_position') or {}
    current=evidence.get('position') or {}
    close_side='long' if number(previous.get('size',0)) > 0 else 'short'
    if current:
        open_side='long' if number(current.get('size',0)) > 0 else 'short'
    else:
        opened = [side for side in ('long', 'short') if number(actions.get(f'open_{side}_notional', 0)) > 0]
        if len(opened) != 1:
            return None
        open_side = opened[0]
    close_amount=number(actions.get(f'close_{close_side}_notional',0))
    open_amount=number(actions.get(f'open_{open_side}_notional',0))
    close_price=actions.get(f'close_{close_side}_average_execution_price_usd')
    open_price=actions.get(f'open_{open_side}_average_execution_price_usd')
    if close_amount <= 0 or open_amount <= 0 or close_price is None or open_price is None:
        return None
    coin=event.get('coin','')
    return (f'窗口内{coin}{"多" if close_side == "long" else "空"}单平仓的加权成交价约为'
            f'{_execution_price(close_price)}美元，{coin}{"多" if open_side == "long" else "空"}单开仓的加权成交价约为'
            f'{_execution_price(open_price)}美元。')


def source_url(event: dict[str, Any]) -> str:
    addresses = event.get('addresses', [])
    if not addresses:
        raise ValueError('Hyperliquid event has no target address')
    return f'https://hyperbot.network/trader/{address_key(addresses[0])}'


def footer(event: dict[str, Any]) -> str:
    """Append an address and only account performance current enough to read as news."""
    lines = []
    observed_ms = event.get('observed_ms')
    if observed_ms is None:
        actions = event.get('evidence', {}).get('actions') or {}
        observed_ms = actions.get('last_fill_ms', actions.get('first_fill_ms'))
    history = event['evidence'].get('history', {})
    histories = event['evidence'].get('histories', {})
    for address in event['addresses']:
        address_history = histories.get(address, histories.get(address.lower(), {})) if len(event['addresses']) > 1 else history
        total = _account_total_sample(address_history, as_of_ms=observed_ms)
        line = f'地址：{address_key(address)}'
        stamp = total.get('observed_ms', total.get('history_sample_ms')) if total else None
        if (total is not None and observed_ms is not None and stamp is not None and
                0 <= int(observed_ms) - int(stamp) <= ACCOUNT_PNL_MAX_AGE_MS):
            pnl = number(total['pnl'])
            pnl_copy = ('盈亏平衡' if pnl == 0 else
                        ('盈利' if pnl > 0 else '亏损') + money(abs(pnl)).replace(' ', ''))
            line += f'；总盈亏：{pnl_copy}'
        lines.append(line)
    return '\n'.join(lines)


def _account_total_sample(history: dict[str, Any], *, as_of_ms: Any = None) -> dict[str, Any] | None:
    candidates = []
    total = history.get('official_account_total_pnl')
    if (isinstance(total, dict) and total.get('source') == 'hyperliquid_portfolio' and
            total.get('scope') == 'perpAllTime_including_unrealized' and total.get('pnl') is not None):
        candidates.append(total)
    allowed_sources = {
        ('hyperliquid_portfolio', 'official_portfolio_including_unrealized'),
        ('hyperliquid_perpetual_portfolio', 'official_account_including_unrealized'),
        ('hyperliquid_portfolio_historical', None),
    }
    for key in ('official_account_performance', 'official_account_loss'):
        performance = history.get(key, {})
        if (not isinstance(performance, dict) or
                (performance.get('source'), performance.get('pnl_scope')) not in allowed_sources):
            continue
        sample = performance.get('windows', {}).get('allTime')
        if isinstance(sample, dict) and sample.get('pnl') is not None:
            candidates.append(sample)
    dated = []
    for sample in candidates:
        stamp = sample.get('observed_ms', sample.get('history_sample_ms'))
        if stamp is None:
            continue
        stamp = int(stamp)
        if as_of_ms is not None and stamp > int(as_of_ms):
            continue
        dated.append((stamp, sample))
    if dated:
        return max(dated, key=lambda item: item[0])[1]
    return candidates[0] if candidates and as_of_ms is None else None


def _ensure_account_title_subject(title: str) -> str:
    if '地址' in title:
        return title
    match = re.match(r'^(总(?:盈利|亏损)(?:约|超|超过)?[\d,.]+(?:万美元|亿美元))(.*)$', title)
    if not match:
        return title
    remainder = re.sub(r'^[，,]\s*', '', match.group(2))
    return f'{match.group(1)}，某地址{remainder}'


def _account_action_detail(event: dict[str, Any]) -> str | None:
    """Build an amount-bound action phrase when the model omitted it."""
    evidence = event.get('evidence', {})
    actions = evidence.get('actions') or {}
    contract = str(event.get('coin', ''))
    coin = contract.rsplit(':', 1)[-1]
    previous = evidence.get('previous_position') or {}
    current = evidence.get('position') or {}
    previous_size = number(previous.get('size', '0'))
    current_size = number(current.get('size', '0'))
    close_side = 'long' if previous_size > 0 else 'short'
    if previous_size == 0:
        close_side = 'long' if number(actions.get('close_long_notional', '0')) > 0 else 'short'
    open_side = 'long' if current_size > 0 else 'short'
    if current_size == 0:
        open_side = 'long' if number(actions.get('open_long_notional', '0')) > 0 else 'short'
    close_amount = number(actions.get(f'close_{close_side}_notional', '0'))
    open_amount = number(actions.get(f'open_{open_side}_notional', '0'))
    if close_amount > 0 and open_amount > 0:
        close_copy = f'平仓{money(close_amount).replace(" ", "")}{coin}{"多" if close_side == "long" else "空"}单'
        open_copy = f'开仓{money(open_amount).replace(" ", "")}{coin}{"多" if open_side == "long" else "空"}单'
        return f'窗口内累计{close_copy}，累计{open_copy}'
    if close_amount > 0:
        side = '多' if close_side == 'long' else '空'
        return f'减仓{money(close_amount).replace(" ", "")}{coin}{side}单'
    if open_amount > 0:
        side = '多' if open_side == 'long' else '空'
        verb = '加仓' if previous_size and previous_size * current_size > 0 else '开仓'
        return f'{verb}{money(open_amount).replace(" ", "")}{coin}{side}单'
    return None


def _ensure_account_action_amounts(title: str, content: str, event: dict[str, Any]) -> tuple[str, str]:
    """Keep account-result copy from collapsing a supported action to a bare verb."""
    detail = _account_action_detail(event) or _highlight_action_description(
        event.get('evidence', {}), str(event.get('coin', '')))
    if not detail:
        return title, content
    action_pattern = re.compile(
        r'(?:开仓|加仓|平仓|减仓|减持|开多|开空|平掉)[^。；，,]{0,50}?(?:多单|空单|开多|开空)(?:后再开[多空])?')
    has_amount = re.compile(r'(?:开仓|加仓|平仓|减仓|减持|增持|减少|开多|开空|平掉)[约\d,.\s]+(?:万美元|亿美元)')
    if not has_amount.search(content):
        if action_pattern.search(content):
            content = action_pattern.sub(detail, content, count=1)
        else:
            content = content.rstrip('。！？') + '。' + detail + '。'
    if not has_amount.search(title):
        subject = title.find('某地址')
        if subject >= 0:
            title = title[:subject] + '某地址' + detail
        elif action_pattern.search(title):
            title = action_pattern.sub(detail, title, count=1)
    change = snapshot_net_change(event.get('evidence', {}))
    operation_only = not set(event.get('kinds', [])).intersection(
        {'large_realized_result', 'actual_liquidation', 'near_liquidation', 'large_reversal',
         'large_profit', 'large_loss', 'profit_giveback', 'profit_to_loss', 'pnl_sign_flip', 'loss_addition'})
    if change and number(change['estimated_value']) > 0 and operation_only:
        description = _highlight_action_description(event['evidence'], str(event.get('coin', '')))
        if description:
            subject = re.search(r'某地址|该地址|地址', title)
            if subject:
                title = title[:subject.start()] + '某地址' + description
    return title, content


def normalize_account_copy(draft: DraftBrief, event: dict[str, Any]) -> DraftBrief:
    """Enforce account profit/loss wording independently of model compliance."""
    labels = event.get('evidence', {}).get('highlights', [])
    if ('highlighted_address_action' not in event.get('kinds', []) or
            not set(labels) & {'official_account_high_profit', 'account_high_profit', 'historical_loss'}):
        _validate_action_chronology(draft, event)
        return draft
    title = draft.title.strip()
    evidence = event['evidence']
    all_time = evidence.get('history', {}).get('official_account_performance', {}).get('windows', {}).get('allTime')
    threshold = number(evidence.get('threshold_rules', {}).get('account_profit', '20000000'))
    prefix = r'^(?:(?:合约)?账户累计(?:收益|盈利)|累计账户(?:收益|盈利)|累计(?:收益|盈利))'
    account = bool(set(labels) & {'official_account_high_profit', 'account_high_profit'})
    if account and re.match(prefix, title):
        if not all_time or number(all_time['pnl']) < threshold:
            raise ValueError('Cumulative account title requires qualifying official allTime evidence')
        title = re.sub(prefix, '总盈利', title, count=1)
    if account and title.startswith('总盈利') and (not all_time or number(all_time['pnl']) < threshold):
        raise ValueError('Total profit title requires qualifying official allTime evidence')
    if account and title.startswith('总盈利'):
        title = _ensure_account_title_subject(title)
    if 'historical_loss' in labels and re.match(r'^(?:(?:合约)?账户)?(?:累计|总)亏损', title):
        loss = evidence.get('history', {}).get('official_account_loss', {})
        if number(loss.get('windows', {}).get('allTime', {}).get('pnl', '0')) > -number(evidence.get('threshold_rules', {}).get('historical_loss', '20000000')):
            raise ValueError('Total loss title requires qualifying perpetual allTime evidence')
        title = re.sub(r'^(?:(?:合约)?账户)?(?:累计|总)亏损', '总亏损', title, count=1)
    if 'historical_loss' in labels and title.startswith('总亏损'):
        title = _ensure_account_title_subject(title)
    title = title.replace('的地址', '地址')
    content = re.sub(r'^(据\s*Odaily\s*监测[，,]\s*)?该地址',
                     lambda match: (match.group(1) or '') + '某地址', draft.content.strip(), count=1)
    if 'historical_loss' in labels:
        content = re.sub(r'^(据\s*Odaily\s*监测[，,]\s*)?某地址(?:在累计统计中|在统计期内累计|累计统计)',
                         lambda m: (m.group(1) or '') + '某地址合约账户累计', content, count=1)
        content = re.sub(r'^(据\s*Odaily\s*监测[，,]\s*)?某地址(?=累计亏损|近\s*\d+\s*[天日周月])',
                         lambda m: (m.group(1) or '') + '某地址合约账户', content, count=1)
    notes = evidence.get('address_notes', {})
    address = event.get('addresses', [''])[0]
    note = notes.get(address, '')
    if address in notes:
        # Remarks are inserted as literal data after generation, never model instructions.
        note = str(note).strip()
        content = re.sub(r'^(据\s*Odaily\s*监测[，,]\s*)?某地址(合约账户)?(?:（[^）]*）)?',
            lambda m: (m.group(1) or '') + '某地址' + (m.group(2) or '') + ('（' + note + '）' if note else ''), content, count=1)
    title, content = _ensure_account_action_amounts(title, content, event)
    if 'position' in evidence and evidence['position'] is None:
        coin = str(event.get('coin', '')).rsplit(':', 1)[-1]
        if not re.search(r'清仓|空仓|已无.*仓位', content):
            content = content.rstrip('。！？') + f'。其已无{coin}仓位。'
    _validate_action_chronology(DraftBrief(title=title, content=content), event)
    return DraftBrief(title=title, content=content)


def _validate_action_chronology(draft: DraftBrief, event: dict[str, Any]) -> None:
    """Every direction shares the same execution-order evidence boundary."""
    actions = event.get('evidence', {}).get('actions') or {}
    if (number(actions.get('open_notional', 0)) > 0 and
            number(actions.get('close_notional', 0)) > 0):
        sequence = actions.get('execution_sequence') or []
        families = []
        for leg in sequence:
            family = leg['action'].split('_', 1)[0]
            if not families or families[-1] != family:
                families.append(family)
        ordered_claim = re.compile(
            r'(平仓|减仓|开仓|加仓|开多|开空|平多|平空)[^。；]*?(?:随后|然后|后再|之后|后)[^。；]*?(平仓|减仓|开仓|加仓|开多|开空|平多|平空)')
        for text in (draft.title, draft.content):
            for match in ordered_claim.finditer(text):
                claimed = ['close' if verb in ('平仓', '减仓', '平多', '平空') else 'open' for verb in match.groups()]
                # A body may describe part of a verified multistep window.
                supported = any(families[i:i+2] == claimed for i in range(len(families)-1))
                if claimed[0] != claimed[1] and not supported:
                    raise ValueError('Action chronology requires matching execution sequence')


def format_news(draft: DraftBrief, event: dict[str, Any]) -> DraftBrief:
    contract = str(event.get('coin', ''))
    display_coin = contract.rsplit(':', 1)[-1]
    if contract and display_coin != contract:
        draft = DraftBrief(title=draft.title.replace(contract, display_coin),
                           content=draft.content.replace(contract, display_coin))
    draft = DraftBrief(title=draft.title.replace('降至清仓', '降至空仓'),
                       content=draft.content.replace('降至清仓', '降至空仓'))
    draft = normalize_account_copy(draft, event)
    draft = normalize_position_words(draft, event)
    content = draft.content.strip()
    for prefix in (ODAILY_PREFIX, 'Odaily星球日报讯', PREFIX, '据 Odaily 监测，'):
        if content.startswith(prefix):
            content = content.removeprefix(prefix).lstrip()
    normalized = format_brief(DraftBrief(title=draft.title, content=content))
    body = normalized.content.removeprefix(ODAILY_PREFIX).lstrip()
    body = normalize_account_copy(DraftBrief(title=normalized.title, content=body), event).content
    first_address = re.search(r'某地址|该地址', body)
    if first_address and first_address.group() == '该地址':
        body = body[:first_address.start()] + '某地址' + body[first_address.end():]
    body = body.replace('该地址', '其')
    price_sentence=_high_win_execution_prices(event)
    if price_sentence and not any(term in body for term in ('加权成交价','平均成交价','成交均价')):
        body=body.rstrip()
        if body and body[-1] not in '。！？':
            body+='。'
        body+=price_sentence
    title = scenario_title(event) or normalized.title
    normalized_account_copy = normalize_account_copy(DraftBrief(title=title, content=body), event)
    title, body = normalized_account_copy.title, normalized_account_copy.content
    evidence = event.get('evidence', {})
    if ('position' in evidence and evidence['position'] is None and evidence.get('actions')
            and not re.search(r'清仓|空仓|已无.*仓位', body)):
        body = body.rstrip('。！？') + f'。其已无{display_coin}仓位。'
    title = _remove_sample_timestamps(title)
    body = _remove_sample_timestamps(body)
    body = _ACCOUNT_PNL_QUALIFIER_PATTERN.sub('', body)
    title = normalize_display_numbers(title)
    body = normalize_display_numbers(body)
    title = normalize_reader_terms(title)
    body = normalize_reader_terms(body)
    return DraftBrief(title=title, content=ODAILY_PREFIX + PREFIX + body + '\n' + footer(event))


def normalize_position_words(draft: DraftBrief, event: dict[str, Any]) -> DraftBrief:
    """Describe long positions as positions, with action verbs only when evidenced."""
    coin = str(event.get('coin', '')).rsplit(':', 1)[-1]
    if not coin:
        return draft
    evidence = event.get('evidence', {})
    actions = evidence.get('actions') or {}
    current = evidence.get('position') or {}
    previous = evidence.get('previous_position') or {}
    side = 'long' if number((current or previous).get('size', '0')) > 0 else 'short'
    opening = number(actions.get(f'open_{side}_notional', '0'))
    change = (evidence.get('net_position_change') or {}).get('direction')
    if opening > 0:
        same_side_before = previous and number(previous.get('size', '0')) * number((current or previous).get('size', '0')) > 0
        verb = '加仓' if same_side_before else '开仓'
    elif change in ('加仓', '减仓'):
        verb = change
    elif 'large_reversal' in event.get('kinds', []):
        verb = '开仓'
    else:
        verb = '持有'
    pattern = re.compile(
        rf'买入(?P<rest>(?:约)?(?:[\d,.]+\s*(?:万美元|万美金|美元)?\s*)?(?:的)?\s*{re.escape(coin)}\s*(?:多单|多仓)?)')
    title = pattern.sub(lambda match: verb + match.group('rest'), draft.title)
    content = pattern.sub(lambda match: verb + match.group('rest'), draft.content)
    return DraftBrief(title=title, content=content)


def scenario_title(event: dict[str, Any]) -> str | None:
    """Use evidence-bound titles for scenarios with a precise requested form."""
    evidence, kinds, coin = event['evidence'], set(event['kinds']), event['coin']
    contract = coin
    coin = str(coin).rsplit(':', 1)[-1]
    address = '某地址'
    position = evidence.get('position') or evidence.get('previous_position') or {}
    side = '多单' if number(position.get('size', '0')) > 0 else '空单'
    actions = evidence.get('actions') or {}
    if 'actual_liquidation' in kinds:
        sides = [(s, number(actions.get(f'liquidated_{key}_notional', 0)))
                 for s, key in [('多单', 'long'), ('空单', 'short')]]
        details = [f'{coin}{s}发生{money(value)}清算' for s, value in sides if value > 0]
        if details:
            return address + '，'.join(details)
        liquidated = number(actions.get('liquidated_notional', '0'))
        if liquidated > 0:
            return f'{address}{coin}合约发生{money(liquidated)}清算'
    if 'near_liquidation' in kinds:
        current = evidence.get('position') or {}
        distance = evidence.get('liquidation_distance')
        if current and distance is not None:
            distance_value, distance_approximate = rounded_percent(distance, fraction=True)
            distance_text = f'{"约" if distance_approximate else ""}{distance_value}%'
            title = (f'距清算价{distance_text}，{address}持仓价值'
                     f'{money(current.get("notional", "0"))}{coin}{side}')
            pnl = current.get('unrealized_pnl')
            if pnl is not None:
                pnl_value = number(pnl)
                title += f'，{"浮盈" if pnl_value >= 0 else "浮亏"}{money(abs(pnl_value))}'
            return title
    if 'large_realized_result' in kinds:
        pnl = number(actions.get('ordinary_closed_pnl', actions.get('closed_pnl', 0)))
        threshold = number(evidence.get('threshold_rules', {}).get(
            'major_pnl' if contract in ('BTC', 'ETH') else 'other_pnl',
            '5000000' if contract in ('BTC', 'ETH') else '2000000'))
        # Completed rounds and the current batch overlap: choose one basis.
        if abs(pnl) < threshold and evidence.get('completed_round_results'):
            pnl = sum((number(r['pnl']) for r in evidence['completed_round_results']), Decimal(0))
        if pnl:
            return f'{address}平仓{coin}合约，已实现{"盈利" if pnl > 0 else "亏损"}{money(abs(pnl))}'
    if 'large_reversal' in kinds:
        previous = evidence.get('previous_position') or {}
        current = evidence.get('position') or {}
        old_side = '多单' if number(previous.get('size', '0')) > 0 else '空单'
        new_side = '多单' if number(current.get('size', '0')) > 0 else '空单'
        actions = evidence.get('actions') or {}
        legs = single_reversal_legs(evidence)
        if legs:
            closed, opened = legs
            return f'{address}平仓{money(closed)}{coin}{old_side}并开仓{money(opened)}{coin}{new_side}'
        if previous and current:
            value = f'价值{money(current["notional"])}' if current.get('notional') is not None else ''
            return f'{address}由{coin}{old_side}反手持有{value}{coin}{new_side}'
    if 'pnl_sign_flip' in kinds or 'profit_to_loss' in kinds:
        before = evidence.get('news_change', {}).get('previous_floating_pnl')
        if before is None:
            before = evidence.get('previous_position', {}).get('unrealized_pnl')
        current = position.get('unrealized_pnl')
        if before is not None and current is not None:
            before_value, current_value = number(before), number(current)
            before_label = '浮盈' if before_value >= 0 else '浮亏'
            current_label = '浮盈' if current_value >= 0 else '浮亏'
            value = f'{address}当前持有价值{money(position.get("notional", "0"))}{coin}{side}'
            return f'{before_label}{money(abs(before_value))}转为{current_label}{money(abs(current_value))}，{value}'
    if 'profit_giveback' in kinds:
        peak = evidence.get('episode_peak_unrealized_pnl')
        current = position.get('unrealized_pnl')
        if peak is not None and current is not None:
            value = f'{address}当前持有价值{money(position.get("notional", "0"))}{coin}{side}'
            change = '回落至' if number(current) >= 0 else '转为浮亏'
            return f'浮盈{money(peak)}{change}{money(abs(number(current)))}，{value}'
    if 'loss_addition' in kinds and not kinds.intersection(
            {'large_profit','large_loss','profit_giveback','profit_to_loss','pnl_sign_flip'}):
        previous=evidence.get('previous_position') or {}
        loss=number(previous.get('unrealized_pnl',0))
        actions=evidence.get('actions') or {}
        net = snapshot_net_change(evidence)
        amount=number(net['estimated_value']) if net else Decimal(0)
        if loss < 0 and amount > 0:
            return f'{address}持有的{coin}{side}浮亏{money(abs(loss))}时净增持{money(amount)}'
    if 'large_profit' in kinds or 'large_loss' in kinds:
        current = position.get('unrealized_pnl')
        if current is not None:
            current_value = number(current)
            previous = evidence.get('previous_reported_floating_pnl_usd')
            if previous is None:
                previous = evidence.get('previous_reported_floating_pnl')
            if previous is None:
                previous = evidence.get('news_change', {}).get('previous_floating_pnl')
            if previous is None:
                return None
            previous_value = number(previous)
            pnl_label = '浮盈' if current_value >= 0 else '浮亏'
            threshold = number(evidence.get('threshold_rules', {}).get(
                'major_pnl' if coin in ('BTC', 'ETH') else 'other_pnl',
                '5000000' if coin in ('BTC', 'ETH') else '2000000'))
            expanded = ((current_value >= threshold and previous_value >= 0 and
                         current_value - previous_value >= threshold) or
                        (current_value <= -threshold and previous_value <= 0 and
                         previous_value - current_value >= threshold))
            if expanded:
                return f'{address}持有的{coin}{side}{pnl_label}扩大至{money(abs(current_value))}'
    labels = set(evidence.get('highlights', []))
    if 'highlighted_address_action' in kinds and labels.intersection(
            {'asset_high_win_rate', 'asset_high_profit'}):
        description = _highlight_action_description(evidence, coin)
        if description:
            asset = evidence.get('history', {}).get('assets', {}).get(contract, {})
            if 'asset_high_profit' in labels:
                profit = asset.get('last_30d_closed_pnl_less_fees')
                if profit is not None:
                    return (f'近30天{coin}交易净盈利{money(profit)}，{address}'
                            f'{description}')
            if 'asset_high_win_rate' in labels and asset.get('win_rate') is not None:
                rate, approximate = rounded_percent(asset['win_rate'], fraction=True)
                lead = f'近期{coin}胜率约{rate}%' if approximate else f'近期{coin}胜率达{rate}%'
                return f'{lead}的{address}{description}'
    if 'large_position_action' in kinds or (evidence.get('net_position_change') and 'large_position' in kinds):
        description = _highlight_action_description(evidence, coin)
        if description:
            return f'{address}{description}'
    return None


def _highlight_action_description(evidence: dict[str, Any], coin: str) -> str | None:
    coin = coin.rsplit(':', 1)[-1]
    actions = evidence.get('actions') or {}
    previous = evidence.get('previous_position') or {}
    current = evidence.get('position') or {}
    side_position = current or previous
    if not side_position:
        return None
    side = '多单' if number(side_position.get('size', '0')) > 0 else '空单'
    opening = number(actions.get('open_long_notional' if side == '多单' else 'open_short_notional', '0'))
    closing = number(actions.get('close_long_notional' if side == '多单' else 'close_short_notional', '0'))
    change = evidence.get('net_position_change') or {}
    if previous and current and number(previous.get('size', '0')) * number(current.get('size', '0')) < 0:
        old_side = '多单' if number(previous.get('size', '0')) > 0 else '空单'
        legs = single_reversal_legs(evidence)
        if legs:
            closed, opened = legs
            return f'平仓{money(closed)}{coin}{old_side}并开仓{money(opened)}{coin}{side}'
        return f'由{coin}{old_side}反手持有价值{money(side_position.get("notional", "0"))}{coin}{side}'
    net = snapshot_net_change(evidence)
    if net and number(net['estimated_value']) > 0:
        if net['closed']:
            return f'清仓{coin}{side}，净减少{money(net["estimated_value"])}仓位'
        return f'{net["direction"]}{money(net["estimated_value"])}{coin}{side}'
    if previous and not current:
        old_side = '多单' if number(previous.get('size', '0')) > 0 else '空单'
        old_close = number(actions.get(f'close_{"long" if old_side == "多单" else "short"}_notional', '0'))
        if old_close > 0:
            return f'平仓{money(old_close)}{coin}{old_side}'
    direction = change.get('direction')
    net_change = number(change.get('estimated_value', '0'))
    if previous and direction in ('加仓', '减仓') and net_change > 0:
        verb = '平仓' if change.get('closed') else direction
        return f'{verb}{money(net_change)}{coin}{side}'
    if opening > 0:
        same_side_before = previous and number(previous.get('size', '0')) * number(side_position.get('size', '0')) > 0
        verb = '加仓' if same_side_before else '开仓'
        return f'{verb}{money(opening)}{coin}{side}'
    if closing > 0:
        verb = '平仓' if not current else '减仓'
        return f'{verb}{money(closing)}{coin}{side}'
    if direction in ('加仓', '减仓'):
        return f'{direction}{money(change.get("estimated_value", "0"))}{coin}{side}'
    value = number(side_position.get('notional', '0'))
    if value > 0:
        return f'当前持有价值{money(value)}{coin}{side}'
    return None


def _position_material(raw: dict[str, Any] | None, *, include_pnl: bool = False) -> dict[str, Any] | None:
    if not raw:
        return None
    facts = {'direction': '多单' if number(raw['size']) > 0 else '空单',
             'quantity': str(abs(number(raw['size']))), 'position_value_usd': raw['notional']}
    if raw.get('mark') is not None and number(raw['mark']) > 0:
        facts['position_valuation_reference_price_usd'] = raw['mark']
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
    if 'asset_high_profit' in labels and asset.get('last_30d_closed_pnl_less_fees') is not None:
        facts['asset_trading_pnl_usd'] = asset['last_30d_closed_pnl_less_fees']
        facts['asset_trading_window'] = '近30天'
    if 'historical_loss' in labels:
        loss = history.get('official_account_loss', {})
        threshold = number(evidence.get('threshold_rules', {}).get('historical_loss', '20000000'))
        facts['account_period_loss'] = [
            {'period': {'day': '近1天', 'week': '近7天', 'month': '近30天', 'allTime': '累计'}[window],
             'loss_usd': str(-number(data['pnl']))}
            for window, data in loss.get('windows', {}).items()
            if window == 'allTime' and number(data['pnl']) <= -threshold]
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
    if 'asset_trading_pnl_usd' in facts:
        asset_start = asset.get('last_30d_first_observed_fill_ms')
        asset_end = asset.get('last_30d_last_observed_fill_ms')
    else:
        asset_start = asset.get('first_observed_fill_ms', history.get('first_observed_fill_ms'))
        asset_end = asset.get('last_observed_fill_ms', history.get('last_observed_fill_ms'))
    if asset_start is not None and asset_end is not None and any(k.startswith('asset_') for k in facts):
        facts['asset_trading_period'] = [timestamp(asset_start)[:10], timestamp(asset_end)[:10]]
    if 'account_trading_pnl_usd' in facts and history.get('first_observed_fill_ms') is not None and history.get('last_observed_fill_ms') is not None:
        facts['account_trading_period'] = [timestamp(history['first_observed_fill_ms'])[:10], timestamp(history['last_observed_fill_ms'])[:10]]
    return facts


def _action_material(raw: dict[str, Any], highlights: list[str], kinds: list[str]) -> dict[str, Any]:
    facts = {key: value for key, value in raw.items()
             if key not in ('fill_keys', 'first_fill_ms', 'last_fill_ms')
             and not key.endswith('_quantity')}
    sequence = raw.get('execution_sequence')
    if sequence:
        facts['execution_sequence'] = [
            {'action': leg['action'], 'quantity': str(leg['quantity']), 'notional': str(leg['notional']),
             'period': [timestamp(leg['first_fill_ms']), timestamp(leg['last_fill_ms'])]}
            for leg in sequence]
    if 'asset_high_win_rate' in highlights and 'large_realized_result' not in kinds:
        facts.pop('closed_pnl', None)
        facts.pop('ordinary_closed_pnl', None)
    return facts


def writer_material(event: dict[str, Any]) -> dict[str, Any]:
    """Allowlist editorial facts. Full provenance remains in the admin evidence."""
    from .dashboard import KIND_LABELS
    e, kinds = event['evidence'], event['kinds']
    pnl_kinds={'large_profit','large_loss','profit_giveback','profit_to_loss','pnl_sign_flip'}
    kind_set=set(kinds)
    loss_addition_context='loss_addition' in kind_set and not kind_set.intersection(pnl_kinds)
    show_pnl = bool(kind_set.intersection(pnl_kinds | {'near_liquidation'}))
    facts: dict[str, Any] = {'position': _position_material(e.get('position'), include_pnl=show_pnl)}
    if e.get('previous_position'):
        facts['previous_position'] = _position_material(
            e['previous_position'], include_pnl=show_pnl or loss_addition_context)
    if loss_addition_context and e.get('previous_position'):
        previous_loss=number(e['previous_position'].get('unrealized_pnl',0))
        if previous_loss < 0:
            facts['floating_loss_before_addition_usd']=str(abs(previous_loss))
    change = e.get('news_change')
    if change and not loss_addition_context:
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
    if 'highlighted_address_action' in kinds or 'loss_addition' in kinds:
        net = snapshot_net_change(e)
        if net:
            facts['net_position_change'] = net
    if 'position' in e:
        facts['current_position_status'] = '持仓' if e['position'] else '空仓'
    if e.get('previous_reported_floating_pnl') is not None and not loss_addition_context:
        facts['previous_reported_floating_pnl_usd'] = e['previous_reported_floating_pnl']
        if e.get('previous_reported_floating_pnl_ms') is not None:
            facts['pnl_change_period'] = [timestamp(e['previous_reported_floating_pnl_ms']), timestamp(event['observed_ms'])]
    if e.get('actions'):
        facts['actions'] = _action_material(e['actions'], e.get('highlights', []), kinds)
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
                         for k, v in e.get('threshold_rules', {}).items() if hasattr(defaults, k)})
        facts['address_highlights'] = {a: _highlight_material({'history': h,
            'highlights': highlights(h, event['coin'], rules),
            'threshold_rules': e.get('threshold_rules', {})}, event['coin'])
            for a, h in e.get('histories', {}).items()}
    return {'coin': event['coin'], 'trigger_rules': [KIND_LABELS.get(k, k) for k in kinds], 'facts': facts}
