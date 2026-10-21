from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from typing import Any

from packages.publisher import PushClient, PushResult

from .aggregation import advance_broadcast
from .client import BinanceStockMarketClient
from .copy import render_digest
from .models import StockInstrument, StockQuote, Window
from .settings import StockMarketSettings
from .store import StockMarketStore
from .theme_classifier import StockThemeClassifier


class StockMarketBroadcastService:
    def __init__(
        self,
        *,
        settings: StockMarketSettings,
        store: StockMarketStore | None = None,
        client: BinanceStockMarketClient | None = None,
        push_client: PushClient | None = None,
        theme_classifier: StockThemeClassifier | None = None,
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
        self.theme_classifier = theme_classifier
        if self.theme_classifier is None and settings.theme_classification_enabled:
            self.theme_classifier = StockThemeClassifier.from_environment()
        self._startup_baseline_pending = True
        self._baseline_symbols_pending: set[str] = set()

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

        references = self.store.one_hour_references(valid_quotes)
        changes_1h: dict[str, Decimal] = {}
        changes: dict[str, dict[Window, Decimal]] = {}
        for symbol, quote in valid_quotes.items():
            changes[symbol] = {"24h": quote.change_24h}
            reference = references.get(symbol)
            if reference is not None:
                change = ((quote.price - reference) / reference) * Decimal(100)
                changes_1h[symbol] = change
                changes[symbol]["1h"] = change
        state = self.store.broadcast_state()
        active_symbols = {instrument.symbol for instrument in instruments}
        self._baseline_symbols_pending.intersection_update(active_symbols)
        if self._startup_baseline_pending:
            self._baseline_symbols_pending.update(active_symbols)
        alerts, baseline_count = advance_broadcast(
            state, instruments=instruments, quotes=valid_quotes, changes=changes, now=now,
            restart=self._startup_baseline_pending, max_gap_seconds=self.settings.quote_max_age_seconds,
            baseline_symbols=self._baseline_symbols_pending,
        )
        classified_alerts = []
        for alert in alerts:
            if self.theme_classifier is None or alert.market != "us" or len(alert.members) < 2:
                classified_alerts.append(alert)
                continue
            theme = self.theme_classifier.classify(
                tuple(member.instrument.asset_name for member in alert.members)
            )
            if theme is None:
                classified_alerts.append(alert)
                continue
            title, content = render_digest(
                market=alert.market,
                direction=alert.direction,
                members=alert.members,
                breadth={},
                theme=theme,
            )
            classified_alerts.append(replace(alert, title=title, content=content))
        alerts = classified_alerts
        baseline_cycle = bool(valid_quotes) and baseline_count == len(valid_quotes)
        claimed = self.store.record_cycle(
            quotes=valid_quotes,
            changes_1h=changes_1h,
            state=state,
            alerts=alerts,
            errors=errors,
            now=now,
            startup_baseline=baseline_cycle,
        )

        self._startup_baseline_pending = False
        self._baseline_symbols_pending.difference_update(valid_quotes)
        results: list[dict[str, Any]] = []
        for alert in claimed:
            try:
                result = self.push_client.push(
                    title=alert.title, content=alert.content, dry_run=False,
                    is_publish=alert.tier == "live", is_push=False,
                    idempotency_key=f"stock-market:{alert.event_key}",
                    origin_key="stock_market",
                    priority_status="not_applicable",
                )
            except Exception as exc:
                # The endpoint has no reliable idempotency contract. Consume a
                # failed/uncertain request rather than automatically retry it.
                result = PushResult(ok=False, error=str(exc))
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
                "assets": [member.instrument.asset_name for member in alert.members],
                "market": alert.market,
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
            "alert_count": len(claimed),
            "pending_count": sum(len(group.pending) for group in state.groups.values()),
            "baseline_count": baseline_count,
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
