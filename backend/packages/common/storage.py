from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv

from .paths import AppPaths, get_paths
from .time_utils import today_key


StorageBackend = Literal["sqlite"]
STORAGE_EPOCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


@dataclass(frozen=True, slots=True)
class StorageSettings:
    backend: StorageBackend
    epoch: str
    sqlite_path: Path


def load_storage_settings() -> StorageSettings:
    load_dotenv()
    raw_backend = (os.getenv("ODAILY_STORAGE_BACKEND") or "sqlite").strip().lower()
    if raw_backend != "sqlite":
        raise ValueError("ODAILY_STORAGE_BACKEND must be sqlite in this version")
    backend: StorageBackend = raw_backend  # type: ignore[assignment]
    epoch = (os.getenv("ODAILY_STORAGE_EPOCH") or "sqlite-primary").strip()
    if not STORAGE_EPOCH_PATTERN.fullmatch(epoch):
        raise ValueError("ODAILY_STORAGE_EPOCH contains unsupported characters")
    default_path = get_paths().data_dir / "database" / "odaily.sqlite"
    sqlite_path = Path(os.getenv("ODAILY_SQLITE_PATH") or default_path).expanduser().resolve()
    return StorageSettings(backend=backend, epoch=epoch, sqlite_path=sqlite_path)


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then release the file handle."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def connect_sqlite(path: Path, *, timeout_seconds: float = 30.0) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout_seconds, factory=ClosingSQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    current_journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if current_journal_mode != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def initialize_storage_metadata(path: Path) -> None:
    with connect_sqlite(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS storage_schema_migrations (
                version integer PRIMARY KEY,
                name text NOT NULL,
                applied_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS storage_runtime (
                singleton_key text PRIMARY KEY CHECK (singleton_key = 'global'),
                active_backend text NOT NULL CHECK (active_backend = 'sqlite'),
                active_epoch text NOT NULL,
                updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()


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


def save_gate_quotes(paths: AppPaths, *, run_id: str, payload: Any) -> Path:
    path = paths.raw_dir / "gate_quotes" / today_key() / f"{run_id}.json"
    _write_json(path, payload)
    return path


def append_brief_result(paths: AppPaths, *, date_key: str, payload: dict[str, Any]) -> Path:
    path = paths.processed_dir / "briefs" / f"{date_key}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, default=_json_default) + "\n")
    return path
