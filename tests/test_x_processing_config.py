from __future__ import annotations

from packages.common.config import load_auditor_settings, load_writer3_settings, load_x_processing_settings
from packages.editor_plugin_api import QUICK_GENERATE_WRITER_MODEL
from packages.x_processing.models import PROMPT_KEY_BY_NEWS_TYPE, PromptTemplateVersion, render_prompt_content
from packages.x_processing.repository import PROMPT_SEEDS


def test_load_x_processing_settings_uses_search_ai_review_overrides(monkeypatch) -> None:
    monkeypatch.delenv("ODAILY_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ODAILY_LLM_API_KEY", raising=False)
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


def test_litellm_alias_defaults_route_text_llm_calls(monkeypatch) -> None:
    monkeypatch.setenv("ODAILY_LLM_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.setenv("ODAILY_LLM_API_KEY", "litellm-key")
    monkeypatch.setenv("X_PROCESS_OPENAI_BASE_URL", "")
    monkeypatch.setenv("X_PROCESS_OPENAI_API_STYLE", "")
    monkeypatch.setenv("X_PROCESS_JUDGE_OPENAI_API_KEY", "")
    monkeypatch.setenv("X_PROCESS_JUDGE_OPENAI_BASE_URL", "")
    monkeypatch.setenv("X_PROCESS_JUDGE_OPENAI_API_STYLE", "")
    monkeypatch.setenv("X_PROCESS_JUDGE_MODEL", "")
    monkeypatch.setenv("X_PROCESS_WRITER_MODEL", "")
    monkeypatch.setenv("X_PROCESS_PUBLISHER_MODEL", "")
    monkeypatch.setenv("SEARCH_AI_REVIEW_MODEL", "")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OPENAI_BASE_URL", "")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OPENAI_API_STYLE", "")
    monkeypatch.setenv("SEARCH_AI_REVIEW_OPENAI_API_KEY", "")
    monkeypatch.setenv("WRITER3_OPENAI_BASE_URL", "")
    monkeypatch.setenv("WRITER3_OPENAI_API_STYLE", "")
    monkeypatch.setenv("WRITER3_ANALYSIS_MODEL", "")
    monkeypatch.setenv("WRITER3_WRITER_MODEL", "")
    monkeypatch.setenv("AUDITOR_OPENAI_BASE_URL", "")
    monkeypatch.setenv("AUDITOR_OPENAI_API_STYLE", "")
    monkeypatch.setenv("AUDITOR_OPENAI_API_KEY", "")
    monkeypatch.setenv("AUDITOR_MODEL", "")

    x_settings = load_x_processing_settings()
    writer3_settings = load_writer3_settings()
    auditor_settings = load_auditor_settings()

    assert x_settings.openai_api_key == "litellm-key"
    assert str(x_settings.openai_base_url) == "http://127.0.0.1:4000/v1"
    assert x_settings.openai_api_style == "chat_completions"
    assert x_settings.judge_model == "odaily-deepseek-review"
    assert x_settings.search_ai_review_model == "odaily-deepseek-review"
    assert x_settings.judge_openai_api_key is None
    assert x_settings.search_ai_review_openai_api_key is None
    assert x_settings.writer_model == "odaily-gpt-writer"
    assert x_settings.publisher_model == "odaily-gpt-writer"
    assert writer3_settings.analysis_model == "odaily-deepseek-fast"
    assert writer3_settings.writer_model == "odaily-gpt-writer"
    assert auditor_settings.model == "odaily-deepseek-auditor"
    assert auditor_settings.openai_api_key == "litellm-key"
    assert QUICK_GENERATE_WRITER_MODEL == "odaily-deepseek-fast"


def test_ai_source_reuses_mainstream_media_writer_prompt_seed() -> None:
    assert PROMPT_KEY_BY_NEWS_TYPE["ai_source"] == "mainstream_media_writer"
    assert "ai_source_writer" not in PROMPT_SEEDS


def test_feature_mode_text_is_prepended_only_when_enabled() -> None:
    prompt = PromptTemplateVersion(
        id=1,
        template_key="x_onchain_writer",
        version_number=1,
        content="正文模板",
        feature_mode_enabled=True,
        feature_mode_text="【标题风格】\n\n保留空行",
    )

    assert render_prompt_content(prompt) == "【标题风格】\n\n保留空行\n\n正文模板"
    assert render_prompt_content(
        PromptTemplateVersion(
            id=1,
            template_key="x_onchain_writer",
            version_number=1,
            content="正文模板",
            feature_mode_enabled=False,
            feature_mode_text="【标题风格】",
        )
    ) == "正文模板"
