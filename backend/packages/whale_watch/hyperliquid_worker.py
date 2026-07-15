from __future__ import annotations

import os
import signal
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from types import FrameType
from typing import Iterator

from packages.common.config import WhaleWatchHyperliquidSettings
from packages.common.heartbeat import HeartbeatThrottle
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.x_processing.telegram import TelegramClient, skipped_telegram_result

from .hyperliquid_client import HyperliquidClient
from .hyperliquid_detector import detect_hyperliquid_activity, format_money
from .hyperliquid_repository import WhaleWatchHyperliquidRepository
from .models import HyperliquidActivity, HyperliquidRunResult, HyperliquidRuntimeSettings, HyperliquidWindowEntry


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
        self.feed_writer = LocalEditorPluginFeedWriter()
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
        runtime = None
        if addresses:
            runtime = self.repository.get_runtime_settings(
                default_single_fill_min_notional_usd=Decimal(str(self.settings.single_fill_min_notional_usd)),
                default_aggregate_min_notional_usd=Decimal(str(self.settings.aggregate_min_notional_usd)),
                default_aggregate_window_seconds=self.settings.aggregate_window_seconds,
            )
        failed: dict[str, str] = {}
        processed = 0
        seeded = 0
        detected = 0
        inserted = 0
        sent = 0
        suppressed = 0
        for whale in addresses:
            processed += 1
            try:
                stats = self._process_address(whale=whale, runtime=runtime)
                seeded += int(stats["seeded"])
                detected += int(stats["detected"])
                inserted += int(stats["inserted"])
                sent += int(stats["sent"])
                suppressed += int(stats["suppressed"])
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
            suppressed=suppressed,
            failed=failed,
        )
        self._record_heartbeat(result)
        return result

    def run_forever(self) -> None:
        print(f"[odaily] whale watch hyperliquid worker started interval={self.settings.interval_seconds}s")
        with self._install_signal_handlers():
            while not self._stop:
                try:
                    result = self.run_once()
                    print(
                        "[odaily] whale watch hyperliquid round "
                        f"addresses={result.addresses} processed={result.processed} seeded={result.seeded} "
                        f"detected={result.detected} inserted={result.inserted} sent={result.sent} "
                        f"suppressed={result.suppressed} failed={len(result.failed)}"
                    )
                except Exception as exc:
                    print(f"[odaily] whale watch hyperliquid round failed: {exc}")
                self._sleep()

    def _process_address(self, *, whale, runtime: HyperliquidRuntimeSettings) -> dict[str, int | bool]:
        polled_at = _utc_now()
        state = self.repository.get_state(address_id=whale.id)
        fills = self.client.user_fills(whale.address)
        fills.sort(key=lambda item: (_int(item.get("time")) or 0, str(item.get("tid") or ""), str(item.get("hash") or "")))
        max_seen_time = max((_int(item.get("time")) or 0 for item in fills), default=state.last_seen_time if state else None)
        seeded_window_entries: list[HyperliquidWindowEntry] = []
        for fill in fills:
            activity = detect_hyperliquid_activity(whale=whale, fill=fill)
            if activity is None:
                continue
            seeded_window_entries.append(self._window_entry_from_activity(activity))
        seeded_window = self._trim_window_entries(
            entries=seeded_window_entries,
            now_ms=max_seen_time,
            window_seconds=runtime.aggregate_window_seconds,
        )
        if state is None or state.seeded_at is None:
            self.repository.mark_seeded(
                address_id=whale.id,
                last_seen_time=max_seen_time,
                polled_at=polled_at,
                aggregate_window_entries=seeded_window,
                aggregate_alert_active=self._sum_window_entries(seeded_window) >= runtime.aggregate_min_notional_usd,
            )
            return {"seeded": True, "detected": 0, "inserted": 0, "sent": 0, "suppressed": 0}

        detected = 0
        inserted = 0
        sent = 0
        suppressed = 0
        telegram_error: str | None = None
        cutoff = state.last_seen_time or 0
        aggregate_window_entries = self._trim_window_entries(
            entries=list(state.aggregate_window_entries),
            now_ms=max_seen_time or cutoff,
            window_seconds=runtime.aggregate_window_seconds,
        )
        aggregate_alert_active = state.aggregate_alert_active and (
            self._sum_window_entries(aggregate_window_entries) >= runtime.aggregate_min_notional_usd
        )
        for fill in fills:
            fill_time = _int(fill.get("time")) or 0
            # `mark_seeded()` stores the latest seen fill timestamp. On later polls
            # we must require a strictly newer fill, otherwise a newly added
            # address will replay the latest historical fill once.
            if fill_time <= cutoff:
                continue
            activity = detect_hyperliquid_activity(whale=whale, fill=fill)
            if activity is None:
                continue
            detected += 1
            aggregate_window_entries = self._append_window_entry(
                aggregate_window_entries,
                activity=activity,
                window_seconds=runtime.aggregate_window_seconds,
            )

            should_send_single = activity.notional_usd >= runtime.single_fill_min_notional_usd
            if should_send_single:
                activity_id = self.repository.save_activity(whale=whale, activity=activity)
                if activity_id is not None:
                    inserted += 1
                    self._write_activity_feed(activity_id=activity_id, whale=whale, activity=activity)
                    sent_ok = self._send_activity(activity)
                    sent += int(sent_ok)
                    if not sent_ok:
                        telegram_error = f"telegram send failed fill={activity.fill_key}"
                continue

            aggregate_total = self._sum_window_entries(aggregate_window_entries)
            if aggregate_total >= runtime.aggregate_min_notional_usd and not aggregate_alert_active:
                aggregate_activity = self._build_aggregate_activity(
                    whale=whale,
                    entries=aggregate_window_entries,
                    aggregate_total=aggregate_total,
                )
                aggregate_activity_id = self.repository.save_activity(whale=whale, activity=aggregate_activity)
                if aggregate_activity_id is not None:
                    inserted += 1
                    self._write_activity_feed(activity_id=aggregate_activity_id, whale=whale, activity=aggregate_activity)
                    sent_ok = self._send_activity(aggregate_activity)
                    sent += int(sent_ok)
                    if not sent_ok:
                        telegram_error = f"telegram send failed fill={aggregate_activity.fill_key}"
                aggregate_alert_active = True
            else:
                suppressed += 1

        aggregate_window_entries = self._trim_window_entries(
            entries=aggregate_window_entries,
            now_ms=max_seen_time or cutoff,
            window_seconds=runtime.aggregate_window_seconds,
        )
        if self._sum_window_entries(aggregate_window_entries) < runtime.aggregate_min_notional_usd:
            aggregate_alert_active = False

        self.repository.record_success(
            address_id=whale.id,
            last_seen_time=max_seen_time,
            polled_at=polled_at,
            aggregate_window_entries=aggregate_window_entries,
            aggregate_alert_active=aggregate_alert_active,
        )
        if telegram_error:
            self.repository.record_error(address_id=whale.id, error=telegram_error, polled_at=polled_at)
        return {
            "seeded": False,
            "detected": detected,
            "inserted": inserted,
            "sent": sent,
            "suppressed": suppressed,
        }

    def _send_activity(self, activity) -> bool:
        result = skipped_telegram_result("whale hyperliquid business signal disabled; use editor plugin feed")
        self.repository.update_activity_telegram_result(
            fill_key=activity.fill_key,
            telegram_result=result.model_dump(mode="json"),
        )
        if not result.ok and not result.skipped:
            print(f"[odaily] whale watch hyperliquid telegram failed fill={activity.fill_key} error={result.error}")
        return result.ok or result.skipped

    def _write_activity_feed(self, *, activity_id: int, whale, activity: HyperliquidActivity) -> None:
        try:
            self.feed_writer.upsert_whale_hyperliquid(
                feed_item_id=f"whale_hyperliquid:{activity_id}",
                address=whale.address,
                address_label=whale.label,
                coin=activity.coin,
                direction=activity.direction,
                notional_usd=str(activity.notional_usd),
                alert_kind=activity.alert_kind,
                summary=activity.summary or activity.telegram_text,
                detail_url=f"https://hyperbot.network/trader/{whale.address}",
                occurred_at=datetime.now(UTC),
            )
        except Exception as exc:
            print(f"[odaily] local feed write skipped whale_hyperliquid_activity_id={activity_id} error={exc}")

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
                    "suppressed": result.suppressed,
                    "failed": result.failed,
                },
            )
        except Exception as exc:
            print(f"[odaily] whale watch hyperliquid heartbeat failed: {exc}")

    def _sleep(self) -> None:
        deadline = time.monotonic() + self.settings.interval_seconds
        while not self._stop and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    def _window_entry_from_activity(self, activity: HyperliquidActivity) -> HyperliquidWindowEntry:
        return HyperliquidWindowEntry(
            fill_key=activity.fill_key,
            fill_time_ms=activity.fill_time_ms,
            notional_usd=activity.notional_usd,
            summary=activity.summary,
            direction=activity.direction,
            coin=activity.coin,
        )

    def _append_window_entry(
        self,
        entries: list[HyperliquidWindowEntry],
        *,
        activity: HyperliquidActivity,
        window_seconds: int,
    ) -> list[HyperliquidWindowEntry]:
        next_entries = [entry for entry in entries if entry.fill_key != activity.fill_key]
        next_entries.append(
            HyperliquidWindowEntry(
                fill_key=activity.fill_key,
                fill_time_ms=activity.fill_time_ms,
                notional_usd=activity.notional_usd,
                summary=activity.summary,
                direction=activity.direction,
                coin=activity.coin,
            )
        )
        return self._trim_window_entries(entries=next_entries, now_ms=activity.fill_time_ms, window_seconds=window_seconds)

    def _trim_window_entries(
        self,
        *,
        entries: list[HyperliquidWindowEntry],
        now_ms: int | None,
        window_seconds: int,
    ) -> list[HyperliquidWindowEntry]:
        if now_ms is None:
            return list(entries)
        cutoff_ms = now_ms - (window_seconds * 1000)
        trimmed = [entry for entry in entries if entry.fill_time_ms >= cutoff_ms]
        trimmed.sort(key=lambda item: (item.fill_time_ms, item.fill_key))
        return trimmed

    def _sum_window_entries(self, entries: list[HyperliquidWindowEntry]) -> Decimal:
        total = Decimal("0")
        for entry in entries:
            total += entry.notional_usd
        return total

    def _build_aggregate_activity(
        self,
        *,
        whale,
        entries: list[HyperliquidWindowEntry],
        aggregate_total: Decimal,
    ) -> HyperliquidActivity:
        latest = entries[-1]
        fill_time = datetime.fromtimestamp(latest.fill_time_ms / 1000, UTC)
        window_minutes = max(1, round((entries[-1].fill_time_ms - entries[0].fill_time_ms) / 60000)) if len(entries) > 1 else 0
        summary = f"{len(entries)} 笔累计约 {format_money(aggregate_total)} USDC"
        text = (
            f"「{whale.label}」在近 {max(window_minutes, 1)} 分钟内累计开平仓 {len(entries)} 笔，"
            f"名义价值约 {format_money(aggregate_total)} USDC，已超过聚合门槛\n"
            f"最近一笔：{latest.summary}\n"
            f"https://hyperbot.network/trader/{whale.address}"
        )
        return HyperliquidActivity(
            alert_kind="aggregate",
            fill_key=f"aggregate:{latest.fill_time_ms}:{len(entries)}:{format_money(aggregate_total)}",
            coin="MULTI",
            direction="Aggregate",
            side="-",
            price=Decimal("0"),
            size=Decimal(str(len(entries))),
            notional_usd=aggregate_total,
            closed_pnl=Decimal("0"),
            fill_time=fill_time,
            fill_time_ms=latest.fill_time_ms,
            telegram_text=text,
            summary=summary,
            aggregate_fill_count=len(entries),
            raw_payload={
                "kind": "aggregate",
                "fill_keys": [entry.fill_key for entry in entries],
                "window_entries": [
                    {
                        "fill_key": entry.fill_key,
                        "fill_time_ms": entry.fill_time_ms,
                        "notional_usd": str(entry.notional_usd),
                        "summary": entry.summary,
                    }
                    for entry in entries
                ],
            },
        )

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
