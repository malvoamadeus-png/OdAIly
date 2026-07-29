from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief

from .models import ReferenceMetrics, RenderedBrief, SymbolConfig


ONE_DECIMAL = Decimal("0.1")


def _format_price(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum), f".{places}f")


def _threshold_places(threshold_text: str) -> int:
    exponent = Decimal(threshold_text).as_tuple().exponent
    return max(0, -exponent)


def render_brief(
    *,
    config: SymbolConfig,
    current_price: Decimal,
    trigger_price: Decimal,
    metrics: ReferenceMetrics,
    templates: dict[str, tuple[str, str]],
) -> RenderedBrief | None:
    if current_price == metrics.reference_price:
        return None
    raw_change = ((current_price - metrics.reference_price) / metrics.reference_price) * Decimal(100)
    absolute_change = abs(raw_change).quantize(ONE_DECIMAL, rounding=ROUND_HALF_UP)
    if absolute_change == Decimal("0.0"):
        return None

    is_up = current_price > metrics.reference_price
    if is_up:
        template_key = (
            "breakout"
            if metrics.high - current_price <= config.threshold
            else "pullback"
        )
    else:
        template_key = (
            "decline"
            if current_price - metrics.low <= config.threshold
            else "rebound"
        )

    direction_word = "上涨" if is_up else "下跌"
    if metrics.reference_kind == "rolling_24h":
        title_change_clause = f"24小时{direction_word}{absolute_change}%"
        body_change_clause = f"24小时{'涨幅' if is_up else '跌幅'}{absolute_change}%"
    elif metrics.reference_kind == "previous_session":
        title_change_clause = f"较上一交易时段收盘{direction_word}{absolute_change}%"
        body_change_clause = title_change_clause
    else:
        title_change_clause = f"开盘以来{direction_word}{absolute_change}%"
        body_change_clause = title_change_clause

    title_template, body_template = templates[template_key]
    values = {
        "display_name": config.display_name,
        "symbol": config.symbol,
        "trigger_price": _format_price(trigger_price, _threshold_places(config.threshold_text)),
        "current_price": _format_price(current_price, config.price_precision),
        "unit": config.unit,
        "title_change_clause": title_change_clause,
        "body_change_clause": body_change_clause,
    }
    formatted = format_brief(
        DraftBrief(
            title=title_template.format_map(values),
            content=body_template.format_map(values),
        )
    )
    return RenderedBrief(
        template_key=template_key,
        title=formatted.title,
        content=formatted.content,
        change_percent=absolute_change,
    )
