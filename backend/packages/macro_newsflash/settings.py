from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from packages.common.paths import get_paths


@dataclass(frozen=True, slots=True)
class MacroNewsflashSettings:
    database_path: Path
    push_endpoint: str
    poll_interval_seconds: int
    release_poll_interval_seconds: float
    release_request_timeout_seconds: float
    quiet_poll_interval_seconds: int
    request_timeout_seconds: float
    push_timeout_seconds: float
    bea_schedule_url: str
    fed_calendar_url: str
    tradingview_calendar_url: str


def load_macro_newsflash_settings() -> MacroNewsflashSettings:
    load_dotenv()
    paths = get_paths()
    return MacroNewsflashSettings(
        database_path=Path(
            os.getenv("MACRO_NEWSFLASH_DB_PATH") or paths.runtime_dir / "macro_newsflash.sqlite"
        ).expanduser().resolve(),
        push_endpoint=(os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data").strip(),
        poll_interval_seconds=max(10, int(os.getenv("MACRO_NEWSFLASH_POLL_INTERVAL_SECONDS") or 30)),
        release_poll_interval_seconds=max(
            1.0,
            float(os.getenv("MACRO_NEWSFLASH_RELEASE_POLL_INTERVAL_SECONDS") or 1),
        ),
        release_request_timeout_seconds=max(
            1.0,
            float(os.getenv("MACRO_NEWSFLASH_RELEASE_REQUEST_TIMEOUT_SECONDS") or 3),
        ),
        quiet_poll_interval_seconds=max(
            60,
            int(os.getenv("MACRO_NEWSFLASH_QUIET_POLL_INTERVAL_SECONDS") or 900),
        ),
        request_timeout_seconds=max(2.0, float(os.getenv("MACRO_NEWSFLASH_REQUEST_TIMEOUT_SECONDS") or 12)),
        push_timeout_seconds=max(2.0, float(os.getenv("MACRO_NEWSFLASH_PUSH_TIMEOUT_SECONDS") or 15)),
        bea_schedule_url=(os.getenv("MACRO_NEWSFLASH_BEA_SCHEDULE_URL") or "https://www.bea.gov/news/schedule").strip(),
        fed_calendar_url=(os.getenv("MACRO_NEWSFLASH_FED_CALENDAR_URL") or "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm").strip(),
        tradingview_calendar_url=(os.getenv("MACRO_NEWSFLASH_TRADINGVIEW_CALENDAR_URL") or "https://economic-calendar.tradingview.com/events").strip(),
    )
