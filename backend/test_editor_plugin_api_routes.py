from __future__ import annotations

import json
from http import HTTPStatus
from http.client import HTTPConnection
from threading import Thread
from types import SimpleNamespace

from packages.editor_plugin_api import (
    AuthenticatedEditor,
    EditorPluginApiServer,
    EditorPluginForbiddenError,
    EditorPluginNewsGenService,
)


class UnusedService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"removed route unexpectedly accessed service method: {name}")


class _AuthenticatedEditor:
    def __init__(self, actor: AuthenticatedEditor) -> None:
        self.actor = actor

    def authenticate(self, _authorization_header: str | None) -> AuthenticatedEditor:
        return self.actor


class _ConsoleAdminRepository:
    def __init__(self, allowed_email: str | None) -> None:
        self.allowed_email = allowed_email

    def get_admin(self, email: str) -> object | None:
        return object() if email == self.allowed_email else None


class _XAgentSubscriptionService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def __init__(self, *, allowed: bool) -> None:
        self.allowed = allowed
        self.subscription_payloads: list[dict[str, object]] = []

    def authenticate_console_admin(self, _authorization_header: str | None) -> AuthenticatedEditor:
        if not self.allowed:
            raise EditorPluginForbiddenError("当前账号不是控制台管理员")
        return AuthenticatedEditor(user_id="operator", email="operator@example.com", display_name="operator")

    def update_x_agent_subscriptions(self, _actor: AuthenticatedEditor, payload: dict[str, object]) -> dict[str, object]:
        self.subscription_payloads.append(payload)
        return {"items": []}


def _post_json(server: EditorPluginApiServer, path: str, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json", "Authorization": "Bearer test-session"},
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_legacy_hottopic_account_routes_are_not_exposed() -> None:
    server = EditorPluginApiServer(("127.0.0.1", 0), UnusedService())  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for path in ("/console/hottopic/accounts", "/console/hottopic/account"):
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
            connection.request("POST", path, body="{}", headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == HTTPStatus.NOT_FOUND
            assert json.loads(response.read()) == {"ok": False, "message": "Not found"}
            connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_console_admin_authentication_rejects_plugin_user_without_admin_record() -> None:
    actor = AuthenticatedEditor(user_id="plugin-user", email="plugin@example.com", display_name="plugin")
    service = object.__new__(EditorPluginNewsGenService)
    service.authenticator = _AuthenticatedEditor(actor)
    service.console_auth_repository = _ConsoleAdminRepository(allowed_email="operator@example.com")

    try:
        service.authenticate_console_admin("Bearer plugin-session")
    except EditorPluginForbiddenError as exc:
        assert str(exc) == "当前账号不是控制台管理员"
    else:
        raise AssertionError("a non-admin plugin session must be rejected")

    service.console_auth_repository = _ConsoleAdminRepository(allowed_email=actor.email)
    assert service.authenticate_console_admin("Bearer operator-session") == actor


def test_x_agent_subscription_route_requires_console_admin() -> None:
    denied = _XAgentSubscriptionService(allowed=False)
    server = EditorPluginApiServer(("127.0.0.1", 0), denied)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {"screen_names": ["alice"], "patch": {"market_sentiment_enabled": True}}
    try:
        status, body = _post_json(server, "/console/x-agent/subscriptions", payload)
        assert status == HTTPStatus.FORBIDDEN
        assert body == {"ok": False, "message": "当前账号不是控制台管理员"}
        assert denied.subscription_payloads == []
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    allowed = _XAgentSubscriptionService(allowed=True)
    server = EditorPluginApiServer(("127.0.0.1", 0), allowed)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(server, "/console/x-agent/subscriptions", payload)
        assert status == HTTPStatus.OK
        assert body == {"ok": True, "data": {"items": []}}
        assert allowed.subscription_payloads == [payload]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
