from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from packages.common.paths import ensure_runtime_dirs, get_paths


BlockbeatsKeyStatus = Literal["unknown", "ok", "quota_exhausted", "request_failed", "missing_key"]


class BlockbeatsKeyConfig(BaseModel):
    api_key: str = ""
    status: BlockbeatsKeyStatus = "unknown"
    last_checked_at: str | None = None
    last_success_at: str | None = None
    last_quota_error_at: str | None = None
    last_error: str | None = None
    last_error_payload: dict[str, Any] | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str:
        return (value or "").strip()


class BlockbeatsKeySaveRequest(BaseModel):
    api_key: str = Field(default="")

    @field_validator("api_key")
    @classmethod
    def normalize_api_key(cls, value: str | None) -> str:
        return (value or "").strip()


def get_blockbeats_key_config_path() -> Path:
    paths = get_paths()
    ensure_runtime_dirs(paths)
    configured = os.getenv("BLOCKBEATS_KEY_CONFIG_PATH")
    return Path(configured) if configured else paths.config_dir / "blockbeats_key.json"


def load_blockbeats_key_config(path: Path | None = None) -> BlockbeatsKeyConfig:
    config_path = path or get_blockbeats_key_config_path()
    if not config_path.exists():
        return BlockbeatsKeyConfig()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return BlockbeatsKeyConfig.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        return BlockbeatsKeyConfig(status="unknown", last_error="local config is unreadable")


def save_blockbeats_key_config(config: BlockbeatsKeyConfig, *, path: Path | None = None) -> BlockbeatsKeyConfig:
    config_path = path or get_blockbeats_key_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_suffix(f"{config_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    return config


def save_blockbeats_key(api_key: str, *, updated_by: str | None = None, path: Path | None = None) -> BlockbeatsKeyConfig:
    now = _now_iso()
    config = BlockbeatsKeyConfig(
        api_key=api_key,
        status="unknown",
        last_checked_at=None,
        last_success_at=None,
        last_quota_error_at=None,
        last_error=None,
        last_error_payload=None,
        updated_at=now,
        updated_by=updated_by,
    )
    return save_blockbeats_key_config(config, path=path)


def record_blockbeats_key_status(
    status: BlockbeatsKeyStatus,
    *,
    error: str | None = None,
    error_payload: dict[str, Any] | None = None,
    path: Path | None = None,
) -> BlockbeatsKeyConfig:
    now = _now_iso()
    current = load_blockbeats_key_config(path=path)
    update: dict[str, Any] = {
        "status": status,
        "last_checked_at": now,
        "updated_at": now,
        "last_error": error,
        "last_error_payload": error_payload,
    }
    if status == "ok":
        update["last_success_at"] = now
        update["last_error"] = None
        update["last_error_payload"] = None
    if status == "quota_exhausted":
        update["last_quota_error_at"] = now
    next_config = current.model_copy(update=update)
    return save_blockbeats_key_config(next_config, path=path)


def error_payload_summary(payload: Any) -> dict[str, Any] | None:
    if payload is None:
        return None
    if isinstance(payload, dict):
        summary: dict[str, Any] = {}
        for key in ("code", "status", "message", "msg", "error", "err_msg", "errmsg", "detail"):
            if key in payload:
                summary[key] = _short_value(payload.get(key))
        return summary or {"raw": _short_value(payload)}
    return {"raw": _short_value(payload)}


def _short_value(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    text = str(value)
    return text[:1000]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
