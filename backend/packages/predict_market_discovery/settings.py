from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from packages.common.paths import get_paths


@dataclass(frozen=True, slots=True)
class PredictMarketSettings:
    api_key: str | None
    api_base_url: str
    database_path: Path
    poll_interval_seconds: int
    request_timeout_seconds: float
    max_pages: int
    page_size: int


def load_predict_market_settings() -> PredictMarketSettings:
    load_dotenv()
    paths = get_paths()
    return PredictMarketSettings(
        api_key=os.getenv("PREDICT_FUN_API_KEY") or None,
        api_base_url=(os.getenv("PREDICT_FUN_API_BASE_URL") or "https://api.predict.fun").rstrip("/"),
        database_path=Path(
            os.getenv("PREDICT_MARKET_DB_PATH") or paths.runtime_dir / "predict_market_discovery.sqlite"
        ).expanduser().resolve(),
        poll_interval_seconds=max(60, int(os.getenv("PREDICT_MARKET_POLL_INTERVAL_SECONDS") or 600)),
        request_timeout_seconds=max(2.0, float(os.getenv("PREDICT_MARKET_REQUEST_TIMEOUT_SECONDS") or 15)),
        max_pages=max(1, min(100, int(os.getenv("PREDICT_MARKET_MAX_PAGES") or 80))),
        page_size=max(1, min(100, int(os.getenv("PREDICT_MARKET_PAGE_SIZE") or 100))),
    )
