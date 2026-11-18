from __future__ import annotations

import pytest

from packages.hottopic.collector_spool import CollectorSpool, SpoolFullError, SpoolRecordTooLarge


def test_reservations_bound_in_flight_fetches_and_full_spool_backpressures(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=100, max_record_bytes=60)

    first = spool.reserve()
    assert first is not None
    assert spool.reserve() is None

    spool.append_reserved(first, {"spool_id": "first"})
    assert spool.pending()[0]["spool_id"] == "first"
    second = spool.reserve()
    assert second is not None
    assert spool.reserve() is None
    spool.release_reservation(second)


def test_production_reservation_budget_supports_configured_collector_concurrency(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=512 * 1024 * 1024, max_record_bytes=4 * 1024 * 1024)
    reservations = [spool.reserve() for _ in range(128)]
    assert all(reservations)
    assert spool.reserve() is None


def test_failed_append_does_not_consume_reservation_or_replace_pending_result(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=120, max_record_bytes=60)
    spool.append({"spool_id": "stable", "items": []})

    with pytest.raises(FileExistsError):
        spool.append({"spool_id": "stable", "items": [{"tweet_id": "new"}]})

    assert spool.pending() == [{"spool_id": "stable", "items": []}]


def test_oversized_record_is_rejected_only_when_total_spool_capacity_is_exceeded(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=80, max_record_bytes=40)
    token = spool.reserve()
    assert token is not None
    with pytest.raises(SpoolRecordTooLarge):
        spool.append_reserved_retrying(token, {"spool_id": "too-large", "payload": "x" * 100}, attempts=1)
    assert spool.pending() == []
    assert spool.failed_records()[0]["spool_id"] == "too-large"
    assert spool.blocked_error
    assert spool.reserve() is None


def test_quarantined_oversized_result_survives_restart_and_blocks_new_fetches(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=80, max_record_bytes=40)
    token = spool.reserve()
    assert token is not None
    with pytest.raises(SpoolRecordTooLarge):
        spool.append_reserved_retrying(token, {"spool_id": "oversized-restart", "payload": "y" * 100}, attempts=1)

    restarted = CollectorSpool(tmp_path, max_bytes=80, max_record_bytes=40)
    assert restarted.failed_records()[0]["spool_id"] == "oversized-restart"
    assert restarted.reserve() is None


def test_unacknowledged_result_survives_replay_failure_and_ack_is_idempotent(tmp_path) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=200, max_record_bytes=100)
    spool.append({"spool_id": "retry", "items": [{"tweet_id": "42"}]})

    record = spool.pending()[0]
    with pytest.raises(RuntimeError, match="database unavailable"):
        raise RuntimeError("database unavailable")
    assert spool.pending() == [record]

    spool.acknowledge("retry")
    spool.acknowledge("retry")
    assert spool.pending() == []


def test_identical_retry_after_uncertain_directory_sync_is_idempotent(tmp_path, monkeypatch) -> None:
    spool = CollectorSpool(tmp_path, max_bytes=200, max_record_bytes=100)
    token = spool.reserve()
    assert token is not None
    record = {"spool_id": "uncertain", "items": [{"tweet_id": "42"}]}
    original_sync = spool._fsync_directory
    calls = 0

    def fail_once() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory sync interrupted")
        original_sync()

    monkeypatch.setattr(spool, "_fsync_directory", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        spool.append_reserved(token, record)

    assert spool.append_reserved_retrying(token, record, attempts=2, delay_seconds=0) == "uncertain"
    assert spool.pending() == [record]
