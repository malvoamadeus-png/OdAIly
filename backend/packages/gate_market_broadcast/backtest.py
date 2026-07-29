from __future__ import annotations

from bisect import bisect_right
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .client import GateMarketClient
from .models import SymbolConfig
from .state_machine import advance_state


def run_symbol_backtest(
    *,
    client: GateMarketClient,
    config: SymbolConfig,
    days: int,
    end_time: int,
) -> dict[str, Any]:
    start_time = end_time - days * 86400
    points = client.fetch_history(
        config.symbol,
        start_time=start_time,
        end_time=end_time,
    )
    timestamps = [item[0] for item in points]
    disarmed: set[int] = set()
    event_count = 0
    previous_price: Decimal | None = None
    for timestamp, price in points:
        if previous_price is None:
            previous_price = price
            continue
        trigger, disarmed = advance_state(
            previous_price=previous_price,
            current_price=price,
            step=config.threshold,
            disarmed_levels=disarmed,
        )
        previous_price = price
        if trigger is None:
            continue
        reference_index = bisect_right(timestamps, timestamp - 86400) - 1
        if reference_index < 0:
            continue
        reference_price = points[reference_index][1]
        if reference_price == 0:
            continue
        change = abs((price - reference_price) / reference_price * Decimal(100))
        if change.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP) == Decimal("0.0"):
            continue
        event_count += 1

    trading_days = len(
        {datetime.fromtimestamp(timestamp, UTC).date() for timestamp, _ in points}
    )
    return {
        "symbol": config.symbol,
        "threshold": config.threshold_text,
        "points": len(points),
        "trading_days": trading_days,
        "events": event_count,
        "events_per_trading_day": event_count / trading_days if trading_days else 0,
        "trading_days_per_event": trading_days / event_count if event_count else None,
        "start_at": datetime.fromtimestamp(start_time, UTC).isoformat(),
        "end_at": datetime.fromtimestamp(end_time, UTC).isoformat(),
    }
