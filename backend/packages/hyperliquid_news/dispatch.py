"""Hard-rule events -> shared write flow; durable semantic dedup, no judging."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.local_pipeline.client import LocalPipelineClient
from packages.x_processing.models import HYPERLIQUID_SOURCE
from .copy import source_url
from .search import search_material
from .eligibility import news_eligibility
from .store import NewsStore, encode


def fingerprint(event: dict[str, Any]) -> str:
    evidence = event['evidence']
    stable = {'addresses': sorted(event['addresses']), 'coin': event['coin'],
              'kinds': sorted(event['kinds']), 'signals': evidence.get('trigger_levels', {}),
              'fills': sorted(evidence.get('actions', {}).get('fill_keys', [])),
              'sides': evidence.get('sides'), 'reversal': evidence.get('reversal_basis'),
              'net_change': evidence.get('net_position_change')}
    if set(event['kinds']) & {'large_profit','large_loss','profit_giveback','profit_to_loss'}:
        stable['pnl_change'] = {
            'anchor': evidence.get('previous_reported_floating_pnl'),
            'previous': evidence.get('news_change', {}).get('previous_floating_pnl'),
            'current': (evidence.get('position') or {}).get('unrealized_pnl')}
    if evidence.get('reversal_basis'):
        stable['direction'] = (evidence.get('position') or {}).get('size')
    if evidence.get('sides'):
        stable['positions'] = evidence.get('positions')
    # Episode anchor allows a later close/reopen cycle to trigger again, while
    # the same first-observed unchanged position cannot create a duplicate task.
    stable['episode_started_ms'] = evidence.get('episode_started_ms')
    return hashlib.sha256(encode(stable).encode()).hexdigest()


class EventDispatcher:
    def __init__(self, store: NewsStore, *, pipeline_client: LocalPipelineClient | None = None) -> None:
        self.store = store
        self.pipeline_client = pipeline_client or LocalPipelineClient()

    def dispatch(self, *, limit: int = 3, event_keys: list[str] | None = None) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError('Dispatch limit must be 1..100')
        results = []
        with connect_sqlite(self.store.path) as conn:
            rows = conn.execute('''SELECT c.event_key,c.payload FROM hl_news_candidates c
                LEFT JOIN hl_news_dispatch d ON d.event_key=c.event_key
                WHERE d.event_key IS NULL OR d.status IN ('created','enqueue_failed')
                ORDER BY c.observed_ms DESC,c.event_key LIMIT 500''').fetchall()
        for row in rows:
            if event_keys and row['event_key'] not in event_keys:
                continue
            if len(results) >= limit:
                break
            import json
            event = json.loads(row['payload'])
            result = self._create_task(event)
            if result['status'] in ('duplicate', 'no_change_evidence'):
                results.append(result)
                continue
            try:
                self.pipeline_client.submit_job(job_type='write_flow', task_id=result['task_id'],
                                               source=HYPERLIQUID_SOURCE, source_item_id=event['key'])
                result['status'] = 'queued'
                self._set_status(event['key'], 'queued', None)
            except Exception as exc:
                result.update(status='enqueue_failed', error=str(exc))
                self._set_status(event['key'], 'enqueue_failed', str(exc))
            results.append(result)
        return results

    def _set_status(self, key: str, status: str, error: str | None) -> None:
        with connect_sqlite(self.store.path) as conn:
            conn.execute('UPDATE hl_news_dispatch SET status=?,error=?,updated_at=? WHERE event_key=?',
                         (status, error, datetime.now(UTC).isoformat(), key))

    def _create_task(self, event: dict[str, Any]) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        signature = fingerprint(event)
        with connect_sqlite(self.store.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing = conn.execute('SELECT * FROM hl_news_dispatch WHERE event_key=?', (event['key'],)).fetchone()
            if existing and news_eligibility(event)['eligible']:
                return {'event_key': event['key'], 'task_id': existing['task_id'], 'status': existing['status']}
            if not news_eligibility(event)['eligible']:
                if existing:
                    conn.execute('UPDATE hl_news_dispatch SET status=?,error=?,updated_at=? WHERE event_key=?',
                                 ('no_change_evidence', 'baseline_without_change', now, event['key']))
                    return {'event_key': event['key'], 'task_id': existing['task_id'], 'status': 'no_change_evidence'}
                conn.execute('INSERT INTO hl_news_dispatch VALUES(?,?,?,?,?,?,?)',
                             (event['key'], signature, None, 'no_change_evidence', 'baseline_without_change', now, now))
                return {'event_key': event['key'], 'task_id': None, 'status': 'no_change_evidence'}
            duplicate = conn.execute('SELECT task_id FROM hl_news_dispatch WHERE signature=? AND status!=?',
                                     (signature, 'duplicate')).fetchone()
            if duplicate:
                conn.execute('INSERT INTO hl_news_dispatch VALUES(?,?,?,?,?,?,?)',
                             (event['key'], signature, duplicate['task_id'], 'duplicate', None, now, now))
                return {'event_key': event['key'], 'task_id': duplicate['task_id'], 'status': 'duplicate'}
            metadata = {'writer_template_key': 'hyperliquid_writer', 'force_backend_only': True,
                        'hyperliquid_event_key': event['key'], 'rule_kinds': event['kinds']}
            url = source_url(event)
            search_title, search_content = search_material(event)
            conn.execute('''INSERT OR IGNORE INTO tasks(source,source_item_id,source_url,title,content,
                published_at,raw_payload,metadata,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?, 'judged',?,?)''',
                (HYPERLIQUID_SOURCE, event['key'], url, search_title, search_content,
                 datetime.fromtimestamp(event['observed_ms']/1000, UTC).isoformat(), encode(event), encode(metadata), now, now))
            task_id = conn.execute('SELECT id FROM tasks WHERE source=? AND source_item_id=?',
                                   (HYPERLIQUID_SOURCE, event['key'])).fetchone()['id']
            conn.execute('INSERT OR IGNORE INTO x_task_pipeline(task_id,news_type) VALUES(?,?)',
                         (task_id, 'onchain'))
            conn.execute('INSERT INTO hl_news_dispatch VALUES(?,?,?,?,?,?,?)',
                         (event['key'], signature, task_id, 'created', None, now, now))
        return {'event_key': event['key'], 'task_id': task_id, 'status': 'created'}
