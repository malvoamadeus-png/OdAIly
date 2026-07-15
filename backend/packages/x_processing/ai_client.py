from __future__ import annotations

import json
import time
from typing import Any, Literal, Protocol

import requests


OpenAIApiStyle = Literal["responses", "chat_completions"]
ChatResponseFormatMode = Literal["json_schema", "json_object"]


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
        omit_reasoning_effort: bool = False,
        chat_response_format_mode: ChatResponseFormatMode = "json_schema",
        append_json_schema_to_prompt: bool = False,
        allow_deepseek_reasoning_effort: bool = False,
    ) -> None:
        self.api_key = api_key
        self.base_url = str(base_url).rstrip("/")
        self.api_style = api_style
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)
        self.omit_reasoning_effort = omit_reasoning_effort
        self.chat_response_format_mode = chat_response_format_mode
        self.append_json_schema_to_prompt = append_json_schema_to_prompt
        self.allow_deepseek_reasoning_effort = allow_deepseek_reasoning_effort

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
            request_payload = self._responses_payload(
                model=model,
                prompt=prompt,
                text_format=text_format,
                reasoning_effort=reasoning_effort,
            )
            extractor = extract_response_text
        elif self.api_style == "chat_completions":
            url = self._chat_completions_url()
            use_deepseek_chat_compat = uses_deepseek_chat_compat(model)
            chat_prompt = self._chat_prompt(
                prompt=prompt,
                text_format=text_format,
                force_append_json_schema=use_deepseek_chat_compat,
            )
            request_payload = self._chat_completions_payload(
                model=model,
                prompt=chat_prompt,
                text_format=text_format,
                reasoning_effort=reasoning_effort,
                force_json_object=use_deepseek_chat_compat,
                force_omit_reasoning_effort=(
                    use_deepseek_chat_compat and not self.allow_deepseek_reasoning_effort
                ),
            )
            extractor = extract_chat_completion_text
        else:
            raise ValueError(f"Unsupported OpenAI API style: {self.api_style}")

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            response: requests.Response | None = None
            try:
                response = requests.post(
                    url,
                    json=request_payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                try:
                    response_payload = parse_openai_response_payload(response)
                    return extractor(response_payload)
                except Exception as exc:
                    fallback_text = self._try_chat_responses_fallback(
                        model=model,
                        prompt=prompt,
                        text_format=text_format,
                        reasoning_effort=reasoning_effort,
                        cause=exc,
                        response=response,
                    )
                    if fallback_text is not None:
                        return fallback_text
                    raise
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
        if reasoning_effort and not self.omit_reasoning_effort:
            payload["reasoning"] = {"effort": reasoning_effort}
        return payload

    def _chat_prompt(
        self,
        *,
        prompt: str,
        text_format: dict[str, Any] | None,
        force_append_json_schema: bool = False,
    ) -> str:
        if not (self.append_json_schema_to_prompt or force_append_json_schema) or text_format is None:
            return prompt
        schema = text_format.get("schema")
        if not isinstance(schema, dict):
            return prompt
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        return (
            f"{prompt}\n\n"
            "请严格输出符合以下 JSON Schema 的 JSON 对象，不要输出 Markdown、解释文本或推理过程：\n"
            f"{schema_text}"
        )

    def _chat_completions_payload(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        force_json_object: bool = False,
        force_omit_reasoning_effort: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if text_format is not None:
            mode = "json_object" if force_json_object else self.chat_response_format_mode
            payload["response_format"] = to_chat_response_format(text_format, mode=mode)
        if reasoning_effort and not (self.omit_reasoning_effort or force_omit_reasoning_effort):
            payload["reasoning_effort"] = reasoning_effort
        return payload

    def _try_chat_responses_fallback(
        self,
        *,
        model: str,
        prompt: str,
        text_format: dict[str, Any] | None,
        reasoning_effort: str | None,
        cause: Exception,
        response: requests.Response,
    ) -> str | None:
        if self.api_style != "chat_completions":
            return None
        content_type = str(response.headers.get("content-type") or "").lower()
        if "text/event-stream" not in content_type:
            return None
        responses_url = self._responses_url()
        responses_payload = self._responses_payload(
            model=model,
            prompt=prompt,
            text_format=text_format,
            reasoning_effort=reasoning_effort,
        )
        fallback_response = requests.post(
            responses_url,
            json=responses_payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=self.timeout_seconds,
        )
        fallback_response.raise_for_status()
        fallback_payload = parse_openai_response_payload(fallback_response)
        try:
            return extract_response_text(fallback_payload)
        except Exception as fallback_exc:
            raise RuntimeError(
                build_ai_fallback_error_message(
                    cause,
                    fallback_exc,
                    model=model,
                    api_style=self.api_style,
                    chat_url=self._chat_completions_url(),
                    responses_url=responses_url,
                    chat_response=response,
                    responses_response=fallback_response,
                )
            ) from fallback_exc


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


def uses_deepseek_chat_compat(model: str) -> bool:
    normalized = model.strip().lower()
    return normalized.startswith("deepseek") or normalized.startswith("odaily-deepseek")


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


def build_ai_fallback_error_message(
    primary_exc: Exception,
    fallback_exc: Exception,
    *,
    model: str,
    api_style: OpenAIApiStyle,
    chat_url: str,
    responses_url: str,
    chat_response: requests.Response,
    responses_response: requests.Response,
) -> str:
    return (
        "OpenAI request failed after responses fallback: "
        f"model={model} api_style={api_style} "
        f"chat_url={chat_url} chat_error={primary_exc} "
        f"chat_status_code={chat_response.status_code} "
        f"chat_content_type={str(chat_response.headers.get('content-type') or '-').strip() or '-'} "
        f"chat_body_prefix={chat_response.text[:200].replace(chr(10), '\\n').replace(chr(13), '\\r')} "
        f"responses_url={responses_url} responses_error={fallback_exc} "
        f"responses_status_code={responses_response.status_code} "
        f"responses_content_type={str(responses_response.headers.get('content-type') or '-').strip() or '-'} "
        f"responses_body_prefix={responses_response.text[:200].replace(chr(10), '\\n').replace(chr(13), '\\r')}"
    )


def to_chat_response_format(
    text_format: dict[str, Any],
    *,
    mode: ChatResponseFormatMode = "json_schema",
) -> dict[str, Any]:
    if mode == "json_object":
        return {"type": "json_object"}
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
