from pathlib import Path

import pytest

from packages.hyperliquid_news.dispatch import EventDispatcher


@pytest.mark.parametrize("payload", [{"observed_ms": 1}, '{"observed_ms": 1}'])
def test_postgres_dispatch_accepts_jsonb_candidate_payload(monkeypatch, payload):
    class Store:
        backend = "postgres"
        path = Path("unused")

        def metadata(self, key, default):
            return None

    dispatcher = EventDispatcher(Store())
    monkeypatch.setattr(dispatcher, "_postgres_candidates", lambda policy, keys: [
        {"event_key": "candidate", "payload": payload}
    ])
    monkeypatch.setattr("packages.hyperliquid_news.dispatch.production_allowed", lambda event, policy: True)
    seen = []
    monkeypatch.setattr(dispatcher, "_create_task", lambda event: (
        seen.append(event) or {"status": "no_change_evidence"}
    ))

    assert dispatcher.dispatch()[0]["status"] == "no_change_evidence"
    assert seen == [{"observed_ms": 1}]
