from __future__ import annotations

import json
import time
from typing import Any, Literal, Protocol

import requests


OpenAIApiStyle = Literal["responses", "chat_completions"]


class TextGenerationClient(Protocol):
    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str: ...


class OpenAIResponsesClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        api_style: OpenAIApiStyle = "responses",
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.base_url = str(base_url).rstrip("/")
        self.api_style = api_style
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> str:
        if self.api_style == "responses":
            url = self._responses_url()
            payload = self._responses_payload(
                model=model,
                prompt=prompt,
                text_format=text_format,
                reasoning_effort=reasoning_effort,
            )
            extractor = extract_response_text
        elif self.api_style == "chat_completions":
            url = self._chat_completions_url()
            payload = self._chat_completions_payload(
                model=model,
                prompt=prompt,
                text_format=text_format,
                reasoning_effort=reasoning_effort,
            )
            extractor = extract_chat_completion_text
        else:
            raise ValueError(f"Unsupported OpenAI API style: {self.api_style}")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                payload = parse_openai_response_payload(response)
                return extractor(payload)
            except Exception as exc:
                last_error = RuntimeError(
                    build_ai_error_message(
                        exc,
                        model=model,
                        api_style=self.api_style,
                        url=url,
                        response=response if "response" in locals() else None,
                    )
                )
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(str(last_error) if last_error else "OpenAI response failed")

    def _responses_url(self) -> str:
        if self.base_url.endswith("/responses"):
            return self.base_url
        return f"{self.base_url}/responses"

    def _chat_completions_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _responses_payload(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
        }
        if text_format is not None:
            payload["text"] = {"format": text_format}
        if reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    def _chat_completions_payload(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if text_format is not None:
            payload["response_format"] = to_chat_response_format(text_format)
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        return payload


def extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        content_items = item.get("content")
        if not isinstance(content_items, list):
            continue
        for content in content_items:
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    result = "\n".join(part.strip() for part in parts if part.strip()).strip()
    if not result:
        raise ValueError("OpenAI response did not contain output_text")
    return result


def parse_openai_response_payload(response: requests.Response) -> dict[str, Any]:
    content_type = str(response.headers.get("content-type") or "").lower()
    if "text/event-stream" in content_type:
        return parse_sse_payload(response.text)
    return response.json()


def parse_sse_payload(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if not text:
        raise ValueError("OpenAI SSE response was empty")

    usage_payload: dict[str, Any] | None = None
    final_payload: dict[str, Any] | None = None
    delta_parts: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        payload = json.loads(data)
        if not isinstance(payload, dict):
            continue
        usage = payload.get("usage")
        if isinstance(usage, dict):
            usage_payload = usage
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if isinstance(content, str) and content:
                delta_parts.append(content)
        message = choice.get("message")
        if isinstance(message, dict):
            final_payload = payload

    if final_payload is not None:
        return final_payload

    content = "".join(delta_parts).strip()
    if not content:
        raise ValueError("OpenAI SSE response did not contain message content")

    payload: dict[str, Any] = {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ]
    }
    if usage_payload is not None:
        payload["usage"] = usage_payload
    return payload


def build_ai_error_message(
    exc: Exception,
    *,
    model: str,
    api_style: OpenAIApiStyle,
    url: str,
    response: requests.Response | None,
) -> str:
    details = [
        f"model={model}",
        f"api_style={api_style}",
        f"url={url}",
        f"error={exc}",
    ]
    if response is not None:
        content_type = str(response.headers.get("content-type") or "").strip() or "-"
        body_prefix = response.text[:200].replace("\n", "\\n").replace("\r", "\\r")
        details.extend(
            [
                f"status_code={response.status_code}",
                f"content_type={content_type}",
                f"body_prefix={body_prefix}",
            ]
        )
    return "OpenAI request failed: " + " ".join(details)


def to_chat_response_format(text_format: dict[str, Any]) -> dict[str, Any]:
    if text_format.get("type") != "json_schema":
        return text_format
    json_schema = {
        "name": text_format.get("name"),
        "schema": text_format.get("schema"),
        "strict": text_format.get("strict", True),
    }
    return {"type": "json_schema", "json_schema": json_schema}


def extract_chat_completion_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI chat completion response did not contain choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        result = "\n".join(part.strip() for part in parts if part.strip()).strip()
        if result:
            return result
    raise ValueError("OpenAI chat completion response did not contain message content")
