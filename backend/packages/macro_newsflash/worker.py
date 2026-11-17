from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, time as wall_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from packages.publisher import PushClient
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief

from .models import MacroUpdate
from .schedule import fetch_release_schedule
from .settings import MacroNewsflashSettings
from .sources import collect_updates
from .store import MacroNewsflashStore


EASTERN_TZ = ZoneInfo("America/New_York")


class MacroNewsflashWorker:
    def __init__(
        self,
        *,
        settings: MacroNewsflashSettings,
        store: MacroNewsflashStore | None = None,
        push_client: PushClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or MacroNewsflashStore(settings.database_path)
        self.store.initialize()
        self.push_client = push_client or PushClient(
            endpoint=settings.push_endpoint,
            timeout_seconds=settings.push_timeout_seconds,
            max_attempts=1,
            backoff_seconds=0,
        )
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_once(
        self,
        *,
        publish_latest: bool = False,
        source_names: set[str] | None = None,
    ) -> dict[str, Any]:
        recovered = self.store.recover_inflight()
        updates, errors = collect_updates(self.settings, source_names=source_names)
        checked_sources = (
            {"bls", "retail_sales", "initial_claims", "pce", "fomc"}
            if source_names is None
            else source_names
        )
        self.store.record_source_refresh(checked_sources=checked_sources, errors=errors)
        calendar_errors: dict[str, str] = {}
        if self.store.schedule_refresh_due():
            releases, calendar_errors, refreshed_sources = fetch_release_schedule(self.settings)
            self.store.replace_release_schedule(releases, refreshed_sources=refreshed_sources)
            self.store.record_schedule_refresh(errors=calendar_errors)
        queued = 0
        baselined = 0
        unchanged = 0
        for update in updates:
            result = self.store.observe_update(
                update,
                title=update.headline(),
                content=self._format_content(update),
                publish_latest=publish_latest,
            )
            queued += result == "queued"
            baselined += result == "baselined"
            unchanged += result == "unchanged"
        pending = self.store.claim_pending()
        published = 0
        uncertain = 0
        for event in pending:
            result = self.push_client.push(
                title=event["title"],
                content=event["content"],
                source_url=event["source_url"],
                dry_run=False,
                is_publish=False,
                is_push=False,
                idempotency_key=event["event_key"],
            )
            self.store.finish_delivery(
                event_key=event["event_key"],
                ok=result.ok,
                error=result.error,
            )
            published += result.ok
            uncertain += not result.ok
        return {
            "fetched": len(updates),
            "queued": queued,
            "baselined": baselined,
            "unchanged": unchanged,
            "published": published,
            "uncertain": uncertain,
            "recovered_inflight": recovered,
            "source_errors": errors,
            "calendar_errors": calendar_errors,
        }

    def run_forever(self, *, publish_latest: bool = False) -> None:
        first = True
        initial_poll_pending = True
        startup_retry_sources: set[str] = set()
        print(
            "[macro-newsflash] worker started "
            f"active_poll={self.settings.poll_interval_seconds}s "
            f"quiet_poll={self.settings.quiet_poll_interval_seconds}s"
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                sources = (
                    None
                    if initial_poll_pending
                    else startup_retry_sources or self._sources_due(datetime.now(EASTERN_TZ))
                )
                result = self.run_once(
                    publish_latest=publish_latest and first,
                    source_names=sources,
                )
                print("[macro-newsflash] " + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
                if initial_poll_pending:
                    startup_retry_sources = set(result["source_errors"])
                    initial_poll_pending = False
                elif startup_retry_sources:
                    startup_retry_sources = (
                        startup_retry_sources - set(sources or ())
                    ) | set(result["source_errors"])
            except Exception as exc:
                print(f"[macro-newsflash] round failed: {exc}")
            first = False
            interval = self._poll_interval(datetime.now(EASTERN_TZ))
            remaining = max(0.0, interval - (time.monotonic() - started))
            self._stop.wait(remaining)

    def _poll_interval(self, now: datetime) -> int:
        if self._sources_due(now):
            return self.settings.poll_interval_seconds
        return self.settings.quiet_poll_interval_seconds

    def _sources_due(self, now: datetime) -> set[str]:
        now_utc = now.astimezone(UTC)
        source_names: set[str] = set()
        metric_sources = {
            "nonfarm_payrolls": "bls",
            "unemployment_rate": "bls",
            "cpi_yoy_nsa": "bls",
            "cpi_mom_nsa": "bls",
            "cpi_mom_sa": "bls",
            "core_cpi_mom_sa": "bls",
            "retail_sales_mom": "retail_sales",
            "initial_claims": "initial_claims",
            "pce_yoy": "pce",
            "fomc_rate": "fomc",
        }
        for release in self.store.upcoming_releases():
            scheduled_at = datetime.fromisoformat(release["scheduled_at"]).astimezone(UTC)
            delta = scheduled_at - now_utc
            if -timedelta(hours=3) <= delta <= timedelta(hours=2):
                source_names.update(
                    metric_sources[metric_id]
                    for metric_id in release["metric_ids"]
                    if metric_id in metric_sources
                )
        if source_names:
            return source_names
        if now.weekday() < 5:
            local_time = now.timetz().replace(tzinfo=None)
            if wall_time(8, 20) <= local_time <= wall_time(10, 30):
                source_names.update({"bls", "retail_sales", "pce"})
                if now.weekday() == 3:
                    source_names.add("initial_claims")
            if wall_time(13, 50) <= local_time <= wall_time(15, 30):
                source_names.add("fomc")
        return source_names

    @staticmethod
    def _format_content(update: MacroUpdate) -> str:
        body = f"据Odaily监测，{update.headline()}。"
        formatted = format_brief(DraftBrief(title=update.headline(), content=body))
        return formatted.content
