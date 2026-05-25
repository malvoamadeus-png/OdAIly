from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, HttpUrl, ValidationError, field_validator

from .freshness import DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS
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


class XCaptureWorkerSettings(BaseModel):
    attempt_retention_days: int = Field(default=3, ge=1, le=3650)
    processing_freshness_window_seconds: int = Field(
        default=DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
        ge=1,
        le=86400,
    )


def load_x_capture_worker_settings() -> XCaptureWorkerSettings:
    load_dotenv()
    payload = {
        "attempt_retention_days": os.getenv("X_CAPTURE_ATTEMPT_RETENTION_DAYS") or 3,
        "processing_freshness_window_seconds": (
            os.getenv("PROCESSING_FRESHNESS_WINDOW_SECONDS") or DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS
        ),
    }
    try:
        return XCaptureWorkerSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid X capture worker settings: {exc}") from exc


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
    writer_reasoning_effort: str = "high"
    dashscope_api_key: str | None = None
    search_embedding_model: str = "text-embedding-v4"
    search_embedding_base_url: HttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    search_window_hours: int = Field(default=24, ge=1, le=168)
    search_duplicate_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    search_ai_review_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    push_endpoint: HttpUrl = "http://47.113.217.70:8501/push/data"
    dry_run: bool = False
    request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=180.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    processing_freshness_window_seconds: int = Field(
        default=DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
        ge=1,
        le=86400,
    )


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
        "writer_reasoning_effort": os.getenv("X_PROCESS_WRITER_REASONING_EFFORT") or "high",
        "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY") or None,
        "search_embedding_model": os.getenv("SEARCH_EMBEDDING_MODEL") or "text-embedding-v4",
        "search_embedding_base_url": os.getenv("SEARCH_EMBEDDING_BASE_URL") or "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "search_window_hours": int(os.getenv("SEARCH_WINDOW_HOURS") or 24),
        "search_duplicate_threshold": float(os.getenv("SEARCH_DUPLICATE_THRESHOLD") or 0.88),
        "search_ai_review_threshold": float(os.getenv("SEARCH_AI_REVIEW_THRESHOLD") or 0.78),
        "push_endpoint": os.getenv("X_PROCESS_PUSH_ENDPOINT") or os.getenv("ODAILY_PUSH_ENDPOINT") or "http://47.113.217.70:8501/push/data",
        "dry_run": _env_bool("X_PROCESS_DRY_RUN", False),
        "request_timeout_seconds": float(os.getenv("X_PROCESS_REQUEST_TIMEOUT_SECONDS") or 30.0),
        "retry": {
            "max_attempts": int(os.getenv("X_PROCESS_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("X_PROCESS_BACKOFF_SECONDS") or 1.0),
        },
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": os.getenv("TELEGRAM_MESSAGE_THREAD_ID") or None,
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
        "processing_freshness_window_seconds": (
            os.getenv("PROCESSING_FRESHNESS_WINDOW_SECONDS") or DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS
        ),
    }
    try:
        return XProcessingSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid X processing settings: {exc}") from exc


class ExternalMediaAlertSettings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: HttpUrl = "https://api.openai.com/v1"
    openai_api_style: Literal["responses", "chat_completions"] = "responses"
    domain_judge_model: str = "gpt-5.4-mini"
    dashscope_api_key: str | None = None
    search_embedding_model: str = "text-embedding-v4"
    search_embedding_base_url: HttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    search_window_hours: int = Field(default=168, ge=1, le=720)
    search_duplicate_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    search_ai_review_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=180.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)


def load_external_media_alert_settings() -> ExternalMediaAlertSettings:
    load_dotenv()
    payload = {
        "openai_api_key": os.getenv("OPENAI_API_KEY") or None,
        "openai_base_url": (
            os.getenv("EXTERNAL_MEDIA_ALERT_OPENAI_BASE_URL")
            or os.getenv("X_PROCESS_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
        "openai_api_style": (
            os.getenv("EXTERNAL_MEDIA_ALERT_OPENAI_API_STYLE")
            or os.getenv("X_PROCESS_OPENAI_API_STYLE")
            or "responses"
        ),
        "domain_judge_model": (
            os.getenv("EXTERNAL_MEDIA_ALERT_DOMAIN_JUDGE_MODEL")
            or os.getenv("X_PROCESS_JUDGE_MODEL")
            or "gpt-5.4-mini"
        ),
        "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY") or None,
        "search_embedding_model": os.getenv("EXTERNAL_MEDIA_ALERT_SEARCH_EMBEDDING_MODEL") or os.getenv("SEARCH_EMBEDDING_MODEL") or "text-embedding-v4",
        "search_embedding_base_url": (
            os.getenv("EXTERNAL_MEDIA_ALERT_SEARCH_EMBEDDING_BASE_URL")
            or os.getenv("SEARCH_EMBEDDING_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "search_window_hours": int(os.getenv("EXTERNAL_MEDIA_ALERT_SEARCH_WINDOW_HOURS") or 168),
        "search_duplicate_threshold": float(
            os.getenv("EXTERNAL_MEDIA_ALERT_SEARCH_DUPLICATE_THRESHOLD")
            or os.getenv("SEARCH_DUPLICATE_THRESHOLD")
            or 0.88
        ),
        "search_ai_review_threshold": float(
            os.getenv("EXTERNAL_MEDIA_ALERT_SEARCH_AI_REVIEW_THRESHOLD")
            or os.getenv("SEARCH_AI_REVIEW_THRESHOLD")
            or 0.78
        ),
        "request_timeout_seconds": float(
            os.getenv("EXTERNAL_MEDIA_ALERT_REQUEST_TIMEOUT_SECONDS")
            or os.getenv("X_PROCESS_REQUEST_TIMEOUT_SECONDS")
            or 30.0
        ),
        "retry": {
            "max_attempts": int(
                os.getenv("EXTERNAL_MEDIA_ALERT_MAX_ATTEMPTS")
                or os.getenv("X_PROCESS_MAX_ATTEMPTS")
                or 3
            ),
            "backoff_seconds": float(
                os.getenv("EXTERNAL_MEDIA_ALERT_BACKOFF_SECONDS")
                or os.getenv("X_PROCESS_BACKOFF_SECONDS")
                or 1.0
            ),
        },
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": (
            os.getenv("EXTERNAL_MEDIA_ALERT_TELEGRAM_MESSAGE_THREAD_ID")
            or os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
            or None
        ),
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
    }
    try:
        return ExternalMediaAlertSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid external media alert settings: {exc}") from exc


class CompetitorMonitorSettings(BaseModel):
    blockbeats_api_key: str | None = None
    fetch_interval_seconds: int = Field(default=60, ge=10, le=3600)
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=180.0)
    event_assignment_timeout_seconds: int = Field(default=240, ge=30, le=1800)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    openai_api_key: str | None = None
    openai_base_url: HttpUrl = "https://api.openai.com/v1"
    openai_api_style: Literal["responses", "chat_completions"] = "responses"
    event_review_model: str = "gpt-5.4-mini"
    dashscope_api_key: str | None = None
    event_embedding_model: str = "text-embedding-v4"
    event_embedding_base_url: HttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    event_window_hours: int = Field(default=6, ge=1, le=168)
    event_duplicate_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    event_ai_review_threshold: float = Field(default=0.78, ge=0.0, le=1.0)
    processing_freshness_window_seconds: int = Field(
        default=DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
        ge=1,
        le=86400,
    )


def load_competitor_monitor_settings() -> CompetitorMonitorSettings:
    load_dotenv()
    payload = {
        "blockbeats_api_key": os.getenv("BLOCKBEATS_API_KEY") or None,
        "fetch_interval_seconds": int(os.getenv("COMPETITOR_FETCH_INTERVAL_SECONDS") or 60),
        "request_timeout_seconds": float(os.getenv("COMPETITOR_REQUEST_TIMEOUT_SECONDS") or 20.0),
        "event_assignment_timeout_seconds": int(os.getenv("COMPETITOR_EVENT_ASSIGNMENT_TIMEOUT_SECONDS") or 240),
        "retry": {
            "max_attempts": int(os.getenv("COMPETITOR_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("COMPETITOR_BACKOFF_SECONDS") or 1.0),
        },
        "openai_api_key": os.getenv("OPENAI_API_KEY") or None,
        "openai_base_url": (
            os.getenv("X_PROCESS_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
        "openai_api_style": os.getenv("COMPETITOR_OPENAI_API_STYLE") or os.getenv("X_PROCESS_OPENAI_API_STYLE") or "responses",
        "event_review_model": os.getenv("COMPETITOR_EVENT_REVIEW_MODEL") or os.getenv("X_PROCESS_JUDGE_MODEL") or "gpt-5.4-mini",
        "dashscope_api_key": os.getenv("DASHSCOPE_API_KEY") or None,
        "event_embedding_model": os.getenv("COMPETITOR_EVENT_EMBEDDING_MODEL") or os.getenv("SEARCH_EMBEDDING_MODEL") or "text-embedding-v4",
        "event_embedding_base_url": (
            os.getenv("COMPETITOR_EVENT_EMBEDDING_BASE_URL")
            or os.getenv("SEARCH_EMBEDDING_BASE_URL")
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        ),
        "event_window_hours": int(os.getenv("COMPETITOR_EVENT_WINDOW_HOURS") or os.getenv("SEARCH_WINDOW_HOURS") or 6),
        "event_duplicate_threshold": float(
            os.getenv("COMPETITOR_EVENT_DUPLICATE_THRESHOLD") or os.getenv("SEARCH_DUPLICATE_THRESHOLD") or 0.88
        ),
        "event_ai_review_threshold": float(
            os.getenv("COMPETITOR_EVENT_AI_REVIEW_THRESHOLD") or os.getenv("SEARCH_AI_REVIEW_THRESHOLD") or 0.78
        ),
        "processing_freshness_window_seconds": (
            os.getenv("PROCESSING_FRESHNESS_WINDOW_SECONDS") or DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS
        ),
    }
    try:
        return CompetitorMonitorSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid competitor monitor settings: {exc}") from exc


class WhaleWatchSettings(BaseModel):
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    chain_keys: tuple[str, ...] = ("ethereum", "base")
    request_timeout_seconds: float = Field(default=20.0, gt=0.0, le=180.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)


def load_whale_watch_settings() -> WhaleWatchSettings:
    load_dotenv()
    raw_chain_keys = os.getenv("WHALE_WATCH_CHAIN_KEYS") or "ethereum,base"
    chain_keys = tuple(item.strip() for item in raw_chain_keys.split(",") if item.strip())
    payload = {
        "interval_seconds": int(os.getenv("WHALE_WATCH_INTERVAL_SECONDS") or 60),
        "chain_keys": chain_keys or ("ethereum", "base"),
        "request_timeout_seconds": float(os.getenv("WHALE_WATCH_REQUEST_TIMEOUT_SECONDS") or 20.0),
        "retry": {
            "max_attempts": int(os.getenv("WHALE_WATCH_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("WHALE_WATCH_BACKOFF_SECONDS") or 1.0),
        },
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": (
            os.getenv("WHALE_TELEGRAM_MESSAGE_THREAD_ID")
            or os.getenv("TELEGRAM_MESSAGE_THREAD_ID")
            or None
        ),
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
    }
    try:
        return WhaleWatchSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid whale watch settings: {exc}") from exc


class PipelineSupervisorSettings(BaseModel):
    interval_seconds: int = Field(default=60, ge=10, le=3600)
    heartbeat_stale_minutes: int = Field(default=10, ge=1, le=1440)
    task_stuck_minutes: int = Field(default=10, ge=1, le=1440)
    alert_dedup_minutes: int = Field(default=30, ge=1, le=1440)
    failed_window_minutes: int = Field(default=30, ge=1, le=1440)
    failed_threshold: int = Field(default=3, ge=1, le=1000)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)


def load_pipeline_supervisor_settings() -> PipelineSupervisorSettings:
    load_dotenv()
    payload = {
        "interval_seconds": int(os.getenv("PIPELINE_SUPERVISOR_INTERVAL_SECONDS") or 60),
        "heartbeat_stale_minutes": int(os.getenv("PIPELINE_SUPERVISOR_HEARTBEAT_STALE_MINUTES") or 10),
        "task_stuck_minutes": int(os.getenv("PIPELINE_SUPERVISOR_TASK_STUCK_MINUTES") or 10),
        "alert_dedup_minutes": int(os.getenv("PIPELINE_SUPERVISOR_ALERT_DEDUP_MINUTES") or 30),
        "failed_window_minutes": int(os.getenv("PIPELINE_SUPERVISOR_FAILED_WINDOW_MINUTES") or 30),
        "failed_threshold": int(os.getenv("PIPELINE_SUPERVISOR_FAILED_THRESHOLD") or 3),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": os.getenv("TELEGRAM_MESSAGE_THREAD_ID") or None,
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
        "retry": {
            "max_attempts": int(os.getenv("PIPELINE_SUPERVISOR_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("PIPELINE_SUPERVISOR_BACKOFF_SECONDS") or 1.0),
        },
    }
    try:
        return PipelineSupervisorSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid pipeline supervisor settings: {exc}") from exc


class Writer3Settings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: HttpUrl = "https://api.openai.com/v1"
    openai_api_style: Literal["responses", "chat_completions"] = "responses"
    analysis_model: str = "gpt-5.4-mini"
    writer_model: str = "gpt-5.5"
    writer_reasoning_effort: str = "medium"
    history_days: int = Field(default=90, ge=1, le=365)
    candidate_limit: int = Field(default=20, ge=1, le=100)
    context_candidates: int = Field(default=5, ge=1, le=20)
    current_freshness_window_seconds: int = Field(default=600, ge=1, le=86400)
    request_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    start_after: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    worker_idle_sleep_seconds: float = Field(default=10.0, ge=1.0, le=300.0)


def load_writer3_settings() -> Writer3Settings:
    load_dotenv()
    payload = {
        "openai_api_key": os.getenv("OPENAI_API_KEY") or None,
        "openai_base_url": (
            os.getenv("WRITER3_OPENAI_BASE_URL")
            or os.getenv("X_PROCESS_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
        "openai_api_style": os.getenv("WRITER3_OPENAI_API_STYLE") or os.getenv("X_PROCESS_OPENAI_API_STYLE") or "responses",
        "analysis_model": os.getenv("WRITER3_ANALYSIS_MODEL") or "gpt-5.4-mini",
        "writer_model": os.getenv("WRITER3_WRITER_MODEL") or "gpt-5.5",
        "writer_reasoning_effort": os.getenv("WRITER3_WRITER_REASONING_EFFORT") or "medium",
        "history_days": int(os.getenv("WRITER3_HISTORY_DAYS") or 90),
        "candidate_limit": int(os.getenv("WRITER3_CANDIDATE_LIMIT") or 20),
        "context_candidates": int(os.getenv("WRITER3_CONTEXT_CANDIDATES") or 5),
        "current_freshness_window_seconds": int(os.getenv("WRITER3_CURRENT_FRESHNESS_WINDOW_SECONDS") or 600),
        "request_timeout_seconds": float(os.getenv("WRITER3_REQUEST_TIMEOUT_SECONDS") or 60.0),
        "retry": {
            "max_attempts": int(os.getenv("WRITER3_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("WRITER3_BACKOFF_SECONDS") or 1.0),
        },
        "start_after": os.getenv("WRITER3_START_AFTER") or None,
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": os.getenv("WRITER3_TELEGRAM_MESSAGE_THREAD_ID") or None,
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
        "worker_idle_sleep_seconds": float(os.getenv("WRITER3_WORKER_IDLE_SLEEP_SECONDS") or 10.0),
    }
    try:
        return Writer3Settings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid Writer3 settings: {exc}") from exc


class AuditorSettings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: HttpUrl = "https://api.openai.com/v1"
    openai_api_style: Literal["responses", "chat_completions"] = "responses"
    model: str = "gpt-5.4-mini"
    lookback_minutes: int = Field(default=120, ge=1, le=10080)
    max_items_per_run: int = Field(default=20, ge=1, le=200)
    request_timeout_seconds: float = Field(default=60.0, gt=0.0, le=300.0)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_message_thread_id: int | None = None
    telegram_timeout_seconds: float = Field(default=10.0, gt=0.0, le=60.0)
    worker_idle_sleep_seconds: float = Field(default=10.0, ge=1.0, le=300.0)


def load_auditor_settings() -> AuditorSettings:
    load_dotenv()
    payload = {
        "openai_api_key": os.getenv("OPENAI_API_KEY") or None,
        "openai_base_url": (
            os.getenv("AUDITOR_OPENAI_BASE_URL")
            or os.getenv("X_PROCESS_OPENAI_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ),
        "openai_api_style": os.getenv("AUDITOR_OPENAI_API_STYLE") or os.getenv("X_PROCESS_OPENAI_API_STYLE") or "responses",
        "model": os.getenv("AUDITOR_MODEL") or "gpt-5.4-mini",
        "lookback_minutes": int(os.getenv("AUDITOR_LOOKBACK_MINUTES") or 120),
        "max_items_per_run": int(os.getenv("AUDITOR_MAX_ITEMS_PER_RUN") or 20),
        "request_timeout_seconds": float(os.getenv("AUDITOR_REQUEST_TIMEOUT_SECONDS") or 60.0),
        "retry": {
            "max_attempts": int(os.getenv("AUDITOR_MAX_ATTEMPTS") or 3),
            "backoff_seconds": float(os.getenv("AUDITOR_BACKOFF_SECONDS") or 1.0),
        },
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN") or None,
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID") or None,
        "telegram_message_thread_id": os.getenv("AUDITOR_TELEGRAM_MESSAGE_THREAD_ID") or None,
        "telegram_timeout_seconds": float(os.getenv("TELEGRAM_TIMEOUT_SECONDS") or 10.0),
        "worker_idle_sleep_seconds": float(os.getenv("AUDITOR_WORKER_IDLE_SLEEP_SECONDS") or 10.0),
    }
    try:
        return AuditorSettings.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"Invalid Auditor settings: {exc}") from exc
