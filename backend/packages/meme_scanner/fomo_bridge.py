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
    _fomo_page_fetch, _fomo_items, _fomo_profile_dir,
)


TRENDING_SCRIPT = """async ({jwt, networks, timeoutMs}) => {
  return await new Promise((resolve, reject) => {
    const ws = new WebSocket('wss://prod-api.fomo.family/ws');
    const timer = setTimeout(() => done(new Error('snapshot_timeout')), timeoutMs);
    let finished = false;
    function done(error, value) {
      if (finished) return;
      finished = true; clearTimeout(timer); ws.close();
      if (error) reject(error); else resolve(value);
    }
    ws.onmessage = event => {
      let message; try { message = JSON.parse(event.data); } catch (_) { return; }
      if (message.type === 'challenge') ws.send(JSON.stringify({type:'challengeResponse', jwt}));
      if (message.type === 'challengeAccepted') ws.send(JSON.stringify({
        type:'subscribe', topicType:'trending_tokens', topicId:networks.join(',')
      }));
      if (message.type === 'error') done(new Error('trending_auth_or_subscription_failed'));
      if (message.type === 'data' && message.topicType === 'trending_tokens'
          && message.payload?.kind === 'snapshot' && Array.isArray(message.payload.tokens))
        done(null, message.payload.tokens);
    };
    ws.onerror = () => done(new Error('websocket_failed'));
    ws.onclose = () => { if (!finished) done(new Error('websocket_closed')); };
  });
}"""


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
                auth = {}
                def capture(response):
                    if response.status != 200 or not response.url.startswith('https://prod-api.fomo.family/v2/users'):
                        return
                    value = response.request.headers.get('authorization', '')
                    if value.startswith('Bearer '):
                        auth['jwt'] = value[7:]
                page.on('response', capture)
                page.goto(FOMO_ENTRY_URL, wait_until='domcontentloaded', timeout=30000)
                if operation == 'thesis':
                    page.wait_for_timeout(1500)
                    response = _fomo_page_fetch(page, contract=address, network_id=FOMO_NETWORK_IDS[chain], last_id=None, timeout_ms=15000)
                    if not response.get('ok'):
                        raise RuntimeError(str(response.get('code') or 'thesis_failed'))
                    return {'items': _fomo_items(response.get('body'))}
                deadline = time.monotonic() + 35
                while not auth and time.monotonic() < deadline:
                    page.wait_for_timeout(250)
                if not auth:
                    raise RuntimeError('authenticated_session_unavailable')
                entries = page.evaluate(TRENDING_SCRIPT, {'jwt':auth['jwt'], 'networks':[56,1399811149], 'timeoutMs':20000})
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
