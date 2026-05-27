from __future__ import annotations

from packages.publisher.push_client import PushClient


def test_push_payload_always_contains_publish_and_push_false(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            return None

    def fake_post(url, json, headers, timeout):  # noqa: ANN001
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("packages.publisher.push_client.requests.post", fake_post)
    result = PushClient(
        endpoint="http://example.test/push/data",
        timeout_seconds=3,
        max_attempts=1,
        backoff_seconds=0,
    ).push(title="title", content="content", dry_run=False)

    assert result.ok is True
    assert captured["json"] == {
        "title": "title",
        "content": "content",
        "isPublish": False,
        "isPush": False,
    }
    assert "imageUrl" not in captured["json"]
    assert captured["headers"]["Content-Type"] == "application/json"


def test_push_payload_includes_source_url_when_provided(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            return None

    def fake_post(url, json, headers, timeout):  # noqa: ANN001
        captured["json"] = json
        return Response()

    monkeypatch.setattr("packages.publisher.push_client.requests.post", fake_post)
    result = PushClient(
        endpoint="http://example.test/push/data",
        timeout_seconds=3,
        max_attempts=1,
        backoff_seconds=0,
    ).push(title="title", content="content", dry_run=False, source_url=" https://x.com/a/status/1 ")

    assert result.ok is True
    assert captured["json"]["sourceUrl"] == "https://x.com/a/status/1"
    assert "imageUrl" not in captured["json"]


def test_push_payload_accepts_custom_publish_and_push_flags(monkeypatch) -> None:
    captured: dict = {}

    class Response:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            return None

    def fake_post(url, json, headers, timeout):  # noqa: ANN001
        captured["json"] = json
        return Response()

    monkeypatch.setattr("packages.publisher.push_client.requests.post", fake_post)
    result = PushClient(
        endpoint="http://example.test/push/data",
        timeout_seconds=3,
        max_attempts=1,
        backoff_seconds=0,
    ).push(title="title", content="content", dry_run=False, is_publish=True, is_push=False)

    assert result.ok is True
    assert captured["json"]["isPublish"] is True
    assert captured["json"]["isPush"] is False
