from __future__ import annotations

import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.heartbeat import HeartbeatThrottle
from packages.common.source_exclusions import SourceExclusionMatcher
from packages.common.freshness import (
    DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
    ensure_utc,
    evaluate_source_freshness,
)
from packages.local_pipeline.client import LocalPipelineClient

from .client import FXTwitterClient
from .models import CaptureRunStats, TweetCandidate, XCaptureAccount, XCaptureSettings
from .repository import XCaptureRepository, utc_now


@dataclass(slots=True)
class WorkerSnapshot:
    settings: XCaptureSettings
    accounts: list[XCaptureAccount]


class XCaptureWorker:
    def __init__(
        self,
        *,
        repository: XCaptureRepository,
        client: FXTwitterClient | None = None,
        config_reload_interval_seconds: float = 300.0,
        timeline_count: int = 20,
        attempt_retention_days: int = 3,
        attempt_prune_interval_seconds: float = 3600.0,
        freshness_window_seconds: int = DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
        pipeline_client: LocalPipelineClient | None = None,
        exclusion_matcher: SourceExclusionMatcher | None = None,
    ) -> None:
        self.repository = repository
        self.client = client or FXTwitterClient()
        self.config_reload_interval_seconds = max(30.0, float(config_reload_interval_seconds))
        self.timeline_count = timeline_count
        self.attempt_retention_days = max(1, int(attempt_retention_days))
        self.attempt_prune_interval_seconds = max(60.0, float(attempt_prune_interval_seconds))
        self.freshness_window_seconds = max(1, int(freshness_window_seconds))
        self.pipeline_client = pipeline_client
        self.exclusion_matcher = exclusion_matcher
        self._stop_event = threading.Event()
        self._config_changed = threading.Event()
        self._wake_event = threading.Event()
        self._next_due: dict[str, float] = {}
        self._snapshot = WorkerSnapshot(XCaptureSettings(), [])
        self._last_attempt_prune_monotonic: float | None = None
        self._last_snapshot_loaded_monotonic: float | None = None
        self.worker_id = f"x_capture-{os.getpid()}"
        self._heartbeat = HeartbeatThrottle(
            component="x_capture",
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

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def request_config_reload(self) -> None:
        self._signal_config_changed()

    def _signal_config_changed(self) -> None:
        self._config_changed.set()
        self._wake_event.set()

    def load_snapshot(self) -> WorkerSnapshot:
        settings = self.repository.get_settings()
        accounts = self.repository.list_accounts(include_disabled=False)
        self._snapshot = WorkerSnapshot(settings, accounts)
        self._last_snapshot_loaded_monotonic = time.monotonic()
        known = {account.username_lower for account in accounts}
        for username in list(self._next_due):
            if username not in known:
                del self._next_due[username]
        now = time.monotonic()
        for account in accounts:
            self._next_due.setdefault(account.username_lower, now + random.uniform(0, settings.jitter_seconds))
        return self._snapshot

    def run_once(self) -> list[CaptureRunStats]:
        try:
            self._prune_attempts_if_due(force=True)
            snapshot = self.load_snapshot()
            stats = self._process_accounts(snapshot.accounts, snapshot.settings)
            failed = [item for item in stats if item.status != "success"]
            self._record_heartbeat(
                success=not failed,
                error="; ".join(item.error or item.status for item in failed) if failed else None,
                metadata={
                    "accounts": len(snapshot.accounts),
                    "processed_accounts": len(stats),
                    "failed_accounts": len(failed),
                    "saved_count": sum(item.saved_count for item in stats),
                },
            )
            return stats
        except Exception as exc:
            self._record_heartbeat(success=False, error=str(exc), metadata={})
            raise

    def run_forever(self) -> None:
        snapshot = self.load_snapshot()
        self._prune_attempts_if_due(force=True)
        print(
            "[odaily] x-capture worker started. "
            f"accounts={len(snapshot.accounts)} interval={snapshot.settings.global_interval_seconds}s "
            f"config_reload_interval={int(self.config_reload_interval_seconds)}s"
        )

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._config_changed.is_set() or self._snapshot_reload_due(now):
                    self._config_changed.clear()
                    snapshot = self.load_snapshot()
                    print(f"[odaily] x-capture config loaded. accounts={len(snapshot.accounts)}")

                self._prune_attempts_if_due()
                due = [account for account in snapshot.accounts if self._next_due.get(account.username_lower, 0) <= now]
                if due:
                    stats = self._process_accounts(due, snapshot.settings)
                    failed = [item for item in stats if item.status != "success"]
                    self._record_heartbeat(
                        success=not failed,
                        error="; ".join(item.error or item.status for item in failed) if failed else None,
                        metadata={
                            "accounts": len(snapshot.accounts),
                            "processed_accounts": len(stats),
                            "failed_accounts": len(failed),
                            "saved_count": sum(item.saved_count for item in stats),
                        },
                    )
                    for item in stats:
                        interval = item.account.effective_interval_seconds(snapshot.settings)
                        self._next_due[item.account.username_lower] = (
                            time.monotonic() + interval + random.uniform(0, snapshot.settings.jitter_seconds)
                        )

                sleep_for = self._sleep_seconds(snapshot)
                if not due:
                    self._record_heartbeat(
                        success=True,
                        error=None,
                        metadata={
                            "accounts": len(snapshot.accounts),
                            "processed_accounts": 0,
                            "failed_accounts": 0,
                            "saved_count": 0,
                        },
                    )
                self._wake_event.wait(sleep_for)
                self._wake_event.clear()
        finally:
            self.stop()

    def _record_heartbeat(self, *, success: bool, error: str | None, metadata: dict[str, Any]) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self._heartbeat.send(
                status="ok" if success else "failed",
                success=success,
                error=error,
                metadata=metadata,
            )
        except Exception as exc:
            print(f"[odaily] x-capture heartbeat failed: {exc}")

    def _prune_attempts_if_due(self, *, force: bool = False) -> int:
        now = time.monotonic()
        if (
            not force
            and self._last_attempt_prune_monotonic is not None
            and now - self._last_attempt_prune_monotonic < self.attempt_prune_interval_seconds
        ):
            return 0
        self._last_attempt_prune_monotonic = now
        cutoff = utc_now() - timedelta(days=self.attempt_retention_days)
        try:
            removed = self.repository.prune_attempts_before(cutoff)
        except Exception as exc:
            print(f"[odaily] x-capture prune attempts failed: {exc}")
            return 0
        if removed:
            print(
                "[odaily] x-capture pruned attempts "
                f"removed={removed} retention_days={self.attempt_retention_days}"
            )
        return removed

    def _sleep_seconds(self, snapshot: WorkerSnapshot) -> float:
        now = time.monotonic()
        reload_due = self._seconds_until_snapshot_reload(now)
        if not snapshot.accounts:
            return max(0.5, min(60.0, reload_due))
        next_due = min(self._next_due.get(account.username_lower, now + 5.0) for account in snapshot.accounts)
        return max(0.5, min(60.0, next_due - now, reload_due))

    def _snapshot_reload_due(self, now: float) -> bool:
        return self._seconds_until_snapshot_reload(now) <= 0.0

    def _seconds_until_snapshot_reload(self, now: float) -> float:
        if self._last_snapshot_loaded_monotonic is None:
            return 0.0
        deadline = self._last_snapshot_loaded_monotonic + self.config_reload_interval_seconds
        return deadline - now

    def _process_accounts(
        self,
        accounts: list[XCaptureAccount],
        settings: XCaptureSettings,
    ) -> list[CaptureRunStats]:
        if not accounts:
            return []
        stats: list[CaptureRunStats] = []
        max_workers = max(1, min(settings.max_concurrency, len(accounts)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self.process_account, account) for account in accounts]
            for future in as_completed(futures):
                item = future.result()
                stats.append(item)
                print(
                    "[odaily] x-capture "
                    f"account={item.account.username} status={item.status} "
                    f"candidates={item.candidate_count} seeded={item.seeded_count} "
                    f"new={item.new_count} saved={item.saved_count} error={item.error or '-'}"
                )
        return stats

    def process_account(self, account: XCaptureAccount) -> CaptureRunStats:
        started_at = utc_now()
        try:
            stats = self._process_account_inner(account, scheduled_at=started_at)
        except Exception as exc:
            stats = CaptureRunStats(account=account, status="fetch_failed", error=str(exc))
        finished_at = utc_now()
        self.repository.record_attempt(stats, started_at=started_at, finished_at=finished_at)
        return stats

    def _process_account_inner(
        self,
        account: XCaptureAccount,
        *,
        scheduled_at: datetime | None = None,
    ) -> CaptureRunStats:
        scheduled_at = scheduled_at or utc_now()
        candidates, attempt = self.client.fetch_timeline(account.username, count=self.timeline_count)
        if attempt.status != "success":
            return CaptureRunStats(
                account=account,
                status=attempt.status,
                candidate_count=attempt.candidate_count,
                error=attempt.error,
                metadata={"attempt_url": attempt.url, "source": attempt.source},
            )

        if not candidates:
            return CaptureRunStats(account=account, status="parse_empty", error="no candidates")

        if account.seeded_at is None:
            tweet_ids = [candidate.tweet_id for candidate in candidates]
            self.repository.mark_account_seeded(account, tweet_ids)
            return CaptureRunStats(
                account=account,
                status="success",
                candidate_count=len(candidates),
                seeded_count=len(tweet_ids),
                metadata={"first_seen_seed": True, "attempt_url": attempt.url},
            )

        unseen = self.repository.unseen_tweet_ids([candidate.tweet_id for candidate in candidates])
        saved_count = 0
        new_count = 0
        ignored_stale_count = 0
        ignored_stale_tweet_ids: list[str] = []
        detail_errors: dict[str, str] = {}
        for candidate in candidates:
            if candidate.tweet_id not in unseen:
                continue
            record = self._record_from_candidate(account, candidate, detail_errors)
            if not is_fresh_record(
                record.created_at,
                scheduled_at,
                window_seconds=self.freshness_window_seconds,
            ):
                if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                    ignored_stale_count += 1
                    ignored_stale_tweet_ids.append(candidate.tweet_id)
                continue
            exclusion_scopes = ["x"]
            if account.is_ai_source:
                exclusion_scopes.append("ai_source")
            if self.exclusion_matcher is not None and self.exclusion_matcher.is_excluded(
                scopes=exclusion_scopes,
                texts=[record.text],
            ):
                self.repository.mark_seen(account, candidate.tweet_id, seeded=False)
                continue
            task_id = self.repository.save_task(account, record)
            if task_id is None:
                if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                    new_count += 1
                continue
            try:
                self._submit_pipeline_job(task_id=task_id, source="x", source_item_id=record.tweet_id)
            except Exception as exc:
                detail_errors[f"pipeline:{candidate.tweet_id}"] = str(exc)
                continue
            if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                new_count += 1
                saved_count += 1

        return CaptureRunStats(
            account=account,
            status="success",
            candidate_count=len(candidates),
            new_count=new_count,
            saved_count=saved_count,
            metadata={
                "attempt_url": attempt.url,
                "detail_errors": detail_errors,
                "freshness_window_seconds": self.freshness_window_seconds,
                "ignored_stale_count": ignored_stale_count,
                "ignored_stale_tweet_ids": ignored_stale_tweet_ids,
            },
        )

    def _submit_pipeline_job(self, *, task_id: int, source: str, source_item_id: str) -> None:
        if self.pipeline_client is None:
            return
        self.pipeline_client.submit_job(
            job_type="write_flow",
            task_id=task_id,
            source=source,
            source_item_id=source_item_id,
        )

    def _record_from_candidate(
        self,
        account: XCaptureAccount,
        candidate: TweetCandidate,
        detail_errors: dict[str, str],
    ):
        detail: dict[str, Any] = {}
        detail_error: str | None = None
        try:
            detail = self.client.fetch_detail(account.username, candidate.tweet_id)
        except Exception as exc:
            detail_error = str(exc)
            detail_errors[candidate.tweet_id] = detail_error
        return self.client.build_record(account.username, candidate, detail=detail, detail_error=detail_error)

def parse_record_created_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def is_fresh_record(
    created_at: str | None,
    scheduled_at: datetime,
    *,
    window_seconds: int = DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
) -> bool:
    parsed = parse_record_created_at(created_at)
    if parsed is not None:
        parsed = ensure_utc(parsed)
    check = evaluate_source_freshness(
        parsed,
        reference_time=scheduled_at,
        window_seconds=window_seconds,
    )
    if check.delay_seconds is not None and check.delay_seconds < 0:
        return abs(check.delay_seconds) <= max(1, int(window_seconds))
    return check.is_fresh
