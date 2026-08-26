from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import requests


OKX_BASE_URL = "https://web3.okx.com"
OKX_LIST_PATH = "/api/v6/dex/market/memepump/tokenList"
OKX_PRICE_INFO_PATH = "/api/v6/dex/market/price-info"
OKX_TOKEN_DETAILS_PATH = "/api/v6/dex/market/memepump/tokenDetails"
OKX_CHAIN_INDEX = {"bsc": "56", "robinhood": "4663", "solana": "501"}


class OKXError(RuntimeError):
    """A retryable or terminal error returned by the OKX market adapter."""


@dataclass(frozen=True)
class OKXResponse:
    code: str
    message: str
    data: Any


class OKXClient:
    """Signed REST adapter for the small market-data surface used by Meme速递."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        secret_key: str | None = None,
        passphrase: str | None = None,
        base_url: str | None = None,
        timeout: int | None = None,
        max_attempts: int | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OKX_API_KEY", "")
        self.secret_key = secret_key or os.getenv("OKX_SECRET_KEY", "")
        self.passphrase = passphrase or os.getenv("OKX_PASSPHRASE", "")
        self.base_url = (base_url or os.getenv("OKX_API_BASE_URL") or OKX_BASE_URL).rstrip("/")
        self.timeout = int(timeout or os.getenv("MEME_OKX_TIMEOUT_SECONDS") or 20)
        self.max_attempts = max(1, int(max_attempts or os.getenv("MEME_OKX_MAX_ATTEMPTS") or 3))
        self.session = session or requests.Session()

    def _require_credentials(self) -> None:
        if not self.api_key or not self.secret_key or not self.passphrase:
            raise OKXError("OKX_API_KEY, OKX_SECRET_KEY and OKX_PASSPHRASE are required")

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any | None = None,
    ) -> OKXResponse:
        self._require_credentials()
        body_text = "" if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        query = urlencode([(key, value) for key, value in (params or {}).items() if value is not None])
        request_path = f"{path}?{query}" if query else path
        timestamp = self._timestamp()
        sign_payload = timestamp + method.upper() + request_path + body_text
        signature = base64.b64encode(
            hmac.new(self.secret_key.encode("utf-8"), sign_payload.encode("utf-8"), hashlib.sha256).digest()
        ).decode("ascii")
        headers = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "User-Agent": "OdAIly-MemeScanner/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method.upper(),
                    f"{self.base_url}{path}",
                    params=params,
                    data=body_text.encode("utf-8") if body is not None else None,
                    headers=headers,
                    timeout=self.timeout,
                )
                if response.status_code == 429 or response.status_code >= 500:
                    raise requests.HTTPError(
                        f"OKX HTTP {response.status_code}: {response.text[:500]}", response=response
                    )
                response.raise_for_status()
                payload = response.json()
                if str(payload.get("code")) != "0":
                    raise OKXError(f"OKX code {payload.get('code')}: {payload.get('msg') or 'unknown error'}")
                return OKXResponse(str(payload.get("code")), str(payload.get("msg") or ""), payload.get("data"))
            except (requests.RequestException, OKXError, ValueError) as exc:
                last_error = exc
                if attempt >= self.max_attempts:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
        raise OKXError(str(last_error) if last_error else "OKX request failed")

    @staticmethod
    def _items(data: Any) -> list[dict[str, Any]]:
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def list_migrated(self, chain: str, *, limit: int = 30) -> list[dict[str, Any]]:
        chain_index = OKX_CHAIN_INDEX.get(chain)
        if not chain_index:
            raise OKXError(f"OKX does not support discovery chain {chain}")
        response = self._request(
            "GET",
            OKX_LIST_PATH,
            params={"chainIndex": chain_index, "stage": "MIGRATED", "limit": min(max(limit, 1), 30)},
        )
        return self._items(response.data)

    def price_info(self, chain: str, addresses: list[str]) -> dict[str, dict[str, Any]]:
        chain_index = OKX_CHAIN_INDEX.get(chain)
        if not chain_index:
            raise OKXError(f"OKX does not support price lookup chain {chain}")
        normalized = list(dict.fromkeys(str(address).strip() for address in addresses if str(address).strip()))
        if not normalized:
            return {}
        if len(normalized) > 100:
            raise OKXError("OKX price-info accepts at most 100 addresses per request")
        response = self._request(
            "POST",
            OKX_PRICE_INFO_PATH,
            body=[{"chainIndex": chain_index, "tokenContractAddress": address} for address in normalized],
        )
        result: dict[str, dict[str, Any]] = {}
        for item in self._items(response.data):
            address = str(item.get("tokenContractAddress") or "").strip().lower()
            if address:
                result[address] = item
        return result

    def token_details(self, chain: str, address: str) -> dict[str, Any]:
        chain_index = OKX_CHAIN_INDEX.get(chain)
        if not chain_index:
            raise OKXError(f"OKX does not support details chain {chain}")
        response = self._request(
            "GET",
            OKX_TOKEN_DETAILS_PATH,
            params={"chainIndex": chain_index, "tokenContractAddress": address},
        )
        if not isinstance(response.data, dict):
            raise OKXError("OKX tokenDetails returned no token object")
        return response.data
