from __future__ import annotations

from decimal import Decimal
from statistics import median
from typing import Any

from .copy import render_digest
from .models import (
    AggregationGroup, AlertMember, AssetState, BroadcastState, Direction, Market,
    PendingCandidate, PriceLock, StockAlert, StockInstrument, StockQuote, Tier, Window,
)
from .state_machine import (
    AGGREGATION_SECONDS, FOLLOWUP_SECONDS, PRICE_LOCK_SECONDS,
    baseline_lock, candidate_allowed, directional, price_step_reached, qualifying_changes, tier_for,
)


def eligible_tier(
    asset: AssetState, changes: dict[Window, Decimal], price: Decimal, direction: Direction,
) -> Tier | None:
    """Both rolling change and movement since recovery must reach the tier."""
    rolling = tier_for(changes, direction)
    if rolling is None or asset.resume_price is None:
        return None
    movement = directional(price - asset.resume_price, direction) * 100
    if movement < asset.resume_price * 5:
        return None
    return "live" if rolling == "live" and movement >= asset.resume_price * 10 else "backend"


def _breadth(
    instruments: list[StockInstrument], quotes: dict[str, StockQuote],
    changes: dict[str, dict[Window, Decimal]], market: Market, direction: Direction,
) -> dict[Window, dict[str, Any]]:
    total = sum(item.market == market for item in instruments)
    result: dict[Window, dict[str, Any]] = {}
    for window in ("1h", "24h"):
        values = [changes[item.symbol][window] for item in instruments
                  if item.market == market and item.symbol in quotes and window in changes.get(item.symbol, {})]
        if not values:
            continue
        share = Decimal(sum(directional(value, direction) > 0 for value in values)) / len(values) * 100
        middle = median(values)
        result[window] = {
            "share": share, "median": middle,
            # Do not call a tiny or mostly missing sample a broad market move.
            "broad": len(values) >= 2 and len(values) * 100 >= total * 80
                     and share >= 70 and directional(middle, direction) >= 3,
        }
    return result


def advance_broadcast(
    state: BroadcastState, *, instruments: list[StockInstrument], quotes: dict[str, StockQuote],
    changes: dict[str, dict[Window, Decimal]], now: int, restart: bool,
    max_gap_seconds: int, baseline_symbols: set[str] | None = None,
) -> tuple[list[StockAlert], int]:
    by_symbol = {item.symbol: item for item in instruments}
    if restart:
        for group in state.groups.values():
            group.pending.clear()
            group.due_at = None
    # Delisted assets must not retain a pending signal or a stale baseline.
    state.assets = {symbol: asset for symbol, asset in state.assets.items() if symbol in by_symbol}
    for group in state.groups.values():
        group.pending = {symbol: pending for symbol, pending in group.pending.items()
                         if symbol in by_symbol and by_symbol[symbol].market == group.market}

    baselined = 0
    for symbol, quote in quotes.items():
        current = changes[symbol]
        asset = state.assets.get(symbol)
        silent = (restart or symbol in (baseline_symbols or set()) or asset is None
                  or quote.observed_at - asset.observed_at > max_gap_seconds)
        if asset is None:
            asset = AssetState(quote.observed_at, current, {
                direction: baseline_lock(quote.price, current, direction, now)
                for direction in ("up", "down")
            })
            state.assets[symbol] = asset
        if silent:
            baselined += 1
            asset.resume_price = quote.price
            asset.resumed_at = now
        for direction in ("up", "down"):
            key = f"{by_symbol[symbol].market}:{direction}"
            group = state.groups.setdefault(key, AggregationGroup(by_symbol[symbol].market, direction))
            lock = asset.locks[direction]
            expired = now - lock.locked_at >= PRICE_LOCK_SECONDS
            if expired:
                lock = baseline_lock(quote.price, current, direction, now)
                asset.locks[direction] = lock
            if silent or expired:
                group.pending.pop(symbol, None)
                # A restart/gap must not enqueue the already-observed move one
                # cycle later. Advance the baseline only in the alert direction;
                # a pullback must not lower an existing repeat-price barrier.
                if silent:
                    if lock.tier is None or directional(quote.price - lock.anchor_price, direction) > 0:
                        lock.anchor_price = quote.price
            elif eligible_tier(asset, current, quote.price, direction) is None:
                group.pending.pop(symbol, None)
            elif symbol not in group.pending and candidate_allowed(
                lock, previous=asset.changes, current=current, price=quote.price, direction=direction,
                tier_limit=eligible_tier(asset, current, quote.price, direction),
            ):
                group.pending[symbol] = PendingCandidate(
                    now, lock.extreme_price if lock.tier == "backend" else None,
                )
                if group.due_at is None:
                    group.due_at = group.next_at if group.next_at > now else now + AGGREGATION_SECONDS
            # Keep the directional extreme through pullbacks and process restarts.
            if directional(quote.price - lock.extreme_price, direction) > 0:
                lock.extreme_price = quote.price
        asset.observed_at = quote.observed_at
        asset.changes = current

    alerts: list[StockAlert] = []
    for key, group in sorted(state.groups.items()):
        if group.due_at is None or now < group.due_at:
            continue
        members_by_tier: dict[str, list[AlertMember]] = {"backend": [], "live": []}
        for symbol, pending in group.pending.items():
            quote = quotes.get(symbol)
            if quote is None:
                continue
            member_changes = qualifying_changes(changes[symbol], group.direction)
            asset = state.assets[symbol]
            tier = eligible_tier(asset, member_changes, quote.price, group.direction)
            if tier is None:
                continue
            lock = state.assets[symbol].locks[group.direction]
            if lock.tier is not None:
                upgrade = (
                    lock.tier == "backend" and tier == "live" and pending.upgrade_floor is not None
                    and directional(quote.price - pending.upgrade_floor, group.direction) > 0
                )
                if not price_step_reached(lock.anchor_price, quote.price, group.direction) and not upgrade:
                    continue
            members_by_tier[tier].append(AlertMember(by_symbol[symbol], member_changes, quote.price, quote.observed_at))

        for tier, members in members_by_tier.items():
            if not members:
                continue
            ordered = tuple(sorted(members, key=lambda member: (-abs(member.change_pct), member.instrument.symbol)))
            title, content = render_digest(
                market=group.market, direction=group.direction, members=ordered,
                breadth=_breadth(instruments, quotes, changes, group.market, group.direction),
            )
            alerts.append(StockAlert(
                event_key=f"digest:{key}:{group.due_at}:{tier}", market=group.market,
                tier=tier, direction=group.direction, members=ordered, title=title, content=content,
            ))
            for member in ordered:
                previous_lock = state.assets[member.instrument.symbol].locks[group.direction]
                extreme = max if group.direction == "up" else min
                state.assets[member.instrument.symbol].locks[group.direction] = PriceLock(
                    member.current_price, extreme(member.current_price, previous_lock.extreme_price), now,
                    "live" if previous_lock.tier == "live" else tier,
                )
            group.next_at = now + FOLLOWUP_SECONDS
        group.pending.clear()
        group.due_at = None
    return alerts, baselined
