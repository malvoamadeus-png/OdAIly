from __future__ import annotations

import time
from typing import Any

import requests


class HyperliquidClient:
    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz",
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.session = session or requests.Session()

    def user_fills(self, address: str) -> list[dict[str, Any]]:
        payload = self._post_info({"type": "userFills", "user": address, "aggregateByTime": True})
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected Hyperliquid userFills payload")
        return [item for item in payload if isinstance(item, dict)]

    def clearinghouse_state(self, address: str) -> dict[str, Any]:
        payload = self._post_info({"type": "clearinghouseState", "user": address})
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected Hyperliquid clearinghouseState payload")
        return payload

    def _post_info(self, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}/info"
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(url, json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(f"Hyperliquid request failed url={url}: {last_error}") from last_error
