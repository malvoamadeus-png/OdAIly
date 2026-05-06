from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
EASTERN_TZ = ZoneInfo("America/New_York")


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def now_eastern() -> datetime:
    return datetime.now(EASTERN_TZ)


def now_iso() -> str:
    return now_shanghai().isoformat(timespec="seconds")


def today_key() -> str:
    return now_shanghai().date().isoformat()


def is_weekend_in_eastern(reference: datetime | None = None) -> bool:
    value = reference or now_eastern()
    return value.astimezone(EASTERN_TZ).weekday() >= 5


def utc_timestamp_to_iso(value: int | float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat(timespec="seconds")


def parse_date_key(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
