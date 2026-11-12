import json
import sqlite3
import time

import pytest

from packages.common.storage import connect_sqlite


def events(capsys):
    return [json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().err.splitlines() if line.startswith("[sqlite-observe] ")]


def test_real_lock_error_is_logged_without_sql_or_values(tmp_path, capsys):
    path = tmp_path / "shared.sqlite"
    with connect_sqlite(path) as db:
        db.execute("CREATE TABLE sample (value TEXT)")
    with connect_sqlite(path) as holder, connect_sqlite(path) as waiter:
        holder.execute("BEGIN IMMEDIATE")
        waiter.execute("PRAGMA busy_timeout=5")
        with pytest.raises(sqlite3.OperationalError):
            waiter.cursor().execute("INSERT INTO sample VALUES (?)", ("secret-value",))
    output = events(capsys)
    locked = next(item for item in output if item["event"] == "locked")
    assert locked["db"] == "shared.sqlite"
    assert locked["operation"] == "INSERT"
    assert "test_real_lock_error" in locked["location"]
    assert "secret-value" not in json.dumps(output)


def test_long_transaction_and_normal_commit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("packages.common.storage._SQLITE_TRANSACTION_SECONDS", 0.01)
    with connect_sqlite(tmp_path / "db.sqlite") as db:
        db.execute("CREATE TABLE sample (value TEXT)")
        db.execute("INSERT INTO sample VALUES (?)", ("private",))
        time.sleep(0.02)
        db.commit()
    records = events(capsys)
    assert len([e for e in records if e["event"] == "long_transaction"]) == 1
    assert records[-1]["outcome"] == "commit"
    assert "private" not in json.dumps(records)
