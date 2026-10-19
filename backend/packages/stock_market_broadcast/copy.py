from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from .models import Direction, Market, StockInstrument, Tier, Window


WINDOW_LABELS: dict[Window, str] = {"1h": "1小时", "24h": "24小时"}
MARKET_ATTRIBUTION: dict[Market, str] = {
    "us": "据MSX.COM数据，",
    "cn": "据行情数据，",
    "hk": "据行情数据，",
    "kr": "据Gate数据，",
    "jp": "据Gate数据，",
}


def render_alert(
    *,
    instrument: StockInstrument,
    window: Window,
    tier: Tier,
    direction: Direction,
    change_pct: Decimal,
    current_price: Decimal,
) -> tuple[str, str]:
    direction_text = "上涨" if direction == "up" else "下跌"
    percent = abs(change_pct).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    price = current_price.quantize(
        Decimal(1).scaleb(-instrument.price_precision),
        rounding=ROUND_HALF_UP,
    )
    price_text = format(price, f".{instrument.price_precision}f")
    window_text = WINDOW_LABELS[window]
    title = f"{instrument.symbol} {window_text}{direction_text}{percent}%"
    content = (
        f"{MARKET_ATTRIBUTION[instrument.market]}{instrument.symbol} {window_text}"
        f"{direction_text}{percent}%，现报{price_text} USDT。"
    )
    return title, content
