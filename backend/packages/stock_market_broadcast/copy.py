from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief

from .models import AlertMember, Direction, Market, StockInstrument, Tier, Window
from .theme_classifier import ThemeClassification


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
    price_text = f"{price:f}"
    if "." in price_text:
        price_text = price_text.rstrip("0").rstrip(".")
    return f"{member.instrument.asset_name}{'、'.join(parts)}，现报{price_text} USDT"


def _format_alert(title: str, content: str) -> tuple[str, str]:
    formatted = format_brief(DraftBrief(title=title, content=content))
    return formatted.title, formatted.content


def render_digest(
    *, market: Market, direction: Direction, members: tuple[AlertMember, ...],
    breadth: dict[Window, dict[str, Any]],
    theme: ThemeClassification | None = None,
) -> tuple[str, str]:
    verb = "上涨" if direction == "up" else "下跌"
    prefix = MARKET_ATTRIBUTION[market]
    if len(members) == 1:
        member = members[0]
        title = f"{member.instrument.asset_name}{WINDOW_LABELS[member.primary_window]}{verb}{percent_text(member.change_pct)}%"
        return _format_alert(title, prefix + _member_text(member, direction, None) + "。")

    common: Window | None = next((window for window in ("24h", "1h") if all(window in member.changes for member in members)), None)
    window_label = WINDOW_LABELS[common] if common else ""
    if theme is not None and market == "us":
        themed = [member for member in members if member.instrument.asset_name in theme.symbols]
        if len(themed) / len(members) >= 0.7:
            others = [member for member in members if member.instrument.asset_name not in theme.symbols]
            movement = "集体上涨" if direction == "up" else "集体下跌"
            theme_name = f"{theme.label}相关美股合约"
            title = f"{theme_name}{window_label}{movement}"
            details = "；".join(_member_text(member, direction, common) for member in themed[:5])
            body_trend = "整体走强" if direction == "up" else "整体走弱"
            lead = f"{theme.label}相关标的{window_label}{body_trend}"
            content = f"{MARKET_ATTRIBUTION[market]}{lead}，{details}"
            if others:
                other_details = "；".join(_member_text(member, direction, common) for member in others[:5])
                other_direction = "下跌" if direction == "down" else "上涨"
                content += f"；另有其他美股合约{other_direction}：{other_details}"
            return _format_alert(title, content + "。")

    stats = breadth.get(common, {}) if common else {}
    broad = stats.get("broad", False)
    market_name = MARKET_LABELS[market] + "相关合约"
    if broad:
        trend = "普涨" if direction == "up" else "普跌"
        title = f"{market_name}{window_label}{trend}"
        lead = f"多只{market_name}{window_label}{trend}"
    else:
        move = ("涨幅" if direction == "up" else "跌幅") + "扩大"
        title = f"{market_name}{window_label}{move}"
        lead = f"多只{market_name}{window_label}{move}"
    details = "；".join(_member_text(member, direction, common) for member in members[:5])
    return _format_alert(title, f"{prefix}{lead}，{details}。")


def render_alert(
    *, instrument: StockInstrument, window: Window, tier: Tier, direction: Direction,
    change_pct: Decimal, current_price: Decimal,
) -> tuple[str, str]:
    return render_digest(market=instrument.market, direction=direction, members=(
        AlertMember(instrument, {window: change_pct}, current_price, 0),
    ), breadth={})
