from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from .models import Trigger


def _bucket(price: Decimal, step: Decimal) -> int:
    return int((price / step).to_integral_value(rounding=ROUND_FLOOR))


def advance_state(
    *,
    previous_price: Decimal,
    current_price: Decimal,
    step: Decimal,
    disarmed_levels: set[int],
) -> tuple[Trigger | None, set[int]]:
    if step <= 0:
        raise ValueError("step must be positive")

    previous_bucket = _bucket(previous_price, step)
    current_bucket = _bucket(current_price, step)
    if current_bucket > previous_bucket:
        crossed = list(range(previous_bucket + 1, current_bucket + 1))
        direction = "up"
    elif current_bucket < previous_bucket:
        crossed = list(range(previous_bucket, current_bucket, -1))
        direction = "down"
    else:
        crossed = []
        direction = None

    next_disarmed = set(disarmed_levels)
    armed_crossed = [level for level in crossed if level not in next_disarmed]
    next_disarmed.update(crossed)

    trigger = None
    if direction and armed_crossed:
        level_index = armed_crossed[-1]
        trigger = Trigger(
            level_index=level_index,
            level=step * Decimal(level_index),
            direction=direction,
        )

    # Rearming happens after crossing evaluation. Reaching the adjacent line on
    # this sample arms a return crossing; it does not retroactively fire the
    # boundary crossed earlier in the same sample.
    rearmed = {
        level
        for level in next_disarmed
        if current_price <= step * Decimal(level - 1)
        or current_price >= step * Decimal(level + 1)
    }
    next_disarmed.difference_update(rearmed)
    return trigger, next_disarmed


def silently_recover(
    *,
    current_price: Decimal,
    step: Decimal,
    disarmed_levels: set[int],
) -> set[int]:
    return {
        level
        for level in disarmed_levels
        if step * Decimal(level - 1) < current_price < step * Decimal(level + 1)
    }
