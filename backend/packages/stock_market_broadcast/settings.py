from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from packages.common.paths import get_paths


@dataclass(frozen=True, slots=True)
class StockMarketSettings:
    database_path: Path
    binance_api_base: str
    push_endpoint: str
    request_timeout_seconds: float
    poll_interval_seconds: int = 30
    instrument_refresh_seconds: int = 3600
    quote_max_age_seconds: int = 90


def load_stock_market_settings() -> StockMarketSettings:
    load_dotenv()
    paths = get_paths()
    database_path = Path(
        os.getenv("STOCK_MARKET_DB_PATH") or paths.runtime_dir / "stock_market.sqlite"
    ).resolve()
    return StockMarketSettings(
        database_path=database_path,
        binance_api_base=(os.getenv("STOCK_MARKET_BINANCE_API_BASE") or "https://fapi.binance.com").rstrip("/"),
        push_endpoint=os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data",
        request_timeout_seconds=max(1.0, float(os.getenv("STOCK_MARKET_REQUEST_TIMEOUT_SECONDS") or 8)),
    )
