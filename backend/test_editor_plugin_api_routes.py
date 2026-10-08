from __future__ import annotations

import json
from http import HTTPStatus
from http.client import HTTPConnection
from threading import Thread
from types import SimpleNamespace

from packages.editor_plugin_api import (
    AuthenticatedEditor,
    EditorPluginApiError,
    EditorPluginApiServer,
    EditorPluginForbiddenError,
    EditorPluginNewsGenService,
    EditorPluginRequestModel,
)
from packages.x_processing.worker import build_writer_prompt
from packages.x_processing.models import PromptTemplateVersion
from packages.x_capture.client import FXTwitterClient


class UnusedService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"removed route unexpectedly accessed service method: {name}")


class _AuthenticatedEditor:
    def __init__(self, actor: AuthenticatedEditor) -> None:
        self.actor = actor

    def authenticate(self, _authorization_header: str | None) -> AuthenticatedEditor:
        return self.actor


def test_plugin_generation_uses_context_chain_without_replacing_edited_current_text() -> None:
    service = object.__new__(EditorPluginNewsGenService)
    service.x_capture_repository = SimpleNamespace(resolve_effective_author_name=lambda **_kwargs: "链上分析师Ai姨")
    fetched = []
    client = FXTwitterClient()

    def fetch(username: str, tweet_id: str) -> dict:
        fetched.append((username, tweet_id))
        if tweet_id == "2108221765877133461":
            return {"id": tweet_id, "replying_to_status": "previous", "replying_to": "ai_9684xtpa"}
        return {"id": tweet_id, "text": "麻吉再次减仓 ETH 多单", "author": {"screen_name": "ai_9684xtpa"}}

    client.fetch_detail = fetch
    service.x_context_client = client
    request = EditorPluginRequestModel(
        post_text="编辑后的最新事实：仅剩 1 万枚，亏损 262.8 万美元",
        post_url="https://x.com/ai_9684xtpa/status/2108221765877133461",
        post_id="2108221765877133461",
        author_handle="@ai_9684xtpa",
    )

    task = service._build_task_record(request, route="onchain")
    prompt = build_writer_prompt(
        task=task,
        prompt=PromptTemplateVersion(id=1, template_key="x_onchain_writer", version_number=1, content="只写事实"),
    )

    assert fetched == [("ai_9684xtpa", "2108221765877133461"), ("ai_9684xtpa", "previous")]
    assert task.content == request.post_text
    assert prompt.index("编辑后的最新事实") < prompt.index("麻吉再次减仓")


def test_plugin_manual_text_without_post_url_does_not_fetch_quotes() -> None:
    service = object.__new__(EditorPluginNewsGenService)
    service.x_capture_repository = SimpleNamespace(resolve_effective_author_name=lambda **_kwargs: None)
    service.x_context_client = SimpleNamespace(fetch_detail=lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected fetch")))

    task = service._build_task_record(EditorPluginRequestModel(post_text="手动输入正文"), route="regular")

    assert task.content == "手动输入正文"
    assert "context_chain" not in task.metadata


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


class _AutoNewsflashReadOnlyService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def authenticate_console_admin(self, _authorization_header: str | None) -> AuthenticatedEditor:
        return AuthenticatedEditor(user_id="operator", email="operator@example.com", display_name="operator")

    def get_auto_newsflash_dashboard(self, _actor: AuthenticatedEditor) -> dict[str, object]:
        self.calls.append(("dashboard", None))
        return {"events": []}

    def get_auto_newsflash_event(self, _actor: AuthenticatedEditor, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append(("event", payload.get("event_id")))
        if not payload.get("event_id"):
            raise EditorPluginApiError("event_id 不能为空")
        return {"event_id": payload["event_id"]}

    def get_auto_newsflash_prompts(self, _actor: AuthenticatedEditor) -> list[dict[str, object]]:
        self.calls.append(("prompts", None))
        return [{"prompt_key": "topic_judgment", "immutable": True}]

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"unexpected auto-newsflash mutation or route: {name}")


class _AutoNewsflashDismissService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def __init__(self, *, allowed: bool) -> None:
        self.allowed = allowed
        self.dismissed: set[str] = set()

    def authenticate_console_admin(self, _authorization_header: str | None) -> AuthenticatedEditor:
        if not self.allowed:
            raise EditorPluginForbiddenError("当前账号不是控制台管理员")
        return AuthenticatedEditor(user_id="operator", email="operator@example.com", display_name="operator")

    def dismiss_auto_newsflash_event(
        self, _actor: AuthenticatedEditor, payload: dict[str, object]
    ) -> dict[str, object]:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise EditorPluginApiError("event_id 不能为空")
        if event_id == "event:missing":
            error = EditorPluginApiError("自动快讯事件不存在")
            error.status_code = HTTPStatus.NOT_FOUND
            raise error
        already_dismissed = event_id in self.dismissed
        self.dismissed.add(event_id)
        return {
            "eventId": event_id,
            "dismissed": True,
            "alreadyDismissed": already_dismissed,
            "dismissedAt": "2026-09-30T00:00:00+00:00",
            "cancelledOutbox": 1 if not already_dismissed else 0,
            "cancelledTasks": 1 if not already_dismissed else 0,
        }


class _XAgentHistoryService:
    api_settings = SimpleNamespace(cors_allow_origin="*")

    def authenticate_console_admin(self, _authorization_header: str | None) -> AuthenticatedEditor:
        return AuthenticatedEditor(user_id="operator", email="operator@example.com", display_name="operator")

    def get_x_agent_market_sentiment_history(
        self, _actor: AuthenticatedEditor, payload: dict[str, object]
    ) -> dict[str, object]:
        return {"instrument_key": payload.get("instrument_key"), "items": []}


def test_x_agent_market_sentiment_history_route_is_exposed() -> None:
    service = _XAgentHistoryService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(server, "/console/x-agent/market-sentiment/history", {"instrument_key": "btc"})
        assert status == HTTPStatus.OK
        assert body == {"ok": True, "data": {"instrument_key": "btc", "items": []}}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


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


def test_auto_newsflash_routes_validate_event_id() -> None:
    service = _AutoNewsflashReadOnlyService()
    server = EditorPluginApiServer(("127.0.0.1", 0), service)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(server, "/console/auto-newsflash/dashboard", {})
        assert status == HTTPStatus.OK
        assert body == {"ok": True, "data": {"events": []}}

        status, body = _post_json(server, "/console/auto-newsflash/prompts", {})
        assert status == HTTPStatus.OK
        assert body["ok"] is True
        assert body["data"][0]["immutable"] is True

        status, body = _post_json(server, "/console/auto-newsflash/event", {"event_id": "event:giwa"})
        assert status == HTTPStatus.OK
        assert body == {"ok": True, "data": {"event_id": "event:giwa"}}

        status, body = _post_json(server, "/console/auto-newsflash/event", {})
        assert status == HTTPStatus.BAD_REQUEST
        assert body == {"ok": False, "message": "event_id 不能为空"}
        assert service.calls == [("dashboard", None), ("prompts", None), ("event", "event:giwa"), ("event", None)]
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_auto_newsflash_dismiss_route_requires_admin_and_is_idempotent() -> None:
    denied = _AutoNewsflashDismissService(allowed=False)
    server = EditorPluginApiServer(("127.0.0.1", 0), denied)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(server, "/console/auto-newsflash/dismiss", {"event_id": "event:one"})
        assert status == HTTPStatus.FORBIDDEN
        assert body == {"ok": False, "message": "当前账号不是控制台管理员"}
        assert denied.dismissed == set()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    allowed = _AutoNewsflashDismissService(allowed=True)
    server = EditorPluginApiServer(("127.0.0.1", 0), allowed)  # type: ignore[arg-type]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(server, "/console/auto-newsflash/dismiss", {})
        assert status == HTTPStatus.BAD_REQUEST
        assert body == {"ok": False, "message": "event_id 不能为空"}

        status, body = _post_json(server, "/console/auto-newsflash/dismiss", {"event_id": "event:missing"})
        assert status == HTTPStatus.NOT_FOUND
        assert body == {"ok": False, "message": "自动快讯事件不存在"}

        status, body = _post_json(server, "/console/auto-newsflash/dismiss", {"event_id": "event:one"})
        assert status == HTTPStatus.OK
        assert body["data"]["alreadyDismissed"] is False

        status, body = _post_json(server, "/console/auto-newsflash/dismiss", {"event_id": "event:one"})
        assert status == HTTPStatus.OK
        assert body["data"]["alreadyDismissed"] is True
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
