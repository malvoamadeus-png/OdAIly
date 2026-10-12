"""Restricted stdin/stdout FOMO bridge; opens a browser only for one request."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from .browser_lock import exclusive_browser
from .fast_evidence import (
    FOMO_ENTRY_URL, FOMO_NETWORK_IDS, _block_heavy_resources,
    _fomo_page_fetch, _fomo_items, _fomo_profile_dir, _fomo_statement,
)


def trending_snapshot(raw: str | bytes) -> list[dict] | None:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(message, dict):
        return None
    topic = set(str(message.get('topicId') or '').split(','))
    payload = message.get('payload') or {}
    if (message.get('type') == 'data' and message.get('topicType') == 'trending_tokens'
            and {'56','1399811149'} <= topic and isinstance(payload, dict)
            and payload.get('kind') == 'snapshot' and isinstance(payload.get('tokens'), list)):
        return payload['tokens']
    return None


def normalize_tokens(entries: list[dict]) -> list[dict]:
    chains = {56: 'bsc', 1399811149: 'solana'}
    result = {}
    for entry in entries:
        token = entry.get('token') or {}
        chain = chains.get(int(entry.get('networkId') or token.get('networkId') or 0))
        address = str(token.get('address') or entry.get('tokenAddress') or '').strip()
        if not chain or not address:
            continue
        if chain == 'bsc':
            address = address.lower()
        result[(chain, address)] = {
            'chain': chain, 'token_address': address,
            'symbol': token.get('symbol') or entry.get('ticker') or '',
            'name': token.get('name') or entry.get('tokenName') or '',
            'market_cap_usd': entry.get('marketCap', token.get('marketCap')),
        }
    return list(result.values())


def normalize_theses(items: list[dict]) -> list[dict]:
    # Nested FOMO comments contain price/cap/trade data. Return public prose
    # only, preserving the collector's text interface and blind evaluation.
    return [{key:item.get(key) for key in ('id','createdAt','userHandle','displayName')}
            | {'comment':_fomo_statement(item)} for item in items if _fomo_statement(item)]


def run(request: dict) -> dict:
    operation = request.get('operation')
    if operation not in {'health', 'trending', 'thesis'}:
        raise ValueError('unsupported operation')
    profile = _fomo_profile_dir()
    if not profile.exists():
        raise RuntimeError('login_profile_missing')
    if operation == 'health':
        return {'ready': True, 'browser': 'on_demand'}
    chain = str(request.get('chain') or '').lower()
    address = str(request.get('token_address') or '')
    if operation == 'thesis' and (chain not in FOMO_NETWORK_IDS or not address or len(address) > 64):
        raise ValueError('invalid thesis target')
    from playwright.sync_api import sync_playwright
    with exclusive_browser(timeout_seconds=90):
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), headless=False, viewport={'width':1440,'height':900},
                args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu'],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.route('**/*', _block_heavy_resources)
                snapshot = {}
                def capture_frame(raw):
                    entries = trending_snapshot(raw)
                    if entries is not None and 'tokens' not in snapshot:
                        snapshot['tokens'] = entries
                def capture_socket(socket):
                    if socket.url.startswith('wss://prod-api.fomo.family/ws'):
                        socket.on('framereceived', capture_frame)
                page.on('websocket', capture_socket)
                page.goto(FOMO_ENTRY_URL, wait_until='domcontentloaded', timeout=30000)
                if operation == 'thesis':
                    page.wait_for_timeout(1500)
                    response = _fomo_page_fetch(page, contract=address, network_id=FOMO_NETWORK_IDS[chain], last_id=None, timeout_ms=15000)
                    if not response.get('ok'):
                        raise RuntimeError(str(response.get('code') or 'thesis_failed'))
                    return {'items': normalize_theses(_fomo_items(response.get('body')))}
                deadline = time.monotonic() + 35
                while 'tokens' not in snapshot and time.monotonic() < deadline:
                    page.wait_for_timeout(250)
                if 'tokens' not in snapshot:
                    raise RuntimeError('complete_trending_snapshot_unavailable')
                entries = snapshot['tokens']
                return {'status':'success', 'complete_chains':['bsc','solana'],
                        'observed_at':datetime.now(timezone.utc).isoformat(),
                        'tokens':normalize_tokens(entries)}
            finally:
                context.close()


def main() -> int:
    # No shell commands or user-supplied executable paths are accepted.
    from pathlib import Path
    env_path = Path(__file__).resolve().parents[3] / '.env'
    if env_path.exists():
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    try:
        request = json.loads(sys.stdin.readline(65536))
        if not isinstance(request, dict):
            raise ValueError('invalid request')
        result = run(request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Authentication material never goes to stdout/stderr.
        print(json.dumps({'status':'error','error':type(exc).__name__ + ':' + str(exc)[:200]}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
