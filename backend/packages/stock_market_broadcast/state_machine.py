from __future__ import annotations

from decimal import Decimal

from .models import Direction, Tier, TriggerDecision, TriggerState, Window


FIVE_PERCENT = Decimal("5")
TEN_PERCENT = Decimal("10")


def advance_trigger_state(
    state: TriggerState | None,
    *,
    symbol: str,
    window: Window,
    change_pct: Decimal,
    silent_baseline: bool = False,
) -> TriggerDecision:
    if silent_baseline or state is None or not state.initialized:
        return TriggerDecision(
            state=TriggerState(
                symbol=symbol,
                window=window,
                initialized=True,
                armed_5_up=change_pct < FIVE_PERCENT,
                armed_5_down=-change_pct < FIVE_PERCENT,
                armed_10_up=change_pct < TEN_PERCENT,
                armed_10_down=-change_pct < TEN_PERCENT,
                last_change_pct=change_pct,
            ),
            tier=None,
            direction=None,
        )

    armed = {
        ("up", 5): state.armed_5_up,
        ("down", 5): state.armed_5_down,
        ("up", 10): state.armed_10_up,
        ("down", 10): state.armed_10_down,
    }
    previous_change = state.last_change_pct
    candidates: list[tuple[int, Direction]] = []
    for direction in ("up", "down"):
        current_directional = change_pct if direction == "up" else -change_pct
        previous_directional = (
            previous_change if direction == "up" else -previous_change
        ) if previous_change is not None else None
        for threshold in (5, 10):
            key = (direction, threshold)
            if current_directional < Decimal(threshold):
                armed[key] = True
                continue
            crossed = previous_directional is not None and previous_directional < Decimal(threshold)
            if armed[key] and crossed:
                candidates.append((threshold, direction))
            armed[key] = False

    tier: Tier | None = None
    direction: Direction | None = None
    if candidates:
        threshold, direction = max(candidates, key=lambda item: item[0])
        tier = "live" if threshold == 10 else "backend"

    return TriggerDecision(
        state=TriggerState(
            symbol=symbol,
            window=window,
            initialized=True,
            armed_5_up=armed[("up", 5)],
            armed_5_down=armed[("down", 5)],
            armed_10_up=armed[("up", 10)],
            armed_10_down=armed[("down", 10)],
            last_change_pct=change_pct,
        ),
        tier=tier,
        direction=direction,
    )
