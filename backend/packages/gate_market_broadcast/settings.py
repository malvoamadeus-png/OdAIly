from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from packages.common.paths import get_paths


@dataclass(frozen=True, slots=True)
class GateMarketSettings:
    database_path: Path
    gate_api_base: str
    push_endpoint: str
    request_timeout_seconds: float
    gate_max_attempts: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    telegram_message_thread_id: int | None
    telegram_timeout_seconds: float
    alert_dedup_minutes: int
    disk_free_alert_mb: int


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value)


def load_gate_market_settings() -> GateMarketSettings:
    load_dotenv()
    paths = get_paths()
    database_path = Path(
        os.getenv("GATE_MARKET_DB_PATH") or paths.runtime_dir / "gate_market.sqlite"
    ).resolve()
    return GateMarketSettings(
        database_path=database_path,
        gate_api_base=(os.getenv("GATE_MARKET_API_BASE") or "https://api.gateio.ws/api/v4").rstrip("/"),
        push_endpoint=os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data",
        request_timeout_seconds=float(os.getenv("GATE_MARKET_REQUEST_TIMEOUT_SECONDS") or 15),
        gate_max_attempts=max(1, int(os.getenv("GATE_MARKET_GATE_MAX_ATTEMPTS") or 2)),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
        telegram_message_thread_id=_optional_int(
            os.getenv("GATE_MARKET_TELEGRAM_MESSAGE_THREAD_ID")
            or os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
        ),
        telegram_timeout_seconds=float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10),
        alert_dedup_minutes=max(1, int(os.getenv("GATE_MARKET_ALERT_DEDUP_MINUTES") or 30)),
        disk_free_alert_mb=max(1, int(os.getenv("GATE_MARKET_DISK_FREE_ALERT_MB") or 500)),
    )
