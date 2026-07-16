from __future__ import annotations

import json

import pytest

from packages.common.config import CompetitorMonitorSettings
from packages.competitor_monitor.blockbeats_key_config import (
    load_blockbeats_key_config,
    record_blockbeats_key_status,
    save_blockbeats_key,
)
from packages.competitor_monitor.fetchers import BlockbeatsQuotaError
from packages.competitor_monitor.worker import CompetitorMonitorWorker


def test_load_missing_blockbeats_key_config(tmp_path):
    config = load_blockbeats_key_config(path=tmp_path / "missing.json")

    assert config.api_key == ""
    assert config.status == "unknown"
    assert config.last_error is None


def test_load_broken_blockbeats_key_config(tmp_path):
    path = tmp_path / "blockbeats_key.json"
    path.write_text("{not json", encoding="utf-8")

    config = load_blockbeats_key_config(path=path)

    assert config.api_key == ""
    assert config.status == "unknown"
    assert config.last_error == "local config is unreadable"


def test_save_new_blockbeats_key_clears_old_error(tmp_path):
    path = tmp_path / "blockbeats_key.json"
    save_blockbeats_key("old-key", path=path)
    record_blockbeats_key_status(
        "quota_exhausted",
        error="quota exhausted",
        error_payload={"message": "quota exhausted"},
        path=path,
    )

    config = save_blockbeats_key("new-key", updated_by="admin@example.com", path=path)
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert config.api_key == "new-key"
    assert config.status == "unknown"
    assert config.last_error is None
    assert config.last_error_payload is None
    assert persisted["api_key"] == "new-key"
    assert persisted["updated_by"] == "admin@example.com"


def test_worker_records_missing_key(monkeypatch, tmp_path):
    path = tmp_path / "blockbeats_key.json"
    monkeypatch.setenv("BLOCKBEATS_KEY_CONFIG_PATH", str(path))
    worker = CompetitorMonitorWorker.__new__(CompetitorMonitorWorker)
    worker.settings = CompetitorMonitorSettings(blockbeats_api_key=None)

    with pytest.raises(RuntimeError, match="Missing BLOCKBEATS_API_KEY"):
        worker._fetch_blockbeats_items()

    config = load_blockbeats_key_config(path=path)
    assert config.status == "missing_key"
    assert config.last_error == "Missing BLOCKBEATS_API_KEY"


def test_worker_records_success(monkeypatch, tmp_path):
    path = tmp_path / "blockbeats_key.json"
    monkeypatch.setenv("BLOCKBEATS_KEY_CONFIG_PATH", str(path))
    save_blockbeats_key("local-key", path=path)
    worker = CompetitorMonitorWorker.__new__(CompetitorMonitorWorker)
    worker.settings = CompetitorMonitorSettings(blockbeats_api_key=None)

    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_blockbeats", lambda **kwargs: [])

    assert worker._fetch_blockbeats_items() == []
    config = load_blockbeats_key_config(path=path)
    assert config.status == "ok"
    assert config.last_success_at is not None
    assert config.last_error is None


def test_worker_records_quota_error(monkeypatch, tmp_path):
    path = tmp_path / "blockbeats_key.json"
    monkeypatch.setenv("BLOCKBEATS_KEY_CONFIG_PATH", str(path))
    save_blockbeats_key("local-key", path=path)
    worker = CompetitorMonitorWorker.__new__(CompetitorMonitorWorker)
    worker.settings = CompetitorMonitorSettings(blockbeats_api_key=None)

    def fail_quota(**kwargs):
        raise BlockbeatsQuotaError("quota exhausted", payload={"message": "quota exhausted"})

    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_blockbeats", fail_quota)

    with pytest.raises(BlockbeatsQuotaError):
        worker._fetch_blockbeats_items()

    config = load_blockbeats_key_config(path=path)
    assert config.status == "quota_exhausted"
    assert config.last_quota_error_at is not None
    assert config.last_error == "quota exhausted"
    assert config.last_error_payload == {"message": "quota exhausted"}


def test_worker_records_request_failure(monkeypatch, tmp_path):
    path = tmp_path / "blockbeats_key.json"
    monkeypatch.setenv("BLOCKBEATS_KEY_CONFIG_PATH", str(path))
    save_blockbeats_key("local-key", path=path)
    worker = CompetitorMonitorWorker.__new__(CompetitorMonitorWorker)
    worker.settings = CompetitorMonitorSettings(blockbeats_api_key=None)

    def fail_request(**kwargs):
        raise RuntimeError("upstream unavailable")

    monkeypatch.setattr("packages.competitor_monitor.worker.fetch_blockbeats", fail_request)

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        worker._fetch_blockbeats_items()

    config = load_blockbeats_key_config(path=path)
    assert config.status == "request_failed"
    assert config.last_error == "upstream unavailable"
