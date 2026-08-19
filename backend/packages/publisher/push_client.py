from __future__ import annotations

import time
from html import escape

import requests
from pydantic import BaseModel


class PushResult(BaseModel):
    ok: bool
    status_code: int | None = None
    response_text: str | None = None
    error: str | None = None
    dry_run: bool = False


def content_to_paragraph_html(content: str) -> str:
    paragraphs = (line.strip() for line in content.splitlines())
    return "".join(
        f"<p>{escape(paragraph, quote=False)}</p>" for paragraph in paragraphs if paragraph
    )


class PushClient:
    def __init__(
        self,
        *,
        endpoint: str,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def push(
        self,
        *,
        title: str,
        content: str,
        dry_run: bool,
        source_url: str | None = None,
        is_publish: bool = False,
        is_push: bool = False,
        idempotency_key: str | None = None,
    ) -> PushResult:
        payload = {
            "title": title,
            "content": content_to_paragraph_html(content),
            "isPublish": bool(is_publish),
            "isPush": bool(is_push),
        }
        if source_url and source_url.strip():
            payload["sourceUrl"] = source_url.strip()
        if dry_run:
            return PushResult(ok=True, response_text="dry-run: not sent", dry_run=True)

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                headers = {"Content-Type": "application/json"}
                if idempotency_key:
                    headers["Idempotency-Key"] = idempotency_key
                response = requests.post(
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return PushResult(
                    ok=True,
                    status_code=response.status_code,
                    response_text=response.text[:1000],
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)

        return PushResult(ok=False, error=str(last_error) if last_error else "push failed")
