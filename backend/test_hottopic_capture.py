from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

from packages.hottopic import capture
from packages.hottopic.capture import AccountRow


def test_rate_limited_fxtwitter_response_is_structured_for_worker_backoff(monkeypatch) -> None:
    headers = Message()
    headers["Retry-After"] = "17"

    requests: list[str] = []

    def fail(*_args, **_kwargs):
        requests.append("request")
        raise HTTPError("https://api.fxtwitter.com/test", 429, "Too Many Requests", headers, BytesIO())

    calls: list[str] = []
    rate_limits: list[float] = []
    monkeypatch.setattr(capture, "fetch_json", fail)
    items, latest, error = capture.scan_account(
        AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice"),
        cutoff=datetime.now(UTC),
        timeline_count=10,
        retries=2,
        before_request=lambda: calls.append("paced"),
        on_rate_limited=rate_limits.append,
    )

    assert items == []
    assert latest is None
    assert calls == ["paced"]
    assert requests == ["request"]
    assert rate_limits == [17.0]
    assert error == {
        "account": "alice",
        "kind": "rate_limited",
        "error": "HTTP Error 429: Too Many Requests",
        "retry_after_seconds": 17.0,
    }
