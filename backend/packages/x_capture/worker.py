from __future__ import annotations

import inspect
import os
import queue
import random
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.freshness import (
    DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
    ensure_utc,
    evaluate_source_freshness,
)
from packages.common.heartbeat import HeartbeatThrottle
from packages.common.source_exclusions import SourceExclusionMatcher
from packages.local_pipeline.client import LocalPipelineClient

from .client import FXTwitterClient, has_context_reference
from .models import CaptureRunStats, TimelineAttempt, TweetCandidate, XCaptureAccount, XCaptureSettings
from .repository import XCaptureRepository, utc_now


KNOWN_SEEN_CACHE_CAPACITY = 100_000
HEARTBEAT_WINDOW_SECONDS = 60.0


class CollectRequestPacer:
    """Space FXTwitter request starts across concurrent polling threads."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0.0:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                delay = self._next_request_at - now
                if delay <= 0.0:
                    self._next_request_at = now + self.min_interval_seconds
                    return
            time.sleep(min(delay, 0.25))


@dataclass(slots=True)
class WorkerSnapshot:
    settings: XCaptureSettings
    accounts: list[XCaptureAccount]


def _client_supports_before_request(client: Any) -> bool:
    fetch = getattr(client, "fetch_timeline", None)
    if fetch is None:
        return False
    try:
        params = inspect.signature(fetch).parameters
    except (TypeError, ValueError):
        return False
    return "before_request" in params or any(
        param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
    )


def _heartbeat_state(
    *,
    processed: int,
    succeeded: int,
    failed: int,
    errors: Sequence[str],
) -> tuple[str, bool, str | None]:
    if processed <= 0 or failed == 0:
        return "ok", True, None
    error_summary = "; ".join(errors[:5]) or "partial capture failure"
    if succeeded > 0:
        return "degraded", True, error_summary
    return "failed", False, error_summary


class XCaptureWorker:
    def __init__(
        self,
        *,
        repository: XCaptureRepository,
        client: FXTwitterClient | None = None,
        config_reload_interval_seconds: float = 30.0,
        timeline_count: int = 20,
        attempt_retention_days: int = 1,
        attempt_prune_interval_seconds: float = 60.0,
        freshness_window_seconds: int = DEFAULT_PROCESSING_FRESHNESS_WINDOW_SECONDS,
        pipeline_client: LocalPipelineClient | None = None,
        exclusion_matcher: SourceExclusionMatcher | None = None,
        min_request_interval_seconds: float = 0.05,
        detail_workers: int = 8,
    ) -> None:
        self.repository = repository
        self.client = client or FXTwitterClient()
        self.config_reload_interval_seconds = max(5.0, float(config_reload_interval_seconds))
        self.timeline_count = timeline_count
        self.attempt_retention_days = max(1, int(attempt_retention_days))
        self.attempt_prune_interval_seconds = max(60.0, float(attempt_prune_interval_seconds))
        self.freshness_window_seconds = max(1, int(freshness_window_seconds))
        self.pipeline_client = pipeline_client
        self.exclusion_matcher = exclusion_matcher
        self.detail_workers = max(1, int(detail_workers))
        self._request_pacer = CollectRequestPacer(min_request_interval_seconds)
        self._client_accepts_before_request = _client_supports_before_request(self.client)
        self._stop_event = threading.Event()
        self._config_changed = threading.Event()
        self._wake_event = threading.Event()
        self._next_due: dict[str, float] = {}
        self._snapshot = WorkerSnapshot(XCaptureSettings(), [])
        self._last_attempt_prune_monotonic: float | None = None
        self._last_snapshot_loaded_monotonic: float | None = None
        self._state_lock = threading.Lock()
        self._known_seen: OrderedDict[str, None] = OrderedDict()
        self._inflight_tweet_ids: set[str] = set()
        self._seen_preloaded = False
        self.worker_id = f"x_capture-{os.getpid()}"
        self._heartbeat = HeartbeatThrottle(
            component="x_capture",
            worker_id=self.worker_id,
            interval_seconds=HEARTBEAT_WINDOW_SECONDS,
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

    def _remember_seen(self, tweet_ids: Iterable[str]) -> None:
        with self._state_lock:
            for tweet_id in tweet_ids:
                if not tweet_id:
                    continue
                if tweet_id in self._known_seen:
                    self._known_seen.move_to_end(tweet_id)
                else:
                    self._known_seen[tweet_id] = None
            while len(self._known_seen) > KNOWN_SEEN_CACHE_CAPACITY:
                self._known_seen.popitem(last=False)

    def _reserve_unknown_tweet_ids(self, tweet_ids: list[str]) -> list[str]:
        unknown: list[str] = []
        with self._state_lock:
            for tweet_id in tweet_ids:
                if tweet_id in self._known_seen or tweet_id in self._inflight_tweet_ids:
                    continue
                if tweet_id not in unknown:
                    unknown.append(tweet_id)
            self._inflight_tweet_ids.update(unknown)
        return unknown

    def _release_inflight_tweets(self, tweet_ids: Iterable[str]) -> None:
        with self._state_lock:
            self._inflight_tweet_ids.difference_update(tweet_ids)

    def _preload_seen_cache(self) -> None:
        if self._seen_preloaded:
            return
        self._seen_preloaded = True
        loader = getattr(self.repository, "list_recent_seen_tweet_ids", None)
        if loader is None:
            return
        try:
            recent = loader(limit=KNOWN_SEEN_CACHE_CAPACITY)
        except Exception as exc:
            print(f"[odaily] x-capture preload seen cache failed: {exc}")
            return
        self._remember_seen(reversed(recent))

    def load_snapshot(self) -> WorkerSnapshot:
        settings = self.repository.get_settings()
        accounts = self.repository.list_accounts(include_disabled=False)
        self._preload_seen_cache()
        self._snapshot = WorkerSnapshot(settings, accounts)
        self._last_snapshot_loaded_monotonic = time.monotonic()
        known = {account.username_lower for account in accounts}
        for username in list(self._next_due):
            if username not in known:
                del self._next_due[username]
        now = time.monotonic()
        for account in accounts:
            interval = account.effective_interval_seconds(settings)
            jitter_cap = max(0, int(settings.jitter_seconds))
            if account.username_lower not in self._next_due:
                jitter = random.uniform(0, jitter_cap) if jitter_cap > 0 else 0.0
                self._next_due[account.username_lower] = now + jitter
            else:
                max_due = now + interval + jitter_cap
                if self._next_due[account.username_lower] > max_due:
                    self._next_due[account.username_lower] = max_due
        return self._snapshot

    def run_once(self) -> list[CaptureRunStats]:
        try:
            self._prune_attempts_if_due(force=True)
            snapshot = self.load_snapshot()
            stats = self._process_accounts(snapshot.accounts, snapshot.settings)
            succeeded = sum(1 for item in stats if item.status == "success")
            failed = [item for item in stats if item.status != "success"]
            status, success, error = _heartbeat_state(
                processed=len(stats),
                succeeded=succeeded,
                failed=len(failed),
                errors=[item.error or item.status for item in failed],
            )
            self._record_heartbeat(
                status=status,
                success=success,
                error=error,
                metadata={
                    "accounts": len(snapshot.accounts),
                    "processed_accounts": len(stats),
                    "succeeded_accounts": succeeded,
                    "failed_accounts": len(failed),
                    "saved_count": sum(item.saved_count for item in stats),
                },
                force=True,
            )
            return stats
        except Exception as exc:
            self._record_heartbeat(status="failed", success=False, error=str(exc), metadata={}, force=True)
            raise

    def run_forever(self) -> None:
        snapshot = self.load_snapshot()
        self._prune_attempts_if_due(force=True)
        self._record_heartbeat(
            status="ok",
            success=True,
            error=None,
            metadata={"accounts": len(snapshot.accounts), "startup": True},
            force=True,
        )
        print(
            "[odaily] x-capture worker started. "
            f"accounts={len(snapshot.accounts)} interval={snapshot.settings.global_interval_seconds}s "
            f"max_concurrency={snapshot.settings.max_concurrency} "
            f"pacer={self._request_pacer.min_interval_seconds:.2f}s "
            f"config_reload_interval={int(self.config_reload_interval_seconds)}s"
        )

        attempt_queue: queue.Queue[tuple[CaptureRunStats, datetime, datetime] | None] = queue.Queue()
        persister = threading.Thread(
            target=self._persist_attempt_queue,
            args=(attempt_queue,),
            name="x-capture-persister",
            daemon=True,
        )
        persister.start()

        active_timeline_workers = max(1, int(snapshot.settings.max_concurrency))
        timeline_executor = ThreadPoolExecutor(
            max_workers=active_timeline_workers,
            thread_name_prefix="x-capture-timeline",
        )
        detail_executor = ThreadPoolExecutor(
            max_workers=self.detail_workers,
            thread_name_prefix="x-capture-detail",
        )

        timeline_futures: dict[Future[tuple[list[TweetCandidate], TimelineAttempt, datetime, datetime]], tuple[XCaptureAccount, datetime]] = {}
        detail_futures: dict[Future[tuple[CaptureRunStats, datetime, datetime | None]], tuple[XCaptureAccount, datetime, str, list[str]]] = {}
        in_flight_accounts: set[str] = set()
        seeding_accounts: set[str] = set()
        pending_resize = False
        hb_window_started = time.monotonic()
        hb_processed = 0
        hb_succeeded = 0
        hb_failed = 0
        hb_saved = 0
        hb_errors: list[str] = []

        def emit_attempt(stats: CaptureRunStats, started_at: datetime, finished_at: datetime) -> None:
            nonlocal hb_processed, hb_succeeded, hb_failed, hb_saved
            self._log_account_stats(stats)
            attempt_queue.put((stats, started_at, finished_at))
            hb_processed += 1
            hb_saved += stats.saved_count
            if stats.status == "success":
                hb_succeeded += 1
            else:
                hb_failed += 1
                if len(hb_errors) < 5:
                    hb_errors.append(stats.error or stats.status)

        def flush_heartbeat_if_due(*, force: bool = False) -> None:
            nonlocal hb_window_started, hb_processed, hb_succeeded, hb_failed, hb_saved, hb_errors
            now_mono = time.monotonic()
            if not force and now_mono - hb_window_started < HEARTBEAT_WINDOW_SECONDS:
                return
            status, success, error = _heartbeat_state(
                processed=hb_processed,
                succeeded=hb_succeeded,
                failed=hb_failed,
                errors=hb_errors,
            )
            self._record_heartbeat(
                status=status,
                success=success,
                error=error,
                metadata={
                    "accounts": len(snapshot.accounts),
                    "processed_accounts": hb_processed,
                    "succeeded_accounts": hb_succeeded,
                    "failed_accounts": hb_failed,
                    "saved_count": hb_saved,
                },
                force=force or status == "failed",
            )
            hb_window_started = now_mono
            hb_processed = 0
            hb_succeeded = 0
            hb_failed = 0
            hb_saved = 0
            hb_errors = []

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._config_changed.is_set() or self._snapshot_reload_due(now):
                    self._config_changed.clear()
                    snapshot = self.load_snapshot()
                    print(
                        f"[odaily] x-capture config loaded. accounts={len(snapshot.accounts)} "
                        f"interval={snapshot.settings.global_interval_seconds}s "
                        f"max_concurrency={snapshot.settings.max_concurrency} "
                        f"jitter={snapshot.settings.jitter_seconds}s"
                    )
                    desired_workers = max(1, int(snapshot.settings.max_concurrency))
                    if desired_workers != active_timeline_workers:
                        pending_resize = True

                if pending_resize and not timeline_futures:
                    timeline_executor.shutdown(wait=True)
                    active_timeline_workers = max(1, int(snapshot.settings.max_concurrency))
                    timeline_executor = ThreadPoolExecutor(
                        max_workers=active_timeline_workers,
                        thread_name_prefix="x-capture-timeline",
                    )
                    pending_resize = False

                self._prune_attempts_if_due()

                now = time.monotonic()
                if not pending_resize:
                    for account in snapshot.accounts:
                        key = account.username_lower
                        if key in in_flight_accounts or key in seeding_accounts:
                            continue
                        due_at = self._next_due.get(key, 0.0)
                        if due_at > now:
                            continue
                        interval = account.effective_interval_seconds(snapshot.settings)
                        jitter = (
                            random.uniform(0, snapshot.settings.jitter_seconds)
                            if snapshot.settings.jitter_seconds > 0
                            else 0.0
                        )
                        self._next_due[key] = now + interval + jitter
                        in_flight_accounts.add(key)
                        scheduled_at = utc_now()
                        future = timeline_executor.submit(self._fetch_timeline_task, account)
                        timeline_futures[future] = (account, scheduled_at)

                sleep_for = self._run_loop_sleep_seconds(
                    snapshot=snapshot,
                    in_flight_accounts=in_flight_accounts,
                    seeding_accounts=seeding_accounts,
                    has_active_futures=bool(timeline_futures or detail_futures),
                    hb_window_started=hb_window_started,
                )
                wait_targets = set(timeline_futures) | set(detail_futures)
                if wait_targets:
                    completed, _ = wait(wait_targets, timeout=sleep_for, return_when=FIRST_COMPLETED)
                else:
                    self._wake_event.wait(sleep_for)
                    self._wake_event.clear()
                    completed = set()

                for finished in completed:
                    if finished in timeline_futures:
                        account, scheduled_at = timeline_futures.pop(finished)
                        in_flight_accounts.discard(account.username_lower)
                        try:
                            candidates, attempt, started_at, finished_at = finished.result()
                        except Exception as exc:
                            finished_at = utc_now()
                            emit_attempt(
                                CaptureRunStats(account=account, status="fetch_failed", error=str(exc)),
                                scheduled_at,
                                finished_at,
                            )
                            continue

                        if attempt.status != "success":
                            emit_attempt(
                                CaptureRunStats(
                                    account=account,
                                    status=attempt.status,
                                    candidate_count=attempt.candidate_count,
                                    error=attempt.error,
                                    metadata={"attempt_url": attempt.url, "source": attempt.source},
                                ),
                                started_at,
                                finished_at,
                            )
                            continue
                        if not candidates:
                            emit_attempt(
                                CaptureRunStats(account=account, status="parse_empty", error="no candidates"),
                                started_at,
                                finished_at,
                            )
                            continue
                        if account.seeded_at is None:
                            seeding_accounts.add(account.username_lower)
                            detail_future = detail_executor.submit(
                                self._seed_account_task,
                                account,
                                candidates,
                                attempt,
                            )
                            detail_futures[detail_future] = (account, started_at, "seed", [])
                            continue

                        unknown_ids = self._reserve_unknown_tweet_ids([item.tweet_id for item in candidates])
                        if not unknown_ids:
                            emit_attempt(
                                self._noop_success_stats(account, candidates, attempt),
                                started_at,
                                finished_at,
                            )
                            continue
                        detail_future = detail_executor.submit(
                            self._ingest_unseen_task,
                            account,
                            candidates,
                            unknown_ids,
                            attempt,
                            scheduled_at,
                        )
                        detail_futures[detail_future] = (account, started_at, "ingest", unknown_ids)
                    elif finished in detail_futures:
                        account, started_at, kind, reserved_ids = detail_futures.pop(finished)
                        if kind == "seed":
                            seeding_accounts.discard(account.username_lower)
                        try:
                            stats, seeded_at = finished.result()
                        except Exception as exc:
                            if reserved_ids:
                                self._release_inflight_tweets(reserved_ids)
                            emit_attempt(
                                CaptureRunStats(account=account, status="fetch_failed", error=str(exc)),
                                started_at,
                                utc_now(),
                            )
                            continue
                        if kind == "seed" and seeded_at is not None:
                            self._mark_snapshot_account_seeded(account.username_lower, seeded_at)
                        emit_attempt(stats, started_at, utc_now())

                flush_heartbeat_if_due()
        finally:
            self.stop()
            for future, (account, scheduled_at) in list(timeline_futures.items()):
                in_flight_accounts.discard(account.username_lower)
                try:
                    candidates, attempt, started_at, finished_at = future.result()
                    if attempt.status != "success":
                        emit_attempt(
                            CaptureRunStats(
                                account=account,
                                status=attempt.status,
                                candidate_count=attempt.candidate_count,
                                error=attempt.error,
                                metadata={"attempt_url": attempt.url, "source": attempt.source},
                            ),
                            started_at,
                            finished_at,
                        )
                    elif not candidates:
                        emit_attempt(
                            CaptureRunStats(account=account, status="parse_empty", error="no candidates"),
                            started_at,
                            finished_at,
                        )
                    elif account.seeded_at is not None:
                        unknown_ids = self._reserve_unknown_tweet_ids([item.tweet_id for item in candidates])
                        if not unknown_ids:
                            emit_attempt(self._noop_success_stats(account, candidates, attempt), started_at, finished_at)
                        else:
                            detail_future = detail_executor.submit(
                                self._ingest_unseen_task,
                                account,
                                candidates,
                                unknown_ids,
                                attempt,
                                scheduled_at,
                            )
                            detail_futures[detail_future] = (account, started_at, "ingest", unknown_ids)
                except Exception as exc:
                    emit_attempt(
                        CaptureRunStats(account=account, status="fetch_failed", error=str(exc)),
                        scheduled_at,
                        utc_now(),
                    )
            for detail_future, (account, started_at, kind, reserved_ids) in list(detail_futures.items()):
                if kind == "seed":
                    seeding_accounts.discard(account.username_lower)
                try:
                    stats, seeded_at = detail_future.result()
                    if kind == "seed" and seeded_at is not None:
                        self._mark_snapshot_account_seeded(account.username_lower, seeded_at)
                    emit_attempt(stats, started_at, utc_now())
                except Exception as exc:
                    if reserved_ids:
                        self._release_inflight_tweets(reserved_ids)
                    emit_attempt(
                        CaptureRunStats(account=account, status="fetch_failed", error=str(exc)),
                        started_at,
                        utc_now(),
                    )
            timeline_executor.shutdown(wait=True)
            detail_executor.shutdown(wait=True)
            flush_heartbeat_if_due(force=bool(hb_processed))
            attempt_queue.put(None)
            persister.join()
            close_client = getattr(self.client, "close", None)
            if callable(close_client):
                close_client()

    def _mark_snapshot_account_seeded(self, username_lower: str, seeded_at: datetime) -> None:
        self._snapshot = WorkerSnapshot(
            self._snapshot.settings,
            [
                replace(account, seeded_at=seeded_at) if account.username_lower == username_lower else account
                for account in self._snapshot.accounts
            ],
        )

    def _persist_attempt_queue(
        self,
        attempt_queue: queue.Queue[tuple[CaptureRunStats, datetime, datetime] | None],
    ) -> None:
        stop_requested = False
        while not stop_requested:
            item = attempt_queue.get()
            if item is None:
                break
            batch: list[tuple[CaptureRunStats, datetime, datetime]] = [item]
            while len(batch) < 128:
                try:
                    nxt = attempt_queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    stop_requested = True
                    break
                batch.append(nxt)
            try:
                self._persist_attempts(batch)
            except Exception as exc:
                print(f"[odaily] x-capture persist attempts failed: {exc}")

    def _persist_attempts(self, items: Sequence[tuple[CaptureRunStats, datetime, datetime]]) -> None:
        if not items:
            return
        batch_writer = getattr(self.repository, "record_attempts_batch", None)
        if callable(batch_writer):
            batch_writer(items)
            return
        for stats, started_at, finished_at in items:
            self.repository.record_attempt(stats, started_at=started_at, finished_at=finished_at)

    def _log_account_stats(self, item: CaptureRunStats) -> None:
        print(
            "[odaily] x-capture "
            f"account={item.account.username} status={item.status} "
            f"candidates={item.candidate_count} seeded={item.seeded_count} "
            f"new={item.new_count} saved={item.saved_count} error={item.error or '-'}"
        )

    def _record_heartbeat(
        self,
        *,
        status: str = "ok",
        success: bool,
        error: str | None,
        metadata: dict[str, Any],
        force: bool = False,
    ) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self._heartbeat.send(
                status=status if success else "failed",
                success=success,
                error=error,
                metadata=metadata,
                force=force,
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

    def _run_loop_sleep_seconds(
        self,
        *,
        snapshot: WorkerSnapshot,
        in_flight_accounts: set[str],
        seeding_accounts: set[str],
        has_active_futures: bool,
        hb_window_started: float,
    ) -> float:
        now = time.monotonic()
        reload_due = self._seconds_until_snapshot_reload(now)
        prune_due = (
            self.attempt_prune_interval_seconds - (now - self._last_attempt_prune_monotonic)
            if self._last_attempt_prune_monotonic is not None
            else 0.0
        )
        hb_due = max(0.05, HEARTBEAT_WINDOW_SECONDS - (now - hb_window_started))
        next_due_values = [
            self._next_due.get(account.username_lower, now + 5.0)
            for account in snapshot.accounts
            if account.username_lower not in in_flight_accounts and account.username_lower not in seeding_accounts
        ]
        if next_due_values:
            candidate_sleep = min(min(next_due_values) - now, reload_due, max(0.05, prune_due), hb_due, 60.0)
        elif has_active_futures:
            candidate_sleep = min(reload_due, max(0.05, prune_due), hb_due, 5.0)
        else:
            candidate_sleep = min(reload_due, max(0.05, prune_due), hb_due, 60.0)
        return max(0.01, candidate_sleep)

    def _sleep_seconds(self, snapshot: WorkerSnapshot) -> float:
        now = time.monotonic()
        reload_due = self._seconds_until_snapshot_reload(now)
        if not snapshot.accounts:
            return max(0.05, min(60.0, reload_due))
        next_due = min(self._next_due.get(account.username_lower, now + 5.0) for account in snapshot.accounts)
        return max(0.01, min(60.0, next_due - now, reload_due))

    def _snapshot_reload_due(self, now: float) -> bool:
        return self._seconds_until_snapshot_reload(now) <= 0.0

    def _seconds_until_snapshot_reload(self, now: float) -> float:
        if self._last_snapshot_loaded_monotonic is None:
            return 0.0
        deadline = self._last_snapshot_loaded_monotonic + self.config_reload_interval_seconds
        return deadline - now

    def _call_fetch_timeline(self, username: str) -> tuple[list[TweetCandidate], TimelineAttempt]:
        if self._client_accepts_before_request:
            return self.client.fetch_timeline(
                username,
                count=self.timeline_count,
                before_request=self._request_pacer.wait,
            )
        self._request_pacer.wait()
        return self.client.fetch_timeline(username, count=self.timeline_count)

    def _fetch_timeline_task(
        self,
        account: XCaptureAccount,
    ) -> tuple[list[TweetCandidate], TimelineAttempt, datetime, datetime]:
        started_at = utc_now()
        try:
            candidates, attempt = self._call_fetch_timeline(account.username)
        except Exception as exc:
            candidates = []
            attempt = TimelineAttempt("fxtwitter", "fetch_failed", "", str(exc))
        finished_at = utc_now()
        return candidates, attempt, started_at, finished_at

    def _noop_success_stats(
        self,
        account: XCaptureAccount,
        candidates: list[TweetCandidate],
        attempt: TimelineAttempt,
    ) -> CaptureRunStats:
        return CaptureRunStats(
            account=account,
            status="success",
            candidate_count=len(candidates),
            new_count=0,
            saved_count=0,
            metadata={
                "attempt_url": attempt.url,
                "detail_errors": {},
                "freshness_window_seconds": self.freshness_window_seconds,
                "ignored_stale_count": 0,
                "ignored_stale_tweet_ids": [],
            },
        )

    def _seed_account_task(
        self,
        account: XCaptureAccount,
        candidates: list[TweetCandidate],
        attempt: TimelineAttempt,
    ) -> tuple[CaptureRunStats, datetime | None]:
        tweet_ids = [candidate.tweet_id for candidate in candidates]
        self.repository.mark_account_seeded(account, tweet_ids)
        self._remember_seen(tweet_ids)
        seeded_at = utc_now()
        return (
            CaptureRunStats(
                account=account,
                status="success",
                candidate_count=len(candidates),
                seeded_count=len(tweet_ids),
                metadata={"first_seen_seed": True, "attempt_url": attempt.url},
            ),
            seeded_at,
        )

    def _ingest_unseen_task(
        self,
        account: XCaptureAccount,
        candidates: list[TweetCandidate],
        reserved_ids: list[str],
        attempt: TimelineAttempt,
        scheduled_at: datetime,
    ) -> tuple[CaptureRunStats, datetime | None]:
        try:
            stats = self._ingest_unseen_candidates(
                account,
                candidates,
                reserved_ids,
                scheduled_at=scheduled_at,
                attempt_url=attempt.url,
            )
            return stats, None
        finally:
            self._release_inflight_tweets(reserved_ids)

    def _ingest_unseen_candidates(
        self,
        account: XCaptureAccount,
        candidates: list[TweetCandidate],
        candidate_ids: list[str],
        *,
        scheduled_at: datetime,
        attempt_url: str,
    ) -> CaptureRunStats:
        unseen = self.repository.unseen_tweet_ids(candidate_ids)
        already_seen = [tweet_id for tweet_id in candidate_ids if tweet_id not in unseen]
        if already_seen:
            self._remember_seen(already_seen)
        saved_count = 0
        new_count = 0
        ignored_stale_count = 0
        ignored_stale_tweet_ids: list[str] = []
        detail_errors: dict[str, str] = {}
        remembered_seen: list[str] = []
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
                remembered_seen.append(candidate.tweet_id)
                continue
            exclusion_scopes = ["x"]
            if account.is_ai_source:
                exclusion_scopes.append("ai_source")
            if self.exclusion_matcher is not None and self.exclusion_matcher.is_excluded(
                scopes=exclusion_scopes,
                title_texts=[record.text],
            ):
                self.repository.mark_seen(account, candidate.tweet_id, seeded=False)
                remembered_seen.append(candidate.tweet_id)
                continue
            task_id = self.repository.save_task(account, record)
            if task_id is None:
                if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                    new_count += 1
                remembered_seen.append(candidate.tweet_id)
                continue
            try:
                self._submit_pipeline_job(task_id=task_id, source="x", source_item_id=record.tweet_id)
            except Exception as exc:
                detail_errors[f"pipeline:{candidate.tweet_id}"] = str(exc)
                continue
            if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                new_count += 1
                saved_count += 1
            remembered_seen.append(candidate.tweet_id)

        if remembered_seen:
            self._remember_seen(remembered_seen)

        return CaptureRunStats(
            account=account,
            status="success",
            candidate_count=len(candidates),
            new_count=new_count,
            saved_count=saved_count,
            metadata={
                "attempt_url": attempt_url,
                "detail_errors": detail_errors,
                "freshness_window_seconds": self.freshness_window_seconds,
                "ignored_stale_count": ignored_stale_count,
                "ignored_stale_tweet_ids": ignored_stale_tweet_ids,
            },
        )

    def _process_accounts(
        self,
        accounts: list[XCaptureAccount],
        settings: XCaptureSettings,
    ) -> list[CaptureRunStats]:
        if not accounts:
            return []
        stats: list[CaptureRunStats] = []
        attempt_items: list[tuple[CaptureRunStats, datetime, datetime]] = []
        max_workers = max(1, min(settings.max_concurrency, len(accounts)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._run_account_without_attempt_write, account) for account in accounts]
            for future in futures:
                item, started_at, finished_at = future.result()
                stats.append(item)
                attempt_items.append((item, started_at, finished_at))
                self._log_account_stats(item)
        self._persist_attempts(attempt_items)
        return stats

    def _run_account_without_attempt_write(
        self,
        account: XCaptureAccount,
    ) -> tuple[CaptureRunStats, datetime, datetime]:
        started_at = utc_now()
        try:
            stats = self._process_account_inner(account, scheduled_at=started_at)
        except Exception as exc:
            stats = CaptureRunStats(account=account, status="fetch_failed", error=str(exc))
        finished_at = utc_now()
        return stats, started_at, finished_at

    def process_account(self, account: XCaptureAccount) -> CaptureRunStats:
        stats, started_at, finished_at = self._run_account_without_attempt_write(account)
        self._persist_attempts([(stats, started_at, finished_at)])
        return stats

    def _process_account_inner(
        self,
        account: XCaptureAccount,
        *,
        scheduled_at: datetime | None = None,
    ) -> CaptureRunStats:
        scheduled_at = scheduled_at or utc_now()
        candidates, attempt = self._call_fetch_timeline(account.username)
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
            stats, _seeded_at = self._seed_account_task(account, candidates, attempt)
            return stats

        candidate_ids = [candidate.tweet_id for candidate in candidates]
        unknown_ids = self._reserve_unknown_tweet_ids(candidate_ids)
        if not unknown_ids:
            return self._noop_success_stats(account, candidates, attempt)
        try:
            return self._ingest_unseen_candidates(
                account,
                candidates,
                unknown_ids,
                scheduled_at=scheduled_at,
                attempt_url=attempt.url,
            )
        finally:
            self._release_inflight_tweets(unknown_ids)

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
        context_chain: list[dict[str, Any]] = []
        context_raw_layers: list[dict[str, Any]] = []
        context_chain_status = "complete"
        context_chain_error = None
        context_source = detail if has_context_reference(detail) else candidate.raw_payload
        if context_source:
            context_chain, context_chain_status, context_chain_error = self.client.collect_context_chain(
                context_source, root_id=candidate.tweet_id, author_hint=account.username,
                raw_layers=context_raw_layers,
            )
        if detail_error:
            context_chain_status = "partial"
            context_chain_error = f"current detail: {detail_error}" + (f"; {context_chain_error}" if context_chain_error else "")
        return self.client.build_record(
            account.username,
            candidate,
            detail=detail,
            detail_error=detail_error,
            context_chain=context_chain,
            context_chain_status=context_chain_status,
            context_chain_error=context_chain_error,
            context_raw_layers=context_raw_layers,
        )

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
