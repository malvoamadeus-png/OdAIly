from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import HyperliquidActivity, HyperliquidAddress, HyperliquidDirection


SUPPORTED_DIRECTIONS: tuple[HyperliquidDirection, ...] = (
    "Open Long",
    "Open Short",
    "Close Long",
    "Close Short",
)


def detect_hyperliquid_activity(
    *,
    whale: HyperliquidAddress,
    fill: dict[str, Any],
) -> HyperliquidActivity | None:
    direction = str(fill.get("dir") or "").strip()
    if direction not in SUPPORTED_DIRECTIONS:
        return None

    price = _decimal(fill.get("px"))
    size = _decimal(fill.get("sz"))
    fill_time_ms = _int(fill.get("time"))
    if price is None or size is None or fill_time_ms is None or price <= 0 or size <= 0:
        return None

    coin = str(fill.get("coin") or "").strip().upper()
    if not coin:
        return None

    closed_pnl = _decimal(fill.get("closedPnl")) or Decimal("0")
    side = str(fill.get("side") or "").strip().upper()
    fill_time = datetime.fromtimestamp(fill_time_ms / 1000, UTC)
    notional_usd = price * size
    fill_key = _fill_key(fill)
    address_url = f"https://hyperbot.network/trader/{whale.address}"

    action_text = _direction_text(direction)
    summary = f"{action_text} {size.normalize()} {coin} @ {format_money(price)}"
    text = (
        f"「{whale.label}」在 Hyperliquid {action_text} {format_amount(size)} {coin}，"
        f"成交价 {format_money(price)}，名义价值约 {format_money(notional_usd)} USDC"
    )
    if direction.startswith("Close") and closed_pnl != 0:
        text += f"，已实现盈亏 {format_signed_money(closed_pnl)} USDC"
    text += f"\n{address_url}"

    return HyperliquidActivity(
        alert_kind="single",
        fill_key=fill_key,
        coin=coin,
        direction=direction,
        side=side or "-",
        price=price,
        size=size,
        notional_usd=notional_usd,
        closed_pnl=closed_pnl,
        fill_time=fill_time,
        fill_time_ms=fill_time_ms,
        telegram_text=text,
        summary=summary,
        aggregate_fill_count=None,
        raw_payload=fill,
    )


def format_amount(value: Decimal) -> str:
    return _strip_decimal(value)


def format_money(value: Decimal) -> str:
    text = _strip_decimal(value.quantize(Decimal("0.01")))
    return text


def format_signed_money(value: Decimal) -> str:
    text = format_money(abs(value))
    return f"-{text}" if value < 0 else text


def _direction_text(direction: HyperliquidDirection) -> str:
    mapping = {
        "Open Long": "开多",
        "Open Short": "开空",
        "Close Long": "平多",
        "Close Short": "平空",
    }
    return mapping[direction]


def _fill_key(fill: dict[str, Any]) -> str:
    tid = fill.get("tid")
    if tid is not None:
        return f"tid:{tid}"
    raw = "|".join(
        [
            str(fill.get("hash") or ""),
            str(fill.get("oid") or ""),
            str(fill.get("time") or ""),
            str(fill.get("coin") or ""),
            str(fill.get("dir") or ""),
            str(fill.get("px") or ""),
            str(fill.get("sz") or ""),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _strip_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
