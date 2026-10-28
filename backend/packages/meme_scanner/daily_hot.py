"""Hourly GMGN leader discovery; first qualification is a permanent identity.

The CLI command, data.rank contract and ratio normalization are migrated from
HideOnBush's leader_baseline/GmgnClient. No leader scoring or factor matching.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

CHAINS = ('solana', 'bsc', 'base', 'eth', 'robinhood')
QUERIES = (('1h', 'change1h', .20), ('6h', 'marketcap', .30), ('24h', 'marketcap', None))


def setup(conn: Any) -> None:
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS daily_hot_chains(chain TEXT PRIMARY KEY, initialized_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_hot_seen(
            chain TEXT NOT NULL, address TEXT NOT NULL, first_qualified_at TEXT NOT NULL,
            baseline INTEGER NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY(chain,address));
        CREATE TABLE IF NOT EXISTS daily_hot_scans(
            id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, completed_at TEXT,
            summary_json TEXT NOT NULL DEFAULT '{}');
    ''')


def fetch_rows(chain: str, interval: str, order: str) -> list[dict[str, Any]]:
    from .gmgn import GMGN, ensure_cli_ready, gmgn_subprocess_env
    if not ensure_cli_ready():
        raise RuntimeError('GMGN CLI is not ready')
    command = [
        GMGN, 'market', 'trending', '--chain', 'sol' if chain == 'solana' else chain,
        '--interval', interval, '--limit', '50', '--order-by', order, '--raw',
    ]
    if chain == 'solana':
        command.extend(['--filter', 'not_wash_trading'])
    result = subprocess.run(
        command,
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=45, env=gmgn_subprocess_env(), check=False,
    )
    # Do not copy CLI stderr/stdout into logs: it may contain private config.
    if result.returncode:
        raise RuntimeError(f'GMGN trending failed (exit {result.returncode})')
    payload = json.loads(result.stdout)
    data = payload.get('data') if isinstance(payload, dict) else None
    rows = data.get('rank') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError('GMGN trending response has no data.rank list')
    return [row for row in rows if isinstance(row, dict)]


def ratio(value: Any) -> float:
    try:
        number = float(value)
    except (ValueError, TypeError):
        return float('-inf')
    if not math.isfinite(number):
        return float('-inf')
    return number / 100 if abs(number) > 5 else number


def ranked_candidates(chain: str, views: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for interval, _, threshold in QUERIES:
        for raw in views.get(interval, []):
            try:
                rank = int(raw['rank'])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            address = str(raw.get('address') or raw.get('token_address') or '').strip()
            if not address or not 1 <= rank <= 20:
                continue
            if chain != 'solana':
                address = address.lower()
                if not address.startswith('0x'):
                    continue
            elif address.startswith('0x'):
                continue
            change = ratio(raw.get('price_change_percent'))
            if threshold is not None and change < threshold:
                continue
            entry = grouped.setdefault(address, {
                'address': address, 'chain': chain, 'symbol': raw.get('symbol') or '',
                'name': raw.get('name') or '', 'launchpad_platform': raw.get('launchpad_platform') or 'gmgn',
                '_market_metrics_complete': False, 'matches': [],
            })
            entry['matches'].append({'interval': interval, 'rank': rank,
                                     'change_ratio': change if math.isfinite(change) else None})
    return grouped


def scan(store: Any, *, fetch: Any = fetch_rows, market: Any = None, force: bool = False) -> dict[str, Any]:
    from .scanner import (_merge_dexscreener_price_info, get_dexscreener_client,
                          token_from_row, volume_ratio_gate)
    setup(store.conn)
    stamp = datetime.now(UTC).isoformat()
    last = store.conn.execute('SELECT started_at FROM daily_hot_scans ORDER BY id DESC LIMIT 1').fetchone()
    if last and not force and (datetime.now(UTC) - datetime.fromisoformat(last[0])).total_seconds() < 3600:
        return {'status': 'not_due'}
    cursor = store.conn.execute('INSERT INTO daily_hot_scans(started_at) VALUES (?)', (stamp,))
    run_id = cursor.lastrowid
    store.conn.commit()
    summary: dict[str, Any] = {'queued': 0, 'baseline': 0, 'errors': {}, 'qualified': 0}
    client = market or get_dexscreener_client()
    for chain in CHAINS:
        try:
            views = {}
            for interval, order, _ in QUERIES:
                views[interval] = fetch(chain, interval, order)
                if fetch is fetch_rows:
                    time.sleep(max(0, float(os.getenv('MEME_HOT_GMGN_REQUEST_INTERVAL') or 35)))
            candidates = ranked_candidates(chain, views)
            qualified = []
            addresses = list(candidates)
            # Even an empty response is a complete successful market lookup.
            for offset in range(0, len(addresses), 30):
                batch = addresses[offset:offset + 30]
                pairs = client.price_info('ethereum' if chain == 'eth' else chain, batch)
                for address in batch:
                    token = token_from_row(candidates[address])
                    token = _merge_dexscreener_price_info(token, pairs.get(address))
                    if (token.metrics_complete and math.isfinite(token.market_cap)
                            and math.isfinite(token.volume_24h) and token.market_cap >= 1_000_000
                            and token.volume_ratio >= volume_ratio_gate(token.market_cap)):
                        qualified.append(token)
            baseline = store.conn.execute('SELECT 1 FROM daily_hot_chains WHERE chain=?', (chain,)).fetchone() is None
            # Ledger and job insertion must commit together, including the initialization.
            with store.conn:
                for token in qualified:
                    evidence = {'first_qualified_at': stamp, 'matches': candidates[token.address]['matches']}
                    inserted = store.conn.execute('''INSERT OR IGNORE INTO daily_hot_seen
                        VALUES (?,?,?,?,?)''', (chain, token.address, stamp, int(baseline), json.dumps(evidence))).rowcount
                    summary['qualified'] += 1
                    if not inserted:
                        continue
                    if baseline:
                        summary['baseline'] += 1
                    else:
                        key = f'daily_hot:{chain}:{token.address}'
                        store.conn.execute('''INSERT OR IGNORE INTO jobs
                            (address,trigger_key,payload_json,trigger_kind,queued_at,status,evidence_json,updated_at)
                            VALUES (?,?,?,'daily_hot',?,'queued',?,?)''',
                            (token.address, key, json.dumps(token.raw, ensure_ascii=False), stamp,
                             json.dumps(evidence), stamp))
                        summary['queued'] += 1
                store.conn.execute('INSERT OR IGNORE INTO daily_hot_chains VALUES (?,?)', (chain, stamp))
        except Exception as exc:
            summary['errors'][chain] = type(exc).__name__ + ': ' + str(exc)[:200]
    with store.conn:
        store.conn.execute('UPDATE daily_hot_scans SET completed_at=?,summary_json=? WHERE id=?',
                           (datetime.now(UTC).isoformat(), json.dumps(summary), run_id))
        store.conn.execute("DELETE FROM daily_hot_scans WHERE julianday(started_at) < julianday('now','-7 days')")
    return summary


def worker(db_path: str, stop: Any) -> None:
    from pathlib import Path
    from .scanner import Store
    store = Store(Path(db_path))
    try:
        while not stop.is_set():
            try:
                result = scan(store)
                if result.get('status') != 'not_due':
                    print('[meme-hot] ' + json.dumps(result, ensure_ascii=False), flush=True)
            except Exception as exc:
                print(f'[meme-hot] scan failed: {type(exc).__name__}', flush=True)
            stop.wait(30)
    finally:
        store.close()
