from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from types import FrameType
from typing import Iterator

from packages.common.config import WhaleWatchHyperliquidSettings
from packages.common.heartbeat import HeartbeatThrottle
from packages.x_processing.telegram import TelegramClient

from .hyperliquid_client import HyperliquidClient
from .hyperliquid_detector import detect_hyperliquid_activity
from .hyperliquid_repository import WhaleWatchHyperliquidRepository
from .models import HyperliquidRunResult


class WhaleWatchHyperliquidWorker:
    def __init__(
        self,
        *,
        repository: WhaleWatchHyperliquidRepository,
        settings: WhaleWatchHyperliquidSettings,
        client: HyperliquidClient | None = None,
        telegram_client: TelegramClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.client = client or HyperliquidClient(
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self.telegram_client = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self.worker_id = worker_id or f"whale_watch_hyperliquid-{os.getpid()}"
        self._heartbeat = HeartbeatThrottle(
            component="whale_watch_hyperliquid",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )
        self._stop = False

    def run_once(self) -> HyperliquidRunResult:
        addresses = self.repository.list_addresses(include_disabled=False)
        failed: dict[str, str] = {}
        processed = 0
        seeded = 0
        detected = 0
        inserted = 0
        sent = 0
        skipped_small = 0
        for whale in addresses:
            processed += 1
            try:
                stats = self._process_address(whale=whale)
                seeded += int(stats["seeded"])
                detected += int(stats["detected"])
                inserted += int(stats["inserted"])
                sent += int(stats["sent"])
                skipped_small += int(stats["skipped_small"])
            except Exception as exc:
                failed[whale.address_lower] = str(exc)
                self.repository.record_error(address_id=whale.id, error=str(exc), polled_at=_utc_now())
                print(f"[odaily] whale watch hyperliquid failed address={whale.address_lower} error={exc}")
        result = HyperliquidRunResult(
            addresses=len(addresses),
            processed=processed,
            seeded=seeded,
            detected=detected,
            inserted=inserted,
            sent=sent,
            skipped_small=skipped_small,
            failed=failed,
        )
        self._record_heartbeat(result)
        return result

    def run_forever(self) -> None:
        print(f"[odaily] whale watch hyperliquid worker started interval={self.settings.interval_seconds}s")
        with self._install_signal_handlers():
            while not self._stop:
                result = self.run_once()
                print(
                    "[odaily] whale watch hyperliquid round "
                    f"addresses={result.addresses} processed={result.processed} seeded={result.seeded} "
                    f"detected={result.detected} inserted={result.inserted} sent={result.sent} "
                    f"skipped_small={result.skipped_small} failed={len(result.failed)}"
                )
                self._sleep()

    def _process_address(self, *, whale) -> dict[str, int | bool]:
        polled_at = _utc_now()
        state = self.repository.get_state(address_id=whale.id)
        fills = self.client.user_fills(whale.address)
        fills.sort(key=lambda item: (_int(item.get("time")) or 0, str(item.get("tid") or ""), str(item.get("hash") or "")))
        max_seen_time = max((_int(item.get("time")) or 0 for item in fills), default=state.last_seen_time if state else None)
        if state is None or state.seeded_at is None:
            self.repository.mark_seeded(address_id=whale.id, last_seen_time=max_seen_time, polled_at=polled_at)
            return {"seeded": True, "detected": 0, "inserted": 0, "sent": 0, "skipped_small": 0}

        detected = 0
        inserted = 0
        sent = 0
        skipped_small = 0
        telegram_error: str | None = None
        cutoff = state.last_seen_time or 0
        for fill in fills:
            fill_time = _int(fill.get("time")) or 0
            if fill_time < cutoff:
                continue
            activity = detect_hyperliquid_activity(whale=whale, fill=fill)
            if activity is None:
                continue
            detected += 1
            if activity.notional_usd < self.settings.min_notional_usd:
                skipped_small += 1
                continue
            if not self.repository.save_activity(whale=whale, activity=activity):
                continue
            inserted += 1
            sent_ok = self._send_activity(activity)
            sent += int(sent_ok)
            if not sent_ok:
                telegram_error = f"telegram send failed fill={activity.fill_key}"

        self.repository.record_success(address_id=whale.id, last_seen_time=max_seen_time, polled_at=polled_at)
        if telegram_error:
            self.repository.record_error(address_id=whale.id, error=telegram_error, polled_at=polled_at)
        return {
            "seeded": False,
            "detected": detected,
            "inserted": inserted,
            "sent": sent,
            "skipped_small": skipped_small,
        }

    def _send_activity(self, activity) -> bool:
        result = self.telegram_client.send_message(
            activity.telegram_text,
            message_thread_id=self.settings.telegram_message_thread_id,
        )
        self.repository.update_activity_telegram_result(
            fill_key=activity.fill_key,
            telegram_result=result.model_dump(mode="json"),
        )
        if not result.ok:
            print(f"[odaily] whale watch hyperliquid telegram failed fill={activity.fill_key} error={result.error}")
        return result.ok

    def _record_heartbeat(self, result: HyperliquidRunResult) -> None:
        try:
            self._heartbeat.send(
                status="ok" if not result.failed else "failed",
                success=not result.failed,
                error=str(result.failed) if result.failed else None,
                metadata={
                    "addresses": result.addresses,
                    "processed": result.processed,
                    "seeded": result.seeded,
                    "detected": result.detected,
                    "inserted": result.inserted,
                    "sent": result.sent,
                    "skipped_small": result.skipped_small,
                    "failed": result.failed,
                },
            )
        except Exception as exc:
            print(f"[odaily] whale watch hyperliquid heartbeat failed: {exc}")

    def _sleep(self) -> None:
        deadline = time.monotonic() + self.settings.interval_seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    @contextmanager
    def _install_signal_handlers(self) -> Iterator[None]:
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)

        def stop(_signum: int, _frame: FrameType | None) -> None:
            self._stop = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now() -> datetime:
    return datetime.now(UTC)
