"""Opt-in business-news storage and a quiescent, verified migration."""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from packages.common.storage import connect_sqlite_readonly, load_storage_settings
from .repository import BusinessNewsRepository

TABLES = (
    "business_telegram_groups",
    "business_message_inbox",
    "business_telegram_recent_messages",
    "business_telegram_state",
)
MARKER = "odaily_business_storage"


def business_news_path() -> Path:
    """Refuse an unprepared database instead of silently starting with an empty inbox."""
    load_dotenv()
    configured = os.getenv("ODAILY_BUSINESS_NEWS_SQLITE_PATH", "").strip()
    if not configured:
        return load_storage_settings().sqlite_path
    path = Path(configured).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("Business news database is missing; migrate it before enabling the separate path")
    with connect_sqlite_readonly(path) as conn:
        try:
            marker = conn.execute(f"SELECT schema_version FROM {MARKER} WHERE singleton=1").fetchone()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as exc:
            raise RuntimeError("Business news database is not a verified migration") from exc
    if marker is None or marker[0] != 1 or not set(TABLES) <= tables:
        raise RuntimeError("Business news database is not a verified migration")
    return path


def migrate_business_news(source: Path, destination: Path, observability: Path | None) -> dict[str, int]:
    """Copy a stopped business worker's state; never overwrite the source or destination.

    The API and business worker must stay stopped from before this snapshot until
    both are configured to use the result. The caller must arrange that quiescence.
    """
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise ValueError("Source database does not exist")
    if source == destination or destination.exists():
        raise ValueError("Destination must be a new file distinct from the source")
    if not destination.parent.is_dir():
        raise ValueError("Destination parent directory does not exist")
    temp_fd, temp_name = tempfile.mkstemp(prefix=".business-news-migration-", suffix=".sqlite", dir=destination.parent)
    os.close(temp_fd)
    temporary = Path(temp_name)
    try:
        source_conn = sqlite3.connect(source, timeout=1)
        source_conn.row_factory = sqlite3.Row
        try:
            source_conn.execute("PRAGMA busy_timeout=1000")
            source_conn.execute("BEGIN IMMEDIATE")
            existing = {row[0] for row in source_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not set(TABLES) <= existing:
                raise RuntimeError("Source database is missing a business news table")
            triggers = source_conn.execute(
                "SELECT name,tbl_name FROM sqlite_master WHERE type='trigger' AND tbl_name IN (?,?,?,?)", TABLES,
            ).fetchall()
            allowed_prefixes = ("odaily_obs_business_message_inbox_", "odaily_dependency_counter_business_message_inbox_")
            if any(not str(row[0]).startswith(allowed_prefixes) for row in triggers):
                raise RuntimeError("Unknown business news trigger; migration stopped")
            inflight = source_conn.execute("SELECT COUNT(*) FROM business_message_inbox WHERE status IN ('processing','formatting','publishing')").fetchone()[0]
            if inflight:
                raise RuntimeError("Business news has in-flight messages; resolve them before migration")
            if "odaily_observability_outbox" in existing:
                if observability is None or not observability.is_file():
                    raise RuntimeError("Observability database is required to verify the projection watermark")
                with connect_sqlite_readonly(observability) as obs:
                    watermark_row = obs.execute("SELECT state_value FROM system_information_state WHERE state_key='source_audit_main_watermark'").fetchone()
                if watermark_row is None:
                    raise RuntimeError("Main observability watermark is missing")
                pending = source_conn.execute("SELECT 1 FROM odaily_observability_outbox WHERE table_name='business_message_inbox' AND seq>? LIMIT 1", (int(watermark_row[0]),)).fetchone()
                if pending:
                    raise RuntimeError("Business news observability events are not yet projected")
            BusinessNewsRepository(temporary)
            target = sqlite3.connect(temporary)
            try:
                target.execute("PRAGMA foreign_keys=ON")
                target.execute("BEGIN IMMEDIATE")
                counts: dict[str, int] = {}
                for table in TABLES:
                    source_columns = [row[1] for row in source_conn.execute(f'PRAGMA table_info("{table}")')]
                    target_columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
                    extra = set(source_columns) - set(target_columns)
                    if table == "business_message_inbox" and extra == {"odaily_external_error_counted"}:
                        target.execute('ALTER TABLE business_message_inbox ADD COLUMN odaily_external_error_counted INTEGER NOT NULL DEFAULT 0')
                        target_columns.append("odaily_external_error_counted")
                        extra.clear()
                    if extra or set(target_columns) - set(source_columns):
                        raise RuntimeError(f"Unexpected schema in {table}; migration stopped")
                    columns = ",".join('"' + name.replace('"', '""') + '"' for name in source_columns)
                    placeholders = ",".join("?" for _ in source_columns)
                    rows = source_conn.execute(f'SELECT {columns} FROM "{table}"')
                    count = 0
                    while batch := rows.fetchmany(500):
                        target.executemany(f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})', [tuple(row) for row in batch])
                        count += len(batch)
                    counts[table] = count
                if "sqlite_sequence" in existing:
                    seq = source_conn.execute("SELECT seq FROM sqlite_sequence WHERE name='business_message_inbox'").fetchone()
                    if seq is not None:
                        target.execute("DELETE FROM sqlite_sequence WHERE name='business_message_inbox'")
                        target.execute("INSERT INTO sqlite_sequence(name,seq) VALUES('business_message_inbox',?)", (seq[0],))
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise RuntimeError("Business news migration failed foreign-key validation")
                target.execute(f"CREATE TABLE {MARKER} (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL)")
                target.execute(f"INSERT INTO {MARKER} VALUES (1,1)")
                target.commit()
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Business news migration failed integrity validation")
            finally:
                target.close()
            # Capture state changes even if the worker starts before the API projector.
            from packages.system_observability.projector import SourceAuditInstaller, table_specs_for_business_path
            specs = tuple(spec for spec in table_specs_for_business_path(separate=True) if spec.database_key == "business")
            result = SourceAuditInstaller().install(temporary, specs)
            if result != {"installed": 1, "skipped": 0}:
                raise RuntimeError("Business news audit trigger installation failed")
            verify = sqlite3.connect(temporary)
            try:
                checkpoint = verify.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
                    raise RuntimeError("Business news migration could not checkpoint its WAL")
            finally:
                verify.close()
            if Path(f"{temporary}-wal").exists() and Path(f"{temporary}-wal").stat().st_size:
                raise RuntimeError("Business news migration has an uncheckpointed WAL")
            source_conn.rollback()
        finally:
            source_conn.close()
        # Hard-link publication fails rather than overwriting a concurrently created target.
        os.link(temporary, destination)
        return counts
    finally:
        temporary.unlink(missing_ok=True)
        Path(f"{temporary}-wal").unlink(missing_ok=True)
        Path(f"{temporary}-shm").unlink(missing_ok=True)


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Copy business-news state to a separate SQLite database")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--observability", type=Path, required=True)
    parser.add_argument("--writers-stopped", action="store_true", required=True,
                        help="Confirm the API and business worker are stopped and will remain stopped until cutover")
    args = parser.parse_args()
    print(json.dumps(migrate_business_news(args.source, args.destination, args.observability), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
