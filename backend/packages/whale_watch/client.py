from __future__ import annotations

import time
from typing import Any

import requests

from .chains import ChainDefinition


class BlockscoutClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_seconds = max(0.0, float(backoff_seconds))
        self.session = session or requests.Session()

    def list_address_transactions(self, chain: ChainDefinition, address: str) -> list[dict[str, Any]]:
        payload = self._get_json(chain, f"/api/v2/addresses/{address}/transactions")
        return _items(payload)

    def list_address_token_transfers(self, chain: ChainDefinition, address: str) -> list[dict[str, Any]]:
        payload = self._get_json(chain, f"/api/v2/addresses/{address}/token-transfers")
        return _items(payload)

    def get_transaction(self, chain: ChainDefinition, tx_hash: str) -> dict[str, Any]:
        payload = self._get_json(chain, f"/api/v2/transactions/{tx_hash}")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected Blockscout transaction payload for {tx_hash}")
        return payload

    def _get_json(self, chain: ChainDefinition, path: str) -> Any:
        url = f"{chain.blockscout_base_url.rstrip('/')}{path}"
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.get(url, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(f"Blockscout request failed url={url}: {last_error}") from last_error


def _items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Blockscout list payload")
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("Blockscout list payload missing items")
    return [item for item in items if isinstance(item, dict)]
