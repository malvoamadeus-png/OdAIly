from __future__ import annotations

import pytest
from unittest.mock import Mock, patch

from packages.x_processing.ai_client import (
    OpenAIResponsesClient,
    extract_chat_completion_text,
    extract_response_text,
    parse_sse_payload,
)


def test_extract_response_text_skips_response_items_with_null_content() -> None:
    payload = {
        "output_text": None,
        "output": [
            {"type": "reasoning", "content": None},
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "标题\n\n正文"},
                ],
            },
        ],
    }

    assert extract_response_text(payload) == "标题\n\n正文"


def test_extract_response_text_raises_when_no_text_can_be_extracted() -> None:
    payload = {
        "output_text": None,
        "output": [
            {"type": "reasoning", "content": None},
        ],
    }

    with pytest.raises(ValueError, match="did not contain output_text"):
        extract_response_text(payload)


def test_parse_sse_payload_reconstructs_chat_completion_message() -> None:
    raw_text = "\n".join(
        [
            'data: {"id":"","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[{"index":0,"delta":{"role":"assistant","content":"标"},"finish_reason":null}]}',
            'data: {"id":"","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[{"index":0,"delta":{"content":"题\\n\\n正"},"finish_reason":null}]}',
            'data: {"id":"","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[{"index":0,"delta":{"content":"文"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":6,"total_tokens":18}}',
            "data: [DONE]",
        ]
    )

    payload = parse_sse_payload(raw_text)

    assert extract_chat_completion_text(payload) == "标题\n\n正文"


def test_parse_sse_payload_prefers_final_message_payload() -> None:
    raw_text = "\n".join(
        [
            'data: {"id":"resp_x","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":0,"total_tokens":12}}',
            'data: {"id":"resp_x","object":"chat.completion","created":0,"model":"gpt-5.5","choices":[{"index":0,"message":{"role":"assistant","content":"OK"},"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":1,"total_tokens":13}}',
        ]
    )

    payload = parse_sse_payload(raw_text)

    assert extract_chat_completion_text(payload) == "OK"


def test_parse_sse_payload_raises_when_no_content_exists() -> None:
    raw_text = "\n".join(
        [
            'data: {"id":"","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":0,"total_tokens":12}}',
            "data: [DONE]",
        ]
    )

    with pytest.raises(ValueError, match="did not contain message content"):
        parse_sse_payload(raw_text)


def test_chat_completions_falls_back_to_responses_when_sse_has_no_content() -> None:
    chat_response = Mock()
    chat_response.raise_for_status.return_value = None
    chat_response.headers = {"content-type": "text/event-stream"}
    chat_response.text = (
        'data: {"id":"","object":"chat.completion.chunk","created":0,"model":"gpt-5.5","choices":[],"usage":{"prompt_tokens":12,"completion_tokens":0,"total_tokens":12}}\n\n'
        "data: [DONE]\n"
    )

    responses_response = Mock()
    responses_response.raise_for_status.return_value = None
    responses_response.headers = {"content-type": "application/json"}
    responses_response.text = '{"output_text":"标题\\n\\n正文"}'
    responses_response.json.return_value = {"output_text": "标题\n\n正文"}

    client = OpenAIResponsesClient(
        api_key="key",
        base_url="https://example.com/v1",
        api_style="chat_completions",
        timeout_seconds=10,
        max_attempts=1,
        backoff_seconds=0,
    )

    with patch("packages.x_processing.ai_client.requests.post", side_effect=[chat_response, responses_response]) as mock_post:
        text = client.generate_text(model="gpt-5.5", prompt="只回复OK", reasoning_effort="low")

    assert text == "标题\n\n正文"
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[0].args[0].endswith("/chat/completions")
    assert mock_post.call_args_list[1].args[0].endswith("/responses")
