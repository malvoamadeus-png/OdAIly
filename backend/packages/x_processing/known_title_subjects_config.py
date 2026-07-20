from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from packages.common.paths import ensure_runtime_dirs, get_paths

from .title_trace import DEFAULT_KNOWN_TITLE_SUBJECT_NAMES, normalize_known_title_subject_names


class KnownTitleSubjectsConfig(BaseModel):
    names: list[str] = Field(default_factory=lambda: list(DEFAULT_KNOWN_TITLE_SUBJECT_NAMES))
    updated_at: str | None = None
    updated_by: str | None = None

    @field_validator("names", mode="before")
    @classmethod
    def normalize_names(cls, value: Any) -> list[str]:
        return normalize_known_title_subject_names(value)


def default_known_title_subjects_config() -> KnownTitleSubjectsConfig:
    return KnownTitleSubjectsConfig(names=list(DEFAULT_KNOWN_TITLE_SUBJECT_NAMES))


def get_known_title_subjects_config_path() -> Path:
    paths = get_paths()
    ensure_runtime_dirs(paths)
    configured = os.getenv("KNOWN_TITLE_SUBJECTS_CONFIG_PATH")
    return Path(configured) if configured else paths.config_dir / "known_title_subjects.json"


def load_known_title_subjects_config(path: Path | None = None) -> KnownTitleSubjectsConfig:
    config_path = path or get_known_title_subjects_config_path()
    if not config_path.exists():
        return default_known_title_subjects_config()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return KnownTitleSubjectsConfig.model_validate(payload)
    except (OSError, ValueError, ValidationError):
        return default_known_title_subjects_config()


def load_known_title_subject_names(path: Path | None = None) -> list[str]:
    return list(load_known_title_subjects_config(path=path).names)


def save_known_title_subject_names(
    names: list[str] | str,
    *,
    updated_by: str | None = None,
    path: Path | None = None,
) -> KnownTitleSubjectsConfig:
    config = KnownTitleSubjectsConfig(
        names=normalize_known_title_subject_names(names),
        updated_at=datetime.now(UTC).isoformat(),
        updated_by=updated_by,
    )
    config_path = path or get_known_title_subjects_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = config_path.with_suffix(f"{config_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(config_path)
    return config
