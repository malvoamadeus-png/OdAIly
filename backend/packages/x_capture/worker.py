from __future__ import annotations

import random
import select
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .client import FXTwitterClient
from .models import CaptureRunStats, TweetCandidate, XCaptureAccount, XCaptureSettings
from .repository import CONFIG_NOTIFY_CHANNEL, PostgresXCaptureRepository, XCaptureRepository, utc_now


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
        notify_wait_seconds: float = 5.0,
        notify_retry_seconds: float = 5.0,
        timeline_count: int = 20,
    ) -> None:
        self.repository = repository
        self.client = client or FXTwitterClient()
        self.notify_wait_seconds = notify_wait_seconds
        self.notify_retry_seconds = notify_retry_seconds
        self.timeline_count = timeline_count
        self._stop_event = threading.Event()
        self._config_changed = threading.Event()
        self._wake_event = threading.Event()
        self._next_due: dict[str, float] = {}
        self._snapshot = WorkerSnapshot(XCaptureSettings(), [])

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
        known = {account.username_lower for account in accounts}
        for username in list(self._next_due):
            if username not in known:
                del self._next_due[username]
        now = time.monotonic()
        for account in accounts:
            self._next_due.setdefault(account.username_lower, now + random.uniform(0, settings.jitter_seconds))
        return self._snapshot

    def run_once(self) -> list[CaptureRunStats]:
        snapshot = self.load_snapshot()
        return self._process_accounts(snapshot.accounts, snapshot.settings)

    def run_forever(self) -> None:
        snapshot = self.load_snapshot()
        notify_thread = self._start_notify_listener()
        print(
            "[odaily] x-capture worker started. "
            f"accounts={len(snapshot.accounts)} interval={snapshot.settings.global_interval_seconds}s"
        )

        try:
            while not self._stop_event.is_set():
                now = time.monotonic()
                if self._config_changed.is_set():
                    self._config_changed.clear()
                    snapshot = self.load_snapshot()
                    print(f"[odaily] x-capture config loaded. accounts={len(snapshot.accounts)}")

                due = [account for account in snapshot.accounts if self._next_due.get(account.username_lower, 0) <= now]
                if due:
                    stats = self._process_accounts(due, snapshot.settings)
                    for item in stats:
                        interval = item.account.effective_interval_seconds(snapshot.settings)
                        self._next_due[item.account.username_lower] = (
                            time.monotonic() + interval + random.uniform(0, snapshot.settings.jitter_seconds)
                        )

                sleep_for = self._sleep_seconds(snapshot)
                self._wake_event.wait(sleep_for)
                self._wake_event.clear()
        finally:
            self.stop()
            if notify_thread:
                notify_thread.join(timeout=2)

    def _sleep_seconds(self, snapshot: WorkerSnapshot) -> float:
        if not snapshot.accounts:
            return 3600.0
        now = time.monotonic()
        next_due = min(self._next_due.get(account.username_lower, now + 5.0) for account in snapshot.accounts)
        return max(0.5, min(60.0, next_due - now))

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
            stats = self._process_account_inner(account)
        except Exception as exc:
            stats = CaptureRunStats(account=account, status="fetch_failed", error=str(exc))
        finished_at = utc_now()
        self.repository.record_attempt(stats, started_at=started_at, finished_at=finished_at)
        return stats

    def _process_account_inner(self, account: XCaptureAccount) -> CaptureRunStats:
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
        detail_errors: dict[str, str] = {}
        for candidate in candidates:
            if candidate.tweet_id not in unseen:
                continue
            record = self._record_from_candidate(account, candidate, detail_errors)
            saved = self.repository.save_task(account, record)
            if self.repository.mark_seen(account, candidate.tweet_id, seeded=False):
                new_count += 1
            if saved:
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
            },
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

    def _start_notify_listener(self) -> threading.Thread | None:
        if not isinstance(self.repository, PostgresXCaptureRepository):
            return None
        thread = threading.Thread(target=self._listen_for_config_changes, name="x-capture-config-listener", daemon=True)
        thread.start()
        return thread

    def _listen_for_config_changes(self) -> None:
        repository = self.repository
        if not isinstance(repository, PostgresXCaptureRepository):
            return
        while not self._stop_event.is_set():
            try:
                with repository._connect() as conn:
                    conn.autocommit = True
                    conn.execute(f"LISTEN {CONFIG_NOTIFY_CHANNEL}")
                    self._signal_config_changed()
                    print(f"[odaily] x-capture listening for {CONFIG_NOTIFY_CHANNEL}")
                    while not self._stop_event.is_set():
                        if select.select([conn], [], [], self.notify_wait_seconds)[0]:
                            for _notify in conn.notifies(timeout=0, stop_after=100):
                                self._signal_config_changed()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                self._signal_config_changed()
                print(
                    "[odaily] x-capture config listener reconnecting "
                    f"in {self.notify_retry_seconds:g}s: {exc}"
                )
                self._stop_event.wait(self.notify_retry_seconds)
