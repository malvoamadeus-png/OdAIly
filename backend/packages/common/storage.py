from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
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


def load_hyperliquid_storage_path() -> Path:
    load_dotenv()
    default_path = get_paths().runtime_dir / "hyperliquid.sqlite"
    return Path(os.getenv("ODAILY_HYPERLIQUID_SQLITE_PATH") or default_path).expanduser().resolve()


_SQLITE_SLOW_SECONDS = 1.0
_SQLITE_TRANSACTION_SECONDS = 2.0


def _sqlite_location() -> str:
    frame = sys._getframe(1)
    while frame and frame.f_code.co_filename == __file__:
        frame = frame.f_back
    if frame is None:
        return "unknown"
    return f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}"


class MonitoredSQLiteCursor(sqlite3.Cursor):
    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self.connection._monitored_call(super().execute, sql, parameters)  # type: ignore[attr-defined]

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self.connection._monitored_call(super().executemany, sql, parameters)  # type: ignore[attr-defined]

    def executescript(self, sql: str, /) -> sqlite3.Cursor:
        return self.connection._monitored_call(super().executescript, sql)  # type: ignore[attr-defined]

    def fetchone(self) -> Any:
        return self.connection._monitored_call(lambda _sql: super(MonitoredSQLiteCursor, self).fetchone(), "FETCH")  # type: ignore[attr-defined]

    def fetchall(self) -> list[Any]:
        return self.connection._monitored_call(lambda _sql: super(MonitoredSQLiteCursor, self).fetchall(), "FETCH")  # type: ignore[attr-defined]

    def fetchmany(self, size: int = 0) -> list[Any]:
        return self.connection._monitored_call(lambda _sql: super(MonitoredSQLiteCursor, self).fetchmany(size or self.arraysize), "FETCH")  # type: ignore[attr-defined]


class ClosingSQLiteConnection(sqlite3.Connection):
    """Commit/rollback like sqlite3.Connection, then release the file handle."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._monitor_db = Path(str(args[0]).split("?", 1)[0]).name if args else "unknown"
        self._transaction_started: float | None = None
        self._transaction_location = "unknown"

    def _log(self, event: str, **fields: Any) -> None:
        try:
            print("[sqlite-observe] " + json.dumps({"event": event, "db": self._monitor_db,
                  "pid": os.getpid(), **fields}, ensure_ascii=False, separators=(",", ":")), file=sys.stderr, flush=True)
        except Exception:
            pass

    def _finish_transaction(self, outcome: str) -> None:
        if self._transaction_started is not None:
            duration = time.monotonic() - self._transaction_started
            if duration >= _SQLITE_TRANSACTION_SECONDS:
                self._log("long_transaction", duration_ms=round(duration * 1000), location=self._transaction_location, outcome=outcome)
        self._transaction_started = None

    def _monitored_call(self, operation: Any, sql: str, *args: Any) -> Any:
        started = time.monotonic()
        was_transaction = self.in_transaction
        error = None
        try:
            return operation(sql, *args)
        except sqlite3.Error as exc:
            error = exc
            raise
        finally:
            duration = time.monotonic() - started
            code = getattr(error, "sqlite_errorcode", None)
            locked = code is not None and (code & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
            if locked or duration >= _SQLITE_SLOW_SECONDS:
                token = (sql.lstrip().split(None, 1) or [""])[0].upper()
                kind = token if token in {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA", "CREATE", "ALTER", "DROP", "WITH", "FETCH"} else "OTHER"
                self._log("locked" if locked else "slow_statement", duration_ms=round(duration * 1000),
                          location=_sqlite_location(), operation=kind,
                          error_code=code, error_name=getattr(error, "sqlite_errorname", None))
            if not was_transaction and self.in_transaction:
                self._transaction_started = time.monotonic()
                self._transaction_location = _sqlite_location()
            elif was_transaction and not self.in_transaction:
                self._finish_transaction("commit" if sql == "COMMIT" else "ended")

    def cursor(self, factory: Any = None) -> sqlite3.Cursor:
        return super().cursor(factory or MonitoredSQLiteCursor)

    def execute(self, sql: str, parameters: Any = (), /) -> sqlite3.Cursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, parameters: Any, /) -> sqlite3.Cursor:
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql: str, /) -> sqlite3.Cursor:
        return self.cursor().executescript(sql)

    def commit(self) -> None:
        try:
            self._monitored_call(lambda _sql: super(ClosingSQLiteConnection, self).commit(), "COMMIT")
        finally:
            if not self.in_transaction:
                self._finish_transaction("commit")

    def rollback(self) -> None:
        try:
            super().rollback()
        finally:
            if not self.in_transaction:
                self._finish_transaction("rollback")

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self._finish_transaction("context_exit")
            self.close()

    def close(self) -> None:
        self._finish_transaction("close")
        super().close()


class MonitoredPersistentSQLiteConnection(ClosingSQLiteConnection):
    """Observe connections whose context manager must not close them."""

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return bool(sqlite3.Connection.__exit__(self, exc_type, exc_value, traceback))
        finally:
            if not self.in_transaction:
                self._finish_transaction("context_exit")


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


def connect_sqlite_readonly(path: Path, *, timeout_seconds: float = 10.0) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout_seconds, factory=ClosingSQLiteConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout_seconds * 1000))}")
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
