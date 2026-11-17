from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from typing import Any

from .client import PredictFunClient, flatten_categories
from .settings import PredictMarketSettings
from .store import PredictMarketStore


class PredictMarketWorker:
    def __init__(
        self,
        *,
        settings: PredictMarketSettings,
        store: PredictMarketStore | None = None,
        client: PredictFunClient | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or PredictMarketStore(settings.database_path)
        self.store.initialize()
        self.client = client
        if self.client is None and settings.api_key:
            self.client = PredictFunClient(
                api_key=settings.api_key,
                base_url=settings.api_base_url,
                timeout_seconds=settings.request_timeout_seconds,
                page_size=settings.page_size,
                max_pages=settings.max_pages,
            )

    def run_once(self) -> dict[str, Any]:
        if self.client is None:
            raise RuntimeError("PREDICT_FUN_API_KEY is not configured")
        started_at = datetime.now(UTC)
        try:
            categories, pages, page_limit_reached = self.client.fetch_open_categories()
            observations = flatten_categories(categories)
            return self.store.record_scan(
                observations=observations,
                pages=pages,
                categories=len(categories),
                started_at=started_at,
                page_limit_reached=page_limit_reached,
            )
        except Exception as exc:
            self.store.record_failure(started_at=started_at, error=f"{type(exc).__name__}: {exc}")
            raise

    def run_forever(self) -> None:
        print(
            "[predict-market] worker started "
            f"interval={self.settings.poll_interval_seconds}s "
            f"pages={self.settings.max_pages} page_size={self.settings.page_size}"
        )
        while True:
            started = time.monotonic()
            try:
                result = self.run_once()
                print("[predict-market] " + json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            except Exception as exc:
                print(f"[predict-market] round failed: {type(exc).__name__}: {exc}")
            delay = max(0, self.settings.poll_interval_seconds - (time.monotonic() - started))
            time.sleep(delay)
