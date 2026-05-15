from __future__ import annotations

import time

import requests
from typing import Any

from pydantic import BaseModel


class TelegramResult(BaseModel):
    ok: bool
    status_code: int | None = None
    response_text: str | None = None
    response_json: dict[str, Any] | None = None
    error: str | None = None
    skipped: bool = False


class TelegramClient:
    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        message_thread_id: int | str | None = None,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.bot_token = bot_token.strip() if bot_token else None
        self.chat_id = chat_id.strip() if chat_id else None
        self.message_thread_id = _normalize_message_thread_id(message_thread_id)
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def send_message(
        self,
        text: str,
        *,
        message_thread_id: int | str | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> TelegramResult:
        if not self.bot_token or not self.chat_id:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        thread_id = _normalize_message_thread_id(message_thread_id)
        if thread_id is None:
            thread_id = self.message_thread_id
        payload: dict[str, object] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._post(url, payload)

    def create_forum_topic(self, name: str) -> TelegramResult:
        if not self.bot_token or not self.chat_id:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "name": name,
        }
        return self._post(f"https://api.telegram.org/bot{self.bot_token}/createForumTopic", payload)

    def get_updates(self, *, offset: int | None = None, timeout_seconds: int = 20) -> TelegramResult:
        if not self.bot_token:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN")
        payload: dict[str, Any] = {
            "timeout": max(0, int(timeout_seconds)),
            "allowed_updates": ["callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        return self._post(f"https://api.telegram.org/bot{self.bot_token}/getUpdates", payload)

    def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> TelegramResult:
        if not self.bot_token:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN")
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        return self._post(f"https://api.telegram.org/bot{self.bot_token}/answerCallbackQuery", payload)

    def edit_message_reply_markup(
        self,
        *,
        chat_id: int | str,
        message_id: int,
        reply_markup: dict[str, Any],
    ) -> TelegramResult:
        if not self.bot_token:
            return TelegramResult(ok=False, skipped=True, error="missing TELEGRAM_BOT_TOKEN")
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": reply_markup,
        }
        return self._post(f"https://api.telegram.org/bot{self.bot_token}/editMessageReplyMarkup", payload)

    def _post(self, url: str, payload: dict[str, Any]) -> TelegramResult:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(url, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                response_json = _safe_json(response)
                return TelegramResult(
                    ok=True,
                    status_code=response.status_code,
                    response_text=response.text[:1000],
                    response_json=response_json,
                )
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        return TelegramResult(ok=False, error=str(last_error) if last_error else "telegram request failed")


def _normalize_message_thread_id(value: int | str | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    stripped = value.strip()
    if not stripped:
        return None
    return int(stripped)


def _safe_json(response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None
