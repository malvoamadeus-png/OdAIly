from __future__ import annotations

import json
from typing import Any

import requests
from packages.common.config import load_auditor_settings, load_writer3_settings, load_x_processing_settings
from packages.competitor_monitor.fetchers import extract_blockbeats_original_link, fetch_blockbeats
from packages.editor_plugin_api import QUICK_GENERATE_WRITER_MODEL
from packages.x_processing.models import PROMPT_KEY_BY_NEWS_TYPE, PromptTemplateVersion, TaskRecord, render_prompt_content
from packages.x_processing.repository import PROMPT_SEEDS
from packages.x_processing.worker import should_omit_publish_source_url


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
    monkeypatch.setenv("WRITER3_ENABLED", "false")
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
    assert writer3_settings.enabled is False
    assert writer3_settings.analysis_model == "odaily-deepseek-fast"
    assert writer3_settings.writer_model == "odaily-gpt-writer"
    assert auditor_settings.model == "odaily-deepseek-auditor"
    assert auditor_settings.openai_api_key == "litellm-key"
    assert QUICK_GENERATE_WRITER_MODEL == "odaily-deepseek-fast"


def test_writer3_can_be_enabled_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("WRITER3_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    settings = load_writer3_settings()

    assert settings.enabled is True


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


def test_extract_blockbeats_original_link_prefers_external_source_url() -> None:
    assert (
        extract_blockbeats_original_link(
            {
                "url": "https://www.theblockbeats.info/flash/123",
                "sourceUrl": "https://x.com/coinbureau/status/2078126324896629220",
            }
        )
        == "https://x.com/coinbureau/status/2078126324896629220"
    )


def test_extract_blockbeats_original_link_ignores_blockbeats_site_url() -> None:
    assert extract_blockbeats_original_link({"url": "https://www.theblockbeats.info/flash/123"}) is None


def test_extract_blockbeats_original_link_uses_url_when_link_is_blockbeats_site() -> None:
    assert (
        extract_blockbeats_original_link(
            {
                "link": "https://m.theblockbeats.info/flash/330276",
                "url": "https://x.com/DeribitOfficial/status/2016799729062154411",
            }
        )
        == "https://x.com/DeribitOfficial/status/2016799729062154411"
    )


def test_fetch_blockbeats_saves_external_original_link(monkeypatch) -> None:
    response = requests.Response()
    response.status_code = 200
    response.headers["content-type"] = "application/json"
    response._content = json.dumps(
        {
            "data": {
                "list": [
                    {
                        "id": 123,
                        "title": "美众议院行政委员会主席：CLARITY Act下周有望在参议院通过",
                        "content": "BlockBeats 消息，CLARITY Act 下周有望在参议院通过。",
                        "url": "https://www.theblockbeats.info/flash/123",
                        "sourceUrl": "https://x.com/coinbureau/status/2078126324896629220",
                    }
                ]
            }
        }
    ).encode("utf-8")

    def fake_get(*args: Any, **kwargs: Any) -> requests.Response:
        return response

    monkeypatch.setattr("packages.competitor_monitor.fetchers.requests.get", fake_get)

    items = fetch_blockbeats(api_key="test-key", timeout_seconds=1)

    assert len(items) == 1
    assert items[0].source_url == "https://x.com/coinbureau/status/2078126324896629220"


def _blockbeats_task(source_url: str | None) -> TaskRecord:
    return TaskRecord(
        id=1,
        source="blockbeats",
        source_item_id="bb-1",
        source_url=source_url,
        title="CLARITY Act下周有望在参议院通过",
        content="美众议院行政委员会主席表示，CLARITY Act 下周有望在参议院通过。",
    )


def test_blockbeats_external_original_link_is_not_hidden_from_publisher() -> None:
    assert should_omit_publish_source_url(_blockbeats_task("https://x.com/coinbureau/status/2078126324896629220")) is False


def test_blockbeats_site_link_is_hidden_from_publisher() -> None:
    assert should_omit_publish_source_url(_blockbeats_task("https://www.theblockbeats.info/flash/123")) is True
