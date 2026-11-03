from __future__ import annotations

import os
from pathlib import Path


def system_observability_directory(default_runtime_dir: Path, configured_dir: str | None = None) -> Path:
    value = (configured_dir if configured_dir is not None else os.getenv("ODAILY_SYSTEM_OBSERVABILITY_DIR", "")).strip()
    return Path(value).expanduser() if value else default_runtime_dir
