from __future__ import annotations

from typing import Any
from urllib.parse import quote

import requests


DEXSCREENER_BASE_URL = "https://api.dexscreener.com"
MAX_TOKEN_ADDRESSES = 30


class DexScreenerError(RuntimeError):
    """A Dexscreener market-data request failed or returned invalid data."""


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _is_complete(pair: dict[str, Any]) -> bool:
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    return pair.get("marketCap") is not None and volume.get("h24") is not None


class DexScreenerClient:
    """Public, unauthenticated market-price adapter for Meme速递."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: int = 15,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = (base_url or DEXSCREENER_BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def price_info(self, chain: str, addresses: list[str]) -> dict[str, dict[str, Any]]:
        normalized = list(dict.fromkeys(str(address).strip().lower() for address in addresses if str(address).strip()))
        if len(normalized) > MAX_TOKEN_ADDRESSES:
            raise DexScreenerError(f"Dexscreener accepts at most {MAX_TOKEN_ADDRESSES} addresses per request")
        if not normalized:
            return {}
        encoded_addresses = quote(",".join(normalized), safe=",")
        try:
            response = self.session.get(
                f"{self.base_url}/tokens/v1/{chain}/{encoded_addresses}",
                headers={"Accept": "application/json", "User-Agent": "OdAIly-MemeScanner/1.0"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DexScreenerError(f"Dexscreener request failed: {exc}") from exc
        if not isinstance(payload, list):
            raise DexScreenerError("Dexscreener response is not a pair list")

        candidates: dict[str, list[dict[str, Any]]] = {address: [] for address in normalized}
        for pair in payload:
            if not isinstance(pair, dict) or str(pair.get("chainId") or "").lower() != chain.lower():
                continue
            base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
            address = str(base_token.get("address") or "").strip().lower()
            if address in candidates:
                candidates[address].append(pair)

        selected: dict[str, dict[str, Any]] = {}
        for address, pairs in candidates.items():
            if not pairs:
                continue
            complete = [pair for pair in pairs if _is_complete(pair)]
            pool = complete or pairs
            selected[address] = max(
                pool,
                key=lambda pair: (
                    _number((pair.get("liquidity") or {}).get("usd")),
                    _number((pair.get("volume") or {}).get("h24")),
                ),
            )
        return selected
