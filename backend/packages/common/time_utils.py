from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


def is_weekday_in_shanghai(reference: datetime | None = None) -> bool:
    value = reference or now_shanghai()
    return value.astimezone(SHANGHAI_TZ).weekday() < 5


def is_weekend_in_eastern(reference: datetime | None = None) -> bool:
    value = reference or now_eastern()
    return value.astimezone(EASTERN_TZ).weekday() >= 5


def _nth_weekday_of_month(*, year: int, month: int, weekday: int, occurrence: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    current += timedelta(days=7 * (occurrence - 1))
    return current


def _last_weekday_of_month(*, year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _observed_market_holiday(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def us_market_holidays(year: int) -> set[date]:
    holidays: set[date] = set()

    for holiday in (
        _observed_market_holiday(date(year, 1, 1)),
        _observed_market_holiday(date(year, 6, 19)),
        _observed_market_holiday(date(year, 7, 4)),
        _observed_market_holiday(date(year, 12, 25)),
    ):
        if holiday.year == year:
            holidays.add(holiday)

    next_new_year_observed = _observed_market_holiday(date(year + 1, 1, 1))
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)

    holidays.add(_nth_weekday_of_month(year=year, month=1, weekday=0, occurrence=3))
    holidays.add(_nth_weekday_of_month(year=year, month=2, weekday=0, occurrence=3))
    holidays.add(_easter_sunday(year) - timedelta(days=2))
    holidays.add(_last_weekday_of_month(year=year, month=5, weekday=0))
    holidays.add(_nth_weekday_of_month(year=year, month=9, weekday=0, occurrence=1))
    holidays.add(_nth_weekday_of_month(year=year, month=11, weekday=3, occurrence=4))
    return holidays


def is_us_market_holiday(reference: datetime | None = None) -> bool:
    value = (reference or now_eastern()).astimezone(EASTERN_TZ).date()
    return value in us_market_holidays(value.year)


def is_us_market_trading_day(reference: datetime | None = None) -> bool:
    value = (reference or now_eastern()).astimezone(EASTERN_TZ)
    return value.weekday() < 5 and value.date() not in us_market_holidays(value.year)


def us_market_calendar_skip_reason(reference: datetime | None = None) -> str | None:
    value = (reference or now_eastern()).astimezone(EASTERN_TZ)
    if value.weekday() >= 5:
        return "skipped: weekend in America/New_York"
    if value.date() in us_market_holidays(value.year):
        return "skipped: market holiday in America/New_York"
    return None


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
