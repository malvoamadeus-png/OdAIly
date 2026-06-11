from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.editor_plugin_api import (
    AuthenticatedEditor,
    EditorPluginApiError,
    EditorPluginForbiddenError,
    EditorPluginLoginRequestModel,
    EditorPluginRequestModel,
    EditorPluginNewsGenService,
    SupabaseEditorPluginAuthenticator,
    EditorPluginUnauthorizedError,
    GENERATE_WRITER_REASONING_EFFORT,
    QUICK_GENERATE_WRITER_MODEL,
    QUICK_GENERATE_WRITER_REASONING_EFFORT,
    EditorPluginApiSettings,
    format_validation_error,
    hash_plugin_token,
    load_editor_plugin_api_settings,
    parse_bearer_token,
)
from packages.common.config import XProcessingSettings
from packages.x_processing.models import PromptTemplateVersion


class FakeAiClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []

    def generate_text(self, *, model: str, prompt: str, text_format: dict | None = None, reasoning_effort: str | None = None) -> str:
        self.calls.append({"model": model, "prompt": prompt, "text_format": text_format, "reasoning_effort": reasoning_effort})
        return self.outputs.pop(0)


class FakePromptRepository:
    def __init__(self) -> None:
        self.prompts = {
            "x_regular_writer": PromptTemplateVersion(
                id=1,
                template_key="x_regular_writer",
                version_number=1,
                content="常规写作模板",
            ),
            "x_funding_writer": PromptTemplateVersion(
                id=2,
                template_key="x_funding_writer",
                version_number=1,
                content="融资写作模板",
            ),
            "x_onchain_writer": PromptTemplateVersion(
                id=3,
                template_key="x_onchain_writer",
                version_number=1,
                content="链上写作模板",
            ),
        }

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        return self.prompts[template_key]


class FakeLogRepository:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def insert_generation_log(self, payload) -> None:
        self.logs.append(payload.__dict__)


class FakeAuthRepository:
    def __init__(self) -> None:
        self.enabled_user = True
        self.sessions = {}

    def verify_supabase_password(self, email: str, password: str):
        if password != "correct":
            raise ValueError("bad password")
        return "user-1", email.strip().lower()

    def get_enabled_user(self, email: str):
        if not self.enabled_user:
            return None
        return type(
            "User",
            (),
            {
                "email": email.strip().lower(),
                "display_name": "Editor",
                "enabled": True,
            },
        )()

    def create_session(self, *, token_hash, user_id, email, display_name, expires_at):
        self.sessions[token_hash] = type(
            "Session",
            (),
            {
                "token_hash": token_hash,
                "user_id": user_id,
                "email": email,
                "display_name": display_name,
                "expires_at": expires_at,
            },
        )()

    def get_session(self, token_hash):
        return self.sessions.get(token_hash)

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)


class FakeXCaptureRepository:
    def resolve_effective_author_name(self, *, author_username: str | None, author_display_name: str | None) -> str | None:
        return author_display_name or author_username


def editor_plugin_service(*, ai_client: FakeAiClient, writer_reasoning_effort: str = "medium") -> EditorPluginNewsGenService:
    service = EditorPluginNewsGenService.__new__(EditorPluginNewsGenService)
    service.x_settings = XProcessingSettings(
        openai_api_key="test",
        dashscope_api_key="dash",
        writer_model="gpt-5.5",
        writer_reasoning_effort=writer_reasoning_effort,
    )
    service.ai_client = ai_client
    service.x_repository = FakePromptRepository()
    service.x_capture_repository = FakeXCaptureRepository()
    service.auth_repository = FakeLogRepository()
    return service


def editor_actor() -> AuthenticatedEditor:
    return AuthenticatedEditor(user_id="user-1", email="editor@example.com", display_name="Editor")


def editor_request(*, news_type: str | None = "regular") -> EditorPluginRequestModel:
    payload = {
        "source_type": "x_post",
        "platform": "x",
        "post_text": "OpenAI 发布新的 AI 产品更新。",
        "post_url": "https://x.com/openai/status/1",
        "post_id": "1",
        "author_display_name": "OpenAI",
        "author_handle": "@OpenAI",
    }
    if news_type is not None:
        payload["news_type"] = news_type
    return EditorPluginRequestModel.model_validate(payload)


def test_parse_bearer_token_accepts_valid_header() -> None:
    assert parse_bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


@pytest.mark.parametrize("value", ["", "Token abc", "Bearer   "])
def test_parse_bearer_token_rejects_invalid_header(value: str) -> None:
    with pytest.raises(EditorPluginUnauthorizedError):
        parse_bearer_token(value)


def test_editor_plugin_request_model_normalizes_payload() -> None:
    model = EditorPluginRequestModel.model_validate(
        {
            "source_type": " x_post ",
            "platform": " X ",
            "post_text": "  hello world  ",
            "author_display_name": "  Alice  ",
            "author_handle": "  @alice  ",
        }
    )
    assert model.source_type == "x_post"
    assert model.platform == "x"
    assert model.post_text == "hello world"
    assert model.author_display_name == "Alice"
    assert model.author_handle == "@alice"


def test_editor_plugin_request_model_requires_post_text() -> None:
    with pytest.raises(ValidationError):
        EditorPluginRequestModel.model_validate(
            {
                "source_type": "x_post",
                "platform": "x",
                "post_text": "   ",
            }
        )


def test_format_validation_error_returns_first_field_message() -> None:
    with pytest.raises(ValidationError) as excinfo:
        EditorPluginRequestModel.model_validate(
            {
                "source_type": "x_post",
                "platform": "x",
                "post_text": "   ",
            }
        )
    assert format_validation_error(excinfo.value) == "post_text: Value error, post_text is required"


def test_editor_plugin_api_settings_uses_generation_timeout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setenv("VITE_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("VITE_SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("EDITOR_PLUGIN_API_GENERATION_TIMEOUT_SECONDS", "150")

    settings = load_editor_plugin_api_settings()

    assert settings.generation_timeout_seconds == 150.0


def test_editor_plugin_api_settings_uses_session_ttl_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setenv("VITE_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("VITE_SUPABASE_ANON_KEY", "anon-key")
    monkeypatch.setenv("EDITOR_PLUGIN_API_SESSION_TTL_HOURS", "24")

    settings = load_editor_plugin_api_settings()

    assert settings.session_ttl_hours == 24.0


def test_plugin_login_creates_session_and_authenticates_with_token() -> None:
    repository = FakeAuthRepository()
    authenticator = SupabaseEditorPluginAuthenticator(
        settings=EditorPluginApiSettings(
            supabase_url="https://example.supabase.co",
            supabase_key="key",
            session_ttl_hours=1,
        ),
        repository=repository,
    )

    token, expires_at, actor = authenticator.login(
        EditorPluginLoginRequestModel(email=" Editor@Example.com ", password="correct")
    )

    assert actor.email == "editor@example.com"
    assert expires_at.timestamp() > 0
    assert hash_plugin_token(token) in repository.sessions
    assert authenticator.authenticate(f"Bearer {token}").email == "editor@example.com"


def test_plugin_login_rejects_disabled_user() -> None:
    repository = FakeAuthRepository()
    repository.enabled_user = False
    authenticator = SupabaseEditorPluginAuthenticator(
        settings=EditorPluginApiSettings(supabase_url="https://example.supabase.co", supabase_key="key"),
        repository=repository,
    )

    with pytest.raises(EditorPluginForbiddenError):
        authenticator.login(EditorPluginLoginRequestModel(email="editor@example.com", password="correct"))


def test_plugin_login_rejects_wrong_password() -> None:
    authenticator = SupabaseEditorPluginAuthenticator(
        settings=EditorPluginApiSettings(supabase_url="https://example.supabase.co", supabase_key="key"),
        repository=FakeAuthRepository(),
    )

    with pytest.raises(EditorPluginUnauthorizedError):
        authenticator.login(EditorPluginLoginRequestModel(email="editor@example.com", password="wrong"))


def test_editor_plugin_generate_uses_configured_writer_model_with_low_reasoning() -> None:
    ai = FakeAiClient(["标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai, writer_reasoning_effort="medium")

    result = service.run_generate(editor_actor(), editor_request(news_type="regular"))

    assert result["kind"] == "generate"
    assert result["route"] == "regular"
    assert len(ai.calls) == 1
    assert ai.calls[0]["model"] == "gpt-5.5"
    assert ai.calls[0]["reasoning_effort"] == GENERATE_WRITER_REASONING_EFFORT
    assert GENERATE_WRITER_REASONING_EFFORT == "low"
    assert service.auth_repository.logs[0]["action"] == "generate"


def test_editor_plugin_quick_generate_uses_gpt_5_4_mini_with_low_reasoning() -> None:
    ai = FakeAiClient(["标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai, writer_reasoning_effort="medium")

    result = service.run_quick_generate(editor_actor(), editor_request(news_type="regular"))

    assert result["kind"] == "generate"
    assert result["route"] == "regular"
    assert len(ai.calls) == 1
    assert ai.calls[0]["model"] == QUICK_GENERATE_WRITER_MODEL
    assert QUICK_GENERATE_WRITER_MODEL == "gpt-5.4-mini"
    assert ai.calls[0]["reasoning_effort"] == QUICK_GENERATE_WRITER_REASONING_EFFORT
    assert QUICK_GENERATE_WRITER_REASONING_EFFORT == "low"
    assert service.auth_repository.logs[0]["action"] == "quick_generate"


@pytest.mark.parametrize(
    ("news_type", "prompt_text"),
    [
        ("regular", "常规写作模板"),
        ("funding", "融资写作模板"),
        ("onchain", "链上写作模板"),
    ],
)
def test_editor_plugin_generate_uses_selected_news_type_template(news_type: str, prompt_text: str) -> None:
    ai = FakeAiClient(["标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai)

    result = service.run_generate(editor_actor(), editor_request(news_type=news_type))

    assert result["route"] == news_type
    assert prompt_text in ai.calls[0]["prompt"]


def test_editor_plugin_generate_requires_news_type() -> None:
    ai = FakeAiClient(["标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai)

    with pytest.raises(EditorPluginApiError) as excinfo:
        service.run_generate(editor_actor(), editor_request(news_type=None))

    assert "请选择生成类型" in str(excinfo.value)
    assert ai.calls == []


def test_editor_plugin_request_model_rejects_unknown_news_type() -> None:
    with pytest.raises(ValidationError):
        editor_request(news_type="market")
