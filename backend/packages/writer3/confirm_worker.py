from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from packages.common.config import Writer3Settings
from packages.x_processing.telegram import TelegramClient

from .index import Writer3Index


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CALLBACK_PREFIX = "w3_confirm:"


@dataclass(frozen=True, slots=True)
class Writer3ConfirmRunResult:
    updates: int
    confirmed: int
    ignored: int
    failed: int
    message: str
    exit_code: int = 0


class Writer3TelegramConfirmWorker:
    def __init__(
        self,
        *,
        index: Writer3Index,
        settings: Writer3Settings,
        telegram_client: TelegramClient | None = None,
        poll_timeout_seconds: int = 20,
    ) -> None:
        self.index = index
        self.settings = settings
        self.telegram_client = telegram_client or self._build_telegram_client()
        self.poll_timeout_seconds = max(0, int(poll_timeout_seconds))
        self.offset: int | None = None

    def run_once(self) -> Writer3ConfirmRunResult:
        result = self.telegram_client.get_updates(offset=self.offset, timeout_seconds=self.poll_timeout_seconds)
        if not result.ok:
            return Writer3ConfirmRunResult(updates=0, confirmed=0, ignored=0, failed=1, message=result.error or "getUpdates failed", exit_code=1)
        payload = result.response_json or {}
        updates = payload.get("result") if isinstance(payload, dict) else []
        if not isinstance(updates, list):
            return Writer3ConfirmRunResult(updates=0, confirmed=0, ignored=0, failed=1, message="invalid getUpdates payload", exit_code=1)

        confirmed = 0
        ignored = 0
        failed = 0
        for update in updates:
            if not isinstance(update, dict):
                ignored += 1
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = update_id + 1
            item = self._handle_update(update)
            confirmed += item.confirmed
            ignored += item.ignored
            failed += item.failed
        return Writer3ConfirmRunResult(
            updates=len(updates),
            confirmed=confirmed,
            ignored=ignored,
            failed=failed,
            message=f"updates={len(updates)} confirmed={confirmed} ignored={ignored} failed={failed}",
            exit_code=1 if failed else 0,
        )

    def run_forever(self) -> None:
        print("[odaily] writer3 telegram confirm worker started")
        while True:
            try:
                result = self.run_once()
                if result.updates or result.failed:
                    print(
                        "[odaily] writer3 confirm round "
                        f"updates={result.updates} confirmed={result.confirmed} ignored={result.ignored} failed={result.failed} "
                        f"message={result.message}"
                    )
            except Exception as exc:
                print(f"[odaily] writer3 confirm round failed: {exc}")
            if self.poll_timeout_seconds == 0:
                time.sleep(self.settings.worker_idle_sleep_seconds)

    def _handle_update(self, update: dict) -> Writer3ConfirmRunResult:
        callback = update.get("callback_query")
        if not isinstance(callback, dict):
            return Writer3ConfirmRunResult(updates=1, confirmed=0, ignored=1, failed=0, message="not callback query")
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        if not data.startswith(CALLBACK_PREFIX):
            self._answer(callback_id, "不是编写者3确认按钮")
            return Writer3ConfirmRunResult(updates=1, confirmed=0, ignored=1, failed=0, message="wrong callback prefix")
        message = callback.get("message")
        if not isinstance(message, dict) or message.get("message_id") is None:
            self._answer(callback_id, "找不到原消息")
            return Writer3ConfirmRunResult(updates=1, confirmed=0, ignored=0, failed=1, message="missing callback message", exit_code=1)

        message_id = int(message["message_id"])
        confirmation = self.index.get_telegram_confirmation_by_message_id(message_id)
        if confirmation is None:
            self._answer(callback_id, "这条消息没有本地确认记录")
            return Writer3ConfirmRunResult(updates=1, confirmed=0, ignored=0, failed=1, message="missing local confirmation", exit_code=1)

        if confirmation.get("confirmed_at"):
            label = confirmed_button_label(confirmation)
            self._edit_button(confirmation=confirmation, message=message, label=label)
            self._answer(callback_id, "已确认")
            return Writer3ConfirmRunResult(updates=1, confirmed=0, ignored=1, failed=0, message="already confirmed")

        user = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        confirmed_at = datetime.now(UTC)
        confirmed_by_name = user_display_name(user)
        self.index.confirm_telegram_message(
            message_id=message_id,
            confirmed_at=confirmed_at,
            confirmed_by_id=str(user.get("id")) if user.get("id") is not None else None,
            confirmed_by_username=str(user.get("username")) if user.get("username") else None,
            confirmed_by_name=confirmed_by_name,
        )
        updated = self.index.get_telegram_confirmation_by_message_id(message_id) or confirmation
        label = confirmed_button_label(updated)
        edit_result = self._edit_button(confirmation=updated, message=message, label=label)
        self._answer(callback_id, "确认已记录")
        if not edit_result:
            return Writer3ConfirmRunResult(updates=1, confirmed=1, ignored=0, failed=1, message="confirmed but edit failed", exit_code=1)
        return Writer3ConfirmRunResult(updates=1, confirmed=1, ignored=0, failed=0, message="confirmed")

    def _edit_button(self, *, confirmation: dict[str, object], message: dict, label: str) -> bool:
        chat_id = confirmation.get("chat_id") or _message_chat_id(message)
        message_id = confirmation.get("message_id") or message.get("message_id")
        if chat_id is None or message_id is None:
            return False
        result = self.telegram_client.edit_message_reply_markup(
            chat_id=str(chat_id),
            message_id=int(message_id),
            reply_markup={"inline_keyboard": [[{"text": label, "callback_data": f"{CALLBACK_PREFIX}{confirmation.get('context_id')}"}]]},
        )
        return result.ok

    def _answer(self, callback_query_id: str, text: str) -> None:
        if callback_query_id:
            self.telegram_client.answer_callback_query(callback_query_id, text=text)

    def _build_telegram_client(self) -> TelegramClient:
        return TelegramClient(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            timeout_seconds=self.settings.telegram_timeout_seconds,
            max_attempts=self.settings.retry.max_attempts,
            backoff_seconds=self.settings.retry.backoff_seconds,
        )


def confirmed_button_label(confirmation: dict[str, object]) -> str:
    confirmed_at = _parse_datetime(confirmation.get("confirmed_at"))
    time_text = confirmed_at.astimezone(SHANGHAI_TZ).strftime("%H:%M") if confirmed_at else "--:--"
    name = str(confirmation.get("confirmed_by_name") or confirmation.get("confirmed_by_username") or "").strip()
    return f"已确认 {time_text} {name}".strip()


def user_display_name(user: dict) -> str:
    first = str(user.get("first_name") or "").strip()
    last = str(user.get("last_name") or "").strip()
    username = str(user.get("username") or "").strip()
    full_name = " ".join(part for part in (first, last) if part).strip()
    return full_name or username or str(user.get("id") or "").strip()


def _message_chat_id(message: dict) -> str | None:
    chat = message.get("chat")
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None
    return str(chat["id"])


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
