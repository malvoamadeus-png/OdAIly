from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from packages.whale_watch.hyperliquid_worker import WhaleWatchHyperliquidWorker


def test_long_interruption_reseeds_without_replaying_fills(monkeypatch):
    now = datetime(2026, 11, 22, tzinfo=UTC)
    monkeypatch.setattr("packages.whale_watch.hyperliquid_worker._utc_now", lambda: now)
    monkeypatch.setattr("packages.whale_watch.hyperliquid_worker.detect_hyperliquid_activity", lambda **_: None)
    repository = Mock()
    repository.get_state.return_value = SimpleNamespace(
        seeded_at=now - timedelta(days=12), last_success_at=now - timedelta(days=11),
        last_seen_time=1, aggregate_window_entries=(), aggregate_alert_active=False,
    )
    worker = WhaleWatchHyperliquidWorker.__new__(WhaleWatchHyperliquidWorker)
    worker.repository = repository
    worker.client = SimpleNamespace(user_fills=lambda _: [{"time": 100}])
    whale = SimpleNamespace(id=1, address="test", address_lower="test")
    runtime = SimpleNamespace(aggregate_window_seconds=600, aggregate_min_notional_usd=Decimal("100"))

    result = worker._process_address(whale=whale, runtime=runtime)

    assert result["seeded"] is True
    repository.mark_seeded.assert_called_once()
    assert repository.mark_seeded.call_args.kwargs["last_seen_time"] == int(now.timestamp() * 1000)
    repository.save_activity.assert_not_called()

    repository.reset_mock()
    repository.get_state.return_value.last_success_at = now - timedelta(hours=1)
    result = worker._process_address(whale=whale, runtime=runtime)
    assert result["seeded"] is False
    repository.mark_seeded.assert_not_called()
    repository.record_success.assert_called_once()
