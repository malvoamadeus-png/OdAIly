from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from .models import (
    AggregationGroup, AssetState, BroadcastState, Direction, PendingCandidate, PriceLock, Tier, Window,
)


AGGREGATION_SECONDS = 180
FOLLOWUP_SECONDS = 1800
PRICE_LOCK_SECONDS = 6 * 3600
PRICE_STEP_PERCENT = Decimal("5")


def directional(value: Decimal, direction: Direction) -> Decimal:
    return value if direction == "up" else -value


def qualifying_changes(changes: dict[Window, Decimal], direction: Direction) -> dict[Window, Decimal]:
    return {window: change for window, change in changes.items() if directional(change, direction) >= 5}


def tier_for(changes: dict[Window, Decimal], direction: Direction) -> Tier | None:
    magnitude = max((directional(change, direction) for change in changes.values()), default=Decimal(0))
    return "live" if magnitude >= 10 else "backend" if magnitude >= 5 else None


def baseline_lock(price: Decimal, changes: dict[Window, Decimal], direction: Direction, now: int) -> PriceLock:
    return PriceLock(price, price, now, tier_for(changes, direction))


def can_repeat(lock: PriceLock, *, price: Decimal, tier: Tier, direction: Direction) -> bool:
    if price_step_reached(lock.anchor_price, price, direction):
        return True
    return (
        lock.tier == "backend" and tier == "live"
        and directional(price - lock.extreme_price, direction) > 0
    )


def price_step_reached(anchor: Decimal, price: Decimal, direction: Direction) -> bool:
    return directional(price - anchor, direction) * 100 >= anchor * PRICE_STEP_PERCENT


def candidate_allowed(
    lock: PriceLock, *, previous: dict[Window, Decimal], current: dict[Window, Decimal],
    price: Decimal, direction: Direction,
) -> bool:
    # A newly available window first establishes its own silent baseline.
    comparable = {window: change for window, change in current.items() if window in previous}
    tier = tier_for(comparable, direction)
    if tier is None:
        return False
    if lock.tier is not None:
        return can_repeat(lock, price=price, tier=tier, direction=direction)
    return any(
        directional(previous[window], direction) < threshold <= directional(change, direction)
        for window, change in comparable.items() for threshold in (5, 10)
    )


def state_to_dict(state: BroadcastState) -> dict[str, Any]:
    return asdict(state)


def state_from_dict(data: dict[str, Any]) -> BroadcastState:
    return BroadcastState(
        assets={symbol: AssetState(
            observed_at=int(asset["observed_at"]),
            changes={window: Decimal(value) for window, value in asset["changes"].items()},
            locks={direction: PriceLock(
                anchor_price=Decimal(lock["anchor_price"]),
                extreme_price=Decimal(lock["extreme_price"]),
                locked_at=int(lock["locked_at"]), tier=lock["tier"],
            ) for direction, lock in asset["locks"].items()},
        ) for symbol, asset in data.get("assets", {}).items()},
        groups={key: AggregationGroup(
            market=group["market"], direction=group["direction"], due_at=group["due_at"],
            next_at=group["next_at"], pending={symbol: PendingCandidate(
                first_at=pending["first_at"],
                upgrade_floor=Decimal(pending["upgrade_floor"]) if pending["upgrade_floor"] is not None else None,
            ) for symbol, pending in group["pending"].items()},
        ) for key, group in data.get("groups", {}).items()},
    )
