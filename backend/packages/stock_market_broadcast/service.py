from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any

from packages.publisher import PushClient

from .client import BinanceStockMarketClient
from .copy import render_alert
from .models import StockAlert, StockInstrument, StockQuote, TriggerState, Window
from .settings import StockMarketSettings
from .state_machine import advance_trigger_state
from .store import StockMarketStore


class StockMarketBroadcastService:
    def __init__(
        self,
        *,
        settings: StockMarketSettings,
        store: StockMarketStore | None = None,
        client: BinanceStockMarketClient | None = None,
        push_client: PushClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or StockMarketStore(settings.database_path)
        self.store.initialize()
        self.client = client or BinanceStockMarketClient(
            base_url=settings.binance_api_base,
            timeout_seconds=settings.request_timeout_seconds,
        )
        self.push_client = push_client or PushClient(
            endpoint=settings.push_endpoint,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=1,
            backoff_seconds=0,
        )
        self._startup_baseline_pending = True
        self._startup_baseline_symbols_pending: set[str] = set()

    def _refresh_instruments(self, now: int) -> None:
        last_refresh = self.store.last_discovery_at()
        if now - last_refresh < self.settings.instrument_refresh_seconds:
            return
        try:
            instruments = self.client.fetch_instruments()
            self.store.sync_instruments(instruments, now=now)
        except Exception as exc:
            self.store.record_discovery_error(str(exc), now=now)
            if not self.store.list_instruments():
                raise

    def run_once(self) -> dict[str, Any]:
        started = time.monotonic()
        now = int(time.time())
        try:
            self._refresh_instruments(now)
            instruments = self.store.list_instruments()
            if not instruments:
                raise RuntimeError("No active Binance stock contracts are available")
            quotes = self.client.fetch_quotes(instruments)
            return self._process_quotes(instruments, quotes, now=now, started=started)
        except Exception as exc:
            self.store.record_worker_failure(str(exc), now=now)
            raise

    def _process_quotes(
        self,
        instruments: list[StockInstrument],
        quotes: dict[str, StockQuote],
        *,
        now: int,
        started: float,
    ) -> dict[str, Any]:
        by_symbol = {item.symbol: item for item in instruments}
        valid_quotes: dict[str, StockQuote] = {}
        errors: dict[str, str] = {}
        for instrument in instruments:
            quote = quotes.get(instrument.symbol)
            if quote is None:
                errors[instrument.symbol] = "Binance ticker missing or stale"
                continue
            age = now - quote.observed_at
            if age > self.settings.quote_max_age_seconds or age < -15:
                errors[instrument.symbol] = f"Binance ticker is stale ({age}s)"
                continue
            valid_quotes[instrument.symbol] = quote

        symbols = list(valid_quotes)
        references = self.store.one_hour_references(valid_quotes)
        prior_changes = self.store.last_changes(symbols)
        states = self.store.get_trigger_states(symbols)
        next_states: list[TriggerState] = []
        changes_1h: dict[str, Decimal] = {}
        alerts: list[StockAlert] = []
        self._startup_baseline_symbols_pending.intersection_update(by_symbol)
        startup_baseline = self._startup_baseline_pending
        if startup_baseline:
            self._startup_baseline_symbols_pending.update(by_symbol)
            self._startup_baseline_pending = False
        baseline_cycle = startup_baseline or bool(self._startup_baseline_symbols_pending)

        for symbol, quote in valid_quotes.items():
            instrument = by_symbol[symbol]
            prior = prior_changes.get(symbol, {})
            prior_observed_at = prior.get("observed_at")
            symbol_baseline = (
                symbol in self._startup_baseline_symbols_pending
                or prior_observed_at is None
                or now - int(prior_observed_at) > self.settings.quote_max_age_seconds
            )
            current_values: dict[Window, Decimal] = {"24h": quote.change_24h}
            reference = references.get(symbol)
            if reference is not None:
                current_values["1h"] = ((quote.price - reference) / reference) * Decimal(100)
                changes_1h[symbol] = current_values["1h"]

            for window, change in current_values.items():
                decision = advance_trigger_state(
                    states.get((symbol, window)),
                    symbol=symbol,
                    window=window,
                    change_pct=change,
                    silent_baseline=symbol_baseline,
                )
                next_states.append(decision.state)
                if decision.tier is None or decision.direction is None:
                    continue
                title, content = render_alert(
                    instrument=instrument,
                    window=window,
                    tier=decision.tier,
                    direction=decision.direction,
                    change_pct=change,
                    current_price=quote.price,
                )
                event_key = (
                    f"{symbol}:{window}:{decision.direction}:{decision.tier}:{quote.observed_at}"
                )
                alerts.append(
                    StockAlert(
                        event_key=event_key,
                        instrument=instrument,
                        window=window,
                        tier=decision.tier,
                        direction=decision.direction,
                        change_pct=change,
                        current_price=quote.price,
                        observed_at=quote.observed_at,
                        title=title,
                        content=content,
                    )
                )
            self._startup_baseline_symbols_pending.discard(symbol)

        self.store.record_cycle(
            quotes=valid_quotes,
            changes_1h=changes_1h,
            states=next_states,
            alerts=alerts,
            errors=errors,
            now=now,
            startup_baseline=baseline_cycle,
        )

        results: list[dict[str, Any]] = []
        for alert in alerts:
            result = self.push_client.push(
                title=alert.title,
                content=alert.content,
                dry_run=False,
                is_publish=alert.tier == "live",
                is_push=False,
                idempotency_key=f"stock-market:{alert.event_key}",
            )
            status = "published" if result.ok and alert.tier == "live" else (
                "backend_created" if result.ok else "push_failed"
            )
            self.store.mark_event(
                alert.event_key,
                status=status,
                error=result.error,
                now=int(time.time()),
            )
            results.append({
                "symbol": alert.instrument.symbol,
                "window": alert.window,
                "tier": alert.tier,
                "status": status,
                "title": alert.title,
                "error": result.error,
            })

        summary = {
            "status": "partial_error" if errors else "ok",
            "contract_count": len(instruments),
            "quoted_count": len(valid_quotes),
            "error_count": len(errors),
            "alert_count": len(alerts),
            "alerts": results,
            "startup_baseline": baseline_cycle,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        print(f"[stock-market] cycle {json.dumps(summary, ensure_ascii=False)}")
        return summary

    def run_forever(self) -> None:
        print(
            f"[stock-market] worker started db={self.settings.database_path} "
            f"poll_interval_seconds={self.settings.poll_interval_seconds}"
        )
        while True:
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                print(f"[stock-market] cycle failed error={exc}")
            elapsed = time.monotonic() - started
            time.sleep(max(1, self.settings.poll_interval_seconds - elapsed))
