from __future__ import annotations

from pathlib import Path

from packages.common.config import load_x_processing_settings
from packages.x_processing.models import PROMPT_KEY_BY_NEWS_TYPE
from packages.x_processing.repository import PROMPT_SEEDS


def test_load_x_processing_settings_uses_search_ai_review_overrides(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.setenv("X_PROCESS_OPENAI_BASE_URL", "https://relay.example/v1")
    monkeypatch.setenv("X_PROCESS_OPENAI_API_STYLE", "chat_completions")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
    monkeypatch.setenv("SEARCH_AI_REVIEW_MODEL", "deepseek-chat")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OPENAI_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OPENAI_API_STYLE", "chat_completions")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OMIT_REASONING_EFFORT", "true")
    monkeypatch.setenv("SEARCH_AI_REVIEW_CHAT_RESPONSE_FORMAT_MODE", "json_object")
    monkeypatch.setenv("SEARCH_AI_REVIEW_APPEND_JSON_SCHEMA_TO_PROMPT", "true")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-key")

    settings = load_x_processing_settings()

    assert settings.search_ai_review_model == "deepseek-chat"
    assert str(settings.search_ai_review_openai_base_url) == "https://api.deepseek.com/"
    assert settings.search_ai_review_openai_api_style == "chat_completions"
    assert settings.search_ai_review_openai_api_key == "deepseek-key"
    assert settings.search_ai_review_omit_reasoning_effort is True
    assert settings.search_ai_review_chat_response_format_mode == "json_object"
    assert settings.search_ai_review_append_json_schema_to_prompt is True


def test_ai_source_uses_dedicated_writer_prompt_seed() -> None:
    assert PROMPT_KEY_BY_NEWS_TYPE["ai_source"] == "ai_source_writer"
    assert "ai_source_writer" in PROMPT_SEEDS
    _display_name, relative_path, _note = PROMPT_SEEDS["ai_source_writer"]
    assert Path(relative_path).name == "AI信源快讯模板.txt"
