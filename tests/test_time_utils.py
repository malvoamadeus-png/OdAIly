from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from packages.common.time_utils import (
    is_us_market_trading_day,
    is_weekday_in_shanghai,
    us_market_calendar_skip_reason,
    us_market_holidays,
)


def test_is_weekday_in_shanghai_uses_shanghai_calendar() -> None:
    assert is_weekday_in_shanghai(datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))) is True
    assert is_weekday_in_shanghai(datetime(2026, 5, 23, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))) is False


def test_us_market_holidays_include_memorial_day_and_good_friday() -> None:
    holidays_2026 = us_market_holidays(2026)

    assert datetime(2026, 5, 25).date() in holidays_2026
    assert datetime(2026, 4, 3).date() in holidays_2026


def test_is_us_market_trading_day_uses_eastern_market_day() -> None:
    assert is_us_market_trading_day(datetime(2026, 5, 20, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))) is True
    assert is_us_market_trading_day(datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))) is False


def test_us_market_calendar_skip_reason_reports_holiday_and_weekend() -> None:
    holiday_reference = datetime(2026, 5, 25, 21, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    weekend_reference = datetime(2026, 5, 25, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert us_market_calendar_skip_reason(holiday_reference) == "skipped: market holiday in America/New_York"
    assert us_market_calendar_skip_reason(weekend_reference) == "skipped: weekend in America/New_York"
