from __future__ import annotations

import time
from typing import Any, Protocol

import requests


class TextGenerationClient(Protocol):
    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
    ) -> str: ...


class OpenAIResponsesClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def generate_text(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
        }
        if text_format is not None:
            payload["text"] = {"format": text_format}

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    "https://api.openai.com/v1/responses",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return extract_response_text(response.json())
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(str(last_error) if last_error else "OpenAI response failed")


def extract_response_text(payload: dict[str, Any]) -> str:
    output_text = payload.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    parts: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
    result = "\n".join(part.strip() for part in parts if part.strip()).strip()
    if not result:
        raise ValueError("OpenAI response did not contain output_text")
    return result
