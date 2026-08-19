from __future__ import annotations

import time
from datetime import UTC, datetime

from packages.local_pipeline.client import LocalPipelineClient
from packages.common.source_exclusions import SourceExclusionMatcher

from .client import BinanceSquareClient
from .models import BINANCE_SQUARE_SOURCE, BinanceSquareAccount, BinanceSquareRunStats
from .repository import BinanceSquareRepository


class BinanceSquareWorker:
    def __init__(self, *, repository: BinanceSquareRepository, client: BinanceSquareClient | None = None,
                 pipeline_client: LocalPipelineClient | None = None,
                 exclusion_matcher: SourceExclusionMatcher | None = None) -> None:
        self.repository = repository
        self.client = client or BinanceSquareClient()
        self.pipeline_client = pipeline_client
        self.exclusion_matcher = exclusion_matcher

    def run_once(self) -> list[BinanceSquareRunStats]:
        settings = self.repository.get_settings()
        if not settings.enabled:
            self.client.close()
            self.repository.set_worker_status("disabled")
            return []
        self.repository.set_worker_status("running")
        return [self.process_account(account) for account in self.repository.list_accounts()]

    def run_forever(self) -> None:
        try:
            while True:
                started = time.monotonic()
                stats = self.run_once()
                settings = self.repository.get_settings()
                print(
                    f"[odaily] binance-square enabled={settings.enabled} accounts={len(stats)} "
                    f"saved={sum(item.saved_count for item in stats)}"
                )
                wait_seconds = settings.interval_seconds if settings.enabled else 30
                time.sleep(max(1.0, wait_seconds - (time.monotonic() - started)))
        finally:
            self.client.close()
            self.repository.set_worker_status("stopped")

    def process_account(self, account: BinanceSquareAccount) -> BinanceSquareRunStats:
        started_at = datetime.now(UTC)
        try:
            stats = self._process_account(account)
        except Exception as exc:
            stats = BinanceSquareRunStats(account=account, status="fetch_failed", error=str(exc))
        self.repository.record_attempt(stats, started_at=started_at, finished_at=datetime.now(UTC))
        return stats

    def _process_account(self, account: BinanceSquareAccount) -> BinanceSquareRunStats:
        posts = self.client.fetch_profile(account.profile_url)
        self.repository.update_account_identity(account.id, posts[-1])
        if account.seeded_at is None:
            self.repository.mark_seeded(account, [post.post_id for post in posts])
            return BinanceSquareRunStats(account=account, status="success", candidate_count=len(posts), seeded_count=len(posts))
        unseen = self.repository.unseen_post_ids([post.post_id for post in posts])
        saved = 0
        errors: dict[str, str] = {}
        for post in posts:
            if post.post_id not in unseen:
                continue
            if self.exclusion_matcher is not None and self.exclusion_matcher.is_excluded(
                scopes=["x"], title_texts=[post.text]
            ):
                self.repository.mark_seen(account.id, post.post_id)
                continue
            task_id = self.repository.save_task(account, post)
            try:
                if self.pipeline_client is None:
                    raise RuntimeError("local pipeline client is not configured")
                self.pipeline_client.submit_job(
                    job_type="write_flow", task_id=task_id, source=BINANCE_SQUARE_SOURCE, source_item_id=post.post_id
                )
            except Exception as exc:
                errors[post.post_id] = str(exc)
                continue
            self.repository.mark_seen(account.id, post.post_id)
            saved += 1
        return BinanceSquareRunStats(
            account=account, status="success", candidate_count=len(posts), new_count=len(unseen), saved_count=saved,
            metadata={"pipeline_errors": errors},
        )
