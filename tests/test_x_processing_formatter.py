from __future__ import annotations

import json
from typing import Any

import requests
from packages.x_processing.ai_client import OpenAIResponsesClient
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief


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
