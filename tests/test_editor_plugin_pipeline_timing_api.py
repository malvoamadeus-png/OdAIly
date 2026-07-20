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
        self.runtime_rules_calls = 0
        self.known_subject_names = ["Rune", "Cobie", "Vitalik", "CZ"]

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

    def get_runtime_rules(self, actor: SimpleNamespace) -> dict[str, Any]:
        self.runtime_rules_calls += 1
        return {"schema_version": 1, "sections": []}

    def get_known_title_subjects(self, actor: SimpleNamespace) -> dict[str, Any]:
        return {"names": list(self.known_subject_names)}

    def save_known_title_subjects(self, actor: SimpleNamespace, payload: dict[str, Any]) -> dict[str, Any]:
        self.known_subject_names = list(payload["names"])
        return {"names": list(self.known_subject_names)}


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


def _get_runtime_rules(port: int, token: str | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": token} if token else {}
    conn.request("GET", "/console/runtime-rules/get", headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def _get_known_subjects(port: int, token: str | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": token} if token else {}
    conn.request("GET", "/console/known-title-subjects/get", headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, payload


def _save_known_subjects(port: int, names: list[str], token: str | None = None) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    conn.request(
        "POST",
        "/console/known-title-subjects/save",
        body=json.dumps({"names": names}),
        headers=headers,
    )
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


def test_runtime_rules_get_requires_admin_and_returns_versioned_payload() -> None:
    service = FakePipelineTimingService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        unauthorized_status, _ = _get_runtime_rules(server.server_port)
        forbidden_status, _ = _get_runtime_rules(server.server_port, "Bearer non-admin")
        status, payload = _get_runtime_rules(server.server_port, "Bearer admin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert unauthorized_status == 401
    assert forbidden_status == 403
    assert status == 200
    assert payload["data"] == {"schema_version": 1, "sections": []}
    assert service.runtime_rules_calls == 1


def test_known_title_subjects_console_api_gets_and_saves_names() -> None:
    service = FakePipelineTimingService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        get_status, get_payload = _get_known_subjects(server.server_port, "Bearer admin")
        save_status, save_payload = _save_known_subjects(server.server_port, ["CZ", "Vitalik"], "Bearer admin")
        get_saved_status, get_saved_payload = _get_known_subjects(server.server_port, "Bearer admin")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert get_status == 200
    assert get_payload["data"]["names"] == ["Rune", "Cobie", "Vitalik", "CZ"]
    assert save_status == 200
    assert save_payload["data"]["names"] == ["CZ", "Vitalik"]
    assert get_saved_status == 200
    assert get_saved_payload["data"]["names"] == ["CZ", "Vitalik"]
