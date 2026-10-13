from __future__ import annotations

import re
import time
from typing import Any

from packages.whale_watch.hyperliquid_client import HyperliquidClient

ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')


def address_key(value: Any) -> str:
    if not isinstance(value, str) or not ADDRESS.fullmatch(value):
        raise ValueError('Expected 42-character hexadecimal address')
    return value.lower()


class NewsClient(HyperliquidClient):
    """Sequential client with conservative per-IP request-weight pacing.

    600 weights/minute reserved to this client, below public 1200 IP limit.
    Other processes on the same IP still require operational coordination.
    """
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._next_request = 0.0

    def _post_info(self, payload: dict[str, Any]) -> Any:
        weight = 120 if payload['type'] in ('userFills', 'userFillsByTime') else (
            2 if payload['type'] == 'clearinghouseState' else 120 if payload['type'] == 'recentTrades' else 20)
        last_error = None
        for attempt in range(self.max_attempts):
            delay = self._next_request - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._next_request = time.monotonic() + weight / 10
            try:
                response = self.session.post(f'{self.base_url}/info', json=payload, timeout=self.timeout_seconds)
                response.raise_for_status()
                return response.json()
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(self.backoff_seconds * (attempt + 1))
        raise RuntimeError(f'Hyperliquid request failed type={payload["type"]}: {last_error}') from last_error

    def dex_names(self) -> list[str]:
        payload = self._post_info({'type': 'perpDexs'})
        if not isinstance(payload, list):
            raise ValueError('Unexpected perpDexs payload')
        return [''] + [str(row['name']) for row in payload if isinstance(row, dict)]

    def markets(self, dex: str) -> dict[str, dict[str, Any]]:
        payload = self._post_info({'type': 'metaAndAssetCtxs', 'dex': dex})
        if not isinstance(payload, list) or len(payload) != 2:
            raise ValueError('Unexpected metaAndAssetCtxs payload')
        universe, contexts = payload[0]['universe'], payload[1]
        if len(universe) != len(contexts):
            raise ValueError('Market/context length mismatch')
        from .models import number
        result = {}
        for meta, ctx in zip(universe, contexts):
            if meta.get('isDelisted'):
                continue
            coin = str(meta['name'])
            if dex and not coin.startswith(dex + ':'):
                coin = dex + ':' + coin
            result[coin] = {'dex': dex, 'mark': str(number(ctx['markPx'])),
                            'oi_notional': str(number(ctx['openInterest']) * number(ctx['markPx'])),
                            'collateral_token': payload[0].get('collateralToken', 0)}
        return result

    def positions(self, address: str, dex: str) -> dict[str, Any]:
        payload = self._post_info({'type': 'clearinghouseState', 'user': address_key(address), 'dex': dex})
        if not isinstance(payload, dict) or not isinstance(payload.get('assetPositions'), list):
            raise ValueError('Unexpected clearinghouseState payload')
        return payload

    def recent_trades(self, coin: str) -> list[dict[str, Any]]:
        payload = self._post_info({'type': 'recentTrades', 'coin': coin})
        if not isinstance(payload, list):
            raise ValueError('Unexpected recentTrades payload')
        return payload

    def leaderboard(self) -> list[dict[str, Any]]:
        response = self.session.get('https://stats-data.hyperliquid.xyz/Mainnet/leaderboard', timeout=self.timeout_seconds)
        response.raise_for_status()
        rows = response.json()['leaderboardRows']
        if not isinstance(rows, list):
            raise ValueError('Unexpected leaderboard payload')
        return rows

    def history(self, address: str, start_ms: int, end_ms: int, *, max_pages: int = 6) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from .history import fill_key
        cursor = start_ms
        collected = {}
        status = {'start_ms': start_ms, 'end_ms': end_ms, 'coverage': 'retained_api_history',
                  'pages': 0, 'pagination_complete': False, 'reason': 'page_budget'}
        for _ in range(max_pages):
            page = self._post_info({'type': 'userFillsByTime', 'user': address_key(address),
                                   'startTime': cursor, 'endTime': end_ms, 'aggregateByTime': False})
            if not isinstance(page, list):
                raise ValueError('Unexpected userFillsByTime payload')
            status['pages'] += 1
            for fill in page:
                stamp = int(fill['time'])
                if not start_ms <= stamp <= end_ms:
                    raise ValueError('Fill outside requested time range')
                collected[fill_key(fill)] = fill
            if len(page) < 2000:
                status.update(pagination_complete=True, reason='available_history_exhausted')
                break
            last = max(int(f['time']) for f in page)
            if last <= cursor:
                status['reason'] = 'timestamp_saturated'
                break
            # Inclusive boundary prevents skipping fills sharing the last timestamp.
            cursor = last
        rows = sorted(collected.values(), key=lambda f: (int(f['time']), int(f['tid'])))
        status['oldest_returned_ms'] = int(rows[0]['time']) if rows else None
        status['next_start_ms'] = cursor
        return rows, status
