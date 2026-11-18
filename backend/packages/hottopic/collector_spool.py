from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any


class CollectorSpool:
    """Bounded, fsynced per-result write-ahead spool for PostgreSQL collection."""

    def __init__(
        self, directory: Path, *, max_bytes: int = 64 * 1024 * 1024,
        max_record_bytes: int | None = None,
    ) -> None:
        self.directory = directory.expanduser().resolve()
        self.failed_directory = self.directory / "failed"
        self.max_bytes = max(1, int(max_bytes))
        self.max_record_bytes = min(self.max_bytes, max(1, int(max_record_bytes or self.max_bytes)))
        self._lock = threading.RLock()
        self._reservations: dict[str, int] = {}
        self._reserved = 0
        if not self.directory.is_dir():
            raise FileNotFoundError(f"collector spool directory must already exist: {self.directory}")
        self.failed_directory.mkdir(mode=0o700, exist_ok=True)
        if any(self.failed_directory.glob("*.json")):
            self._failed_error = "collector spool has terminal oversized records; investigate before resuming"
        else:
            self._failed_error = None
        if any(self.directory.glob("*.tmp")):
            raise RuntimeError("collector spool contains incomplete temporary files; inspect before restarting collection")

    def reserve(self) -> str | None:
        with self._lock:
            if self._failed_error:
                return None
            if self._size() + self._reserved + self.max_record_bytes > self.max_bytes:
                return None
            token = uuid.uuid4().hex
            self._reservations[token] = self.max_record_bytes
            self._reserved += self.max_record_bytes
            return token

    def release_reservation(self, token: str) -> None:
        with self._lock:
            self._reserved -= self._reservations.pop(token, 0)

    def append_reserved(self, token: str, record: dict[str, Any]) -> str:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise SpoolRecordTooLarge(f"collector result exceeds total spool capacity ({self.max_bytes} bytes)")
        spool_id = str(record.get("spool_id") or uuid.uuid4().hex)
        target = self.directory / f"{spool_id}.json"
        temporary = self.directory / f"{spool_id}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            reserved = self._reservations.get(token)
            if reserved is None:
                raise RuntimeError("collector spool reservation is missing")
            if target.exists():
                try:
                    existing = target.read_bytes()
                except OSError:
                    raise
                if existing != encoded:
                    raise FileExistsError(f"collector spool record already exists: {target.name}")
                self._fsync_directory()
                self.release_reservation(token)
                return spool_id
            if self._size() + self._reserved - reserved + len(encoded) > self.max_bytes:
                raise SpoolFullError(f"collector spool is full ({self.max_bytes} bytes)")
            try:
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                self._fsync_directory()
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            self.release_reservation(token)
        return spool_id

    def append(self, record: dict[str, Any]) -> str:
        token = self.reserve()
        if token is None:
            raise SpoolFullError(f"collector spool is full ({self.max_bytes} bytes)")
        try:
            return self.append_reserved(token, record)
        except Exception:
            self.release_reservation(token)
            raise

    def append_reserved_retrying(
        self, token: str, record: dict[str, Any], *, attempts: int = 5, delay_seconds: float = 0.1,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                return self.append_reserved(token, record)
            except SpoolRecordTooLarge:
                self.quarantine_oversized(token, record)
                raise
            except FileExistsError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(max(0.0, delay_seconds) * (attempt + 1))
        assert last_error is not None
        raise last_error

    def quarantine_oversized(self, token: str, record: dict[str, Any]) -> Path:
        """Durably retain a result that can never fit, then block new reservations."""
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        record_id = str(record.get("spool_id") or uuid.uuid4().hex)
        target = self.failed_directory / f"{record_id}.json"
        temporary = self.failed_directory / f"{record_id}.{uuid.uuid4().hex}.tmp"
        with self._lock:
            if target.exists():
                if target.read_bytes() != encoded:
                    raise FileExistsError(f"collector failed record already exists: {target.name}")
            else:
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                self._fsync_path(self.failed_directory)
            self._failed_error = f"oversized collector result quarantined: {target.name}"
            self.release_reservation(token)
        return target

    def failed_records(self) -> list[dict[str, Any]]:
        records = []
        for path in sorted(self.failed_directory.glob("*.json")):
            with path.open("rb") as stream:
                value = json.load(stream)
            if not isinstance(value, dict):
                raise ValueError(f"collector failed record has invalid shape: {path.name}")
            records.append(value)
        return records

    @property
    def blocked_error(self) -> str | None:
        return self._failed_error

    def pending(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                with path.open("rb") as stream:
                    row = json.load(stream)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"collector spool record is corrupt: {path.name}") from exc
            if not isinstance(row, dict) or not row.get("spool_id"):
                raise ValueError(f"collector spool record has invalid shape: {path.name}")
            records.append(row)
        return records

    def acknowledge(self, record_id: str) -> None:
        with self._lock:
            (self.directory / f"{record_id}.json").unlink(missing_ok=True)
            self._fsync_directory()

    def _size(self) -> int:
        return sum(path.stat().st_size for path in self.directory.glob("*.json") if path.is_file())

    def _fsync_directory(self) -> None:
        self._fsync_path(self.directory)

    @staticmethod
    def _fsync_path(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class SpoolFullError(RuntimeError):
    pass


class SpoolRecordTooLarge(SpoolFullError):
    """A single immutable collection result cannot fit the configured spool."""
