"""CoinGecko market caps via explicit market-to-coin-ID mapping."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from .models import number


class CoinGeckoMarketCaps:
    def __init__(self, *, session: requests.Session | None = None, demo_api_key: str | None = None,
                 timeout_seconds: float = 20, ttl_ms: int = 3600000) -> None:
        if ttl_ms <= 0:
            raise ValueError('Market-cap TTL must be positive')
        self.session = session or requests.Session()
        self.demo_api_key = demo_api_key
        self.timeout_seconds = timeout_seconds
        self.ttl_ms = ttl_ms

    def fetch(self, mapping: dict[str, str], *, now_ms: int,
              cached: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
        """Mappings must be explicit IDs, never a ticker/symbol search guess.

        Invalid/missing/expired caps do not become zero or a stale denominator.
        At most one batch per call, preventing unbounded external API requests.
        """
        ids = sorted(set(mapping.values()))
        if not ids:
            return {}
        if len(ids) > 250 or any(not isinstance(i, str) or not i for i in ids):
            raise ValueError('CoinGecko requires 1..250 explicit coin IDs per batch')
        cache = cached or {}
        reusable = {coin_id: cap for coin_id, cap in cache.items()
                    if coin_id in ids and 0 <= now_ms - int(cap['observed_ms']) < self.ttl_ms
                    and 0 <= now_ms - int(cap['provider_updated_ms']) < self.ttl_ms
                    and number(cap['value']) > 0}
        missing = [i for i in ids if i not in reusable]
        if missing:
            headers = {'x-cg-demo-api-key': self.demo_api_key} if self.demo_api_key else {}
            response = self.session.get('https://api.coingecko.com/api/v3/coins/markets',
                params={'vs_currency': 'usd', 'ids': ','.join(missing), 'per_page': 250},
                headers=headers, timeout=self.timeout_seconds)
            # Do not include response.request headers or URLs containing secrets.
            if response.status_code != 200:
                raise RuntimeError(f'CoinGecko market-cap request failed HTTP {response.status_code}')
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError('Unexpected CoinGecko markets payload')
            for row in payload:
                coin_id = row.get('id')
                if coin_id not in missing or row.get('market_cap') is None:
                    continue
                value = number(row['market_cap'])
                if value <= 0:
                    continue
                updated = row.get('last_updated')
                if not updated:
                    continue
                updated_ms = int(datetime.fromisoformat(updated.replace('Z', '+00:00')).timestamp() * 1000)
                if not 0 <= now_ms - updated_ms < self.ttl_ms:
                    continue
                reusable[coin_id] = {'value': str(value), 'observed_ms': now_ms,
                    'provider_updated_ms': updated_ms, 'source': 'coingecko_coins_markets', 'coin_id': coin_id}
        return {coin: reusable[coin_id] for coin, coin_id in mapping.items() if coin_id in reusable}
