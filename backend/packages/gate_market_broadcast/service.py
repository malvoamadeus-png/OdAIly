from __future__ import annotations

import json
import shutil
import time
from decimal import Decimal
from typing import Any

from packages.publisher import PushClient
from packages.x_processing.telegram import TelegramClient

from .backtest import run_symbol_backtest
from .client import GateMarketClient
from .copy import render_brief
from .models import PublishMode, SymbolConfig, SymbolState, TickerQuote
from .settings import GateMarketSettings
from .state_machine import advance_state, silently_recover
from .store import GateMarketStore


class GateMarketBroadcastService:
    def __init__(
        self,
        *,
        settings: GateMarketSettings,
        store: GateMarketStore | None = None,
        client: GateMarketClient | None = None,
        push_client: PushClient | None = None,
        telegram_client: TelegramClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or GateMarketStore(settings.database_path)
        self.store.initialize()
        self.client = client or GateMarketClient(
            base_url=settings.gate_api_base,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.gate_max_attempts,
        )
        # A publish event is never retried because the receiving endpoint has no
        # idempotency key and a timeout could otherwise create duplicate briefs.
        self.push_client = push_client or PushClient(
            endpoint=settings.push_endpoint,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=1,
            backoff_seconds=0,
        )
        self.telegram = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=3,
            backoff_seconds=1,
        )
        self._closed_until: dict[str, int] = {}
        self._history_refresh_after: dict[str, int] = {}
        self._last_disk_check = 0

    def _alert(self, key: str, message: str) -> None:
        try:
            allowed = self.store.should_send_alert(
                key,
                message,
                self.settings.alert_dedup_minutes * 60,
            )
        except Exception as exc:
            # SQLite failures must not prevent their own Telegram alert.
            print(f"[gate-market] alert dedup store failed key={key} error={exc}")
            allowed = True
        if not allowed:
            return
        result = self.telegram.send_message(f"【Gate行情播报告警】{message}")
        if not result.ok:
            print(f"[gate-market] telegram alert failed key={key} error={result.error}")

    def _recover_alert(self, key: str, message: str) -> None:
        try:
            active = self.store.recover_alert(key)
        except Exception as exc:
            print(f"[gate-market] recovery store failed key={key} error={exc}")
            return
        if not active:
            return
        result = self.telegram.send_message(f"【Gate行情播报恢复】{message}")
        if not result.ok:
            print(f"[gate-market] telegram recovery failed key={key} error={result.error}")

    def _check_disk(self, now: int) -> None:
        if now - self._last_disk_check < 300:
            return
        self._last_disk_check = now
        free_mb = shutil.disk_usage(self.settings.database_path.parent).free // (1024 * 1024)
        key = "disk_free"
        if free_mb < self.settings.disk_free_alert_mb:
            self._alert(key, f"磁盘剩余空间仅{free_mb}MB，请立即清理。")
        else:
            self._recover_alert(key, f"磁盘剩余空间已恢复至{free_mb}MB。")

    def _ensure_recent_history(self, config: SymbolConfig, now: int) -> bool:
        since = now - 49 * 3600
        if self.store.recent_sample_count(config.symbol, since) >= 60:
            return True
        if self._history_refresh_after.get(config.symbol, 0) > now:
            return False
        # Avoid re-downloading the same empty weekend/session window every
        # minute. A failed attempt is retried after 15 minutes; a completed
        # backfill is reconsidered after one hour until enough live points exist.
        self._history_refresh_after[config.symbol] = now + 900
        points = self.client.fetch_history(
            config.symbol,
            start_time=since,
            end_time=now,
        )
        self.store.add_samples(config.symbol, points)
        self._history_refresh_after[config.symbol] = now + 3600
        return True

    def _update_closed_state(
        self,
        state: SymbolState,
        quote: TickerQuote,
    ) -> None:
        state.market_status = quote.status
        state.next_open_at = quote.next_open_time
        state.last_success_at = quote.observed_at
        state.last_error = None
        self.store.save_state_and_event(state)
        next_check = int(time.time()) + 900
        if quote.next_open_time:
            next_check = min(next_check, max(int(time.time()) + 60, quote.next_open_time - 60))
        self._closed_until[quote.symbol] = next_check

    def process_symbol(self, config: SymbolConfig, *, mode: PublishMode | None = None) -> dict[str, Any]:
        active_mode = mode or self.store.get_mode()
        now = int(time.time())
        if self._closed_until.get(config.symbol, 0) > now:
            return {"symbol": config.symbol, "status": "closed_wait"}
        state = self.store.get_state(config.symbol, active_mode)
        try:
            quote = self.client.fetch_ticker(config.symbol)
        except Exception as exc:
            message = str(exc)
            state = self.store.record_fetch_error(config.symbol, active_mode, message)
            if state.market_status == "open" and (
                state.last_success_at is None or now - state.last_success_at >= 300
            ):
                self._alert(f"ticker:{config.symbol}", f"{config.symbol}开市期间连续取数失败：{message}")
            return {"symbol": config.symbol, "status": "fetch_failed", "error": message}

        self._recover_alert(f"ticker:{config.symbol}", f"{config.symbol}行情读取已恢复。")
        if quote.status != "open":
            self._update_closed_state(state, quote)
            return {"symbol": config.symbol, "status": quote.status}

        history_ready = False
        try:
            history_ready = self._ensure_recent_history(config, quote.observed_at)
        except Exception as exc:
            # Current quotes can still drive state, but the 24h comparison will
            # use the ticker's previous-session/open fallback.
            self._alert(
                f"history:{config.symbol}",
                f"{config.symbol}短期历史回填失败，将使用交易时段基准：{exc}",
            )
        if history_ready:
            self._recover_alert(f"history:{config.symbol}", f"{config.symbol}短期历史回填已恢复。")

        self.store.add_samples(config.symbol, [(quote.observed_at, quote.price)])
        state.market_status = quote.status
        state.next_open_at = quote.next_open_time
        state.last_success_at = quote.observed_at
        state.last_error = None

        if not state.initialized or state.last_price is None or state.last_quote_at is None:
            state.initialized = True
            state.last_price = quote.price
            state.last_quote_at = quote.observed_at
            self.store.save_state_and_event(state)
            return {"symbol": config.symbol, "status": "initialized"}

        poll_interval = self.store.poll_interval_seconds()
        if quote.observed_at - state.last_quote_at > max(180, poll_interval * 2):
            state.disarmed_levels = silently_recover(
                current_price=quote.price,
                step=config.threshold,
                disarmed_levels=state.disarmed_levels,
            )
            state.last_price = quote.price
            state.last_quote_at = quote.observed_at
            self.store.save_state_and_event(state)
            return {"symbol": config.symbol, "status": "silently_recovered"}

        trigger, next_disarmed = advance_state(
            previous_price=state.last_price,
            current_price=quote.price,
            step=config.threshold,
            disarmed_levels=state.disarmed_levels,
        )
        state.disarmed_levels = next_disarmed
        state.last_price = quote.price
        state.last_quote_at = quote.observed_at
        if trigger is None:
            self.store.save_state_and_event(state)
            return {"symbol": config.symbol, "status": "no_trigger"}

        metrics = self.store.reference_metrics(symbol=config.symbol, quote=quote)
        event_key = (
            f"{active_mode}:{config.symbol}:{quote.observed_at}:"
            f"{trigger.direction}:{trigger.level_index}"
        )
        event_id = self.store.save_state_and_event(
            state,
            event={
                "event_key": event_key,
                "direction": trigger.direction,
                "trigger_level": str(trigger.level),
                "current_price": str(quote.price),
                "reference_kind": metrics.reference_kind if metrics else None,
                "reference_price": str(metrics.reference_price) if metrics else None,
                "window_high": str(metrics.high) if metrics else None,
                "window_low": str(metrics.low) if metrics else None,
                "observed_at": quote.observed_at,
                "status": "pending",
            },
        )
        if event_id is None:
            return {"symbol": config.symbol, "status": "duplicate_event"}
        if metrics is None:
            self.store.mark_event(
                event_id,
                status="suppressed",
                error="missing 24h/previous-session/open reference",
            )
            return {"symbol": config.symbol, "status": "suppressed_no_reference"}

        brief = render_brief(
            config=config,
            current_price=quote.price,
            trigger_price=trigger.level,
            metrics=metrics,
            templates=self.store.templates(),
        )
        if brief is None:
            self.store.mark_event(event_id, status="suppressed")
            return {"symbol": config.symbol, "status": "suppressed_zero_change"}

        result = self.push_client.push(
            title=brief.title,
            content=brief.content,
            dry_run=False,
            is_publish=active_mode == "live",
            is_push=False,
        )
        if not result.ok:
            error = result.error or "push failed"
            self.store.mark_event(
                event_id,
                status="push_failed",
                template_key=brief.template_key,
                change_percent=brief.change_percent,
                title=brief.title,
                content=brief.content,
                push_status_code=result.status_code,
                push_response=result.response_text,
                error=error,
            )
            self._alert(f"push:{config.symbol}", f"{config.symbol}发布失败，已放弃本次事件：{error}")
            return {"symbol": config.symbol, "status": "push_failed", "error": error}

        self.store.mark_event(
            event_id,
            status="published" if active_mode == "live" else "backend_created",
            template_key=brief.template_key,
            change_percent=brief.change_percent,
            title=brief.title,
            content=brief.content,
            push_status_code=result.status_code,
            push_response=result.response_text,
        )
        self._recover_alert(f"push:{config.symbol}", f"{config.symbol}发布接口已恢复。")
        return {
            "symbol": config.symbol,
            "status": "published" if active_mode == "live" else "backend_created",
            "title": brief.title,
        }

    def run_once(self) -> list[dict[str, Any]]:
        now = int(time.time())
        self._check_disk(now)
        results: list[dict[str, Any]] = []
        for config in self.store.list_symbol_configs():
            if not config.enabled:
                continue
            try:
                results.append(self.process_symbol(config))
            except Exception as exc:
                message = str(exc)
                self._alert(f"worker:{config.symbol}", f"{config.symbol}处理异常：{message}")
                results.append({"symbol": config.symbol, "status": "error", "error": message})
            time.sleep(0.22)
        self.store.prune(sample_retention_hours=48, event_limit=100)
        return results

    def run_forever(self) -> None:
        print(
            f"[gate-market] worker started mode={self.store.get_mode()} "
            f"db={self.settings.database_path}"
        )
        while True:
            started = time.time()
            try:
                results = self.run_once()
                print(f"[gate-market] cycle {json.dumps(results, ensure_ascii=False)}")
                self._recover_alert("worker_cycle", "worker主循环已恢复。")
            except Exception as exc:
                self._alert("worker_cycle", f"worker主循环异常，将继续下一轮：{exc}")
            interval = self.store.poll_interval_seconds()
            next_boundary = (int(started) // interval + 1) * interval
            time.sleep(max(1, next_boundary - time.time()))

    def backtest(self, *, days: int = 90) -> list[dict[str, Any]]:
        end_time = int(time.time())
        return [
            run_symbol_backtest(
                client=self.client,
                config=config,
                days=days,
                end_time=end_time,
            )
            for config in self.store.list_symbol_configs()
            if config.enabled
        ]
