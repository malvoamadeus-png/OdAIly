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
from .grok_callback import callback_ca_matches, create_callback_registration, callback_result_path
from .grok_routine import DEFAULT_CALLBACK_BASE_URL, load_bearer_headers, load_webhook_url, validate_callback_url

GROK_WAIT_TIMEOUT_SECONDS = 5 * 60
GROK_WAIT_TIMEOUT = timedelta(seconds=GROK_WAIT_TIMEOUT_SECONDS)


def setup(conn: Any) -> None:
    if hasattr(conn, "record_grok_request"):
        return
    conn.execute('''CREATE TABLE IF NOT EXISTS meme_grok_jobs(
        trigger_key TEXT PRIMARY KEY, request_id TEXT NOT NULL, started_at TEXT NOT NULL,
        state TEXT NOT NULL, result_json TEXT, error TEXT, webhook_run_id TEXT)''')
    columns = {row[1] for row in conn.execute("PRAGMA table_info(meme_grok_jobs)").fetchall()}
    if "webhook_run_id" not in columns:
        conn.execute("ALTER TABLE meme_grok_jobs ADD COLUMN webhook_run_id TEXT")
    conn.commit()


def prepare(store: Any, job: Any, token: Any) -> tuple[bool, dict[str, Any]]:
    """Return (ready_for_writer, safe_audit); never wait for a callback here."""
    key = job['trigger_key']
    if hasattr(store, "record_grok_request"):
        row = store.get_grok_request(key)
    else:
        setup(store.conn)
        row = store.conn.execute('SELECT * FROM meme_grok_jobs WHERE trigger_key=?', (key,)).fetchone()
    root = get_paths().runtime_dir / 'grok_narrative_callbacks'
    created_by_this_worker = False
    created_request = row is None
    if row is None:
        request_id = 'meme-' + uuid.uuid4().hex
        started = datetime.now(UTC).isoformat()
        # Commit before HTTP: after a crash do not resubmit an uncertain delivery.
        if hasattr(store, "record_grok_request"):
            row, created_by_this_worker = store.record_grok_request(trigger_key=key, request_id=request_id, started_at=started)
            request_id = row['request_id']
        else:
            with store.conn:
                store.conn.execute('''INSERT INTO meme_grok_jobs(
                    trigger_key, request_id, started_at, state, result_json, error, webhook_run_id
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL)''',
                                   (key, request_id, started, 'pending'))
            row = store.conn.execute('SELECT * FROM meme_grok_jobs WHERE trigger_key=?', (key,)).fetchone()
            created_by_this_worker = True
    if hasattr(store, "record_grok_request") and not created_request and row is not None and row['state'] == 'pending':
        row, created_by_this_worker = store.record_grok_request(
            trigger_key=key, request_id=row['request_id'], started_at=row['started_at'],
        )
    if created_by_this_worker:
        request_id = row['request_id']
        try:
            credentials = Path(os.getenv('GROK_ROUTINE_CREDENTIALS_FILE') or
                               get_paths().config_dir / 'grok.txt')
            headers = load_bearer_headers(credentials)
            webhook = os.getenv('GROK_ROUTINE_WEBHOOK_URL') or load_webhook_url(credentials)
            if not webhook:
                raise ValueError('Missing Grok webhook URL')
            validate_callback_url(webhook)
            secret = create_callback_registration(
                root, request_id, token.address, pending_retention=GROK_WAIT_TIMEOUT,
            )
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
            else:
                try:
                    parsed_receipt = response.json()
                except (AttributeError, ValueError):
                    parsed_receipt = {}
                receipt = parsed_receipt if isinstance(parsed_receipt, dict) else {}
            webhook_run_id = receipt.get('runUuid') if isinstance(receipt, dict) else None
            if isinstance(webhook_run_id, str) and webhook_run_id.strip() and len(webhook_run_id) <= 256:
                if hasattr(store, "update_grok_request"):
                    store.update_grok_request(key, webhook_run_id=webhook_run_id)
                else:
                    with store.conn:
                        store.conn.execute(
                            "UPDATE meme_grok_jobs SET webhook_run_id=? WHERE trigger_key=?",
                            (webhook_run_id, key),
                        )
        except Exception as exc:
            # Never persist response text, URLs, headers or credentials in diagnostics.
            if hasattr(store, "update_grok_request"):
                store.update_grok_request(key, state='failed', error=type(exc).__name__)
            else:
                with store.conn:
                    store.conn.execute("UPDATE meme_grok_jobs SET state='failed',error=? WHERE trigger_key=?",
                                       (type(exc).__name__, key))
        row = store.get_grok_request(key) if hasattr(store, "get_grok_request") else store.conn.execute(
            'SELECT * FROM meme_grok_jobs WHERE trigger_key=?', (key,)
        ).fetchone()
    state = row['state']
    result = json.loads(row['result_json']) if row['result_json'] else None
    started_at = datetime.fromisoformat(row['started_at'])
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    wait_deadline = started_at + GROK_WAIT_TIMEOUT
    if state == 'pending':
        try:
            record = json.loads(callback_result_path(root, row['request_id']).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            record = {}
        candidate = record.get('result')
        if (record.get('state') == 'complete' and isinstance(candidate, dict)
                and candidate.get('request_id') == row['request_id']
                and callback_ca_matches(candidate.get('ca'), token.address)):
            result = candidate
            state = 'complete' if candidate.get('ok') is True else 'failed'
        elif datetime.now(UTC) >= wait_deadline:
            state = 'timeout'
        if state != 'pending':
            if hasattr(store, "update_grok_request"):
                store.update_grok_request(key, state=state, result_json=json.dumps(result, ensure_ascii=False) if result else None)
            else:
                with store.conn:
                    store.conn.execute('UPDATE meme_grok_jobs SET state=?,result_json=? WHERE trigger_key=?',
                                       (state, json.dumps(result, ensure_ascii=False) if result else None, key))
    audit = {'request_id': row['request_id'], 'state': state, 'started_at': row['started_at'],
             'webhook_run_id': row['webhook_run_id'], 'error': row['error'], 'result': result}
    if state == 'pending':
        now = datetime.now(UTC)
        next_at = min(now + timedelta(seconds=15), wait_deadline).isoformat()
        updated_at = now.isoformat()
        narrative_json = json.dumps({'status': 'pending', 'grok_routine': audit,
                                     'discovery': json.loads(job['evidence_json'] or '{}')}, ensure_ascii=False)
        if hasattr(store, "defer_grok_job"):
            if not store.defer_grok_job(job_id=job['id'], next_attempt_at=next_at, updated_at=updated_at, narrative_json=narrative_json,
                                        claim_token=job.get('claim_token') if isinstance(job, dict) else None):
                raise RuntimeError("Meme job claim was superseded before Grok deferral")
        else:
            with store.conn:
                store.conn.execute('''UPDATE jobs SET status='retry_wait', reason='grok_pending',
                    next_attempt_at=?, attempts=MAX(0,attempts-1), updated_at=?, narrative_json=? WHERE id=?''',
                    (next_at, updated_at, narrative_json, job['id']))
        return False, audit
    return True, audit
