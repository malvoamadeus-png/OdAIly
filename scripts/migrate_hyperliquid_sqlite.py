from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path


def _tables(conn: sqlite3.Connection, schema: str = "main") -> list[str]:
    return [str(row[0]) for row in conn.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type='table' AND name GLOB 'hl_news_*' ORDER BY name"
    )]


def _count(conn: sqlite3.Connection, schema: str, table: str) -> int:
    escaped = table.replace('"', '""')
    return int(conn.execute(f'SELECT count(*) FROM {schema}."{escaped}"').fetchone()[0])


def _copy(source: Path, destination: Path) -> dict[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(destination, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        existing = [str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        if existing:
            raise RuntimeError(f"Destination is not empty: {existing[:10]}")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS origin", (str(source),))
        tables = _tables(conn, "origin")
        if not tables:
            raise RuntimeError("No hl_news_* tables found in source")
        indexes = conn.execute(
            "SELECT sql FROM origin.sqlite_master WHERE type='index' AND tbl_name GLOB 'hl_news_*' "
            "AND sql IS NOT NULL ORDER BY name"
        ).fetchall()
        conn.execute("BEGIN IMMEDIATE")
        for table in tables:
            row = conn.execute(
                "SELECT sql FROM origin.sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if row is None or not row[0]:
                raise RuntimeError(f"Missing table definition: {table}")
            conn.execute(str(row[0]))
        copied: dict[str, int] = {}
        for table in tables:
            escaped = table.replace('"', '""')
            conn.execute(f'INSERT INTO main."{escaped}" SELECT * FROM origin."{escaped}"')
            copied[table] = int(conn.execute("SELECT changes()").fetchone()[0])
        has_sequence = conn.execute(
            "SELECT 1 FROM origin.sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if has_sequence:
            for name, sequence in conn.execute(
                "SELECT name,seq FROM origin.sqlite_sequence WHERE name GLOB 'hl_news_*'"
            ):
                conn.execute("UPDATE main.sqlite_sequence SET seq=? WHERE name=?", (sequence, name))
                if conn.execute("SELECT changes()").fetchone()[0] == 0:
                    conn.execute("INSERT INTO main.sqlite_sequence(name,seq) VALUES(?,?)", (name, sequence))
        for (sql,) in indexes:
            conn.execute(str(sql))
        conn.commit()
        for table, expected in copied.items():
            actual = _count(conn, "main", table)
            if actual != expected:
                raise RuntimeError(f"Row count mismatch for {table}: copied={expected} destination={actual}")
        integrity = str(conn.execute("PRAGMA main.integrity_check").fetchone()[0])
        if integrity != "ok":
            raise RuntimeError(f"Destination integrity check failed: {integrity}")
        violations = conn.execute("PRAGMA main.foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Foreign key violations: {len(violations)}")
        return copied
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _remove_source(source: Path, destination: Path) -> dict[str, int]:
    conn = sqlite3.connect(source, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS moved", (str(destination),))
        source_tables = _tables(conn, "main")
        destination_tables = _tables(conn, "moved")
        if not source_tables or source_tables != destination_tables:
            raise RuntimeError("Source and destination table sets do not match")
        counts = {}
        for table in source_tables:
            source_count = _count(conn, "main", table)
            destination_count = _count(conn, "moved", table)
            if source_count != destination_count:
                raise RuntimeError(
                    f"Row count mismatch for {table}: source={source_count} destination={destination_count}"
                )
            counts[table] = source_count
        conn.execute("BEGIN IMMEDIATE")
        for table in sorted(source_tables, key=lambda name: (name == "hl_news_addresses", name)):
            escaped = table.replace('"', '""')
            conn.execute(f'DROP TABLE main."{escaped}"')
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Move Hyperliquid news tables between SQLite databases.")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--remove-source", action="store_true")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    if source == destination:
        parser.error("Source and destination must differ")
    if not source.is_file():
        parser.error(f"Source database does not exist: {source}")
    if args.remove_source and not args.execute:
        parser.error("--remove-source requires --execute")
    if args.remove_source:
        result = _remove_source(source, destination) if args.execute else {}
        phase = "source_removed" if args.execute else "remove_source_requires_execute"
    else:
        result = {}
        if args.execute:
            if destination.exists():
                parser.error(f"Destination already exists: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                result = _copy(source, temporary)
                os.replace(temporary, destination)
            except Exception:
                temporary.unlink(missing_ok=True)
                Path(str(temporary) + "-wal").unlink(missing_ok=True)
                Path(str(temporary) + "-shm").unlink(missing_ok=True)
                raise
        phase = "copied" if args.execute else "copy_requires_execute"
    print(json.dumps({"phase": phase, "source": str(source), "destination": str(destination),
                      "tables": result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
