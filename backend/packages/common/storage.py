from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .paths import AppPaths
from .time_utils import today_key


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def save_market_quotes(paths: AppPaths, *, run_id: str, payload: Any) -> Path:
    path = paths.raw_dir / "market_quotes" / today_key() / f"{run_id}.json"
    _write_json(path, payload)
    return path


def append_brief_result(paths: AppPaths, *, date_key: str, payload: dict[str, Any]) -> Path:
    path = paths.processed_dir / "briefs" / f"{date_key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
    return path
