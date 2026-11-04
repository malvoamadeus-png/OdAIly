"""On-demand Solana market-cap verification before narrative work."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import requests

GECKOTERMINAL_URL = 'https://api.geckoterminal.com/api/v2'
JUPITER_URL = 'https://lite-api.jup.ag/price/v3'
SOLANA_RPC_URL = 'https://api.mainnet-beta.solana.com'


def positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


class VerificationClient:
    def __init__(self, timeout: float = 8) -> None:
        self.timeout = timeout

    def gecko(self, address: str, pair: str | None) -> dict[str, Any]:
        response = requests.get(
            f'{GECKOTERMINAL_URL}/networks/solana/tokens/{quote(address, safe="")}/pools',
            params={'page': 1}, timeout=self.timeout,
        )
        response.raise_for_status()
        pools = response.json().get('data') or []
        # Solana mint identities are case sensitive. Only accept pools for this mint.
        pools = [p for p in pools if isinstance(p, dict) and
                 str(p.get('relationships', {}).get('base_token', {}).get('data', {}).get('id', '')) == f'solana_{address}']
        matching = [p for p in pools if pair and p.get('attributes', {}).get('address') == pair]
        matched = bool(matching)
        pool = matching[0] if matching else max(pools,
            key=lambda p: positive(p.get('attributes', {}).get('reserve_in_usd')) or 0, default=None)
        attrs = (pool or {}).get('attributes') or {}
        return {'market_cap': positive(attrs.get('fdv_usd')), 'pool_matched': matched,
                'pool_address': attrs.get('address')}

    def jupiter(self, address: str) -> dict[str, Any]:
        response = requests.get(JUPITER_URL, params={'ids': address}, timeout=self.timeout)
        response.raise_for_status()
        price = positive((response.json().get(address) or {}).get('usdPrice'))
        if price is None:
            return {'market_cap': None}
        response = requests.post(SOLANA_RPC_URL, timeout=self.timeout, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'getTokenSupply', 'params': [address],
        })
        response.raise_for_status()
        supply = response.json().get('result', {}).get('value', {})
        amount = int(supply['amount'])
        decimals = int(supply['decimals'])
        if amount <= 0 or not 0 <= decimals <= 30:
            return {'market_cap': None}
        return {'market_cap': positive(price * amount / 10 ** decimals), 'price': price}


def verify_market_cap(token: Any, client: VerificationClient | None = None) -> dict[str, Any]:
    if token.chain != 'solana':
        return {'status': 'not_applicable'}
    client = client or VerificationClient()
    baseline = positive(token.market_cap)
    result: dict[str, Any] = {'status': 'unavailable', 'checked_at': datetime.now(UTC).isoformat(),
                             'chain': token.chain, 'address': token.address,
                             'dexscreener_market_cap': baseline, 'threshold': 0.15, 'providers': {}}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {'geckoterminal': executor.submit(client.gecko, token.address, token.raw.get('pair_address')),
                   'jupiter': executor.submit(client.jupiter, token.address)}
        for provider, future in futures.items():
            try:
                record = future.result()
                cap = positive(record.get('market_cap'))
                record['market_cap'] = cap
                record['status'] = 'success' if cap else 'missing'
                if cap and baseline:
                    deviation = abs(Decimal(str(cap)) - Decimal(str(baseline))) / Decimal(str(baseline))
                    record['deviation'] = float(deviation)
                    record['within_threshold'] = deviation <= Decimal('0.15')
                result['providers'][provider] = record
            except Exception as exc:
                result['providers'][provider] = {'status': 'failed', 'error': type(exc).__name__, 'market_cap': None}
    valid = [r for r in result['providers'].values() if 'within_threshold' in r]
    if valid:
        result['status'] = 'passed' if all(r['within_threshold'] for r in valid) else 'suspect'
    return result
