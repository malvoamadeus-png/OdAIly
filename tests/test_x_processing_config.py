from __future__ import annotations

import json
from typing import Any

import requests
from packages.common.config import (
    XProcessingSettings,
    load_auditor_settings,
    load_writer3_settings,
    load_x_processing_settings,
)
from packages.competitor_monitor.fetchers import extract_blockbeats_original_link, fetch_blockbeats
from packages.editor_plugin_api import QUICK_GENERATE_WRITER_MODEL
from packages.local_pipeline.processor import LocalPipelineProcessor
from packages.non_mainstream_media.fetcher import (
    JINA_REQUEST_HEADERS,
    REQUEST_HEADERS,
    fetch_html,
    request_headers_for_url,
)
from packages.non_mainstream_media.models import (
    NonMainstreamMediaSource,
    SiteDefinition,
    SourceRunStats,
)
from packages.non_mainstream_media.repository import InMemoryNonMainstreamMediaRepository
from packages.non_mainstream_media.worker import NonMainstreamMediaWorker
from packages.x_processing.models import (
    PROMPT_KEY_BY_NEWS_TYPE,
    PipelineRecord,
    PromptTemplateVersion,
    TaskRecord,
    render_prompt_content,
)
from packages.x_processing.publisher_config import build_publisher_rule_prompt, default_publisher_rule_config
from packages.x_processing.repository import PROMPT_SEEDS
from packages.x_processing.worker import XProcessingWorker, should_omit_publish_source_url


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
    monkeypatch.setenv("AUDITOR_REASONING_EFFORT", "")

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
    assert auditor_settings.model == "odaily-gpt-auditor"
    assert auditor_settings.reasoning_effort == "medium"
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


def test_default_publisher_rules_include_soft_pr_deny_rule() -> None:
    config = default_publisher_rule_config()
    rule_names = [rule.name for rule in config.regular.deny_rules]
    assert "软性商务 / 吹捧型内容" in rule_names

    task = TaskRecord(
        id=1,
        source="blockbeats",
        source_item_id="bb-soft-pr",
        source_url=None,
        title="Bitroot完成非洲社区生态轮融资",
        content="该项目称此次生态布局是全球化战略及AI公链生态落地的重要进展。",
    )
    pipeline = PipelineRecord(
        task_id=1,
        final_title="Bitroot宣布完成非洲社区生态轮融资",
        final_content="Bitroot宣布完成非洲社区生态轮融资，此次布局被视为生态落地的重要进展。",
    )

    prompt = build_publisher_rule_prompt(task=task, pipeline=pipeline, profile=config.regular)

    assert "软性商务稿、吹捧型稿件和太虚的宣传表达" in prompt
    assert "即使主体属于 Crypto/Web3，也必须 reject" in prompt


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


def test_jina_requests_do_not_reuse_browser_headers(monkeypatch) -> None:
    seen_headers: list[dict[str, str]] = []

    def fake_get(*args: Any, **kwargs: Any) -> requests.Response:
        seen_headers.append(kwargs["headers"])
        response = requests.Response()
        response.status_code = 200
        response._content = b"ok"
        return response

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.requests.get", fake_get)

    fetch_html(
        "https://r.jina.ai/http://https://example.test/article",
        timeout_seconds=1,
        max_attempts=1,
    )
    fetch_html(
        "https://example.test/article",
        timeout_seconds=1,
        max_attempts=1,
    )

    assert seen_headers == [JINA_REQUEST_HEADERS, REQUEST_HEADERS]
    assert "User-Agent" not in request_headers_for_url("https://r.jina.ai/http://example.test")


def _heartbeat_test_worker(repository: InMemoryNonMainstreamMediaRepository) -> NonMainstreamMediaWorker:
    return NonMainstreamMediaWorker(
        repository=repository,
        site_registry={
            "test": SiteDefinition(
                site_key="test",
                display_name="Test",
                homepage_url="https://example.test",
                list_url="https://example.test/feed",
                capture_method="html_request",
                pipeline_mode="write_flow",
            )
        },
        mixed_classifier=object(),  # type: ignore[arg-type]
    )


def _heartbeat_test_source(source_id: int, site_key: str) -> NonMainstreamMediaSource:
    return NonMainstreamMediaSource(
        id=source_id,
        site_key=site_key,
        display_name=site_key,
        homepage_url=f"https://{site_key}.test",
        capture_method="html_request",
    )


def test_partial_source_failure_records_degraded_success_heartbeat() -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    worker = _heartbeat_test_worker(repository)

    worker._record_heartbeat(
        stats=[
            SourceRunStats(source=_heartbeat_test_source(1, "healthy"), status="success"),
            SourceRunStats(
                source=_heartbeat_test_source(2, "failed"),
                status="fetch_failed",
                error="blocked",
                metadata={"detail_errors": {"https://failed.test/item": "403 forbidden"}},
            ),
        ],
        source_count=2,
    )

    heartbeat = repository.heartbeats[-1]
    assert heartbeat["success"] is True
    assert heartbeat["status"] == "degraded"
    assert heartbeat["error"] == "blocked"
    assert heartbeat["metadata"]["failed_sources"] == 1
    assert heartbeat["metadata"]["sites"][1]["failure_details"] == {
        "https://failed.test/item": "403 forbidden"
    }


def test_all_source_failures_still_record_failed_heartbeat() -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    worker = _heartbeat_test_worker(repository)

    worker._record_heartbeat(
        stats=[
            SourceRunStats(
                source=_heartbeat_test_source(1, "failed"),
                status="fetch_failed",
                error="blocked",
            )
        ],
        source_count=1,
    )

    assert repository.heartbeats[-1]["success"] is False
    assert repository.heartbeats[-1]["status"] == "failed"


def test_failed_pipeline_statuses_resume_at_the_failed_stage() -> None:
    processor = object.__new__(LocalPipelineProcessor)
    task = TaskRecord(
        id=1,
        source="panews",
        source_item_id="item-1",
        source_url=None,
        title="Title",
        content="Content",
        status="write_failed",
    )

    assert processor._remaining_write_flow_sequence(task) == [
        "write",
        "format_publish",
        "publish",
    ]
    assert processor._remaining_alert_sequence("domain_failed") == [
        "domain_judge",
        "search",
        "notify",
    ]
    assert processor._remaining_alert_sequence("search_failed") == ["search", "notify"]
    assert processor._remaining_alert_sequence("notify_failed") == ["notify"]


def test_writer_timeout_exceeds_litellm_request_window(monkeypatch) -> None:
    settings = XProcessingSettings()
    captured: dict[str, Any] = {}

    def fake_client(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("packages.x_processing.worker.OpenAIResponsesClient", fake_client)
    worker = object.__new__(XProcessingWorker)
    worker.settings = XProcessingSettings(openai_api_key="test-key")
    worker._build_ai_client()

    assert settings.request_timeout_seconds == 30.0
    assert settings.writer_request_timeout_seconds == 150.0
    assert captured["timeout_seconds"] == 150.0
