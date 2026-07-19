from __future__ import annotations

from packages.common.heartbeat import HeartbeatThrottle
from packages.whale_watch.hyperliquid_worker import WhaleWatchHyperliquidWorker
from packages.whale_watch.models import HyperliquidRunResult


def test_hyperliquid_upstream_failures_record_degraded_success_heartbeat():
    heartbeats = []
    worker = WhaleWatchHyperliquidWorker.__new__(WhaleWatchHyperliquidWorker)
    worker._heartbeat = HeartbeatThrottle(
        component="whale_watch_hyperliquid",
        worker_id="test-worker",
        writer=lambda component, worker_id, status, success, error, metadata: heartbeats.append(
            {
                "component": component,
                "worker_id": worker_id,
                "status": status,
                "success": success,
                "error": error,
                "metadata": metadata,
            }
        ),
    )

    worker._record_heartbeat(
        HyperliquidRunResult(
            addresses=1,
            processed=1,
            seeded=0,
            detected=0,
            inserted=0,
            sent=0,
            suppressed=0,
            failed={
                "0x92ea19eceb7a8de0f50978a1583a5d8b018050e9": (
                    "Hyperliquid request failed url=https://api.hyperliquid.xyz/info: "
                    "500 Server Error: Internal Server Error for url: https://api.hyperliquid.xyz/info"
                )
            },
        )
    )

    assert heartbeats[-1]["status"] == "degraded"
    assert heartbeats[-1]["success"] is True
    assert heartbeats[-1]["metadata"]["degraded"] is True
    assert heartbeats[-1]["metadata"]["failed"]


def test_non_upstream_failures_record_failed_heartbeat():
    heartbeats = []
    worker = WhaleWatchHyperliquidWorker.__new__(WhaleWatchHyperliquidWorker)
    worker._heartbeat = HeartbeatThrottle(
        component="whale_watch_hyperliquid",
        worker_id="test-worker",
        writer=lambda component, worker_id, status, success, error, metadata: heartbeats.append(
            {
                "component": component,
                "worker_id": worker_id,
                "status": status,
                "success": success,
                "error": error,
                "metadata": metadata,
            }
        ),
    )

    worker._record_heartbeat(
        HyperliquidRunResult(
            addresses=1,
            processed=1,
            seeded=0,
            detected=0,
            inserted=0,
            sent=0,
            suppressed=0,
            failed={"0xabc": "Unexpected Hyperliquid userFills payload"},
        )
    )

    assert heartbeats[-1]["status"] == "failed"
    assert heartbeats[-1]["success"] is False
    assert heartbeats[-1]["metadata"]["degraded"] is False
