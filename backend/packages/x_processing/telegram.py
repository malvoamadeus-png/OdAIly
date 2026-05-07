from __future__ import annotations

import time

import requests
from pydantic import BaseModel


class TelegramResult(BaseModel):
    ok: bool
    status_code: int | None = None
    response_text: str | None = None
    error: str | None = None
    skipped: bool = False


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.bot_token = bot_token.strip() if bot_token else None
        self.chat_id = chat_id.strip() if chat_id else None
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def send_message(self, text: str) -> TelegramResult:
        if not self.bot_token or not self.chat_id:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

        last_error: Exception | None = None
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return TelegramResult(ok=True, status_code=response.status_code, response_text=response.text[:1000])
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)

        return TelegramResult(ok=False, error=str(last_error) if last_error else "telegram send failed")
