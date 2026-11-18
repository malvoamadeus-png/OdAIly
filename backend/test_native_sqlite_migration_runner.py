from __future__ import annotations

import json
import types
import sys
from pathlib import Path

import pytest

from scripts import run_native_sqlite_migration as runner


def test_runner_requires_manual_freeze_confirmation(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="confirm-frozen"):
        runner.run(report_dir=tmp_path / "reports", confirm_frozen=False, paths={"main": tmp_path / "main.sqlite"}, selected=("main",))


def test_report_directory_must_be_under_data(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path / "data")
    with pytest.raises(ValueError, match="under /data"):
        runner._report_path(tmp_path / "reports", "main")


def test_capacity_priority_starts_with_largest_observability_sources() -> None:
    assert runner.SOURCE_ORDER[:2] == ("system_information", "small_evidence")
    assert len(runner.SOURCE_ORDER) == 21
    assert len(set(runner.SOURCE_ORDER)) == 21


def test_runner_rejects_duplicate_physical_source_paths_before_initialization(tmp_path, monkeypatch) -> None:
    initialized = []
    fake_importer = types.ModuleType("packages.common.native_sqlite_import")
    fake_importer.initialize_native_schemas = lambda: initialized.append(True)
    fake_importer.preflight_native_sqlite_source = lambda **kwargs: {"ready_for_import": True, "unknown_tables": []}
    fake_importer.import_native_sqlite_source = lambda **kwargs: {}
    monkeypatch.setitem(sys.modules, "packages.common.native_sqlite_import", fake_importer)
    source = tmp_path / "same.sqlite"
    source.touch()
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path / "data")
    monkeypatch.delenv("ODAILY_SQLITE_TMPDIR", raising=False)
    with pytest.raises(ValueError, match="selected more than once"):
        runner.run(report_dir=tmp_path / "data" / "reports", confirm_frozen=True,
                   paths={"system_information": source, "small_evidence": source},
                   selected=("system_information", "small_evidence"))
    assert initialized == []


def test_runner_restores_sqlite_temp_environment_after_completion(tmp_path, monkeypatch) -> None:
    fake_importer = types.ModuleType("packages.common.native_sqlite_import")
    fake_importer.initialize_native_schemas = lambda: None
    fake_importer.preflight_native_sqlite_source = lambda **kwargs: {"ready_for_import": True, "unknown_tables": []}
    fake_importer.import_native_sqlite_source = lambda **kwargs: {}
    monkeypatch.setitem(sys.modules, "packages.common.native_sqlite_import", fake_importer)
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path / "data")
    monkeypatch.delenv("ODAILY_SQLITE_TMPDIR", raising=False)
    source = tmp_path / "source.sqlite"
    source.touch()
    runner.run(report_dir=tmp_path / "data" / "reports", confirm_frozen=True,
               paths={"system_information": source}, selected=("system_information",))
    assert "ODAILY_SQLITE_TMPDIR" not in __import__("os").environ


def test_runner_stops_at_first_failed_source_and_keeps_retry_report(tmp_path, monkeypatch) -> None:
    imported = []

    def initialize_native_schemas():
        return {"initialized": ["main"]}

    def preflight_native_sqlite_source(**kwargs):
        return {"ready_for_import": True, "unknown_tables": []}

    def import_source(*, source_key, source_path):
        imported.append(source_key)
        if source_key == "business_news":
            raise RuntimeError("synthetic import failure")
        return {"source_key": source_key, "tables": [], "table_count": 0}

    fake_importer = types.ModuleType("packages.common.native_sqlite_import")
    fake_importer.initialize_native_schemas = initialize_native_schemas
    fake_importer.preflight_native_sqlite_source = preflight_native_sqlite_source
    fake_importer.import_native_sqlite_source = import_source
    monkeypatch.setitem(sys.modules, "packages.common.native_sqlite_import", fake_importer)
    paths = {key: tmp_path / f"{key}.sqlite" for key in ("main", "business_news", "gate_market")}
    for path in paths.values():
        path.touch()
    reports = tmp_path / "data" / "reports"
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path / "data")
    with pytest.raises(RuntimeError, match="synthetic import failure"):
        runner.run(report_dir=reports, confirm_frozen=True, paths=paths, selected=("main", "business_news", "gate_market"))

    assert imported == ["main", "business_news"]
    run_report = json.loads((reports / "run.json").read_text())
    failed_report = json.loads((reports / "business_news.json").read_text())
    assert run_report["status"] == "failed"
    assert run_report["failed_source"] == "business_news"
    assert run_report["search_postings"] == "ignored"
    assert failed_report["retryable"] is True
    assert not (reports / "gate_market.json").exists()


def test_completed_source_report_marks_owner_review_without_deleting_source(tmp_path, monkeypatch) -> None:
    fake_importer = types.ModuleType("packages.common.native_sqlite_import")
    fake_importer.initialize_native_schemas = lambda: {"initialized": ["system_information"]}
    fake_importer.preflight_native_sqlite_source = lambda **kwargs: {"ready_for_import": True, "unknown_tables": []}
    fake_importer.import_native_sqlite_source = lambda **kwargs: {"source_key": "system_information", "tables": [], "table_count": 0}
    monkeypatch.setitem(sys.modules, "packages.common.native_sqlite_import", fake_importer)
    monkeypatch.setattr(runner, "DATA_ROOT", tmp_path / "data")
    monkeypatch.delenv("ODAILY_SQLITE_TMPDIR", raising=False)
    source = tmp_path / "system_information.sqlite"
    source.touch()
    report = runner.run(report_dir=tmp_path / "data" / "reports", confirm_frozen=True,
                        paths={"system_information": source}, selected=("system_information",))
    assert report["sources"][0]["source_file_group_release"]["eligible_for_owner_review"] is True
    assert report["sources"][0]["source_file_group_release"]["automatic_deletion"] is False
    assert source.exists()
