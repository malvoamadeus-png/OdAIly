from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.jin10_monitor.fetcher import fetch_jin10_items
from packages.jin10_monitor.models import DEFAULT_JIN10_ENDPOINT_URL, DEFAULT_JIN10_HEADERS
from packages.publisher import PushClient
from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief

from .jin10_release import (
    JIN10_RELEASE_END_AFTER,
    JIN10_RELEASE_START_BEFORE,
    release_event_key,
    release_updates_from_items,
)
from .models import MacroUpdate
from .schedule import fetch_release_schedule
from .settings import MacroNewsflashSettings
from .store import MacroNewsflashStore


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

    def run_once(self, *, publish_latest: bool = False) -> dict[str, Any]:
        del publish_latest
        recovered = self.store.recover_inflight()
        now = datetime.now(UTC)
        releases = self.store.upcoming_releases()
        active_releases = self._active_jin10_releases(releases, now)
        calendar_errors: dict[str, str] = {}
        calendar_refreshed = False
        refresh_interval = timedelta(seconds=self.settings.quiet_poll_interval_seconds)

        if self.store.schedule_refresh_due(max_age=refresh_interval) and not active_releases:
            releases, calendar_errors, refreshed_sources = fetch_release_schedule(self.settings, now=now)
            self.store.replace_release_schedule(releases, refreshed_sources=refreshed_sources)
            self.store.record_schedule_refresh(errors=calendar_errors)
            calendar_refreshed = True
            releases = self.store.upcoming_releases()
            active_releases = self._active_jin10_releases(releases, now)

        updates: list[MacroUpdate] = []
        errors: dict[str, str] = {}
        if active_releases:
            try:
                items = fetch_jin10_items(
                    endpoint_url=DEFAULT_JIN10_ENDPOINT_URL,
                    headers=DEFAULT_JIN10_HEADERS,
                    channel=None,
                    timeout_seconds=self.settings.release_request_timeout_seconds,
                )
                for release, pending_metrics in active_releases:
                    updates.extend(
                        release_updates_from_items(
                            release,
                            items,
                            observed_at=now,
                            metric_ids=pending_metrics,
                        )
                    )
            except Exception as exc:
                errors["jin10_macro"] = str(exc)
            self.store.record_source_refresh(checked_sources={"jin10_macro"}, errors=errors)

        queued = 0
        unchanged = 0
        for update in updates:
            result = self.store.observe_update(
                update,
                title=update.headline(),
                content=self._format_content(update),
                publish_latest=True,
            )
            queued += result == "queued"
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
            "mode": "window" if active_releases else "quiet",
            "calendar_refreshed": calendar_refreshed,
            "active_releases": [release["release_id"] for release, _ in active_releases],
            "fetched": len(updates),
            "queued": queued,
            "unchanged": unchanged,
            "published": published,
            "uncertain": uncertain,
            "recovered_inflight": recovered,
            "source_errors": errors,
            "calendar_errors": calendar_errors,
        }

    def run_forever(self, *, publish_latest: bool = False) -> None:
        self.store.prune_source_errors(allowed_sources={"jin10_macro"})
        print(
            "[macro-newsflash] worker started "
            f"window_poll={self.settings.release_poll_interval_seconds}s "
            f"calendar_confirm={self.settings.quiet_poll_interval_seconds}s "
            f"release_request_timeout={self.settings.release_request_timeout_seconds}s"
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = self.run_once(publish_latest=publish_latest)
                print("[macro-newsflash] " + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            except Exception as exc:
                print(f"[macro-newsflash] round failed: {exc}")
            now_utc = datetime.now(UTC)
            interval = self._poll_interval(now_utc)
            if interval <= float(self.settings.release_poll_interval_seconds):
                remaining = max(0.0, interval - (time.monotonic() - started))
            else:
                remaining = interval
            self._stop.wait(remaining)

    def _poll_interval(self, now_utc: datetime) -> float:
        releases = self.store.upcoming_releases()
        if self._active_jin10_releases(releases, now_utc):
            return float(self.settings.release_poll_interval_seconds)
        next_start = self._next_jin10_tracking_start(releases, now_utc)
        next_calendar = self.store.next_schedule_refresh_at(
            max_age=timedelta(seconds=self.settings.quiet_poll_interval_seconds)
        )
        targets = [target for target in (next_start, next_calendar) if target is not None]
        if not targets:
            return float(self.settings.quiet_poll_interval_seconds)
        seconds_until_next = max(0.1, (min(targets) - now_utc).total_seconds())
        return min(float(self.settings.quiet_poll_interval_seconds), seconds_until_next)

    def _active_jin10_releases(
        self,
        releases: list[dict[str, Any]],
        now_utc: datetime,
    ) -> list[tuple[dict[str, Any], list[str]]]:
        active: list[tuple[dict[str, Any], list[str]]] = []
        for release in releases:
            scheduled_at = _parse_scheduled_at(release["scheduled_at"])
            delta = scheduled_at - now_utc
            if not -JIN10_RELEASE_END_AFTER <= delta <= JIN10_RELEASE_START_BEFORE:
                continue
            pending_metrics = self._pending_metrics(release)
            if pending_metrics:
                active.append((release, pending_metrics))
        return active

    def _next_jin10_tracking_start(
        self,
        releases: list[dict[str, Any]],
        now_utc: datetime,
    ) -> datetime | None:
        starts: list[datetime] = []
        for release in releases:
            if not self._pending_metrics(release):
                continue
            scheduled_at = _parse_scheduled_at(release["scheduled_at"])
            start_at = scheduled_at - JIN10_RELEASE_START_BEFORE
            if start_at > now_utc:
                starts.append(start_at)
        return min(starts) if starts else None

    def _pending_metrics(self, release: dict[str, Any]) -> list[str]:
        return [
            metric_id
            for metric_id in release.get("metric_ids") or ()
            if not self.store.event_exists(release_event_key(release, metric_id))
        ]

    @staticmethod
    def _format_content(update: MacroUpdate) -> str:
        body = f"据Odaily监测，{update.headline()}。"
        formatted = format_brief(DraftBrief(title=update.headline(), content=body))
        return formatted.content


def _parse_scheduled_at(raw: str) -> datetime:
    scheduled_at = datetime.fromisoformat(raw)
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    return scheduled_at.astimezone(UTC)
