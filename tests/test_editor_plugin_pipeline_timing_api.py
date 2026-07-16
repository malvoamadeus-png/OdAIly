from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace
from typing import Any

from packages.editor_plugin_api import (
    EditorPluginApiServer,
    EditorPluginForbiddenError,
    EditorPluginUnauthorizedError,
)


class FakePipelineTimingService:
    def __init__(self) -> None:
        self.api_settings = SimpleNamespace(cors_allow_origin="*")
        self.dashboard_calls = 0

    def authenticate_console_admin(self, authorization_header: str | None) -> SimpleNamespace:
        if not authorization_header:
            raise EditorPluginUnauthorizedError("missing token")
        if authorization_header == "Bearer non-admin":
            raise EditorPluginForbiddenError("not admin")
        return SimpleNamespace(email="admin@example.com")

    def get_pipeline_timing(self, actor: SimpleNamespace) -> dict[str, Any]:
        self.dashboard_calls += 1
        return {
            "generated_at": "2026-07-16T00:00:00Z",
            "windows": [{"hours": 24, "overall": {}, "by_stage": [], "by_flow": [], "status_breakdown": []}],
        }


def _post_pipeline_timing(port: int, token: str | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    conn.request("POST", "/console/pipeline-timing/get", body=json.dumps({}), headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def test_pipeline_timing_api_requires_login() -> None:
    service = FakePipelineTimingService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _post_pipeline_timing(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 401
    assert payload["ok"] is False
    assert service.dashboard_calls == 0


def test_pipeline_timing_api_requires_console_admin() -> None:
    service = FakePipelineTimingService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _post_pipeline_timing(server.server_port, "Bearer non-admin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 403
    assert payload["ok"] is False
    assert service.dashboard_calls == 0


def test_pipeline_timing_api_returns_local_snapshot_for_admin() -> None:
    service = FakePipelineTimingService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload = _post_pipeline_timing(server.server_port, "Bearer admin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status == 200
    assert payload["ok"] is True
    assert payload["data"]["windows"][0]["hours"] == 24
    assert service.dashboard_calls == 1
