from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from packages.hottopic import capture
from packages.hottopic.capture import AccountRow


def test_rate_limited_fxtwitter_response_is_structured_for_worker_backoff(monkeypatch) -> None:
    headers = Message()
    headers["Retry-After"] = "17"

    requests: list[str] = []

    def fail(*_args, **_kwargs):
        requests.append("request")
        raise HTTPError("https://api.fxtwitter.com/test", 429, "Too Many Requests", headers, BytesIO())

    monkeypatch.setattr(capture, "fetch_json", fail)
    items, latest, error = capture.scan_account(
        AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice"),
        cutoff=datetime.now(UTC),
        timeline_count=10,
        retries=2,
    )

    assert items == []
    assert latest is None
    assert requests == ["request"]
    assert error == {
        "account": "alice",
        "kind": "rate_limited",
        "error": "HTTP Error 429: Too Many Requests",
        "retry_after_seconds": 17.0,
    }


def test_not_found_account_is_not_retried_immediately(monkeypatch) -> None:
    requests: list[str] = []
    headers = Message()

    def fail(*_args, **_kwargs):
        requests.append("request")
        raise HTTPError("https://api.fxtwitter.com/alice/statuses", 404, "Not Found", headers, BytesIO())

    monkeypatch.setattr(capture, "fetch_json", fail)
    monkeypatch.setattr(capture.time, "sleep", lambda _seconds: pytest.fail("404 must not sleep and retry"))

    _items, _latest, error = capture.scan_account(
        AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice"),
        cutoff=datetime.now(UTC),
        timeline_count=10,
        retries=1,
    )

    assert len(requests) == 1
    assert error is not None and error["kind"] == "fetch_failed"
    assert not error.get("retryable")


def test_server_error_is_marked_for_scheduler_retry(monkeypatch) -> None:
    headers = Message()

    def fail(*_args, **_kwargs):
        raise HTTPError("https://api.fxtwitter.com/alice/statuses", 500, "Server Error", headers, BytesIO())

    monkeypatch.setattr(capture, "fetch_json", fail)

    _items, _latest, error = capture.scan_account(
        AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice"),
        cutoff=datetime.now(UTC),
        timeline_count=10,
        retries=0,
    )

    assert error is not None and error["retryable"] is True


def test_one_account_retry_wait_does_not_block_another(monkeypatch) -> None:
    retry_wait_started = Event()
    release_retry = Event()
    first_account_calls = 0
    headers = Message()

    def fetch(url, _params=None):
        nonlocal first_account_calls
        if "/alice/" in url:
            first_account_calls += 1
            if first_account_calls == 1:
                raise HTTPError(url, 500, "Server Error", headers, BytesIO())
        return {"results": []}

    def sleep(_seconds: float) -> None:
        retry_wait_started.set()
        assert release_retry.wait(timeout=2)

    monkeypatch.setattr(capture, "fetch_json", fetch)
    monkeypatch.setattr(capture.time, "sleep", sleep)
    cutoff = datetime.now(UTC)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            capture.scan_account,
            AccountRow("alice", "", "", 0, 0, False, "https://x.com/alice"),
            cutoff=cutoff,
            timeline_count=10,
            retries=1,
        )
        assert retry_wait_started.wait(timeout=2)
        second = capture.scan_account(
            AccountRow("bob", "", "", 0, 0, False, "https://x.com/bob"),
            cutoff=cutoff,
            timeline_count=10,
            retries=1,
        )
        release_retry.set()
        first_result = first.result(timeout=2)

    assert second[2] is None
    assert first_result[2] is None
    assert first_account_calls == 2
