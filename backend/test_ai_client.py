from packages.x_processing.ai_client import OpenAIResponsesClient


def test_chat_payload_can_omit_unsupported_response_format() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        api_style="chat_completions",
        timeout_seconds=1,
        max_attempts=1,
        backoff_seconds=0,
        omit_response_format=True,
    )

    payload = client._chat_completions_payload(
        model="gpt-5.6-luna",
        prompt="Return JSON.",
        text_format={
            "type": "json_schema",
            "name": "test",
            "schema": {"type": "object"},
            "strict": True,
        },
        reasoning_effort="medium",
    )

    assert "response_format" not in payload
    assert payload["reasoning_effort"] == "medium"


def test_chat_payload_can_omit_reasoning_effort() -> None:
    client = OpenAIResponsesClient(
        api_key="test-key",
        api_style="chat_completions",
        timeout_seconds=90,
        max_attempts=1,
        backoff_seconds=0,
        omit_reasoning_effort=True,
    )

    payload = client._chat_completions_payload(
        model="gpt-5.6-luna",
        prompt="Return JSON.",
        text_format=None,
        reasoning_effort="medium",
    )

    assert "reasoning_effort" not in payload
