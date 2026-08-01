from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .storage import connect_sqlite


@dataclass(frozen=True, slots=True)
class SQLiteBackupManifest:
    source: str
    backup: str
    created_at: str
    size_bytes: int
    sha256: str
    integrity_check: str


def create_sqlite_backup(source: Path, destination: Path) -> SQLiteBackupManifest:
    if not source.is_file():
        raise FileNotFoundError(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with connect_sqlite(source) as source_conn:
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
    with sqlite3.connect(destination) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
    if integrity != "ok":
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"SQLite backup integrity check failed: {integrity}")
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    manifest = SQLiteBackupManifest(
        source=str(source.resolve()),
        backup=str(destination.resolve()),
        created_at=datetime.now(UTC).isoformat(),
        size_bytes=destination.stat().st_size,
        sha256=digest,
        integrity_check=integrity,
    )
    destination.with_suffix(destination.suffix + ".manifest.json").write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest
