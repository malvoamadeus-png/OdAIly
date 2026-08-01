from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import sqlite3
from types import SimpleNamespace

from packages.auditor.sqlite_repository import SQLiteAuditorRepository
from packages.common.legacy_database_import import _encode_import_row, initialize_sqlite_schema
from packages.common.storage import connect_sqlite
from packages.console_data_api import ConsoleDataApi
from packages.editor_plugin_api import EditorPluginNewsGenService
from packages.pipeline_supervisor.sqlite_repository import SQLitePipelineSupervisorRepository
from packages.whale_watch.hyperliquid_sqlite_repository import SQLiteWhaleWatchHyperliquidRepository
from packages.whale_watch.sqlite_repository import SQLiteWhaleWatchRepository
from packages.writer3.models import OdailyReference
from packages.writer3.sqlite_repository import SQLiteWriter3Repository


def configure(monkeypatch, tmp_path):
    path = tmp_path / "odaily.sqlite"
    monkeypatch.setenv("ODAILY_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("ODAILY_STORAGE_EPOCH", "test")
    monkeypatch.setenv("ODAILY_SQLITE_PATH", str(path))
    initialize_sqlite_schema()
    return path


def test_complete_schema_and_console_mutation(monkeypatch, tmp_path):
    path = configure(monkeypatch, tmp_path)
    with connect_sqlite(path) as conn:
        names = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "auditor_checks", "writer3_contexts", "whale_watch_activities", "pipeline_alerts"} <= names

    rows = ConsoleDataApi(path).execute({
        "table": "whale_watch_addresses",
        "operation": "insert",
        "select": "*",
        "data": {"address": "0x" + "1" * 40, "address_lower": "0x" + "1" * 40, "label": "test"},
    })
    assert len(rows) == 1
    assert rows[0]["label"] == "test"


def test_writer_auditor_whale_and_supervisor_repositories(monkeypatch, tmp_path):
    path = configure(monkeypatch, tmp_path)
    published = datetime.now(UTC) - timedelta(minutes=1)
    writer = SQLiteWriter3Repository(path)
    writer.upsert_odaily_references([
        OdailyReference("ref-1", "https://example.com/1", "Title", "Body", published)
    ])
    task = writer.claim_task(worker_id="writer", start_after=published - timedelta(days=1), freshness_window_seconds=3600)
    assert task is not None
    writer.complete_skipped(task, reason="test")

    auditor = SQLiteAuditorRepository(path)
    audit_task = auditor.claim_task(worker_id="auditor", prompt_version="v1", lookback_minutes=60)
    assert audit_task is not None
    auditor.complete_passed(audit_task, model="test", prompt_version="v1", raw_output="{}", result={"has_issue": False})

    whale = SQLiteWhaleWatchRepository(path)
    assert len(whale.list_addresses(include_disabled=True)) == 0
    hyper = SQLiteWhaleWatchHyperliquidRepository(path)
    settings = hyper.get_runtime_settings(
        default_single_fill_min_notional_usd=Decimal("500000"),
        default_aggregate_min_notional_usd=Decimal("1000000"),
        default_aggregate_window_seconds=600,
    )
    assert settings.aggregate_window_seconds == 600

    supervisor = SQLitePipelineSupervisorRepository(path)
    stale = supervisor.list_stale_heartbeats(cutoff=datetime.now(UTC))
    assert any(row["component"] == "x_capture" for row in stale)
    assert supervisor.claim_alert(alert_key="test", message="test", dedup_cutoff=datetime.now(UTC) - timedelta(hours=1))
    assert not supervisor.claim_alert(alert_key="test", message="test", dedup_cutoff=datetime.now(UTC) - timedelta(hours=1))


def test_legacy_nulls_are_normalized_without_collapsing_media_rows():
    columns = ("id", "source", "title", "title_key")
    seen: set[tuple[str, str]] = set()
    first = _encode_import_row(
        "media_newsflash",
        {"id": 1, "source": "example", "title": "Same title", "title_key": None},
        columns,
        media_title_keys=seen,
    )
    duplicate = _encode_import_row(
        "media_newsflash",
        {"id": 2, "source": "example", "title": "Same title", "title_key": None},
        columns,
        media_title_keys=seen,
    )
    writer = _encode_import_row(
        "writer3_contexts",
        {"id": 3, "current_content": None},
        ("id", "current_content"),
        media_title_keys=set(),
    )

    assert first[3] == "sametitle"
    assert duplicate[3] == "sametitle::legacy:2"
    assert writer == (3, "")


def test_editor_plugin_health_has_no_removed_remote_sync_fields():
    service = EditorPluginNewsGenService.__new__(EditorPluginNewsGenService)
    service.feed_syncer = SimpleNamespace(
        status=lambda: {
            "enabled": True,
            "last_feed_sync_at": None,
            "last_error": None,
        }
    )
    service.local_store = SimpleNamespace(
        stats=lambda **_kwargs: {
            "max_age_hours": 2,
            "feed_items": {"recent": 1, "latest_occurred_at": None, "by_lane": {}},
            "feedbacks": {"pending": 0, "failed": 0},
            "sessions": {"active": 0},
        }
    )
    service.api_settings = SimpleNamespace(local_feed_max_age_hours=2)

    health = service.local_feed_health()

    assert health["ok"] is True
    assert health["syncer"] == {
        "enabled": True,
        "last_feed_sync_at": None,
        "last_error": None,
    }


def test_shared_sqlite_context_closes_connection(tmp_path):
    connection = connect_sqlite(tmp_path / "closed.sqlite")
    with connection as active:
        active.execute("CREATE TABLE sample(id integer PRIMARY KEY)")

    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError as exc:
        assert "closed" in str(exc).lower()
    else:
        raise AssertionError("SQLite connection should close after its context exits")
