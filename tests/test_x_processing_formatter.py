from __future__ import annotations

import json
from typing import Any

import pytest
import requests
from packages.x_processing.ai_client import OpenAIResponsesClient
from packages.x_processing.formatter import format_brief, parse_draft_output
from packages.x_processing.models import DraftBrief


@pytest.mark.parametrize("account", ["@Jason60704294", "Jason60704294"])
def test_format_brief_replaces_jason_account_in_title_and_content(account: str) -> None:
    draft = DraftBrief(
        title=f"{account} opens a BTC long position",
        content=f"{account} opened another BTC long position.",
    )

    formatted = format_brief(draft)

    assert formatted.title == '“先定10个大目标” opens a BTC long position'
    assert '“先定10个大目标” opened another BTC long position.' in formatted.content


def test_format_brief_normalizes_han_ascii_spacing_in_content() -> None:
    draft = DraftBrief(
        title="标题",
        content="Altman表示OpenAI在2026年投入1000美元支持dapp生态",
    )

    formatted = format_brief(draft)

    assert formatted.content == "Odaily星球日报讯 Altman 表示 OpenAI 在 2026 年投入 1000 美元支持 DApp 生态。"


def test_format_brief_preserves_existing_odaily_prefix_when_normalizing_content() -> None:
    draft = DraftBrief(
        title="标题",
        content="Odaily星球日报讯Altman表示将投入1000美元支持OpenAI生态",
    )

    formatted = format_brief(draft)

    assert formatted.content == "Odaily星球日报讯 Altman 表示将投入 1000 美元支持 OpenAI 生态。"


def test_format_brief_uses_common_chinese_names_for_large_foreign_companies() -> None:
    draft = DraftBrief(
        title="AAPL超越NVDA，成为全球市值最高公司",
        content="AAPL超越NVDA，Samsung相关仓位同步增加。",
    )

    formatted = format_brief(draft)

    assert formatted.title == "苹果超越英伟达，成为全球市值最高公司"
    assert formatted.content == "Odaily星球日报讯 苹果超越英伟达，三星相关仓位同步增加。"


def test_parse_draft_output_rejects_markdown_link_meta_title() -> None:
    raw_output = (
        "[**我会先读取原文链接内容，提取可核验的信息后按指定格式输出。"
        "长鑫存储DRAM全球市场份额升至5%**](https://x.com/jukan05/status/2077648404600254508)\n\n"
        "Odaily星球日报讯 Citrini 分析师 jukan 在 X 平台发文表示，"
        "Counterpoint Research 数据显示，长鑫存储 DRAM 全球市场份额已升至 5%。"
    )

    with pytest.raises(ValueError, match="forbidden link|meta text|explanatory text"):
        parse_draft_output(raw_output)


def test_parse_draft_output_rejects_explanatory_preamble() -> None:
    raw_output = (
        "我会先读取原文链接内容，提取可核验的信息后按指定格式输出。\n\n"
        "长鑫存储DRAM全球市场份额升至5%\n\n"
        "Citrini 分析师 jukan 在 X 平台发文表示，Counterpoint Research 数据显示，"
        "长鑫存储 DRAM 全球市场份额已升至 5%。"
    )

    with pytest.raises(ValueError, match="explanatory text|meta text"):
        parse_draft_output(raw_output)


def test_format_brief_rejects_prefixed_explanatory_content() -> None:
    draft = DraftBrief(
        title="长鑫存储DRAM全球市场份额升至5%",
        content="Odaily星球日报讯 我会先读取原文链接内容，提取可核验的信息后按指定格式输出。",
    )

    with pytest.raises(ValueError, match="explanatory text|meta text"):
        format_brief(draft)


def test_parse_draft_output_rejects_title_trace_label_in_content() -> None:
    raw_output = (
        "Michael Saylor\uff1a\u6bd4\u7279\u5e01\u8981\u6210\u4e3a\u5168\u7403\u8d27\u5e01\u7f51\u7edc\u9700\u8981\u4f01\u4e1a\u91c7\u7528\n\n"
        "**\u6807\u9898\uff1a\u53d1\u8a00\u4eba\u524d\u7f6e**\n"
        "Odaily\u661f\u7403\u65e5\u62a5\u8baf Michael Saylor \u8868\u793a\uff0c\u6bd4\u7279\u5e01\u8981\u6210\u4e3a\u5168\u7403\u8d27\u5e01\u7f51\u7edc\uff0c\u4f01\u4e1a\u91c7\u7528\u662f\u5fc5\u8981\u7684\u3002"
    )

    with pytest.raises(ValueError, match="structured field label"):
        parse_draft_output(raw_output)


def test_parse_draft_output_rejects_repeated_title_in_content() -> None:
    title = "Michael Saylor\uff1a\u6bd4\u7279\u5e01\u8981\u6210\u4e3a\u5168\u7403\u8d27\u5e01\u7f51\u7edc\u9700\u8981\u4f01\u4e1a\u91c7\u7528"
    raw_output = (
        f"{title}\n\n"
        f"{title}\n\n"
        "Michael Saylor \u5728 X \u5e73\u53f0\u53d1\u6587\u8868\u793a\uff0c\u4f01\u4e1a\u91c7\u7528\u662f\u5fc5\u8981\u7684\u3002"
    )

    with pytest.raises(ValueError, match="repeats title"):
        parse_draft_output(raw_output)


def _json_response(payload: dict[str, Any]) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers["content-type"] = "application/json"
    response._content = json.dumps(payload).encode("utf-8")
    return response


def test_chat_completion_retry_keeps_request_payload(monkeypatch) -> None:
    requests_seen: list[dict[str, Any]] = []
    responses = [
        _json_response({"choices": [{"message": {"content": ""}}]}),
        _json_response({"choices": [{"message": {"content": "ok"}}]}),
    ]

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)

    client = OpenAIResponsesClient(
        api_key="test-key",
        base_url="https://example.test/v1",
        api_style="chat_completions",
        timeout_seconds=1,
        max_attempts=2,
        backoff_seconds=0,
    )

    assert client.generate_text(model="gpt-test", prompt="hello") == "ok"
    assert len(requests_seen) == 2
    assert requests_seen[0] == requests_seen[1]
    assert requests_seen[1]["messages"] == [{"role": "user", "content": "hello"}]


def test_chat_json_object_mode_can_omit_reasoning_and_append_schema(monkeypatch) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return _json_response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)

    client = OpenAIResponsesClient(
        api_key="test-key",
        base_url="https://example.test/v1",
        api_style="chat_completions",
        timeout_seconds=1,
        max_attempts=1,
        backoff_seconds=0,
        omit_reasoning_effort=True,
        chat_response_format_mode="json_object",
        append_json_schema_to_prompt=True,
    )

    assert (
        client.generate_text(
            model="deepseek-chat",
            prompt="hello",
            text_format={
                "type": "json_schema",
                "name": "test_schema",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            },
            reasoning_effort="high",
        )
        == '{"ok":true}'
    )
    assert requests_seen[0]["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in requests_seen[0]
    assert "JSON Schema" in requests_seen[0]["messages"][0]["content"]
    assert '"required":["ok"]' in requests_seen[0]["messages"][0]["content"]


def test_deepseek_alias_uses_json_object_compat_by_default(monkeypatch) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return _json_response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)

    client = OpenAIResponsesClient(
        api_key="test-key",
        base_url="https://example.test/v1",
        api_style="chat_completions",
        timeout_seconds=1,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert (
        client.generate_text(
            model="odaily-deepseek-fast",
            prompt="hello",
            text_format={
                "type": "json_schema",
                "name": "test_schema",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            },
            reasoning_effort="low",
        )
        == '{"ok":true}'
    )
    assert requests_seen[0]["response_format"] == {"type": "json_object"}
    assert "reasoning_effort" not in requests_seen[0]
    assert "JSON Schema" in requests_seen[0]["messages"][0]["content"]


def test_deepseek_auditor_can_send_reasoning_effort(monkeypatch) -> None:
    requests_seen: list[dict[str, Any]] = []

    def fake_post(*args, **kwargs):
        requests_seen.append(kwargs["json"])
        return _json_response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("packages.x_processing.ai_client.requests.post", fake_post)

    client = OpenAIResponsesClient(
        api_key="test-key",
        base_url="https://example.test/v1",
        api_style="chat_completions",
        timeout_seconds=1,
        max_attempts=1,
        backoff_seconds=0,
        allow_deepseek_reasoning_effort=True,
    )

    assert (
        client.generate_text(
            model="odaily-deepseek-auditor",
            prompt="hello",
            text_format={
                "type": "json_schema",
                "name": "test_schema",
                "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
            },
            reasoning_effort="max",
        )
        == '{"ok":true}'
    )
    assert requests_seen[0]["response_format"] == {"type": "json_object"}
    assert requests_seen[0]["reasoning_effort"] == "max"
    assert "JSON Schema" in requests_seen[0]["messages"][0]["content"]
