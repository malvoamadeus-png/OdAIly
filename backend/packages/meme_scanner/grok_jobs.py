"""Durable nonblocking Grok Bot requests for queued Meme jobs."""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from packages.common.paths import get_paths
from .grok_callback import create_callback_registration, callback_result_path
from .grok_routine import DEFAULT_CALLBACK_BASE_URL, load_bearer_headers, load_webhook_url, validate_callback_url


def setup(conn: Any) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS meme_grok_jobs(
        trigger_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, started_at TEXT NOT NULL,
        state TEXT NOT NULL, result_json TEXT, error TEXT)''')
    conn.commit()


def prepare(store: Any, job: Any, token: Any) -> tuple[bool, dict[str, Any]]:
    """Return (ready_for_writer, safe_audit); never wait for a callback here."""
    setup(store.conn)
    key = job['trigger_key']
    row = store.conn.execute('SELECT * FROM meme_grok_jobs WHERE trigger_key=?', (key,)).fetchone()
    root = get_paths().runtime_dir / 'grok_narrative_callbacks'
    if row is None:
        request_id = 'meme-' + uuid.uuid4().hex
        started = datetime.now(UTC).isoformat()
        # Commit before HTTP: after a crash do not resubmit an uncertain delivery.
        with store.conn:
            store.conn.execute('INSERT INTO meme_grok_jobs VALUES (?,?,?,?,NULL,NULL)',
                               (key, request_id, started, 'pending'))
        try:
            credentials = Path(os.getenv('GROK_ROUTINE_CREDENTIALS_FILE') or
                               get_paths().config_dir / 'grok.txt')
            headers = load_bearer_headers(credentials)
            webhook = os.getenv('GROK_ROUTINE_WEBHOOK_URL') or load_webhook_url(credentials)
            if not webhook:
                raise ValueError('Missing Grok webhook URL')
            validate_callback_url(webhook)
            secret = create_callback_registration(root, request_id, token.address)
            if secret is None:
                raise RuntimeError('Callback registration failed')
            base = validate_callback_url(os.getenv('GROK_CALLBACK_BASE_URL') or DEFAULT_CALLBACK_BASE_URL)
            response = requests.post(webhook, headers={**headers, 'Content-Type': 'application/json'},
                json={'type': 'meme_narrative', 'request_id': request_id, 'ca': token.address,
                      'chain': token.chain, 'token': token.symbol,
                      'callback_url': f'{base.rstrip("/")}/{request_id}/{secret}'},
                timeout=20, allow_redirects=False)
            if response.status_code not in (200, 202):
                raise RuntimeError(f'Grok webhook HTTP {response.status_code}')
            if response.status_code == 200:
                receipt = response.json()
                if not isinstance(receipt, dict) or receipt.get('success') is not True:
                    raise RuntimeError('Grok webhook rejected the request')
        except Exception as exc:
            # Never persist response text, URLs, headers or credentials in diagnostics.
            with store.conn:
                store.conn.execute("UPDATE meme_grok_jobs SET state='failed',error=? WHERE trigger_key=?",
                                   (type(exc).__name__, key))
        row = store.conn.execute('SELECT * FROM meme_grok_jobs WHERE trigger_key=?', (key,)).fetchone()
    state = row['state']
    result = json.loads(row['result_json']) if row['result_json'] else None
    if state == 'pending':
        try:
            record = json.loads(callback_result_path(root, row['request_id']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {}
        candidate = record.get('result')
        if (record.get('state') == 'complete' and isinstance(candidate, dict)
                and candidate.get('request_id') == row['request_id'] and candidate.get('ca') == token.address):
            result = candidate
            state = 'complete' if candidate.get('ok') is True else 'failed'
        elif (datetime.now(UTC) - datetime.fromisoformat(row['started_at'])).total_seconds() >= 900:
            state = 'timeout'
        if state != 'pending':
            with store.conn:
                store.conn.execute('UPDATE meme_grok_jobs SET state=?,result_json=? WHERE trigger_key=?',
                                   (state, json.dumps(result, ensure_ascii=False) if result else None, key))
    audit = {'request_id': row['request_id'], 'state': state, 'started_at': row['started_at'],
             'error': row['error'], 'result': result}
    if state == 'pending':
        with store.conn:
            store.conn.execute('''UPDATE jobs SET status='retry_wait', reason='grok_pending',
                next_attempt_at=?, attempts=MAX(0,attempts-1), updated_at=?, narrative_json=? WHERE id=?''',
                ((datetime.now(UTC) + timedelta(seconds=15)).isoformat(), datetime.now(UTC).isoformat(),
                 json.dumps({'status': 'pending', 'grok_routine': audit,
                             'discovery': json.loads(job['evidence_json'] or '{}')}, ensure_ascii=False), job['id']))
        return False, audit
    return True, audit
