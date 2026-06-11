from __future__ import annotations

from packages.common.pipeline_schema import EDITOR_PLUGIN_SCHEMA_SQL
from packages.maintenance import PostgresMaintenanceRepository


def test_editor_plugin_feed_limits_each_source_before_union() -> None:
    assert "v_source_candidate_limit := GREATEST(p_limit, 40);" in EDITOR_PLUGIN_SCHEMA_SQL
    assert EDITOR_PLUGIN_SCHEMA_SQL.count("LIMIT $9") >= 5
    assert "v_source_candidate_limit;" in EDITOR_PLUGIN_SCHEMA_SQL


def test_editor_plugin_state_no_longer_reads_receipts() -> None:
    state_sql = EDITOR_PLUGIN_SCHEMA_SQL.split("CREATE OR REPLACE FUNCTION editor_plugin_state", 1)[1].split(
        "CREATE OR REPLACE FUNCTION editor_plugin_mark_seen", 1
    )[0]
    assert "FROM editor_plugin_feedbacks" in state_sql
    assert "FROM editor_plugin_receipts" not in state_sql


def test_editor_plugin_mark_seen_is_compatibility_noop() -> None:
    mark_seen_sql = EDITOR_PLUGIN_SCHEMA_SQL.split("CREATE OR REPLACE FUNCTION editor_plugin_mark_seen", 1)[1].split(
        "CREATE OR REPLACE FUNCTION editor_plugin_submit_feedback", 1
    )[0]
    assert "INSERT INTO editor_plugin_receipts" not in mark_seen_sql
    assert "'recorded', false" in mark_seen_sql


class FakeRow(dict):
    def __getitem__(self, key):
        return super().__getitem__(key)


class FakeConnection:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.rolled_back = False
        self.committed = False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        normalized = " ".join(sql.split())
        if "to_regclass" in normalized:
            return FakeCursor([FakeRow(table_oid="public.fake")])
        if "information_schema.columns" in normalized:
            columns = params[1] if params else []
            return FakeCursor([FakeRow(column_name=column) for column in columns])
        if normalized.startswith("SELECT count(*) AS count"):
            return FakeCursor([FakeRow(count=2)])
        raise AssertionError(f"unexpected SQL in dry-run: {sql}")

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class FakeMaintenanceRepository(PostgresMaintenanceRepository):
    def __init__(self) -> None:
        self.conn = FakeConnection()

    def _connect(self):
        return self.conn


def test_maintenance_cleanup_dry_run_counts_without_mutating() -> None:
    repo = FakeMaintenanceRepository()

    result = repo.cleanup(dry_run=True)

    assert result.dry_run is True
    assert result.deleted["editor_plugin_receipts"] == 2
    assert result.cleared["tasks_payloads"] == 2
    assert repo.conn.rolled_back is True
    assert repo.conn.committed is False
    assert not any(sql.lstrip().upper().startswith(("DELETE", "UPDATE")) for sql in repo.conn.sql)
