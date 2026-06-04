from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.editor_plugin_api import (
    AuthenticatedEditor,
    EditorPluginRequestModel,
    EditorPluginNewsGenService,
    EditorPluginUnauthorizedError,
    QUICK_GENERATE_WRITER_MODEL,
    format_validation_error,
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
            )
        }

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        return self.prompts[template_key]


class FakeLogRepository:
    def __init__(self) -> None:
        self.logs: list[dict] = []

    def insert_generation_log(self, payload) -> None:
        self.logs.append(payload.__dict__)


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
    service.auth_repository = FakeLogRepository()
    return service


def editor_actor() -> AuthenticatedEditor:
    return AuthenticatedEditor(user_id="user-1", email="editor@example.com", display_name="Editor")


def editor_request() -> EditorPluginRequestModel:
    return EditorPluginRequestModel.model_validate(
        {
            "source_type": "x_post",
            "platform": "x",
            "post_text": "OpenAI 发布新的 AI 产品更新。",
            "post_url": "https://x.com/openai/status/1",
            "post_id": "1",
            "author_display_name": "OpenAI",
            "author_handle": "@OpenAI",
        }
    )


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


def test_editor_plugin_generate_uses_configured_writer_model_and_reasoning() -> None:
    ai = FakeAiClient(['{"route":"regular","discard_type":"none"}', "标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai, writer_reasoning_effort="medium")

    result = service.run_generate(editor_actor(), editor_request())

    assert result["kind"] == "generate"
    assert result["route"] == "regular"
    assert ai.calls[0]["model"] == "gpt-5.4-mini"
    assert ai.calls[1]["model"] == "gpt-5.5"
    assert ai.calls[1]["reasoning_effort"] == "medium"
    assert service.auth_repository.logs[0]["action"] == "generate"


def test_editor_plugin_quick_generate_uses_gpt_5_4_mini_for_writer_only() -> None:
    ai = FakeAiClient(['{"route":"regular","discard_type":"none"}', "标题\n\n正文"])
    service = editor_plugin_service(ai_client=ai, writer_reasoning_effort="medium")

    result = service.run_quick_generate(editor_actor(), editor_request())

    assert result["kind"] == "generate"
    assert result["route"] == "regular"
    assert ai.calls[0]["model"] == "gpt-5.4-mini"
    assert ai.calls[1]["model"] == QUICK_GENERATE_WRITER_MODEL
    assert QUICK_GENERATE_WRITER_MODEL == "gpt-5.4-mini"
    assert ai.calls[1]["reasoning_effort"] == "medium"
    assert service.auth_repository.logs[0]["action"] == "quick_generate"
