from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .models import AlertMember, Direction, Market, StockInstrument, Tier, Window


WINDOW_LABELS: dict[Window, str] = {"1h": "1小时", "24h": "24小时"}
MARKET_LABELS: dict[Market, str] = {"us": "美股", "cn": "A股", "hk": "港股", "kr": "韩股", "jp": "日股"}
MARKET_ATTRIBUTION: dict[Market, str] = {
    "us": "据MSX.COM数据，", "cn": "据行情数据，", "hk": "据行情数据，",
    "kr": "据Gate数据，", "jp": "据Gate数据，",
}


def percent_text(value: Decimal) -> str:
    return str(abs(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _member_text(member: AlertMember, direction: Direction, common_window: Window | None) -> str:
    verb = "上涨" if direction == "up" else "下跌"
    ordered = [member.primary_window] + [window for window in member.changes if window != member.primary_window]
    if common_window:
        ordered = [common_window] + [window for window in ordered if window != common_window]
    parts = [f"{'' if window == common_window else WINDOW_LABELS[window]}{verb}{percent_text(member.changes[window])}%" for window in ordered]
    price = member.current_price.quantize(Decimal(1).scaleb(-member.instrument.price_precision), rounding=ROUND_HALF_UP)
    return f"{member.instrument.asset_name}{'、'.join(parts)}，现报{price:f} USDT"


def render_digest(
    *, market: Market, direction: Direction, members: tuple[AlertMember, ...],
    breadth: dict[Window, dict[str, Any]],
) -> tuple[str, str]:
    verb = "上涨" if direction == "up" else "下跌"
    prefix = MARKET_ATTRIBUTION[market]
    if len(members) == 1:
        member = members[0]
        title = f"{member.instrument.asset_name}{WINDOW_LABELS[member.primary_window]}{verb}{percent_text(member.change_pct)}%"
        return title, prefix + _member_text(member, direction, None) + "。"

    common: Window | None = next((window for window in ("24h", "1h") if all(window in member.changes for member in members)), None)
    stats = breadth.get(common, {}) if common else {}
    broad = stats.get("broad", False)
    market_name = MARKET_LABELS[market] + "相关合约"
    window_label = WINDOW_LABELS[common] if common else ""
    if broad:
        trend = "普涨" if direction == "up" else "普跌"
        title = f"{market_name}{window_label}{trend}，{len(members)}只达到播报条件"
        share = percent_text(stats["share"])
        median = percent_text(stats["median"])
        lead = f"{market_name}{window_label}{trend}，有效报价中{verb}标的占比为{share}%，涨跌幅中位数为{'-' if direction == 'down' else '+'}{median}%"
    else:
        move = ("涨幅" if direction == "up" else "跌幅") + "扩大"
        title = f"{len(members)}只{market_name}{window_label}{move}"
        lead = f"多只{market_name}{window_label}{move}"
    details = "；".join(_member_text(member, direction, common) for member in members[:5])
    return title, f"{prefix}{lead}，达到本档播报条件的共{len(members)}只，其中{details}。"


def render_alert(
    *, instrument: StockInstrument, window: Window, tier: Tier, direction: Direction,
    change_pct: Decimal, current_price: Decimal,
) -> tuple[str, str]:
    return render_digest(market=instrument.market, direction=direction, members=(
        AlertMember(instrument, {window: change_pct}, current_price, 0),
    ), breadth={})
