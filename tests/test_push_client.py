from __future__ import annotations

from packages.publisher.push_client import PushClient


def test_push_payload_always_contains_is_publish_false(monkeypatch) -> None:
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
    assert captured["json"] == {"title": "title", "content": "content", "isPublish": False}
    assert captured["headers"]["Content-Type"] == "application/json"
