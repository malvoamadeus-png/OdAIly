from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from .paths import get_paths


DEFAULT_WATCHLIST = [
    "FRMM",
    "CRCL",
    "BTBT",
    "HOOD",
    "HUT",
    "COIN",
    "DFDV",
    "TRON",
    "BLSH",
    "RIOT",
    "MARA",
    "ABTC",
    "MSTR",
    "SBET",
    "UPXI",
    "BTCS",
    "BNC",
    "HODL",
    "SPX",
    "VIX",
    "IXIC",
    "DJI",
]


class RetrySettings(BaseModel):
    max_attempts: int = Field(default=3, ge=1, le=5)
    backoff_seconds: float = Field(default=1.0, ge=0.0, le=60.0)


class MarketBriefSettings(BaseModel):
    watchlist: list[str] = Field(default_factory=lambda: list(DEFAULT_WATCHLIST))
    market_data_sources: list[Literal["yahoo_quote", "yahoo_chart", "finnhub"]] = Field(
        default_factory=lambda: ["yahoo_quote", "yahoo_chart", "finnhub"]
    )
    yahoo_symbol_overrides: dict[str, str] = Field(
        default_factory=lambda: {
            "SPX": "^GSPC",
            "VIX": "^VIX",
            "IXIC": "^IXIC",
            "DJI": "^DJI",
        }
    )
    finnhub_api_key: str | None = None
    finnhub_symbol_overrides: dict[str, str] = Field(
        default_factory=lambda: {
            "SPX": "^GSPC",
            "VIX": "^VIX",
            "IXIC": "^IXIC",
            "DJI": "^DJI",
        }
    )
    min_valid_crypto_stocks: int = Field(default=10, ge=1, le=100)
    min_valid_indices: int = Field(default=2, ge=0, le=4)
    max_quote_age_minutes: int = Field(default=10, ge=1, le=1440)
    push_endpoint: HttpUrl = "http://47.113.217.70:8501/push/data"
    dry_run: bool = True
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    log_retention_days: int = Field(default=30, ge=1, le=3650)

    @field_validator("watchlist")
    @classmethod
    def normalize_watchlist(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in value:
            symbol = item.strip().upper()
            if not symbol or symbol in seen:
                continue
            seen.add(symbol)
            result.append(symbol)
        if not result:
            raise ValueError("watchlist cannot be empty")
        return result

    @field_validator("market_data_sources")
    @classmethod
    def normalize_market_data_sources(
        cls,
        value: list[Literal["yahoo_quote", "yahoo_chart", "finnhub"]],
    ) -> list[Literal["yahoo_quote", "yahoo_chart", "finnhub"]]:
        result = list(dict.fromkeys(value))
        if not result:
            raise ValueError("market_data_sources cannot be empty")
        return result

    @field_validator("yahoo_symbol_overrides")
    @classmethod
    def normalize_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        return {key.strip().upper(): symbol.strip() for key, symbol in value.items() if key and symbol}

    @field_validator("finnhub_symbol_overrides")
    @classmethod
    def normalize_provider_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        return {key.strip().upper(): symbol.strip().upper() for key, symbol in value.items() if key and symbol}


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON config: {path}") from exc


def load_settings(config_path: str | None = None) -> MarketBriefSettings:
    load_dotenv()
    paths = get_paths()
    raw_path = Path(config_path).resolve() if config_path else paths.market_brief_config_path
    payload = _apply_common_env(_load_json(raw_path))

    try:
        return MarketBriefSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid market brief settings in {raw_path}: {exc}") from exc


BriefKind = Literal["close", "premarket", "open"]


DEFAULT_GATE_TRADFI_SYMBOLS = {
    "XAUUSD": "黄金",
    "XAGUSD": "白银",
    "USDCNH": "美元兑离岸人民币",
    "USDJPY": "美元兑日元",
    "EUSTX50": "欧洲50指数",
    "UK100": "英国富时100指数",
    "GER40": "德国DAX40指数",
    "XTIUSD": "WTI 原油",
    "XBRUSD": "布伦特原油",
}

DEFAULT_GATE_FUTURES_SYMBOLS = {
    "BVIXUSDT": {"contract": "BVIX_USDT", "display_name": "BVIX"},
    "EVIXUSDT": {"contract": "EVIX_USDT", "display_name": "EVIX"},
}


class GateFuturesSymbolSettings(BaseModel):
    contract: str
    display_name: str

    @field_validator("contract")
    @classmethod
    def normalize_contract(cls, value: str) -> str:
        return value.strip().upper()


class GateTradfiSettings(BaseModel):
    tradfi_symbols: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_GATE_TRADFI_SYMBOLS))
    futures_symbols: dict[str, GateFuturesSymbolSettings] = Field(
        default_factory=lambda: {
            key: GateFuturesSymbolSettings.model_validate(value)
            for key, value in DEFAULT_GATE_FUTURES_SYMBOLS.items()
        }
    )
    push_endpoint: HttpUrl = "http://47.113.217.70:8501/push/data"
    dry_run: bool = True
    request_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    log_retention_days: int = Field(default=30, ge=1, le=3650)

    @field_validator("tradfi_symbols")
    @classmethod
    def normalize_tradfi_symbols(cls, value: dict[str, str]) -> dict[str, str]:
        result = {key.strip().upper(): display.strip() for key, display in value.items() if key and display}
        if not result:
            raise ValueError("tradfi_symbols cannot be empty")
        return result

    @field_validator("futures_symbols")
    @classmethod
    def normalize_futures_symbols(
        cls,
        value: dict[str, GateFuturesSymbolSettings],
    ) -> dict[str, GateFuturesSymbolSettings]:
        return {key.strip().upper(): item for key, item in value.items() if key}


def _apply_common_env(payload: dict) -> dict:
    endpoint = os.getenv("ODAILY_PUSH_ENDPOINT")
    if endpoint:
        payload["push_endpoint"] = endpoint
    dry_run = os.getenv("ODAILY_DRY_RUN")
    if dry_run is not None:
        payload["dry_run"] = dry_run.strip().lower() not in {"0", "false", "no", "off"}
    finnhub_api_key = os.getenv("FINNHUB_API_KEY")
    if finnhub_api_key:
        payload["finnhub_api_key"] = finnhub_api_key.strip()
    return payload


def load_gate_settings(config_path: str | None = None) -> GateTradfiSettings:
    load_dotenv()
    paths = get_paths()
    raw_path = Path(config_path).resolve() if config_path else paths.gate_tradfi_config_path
    payload = _apply_common_env(_load_json(raw_path))

    try:
        return GateTradfiSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid Gate TradFi settings in {raw_path}: {exc}") from exc


GateBriefKind = Literal["morning", "open"]


class XProcessingSettings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: HttpUrl = "https://api.openai.com/v1"
    openai_api_style: Literal["responses", "chat_completions"] = "responses"
    judge_model: str = "gpt-5.4-mini"
    writer_model: str = "gpt-5.5"
    push_endpoint: HttpUrl = "http://47.113.217.70:8501/push/data"
    dry_run: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=180.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def load_x_processing_settings() -> XProcessingSettings:
    load_dotenv()
    payload = {
        "openai_api_key": os.getenv("OPENAI_API_KEY") or None,
        "openai_base_url": (
            os.getenv("X_PROCESS_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
        "openai_api_style": os.getenv("X_PROCESS_OPENAI_API_STYLE") or "responses",
        "judge_model": os.getenv("X_PROCESS_JUDGE_MODEL") or "gpt-5.4-mini",
        "writer_model": os.getenv("X_PROCESS_WRITER_MODEL") or "gpt-5.5",
        "push_endpoint": os.getenv("X_PROCESS_PUSH_ENDPOINT") or os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data",
        "dry_run": _env_bool("X_PROCESS_DRY_RUN", False),
        "request_timeout_seconds": float(os.getenv("X_PROCESS_REQUEST_TIMEOUT_SECONDS") or 30.0),
        "retry": {
            "max_attempts": int(os.getenv("X_PROCESS_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("X_PROCESS_BACKOFF_SECONDS") or 1.0),
        },
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
    }
    try:
        return XProcessingSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid X processing settings: {exc}") from exc
