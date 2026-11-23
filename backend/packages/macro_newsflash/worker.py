from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, time as wall_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from packages.publisher import PushClient
from packages.jin10_monitor.fetcher import fetch_jin10_items
from packages.jin10_monitor.models import DEFAULT_JIN10_ENDPOINT_URL, DEFAULT_JIN10_HEADERS
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief

from .jin10_release import (
    JIN10_RELEASE_END_AFTER,
    JIN10_RELEASE_START_BEFORE,
    release_update_from_items,
)
from .models import MacroUpdate
from .schedule import fetch_release_schedule
from .settings import MacroNewsflashSettings
from .sources import collect_updates
from .store import MacroNewsflashStore


EASTERN_TZ = ZoneInfo("America/New_York")
JIN10_TRACKED_METRICS = {"initial_claims", "pce_yoy"}
JIN10_SOURCE_NAMES = {"initial_claims", "pce"}
INITIAL_NON_JIN10_SOURCES = {"bls", "retail_sales", "fomc"}


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
        request_timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        settings = (
            replace(self.settings, request_timeout_seconds=request_timeout_seconds)
            if request_timeout_seconds is not None
            else self.settings
        )
        recovered = self.store.recover_inflight()
        now = datetime.now(UTC)
        releases = self.store.upcoming_releases()
        active_releases = self._active_jin10_releases(releases, now)
        calendar_errors: dict[str, str] = {}
        if self.store.schedule_refresh_due() and not active_releases:
            releases, calendar_errors, refreshed_sources = fetch_release_schedule(settings)
            self.store.replace_release_schedule(releases, refreshed_sources=refreshed_sources)
            self.store.record_schedule_refresh(errors=calendar_errors)
            releases = self.store.upcoming_releases()
            active_releases = self._active_jin10_releases(releases, now)

        requested_sources = (
            INITIAL_NON_JIN10_SOURCES | JIN10_SOURCE_NAMES
            if source_names is None
            else set(source_names)
        )
        macro_sources = requested_sources - JIN10_SOURCE_NAMES
        updates, errors = collect_updates(settings, source_names=macro_sources)
        checked_sources = set(macro_sources)
        if active_releases:
            checked_sources.add("jin10_macro")
            active_metrics = {
                metric_id
                for release in active_releases
                for metric_id in release["metric_ids"]
            }
            if "initial_claims" in active_metrics:
                checked_sources.add("initial_claims")
            if "pce_yoy" in active_metrics:
                checked_sources.add("pce")
            try:
                items = fetch_jin10_items(
                    endpoint_url=DEFAULT_JIN10_ENDPOINT_URL,
                    headers=DEFAULT_JIN10_HEADERS,
                    channel=None,
                    timeout_seconds=settings.release_request_timeout_seconds,
                )
                for release in active_releases:
                    update = release_update_from_items(release, items, observed_at=now)
                    if update is not None:
                        updates.append(update)
            except Exception as exc:
                errors["jin10_macro"] = str(exc)
        self.store.record_source_refresh(checked_sources=checked_sources, errors=errors)
        queued = 0
        baselined = 0
        unchanged = 0
        for update in updates:
            result = self.store.observe_update(
                update,
                title=update.headline(),
                content=self._format_content(update),
                publish_latest=publish_latest or bool(update.raw_payload.get("jin10_release_id")),
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
            f"release_poll={self.settings.release_poll_interval_seconds}s "
            f"release_request_timeout={self.settings.release_request_timeout_seconds}s "
            f"quiet_poll={self.settings.quiet_poll_interval_seconds}s"
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                now = datetime.now(EASTERN_TZ)
                fast_poll = self._fast_poll_due(now)
                due_sources = self._sources_due(now)
                sources = (
                    None
                    if initial_poll_pending
                    else startup_retry_sources | due_sources
                )
                result = self.run_once(
                    publish_latest=publish_latest and first,
                    source_names=sources,
                    request_timeout_seconds=(
                        self.settings.release_request_timeout_seconds if fast_poll else None
                    ),
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

    def _poll_interval(self, now: datetime) -> float:
        now_utc = now.astimezone(UTC)
        releases = self.store.upcoming_releases()
        if self._active_jin10_releases(releases, now_utc):
            return 1
        if self._fast_poll_due(now):
            return self.settings.release_poll_interval_seconds
        if self._sources_due(now) - JIN10_SOURCE_NAMES:
            return self.settings.poll_interval_seconds
        next_start = self._next_jin10_tracking_start(releases, now_utc)
        if next_start is None:
            return self.settings.quiet_poll_interval_seconds
        seconds_until_start = max(0.1, (next_start - now_utc).total_seconds())
        return min(self.settings.quiet_poll_interval_seconds, seconds_until_start)

    def _fast_poll_due(self, now: datetime) -> bool:
        now_utc = now.astimezone(UTC)
        for release in self.store.upcoming_releases():
            if JIN10_TRACKED_METRICS.intersection(release["metric_ids"]):
                continue
            scheduled_at = datetime.fromisoformat(release["scheduled_at"]).astimezone(UTC)
            delta = scheduled_at - now_utc
            if -timedelta(minutes=2) <= delta <= timedelta(minutes=15):
                return True
        if now.weekday() >= 5:
            return False
        local_time = now.timetz().replace(tzinfo=None)
        return (
            wall_time(8, 28) <= local_time <= wall_time(8, 45)
            or wall_time(13, 58) <= local_time <= wall_time(14, 15)
        )

    def _active_jin10_releases(
        self,
        releases: list[dict[str, Any]],
        now_utc: datetime,
    ) -> list[dict[str, Any]]:
        active: list[dict[str, Any]] = []
        for release in releases:
            if not JIN10_TRACKED_METRICS.intersection(release["metric_ids"]):
                continue
            if self.store.event_exists(f"jin10:{release['release_id']}"):
                continue
            scheduled_at = datetime.fromisoformat(release["scheduled_at"])
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            scheduled_at = scheduled_at.astimezone(UTC)
            delta = scheduled_at - now_utc
            if -JIN10_RELEASE_END_AFTER <= delta <= JIN10_RELEASE_START_BEFORE:
                active.append(release)
        return active

    def _next_jin10_tracking_start(
        self,
        releases: list[dict[str, Any]],
        now_utc: datetime,
    ) -> datetime | None:
        starts: list[datetime] = []
        for release in releases:
            if not JIN10_TRACKED_METRICS.intersection(release["metric_ids"]):
                continue
            if self.store.event_exists(f"jin10:{release['release_id']}"):
                continue
            scheduled_at = datetime.fromisoformat(release["scheduled_at"])
            if scheduled_at.tzinfo is None:
                scheduled_at = scheduled_at.replace(tzinfo=UTC)
            start_at = scheduled_at.astimezone(UTC) - JIN10_RELEASE_START_BEFORE
            if start_at > now_utc:
                starts.append(start_at)
        return min(starts) if starts else None

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
