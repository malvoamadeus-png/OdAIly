"""Bounded dependency-error cleanup, with counters committed before details disappear."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import ClosingSQLiteConnection
from .error_policy import ERROR_KEYS, EXTERNAL_CATEGORIES, day_start_ms, error_messages, external_error, failure_category, scrub_external_errors

DAILY_SCHEMA = '''CREATE TABLE IF NOT EXISTS odaily_external_error_daily (
 error_date TEXT NOT NULL, source TEXT NOT NULL, stage TEXT NOT NULL, category TEXT NOT NULL,
 occurrences INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(error_date,source,stage,category)
)'''
COUNTED_COLUMN = 'odaily_external_error_counted'
COUNTED_TABLES = frozenset({'x_processing_attempts', 'x_capture_attempts', 'binance_square_attempts',
                          'jobs', 'meme_grok_jobs', 'trigger_events', 'fact_checks', 'auditor_checks',
                          'editor_plugin_generation_logs', 'business_message_inbox'})


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _install_counters(conn: sqlite3.Connection, table: str, columns: dict[str, Any], database_key: str) -> bool:
    """Count known dependency failures in the source transaction, without a per-call ledger."""
    errors = [key for key in ERROR_KEYS if key in columns]
    if table in {'jobs', 'trigger_events'} and 'reason' in columns:
        errors.append('reason')
    if table not in COUNTED_TABLES or not errors:
        return False
    if COUNTED_COLUMN not in columns:
        conn.execute(f'ALTER TABLE {_quote(table)} ADD COLUMN {COUNTED_COLUMN} INTEGER NOT NULL DEFAULT 0')
    text = 'lower(' + " || ' ' || ".join(f"COALESCE(CAST(NEW.{_quote(key)} AS TEXT),'')" for key in errors) + ')'
    def matches(terms: tuple[str, ...]) -> str:
        return '(' + ' OR '.join(f"{text} LIKE '%{term}%'" for term in terms) + ')'
    expected = matches(('database is locked', 'database table is locked', 'no space left', 'disk i/o', 'disk is full', 'unable to open database', 'local_storage', 'freshness', 'no target-author posts', 'no_target_author', 'no new posts', 'empty_result', 'expected_result'))
    auth = matches(('401', '402', '403', '429', 'access denied', 'rate limit', 'quota', 'authenticationerror', 'permissiondenied', 'upstream_auth'))
    timeout = matches(('timeout', 'timed out', 'time out', 'upstream_timeout'))
    http = matches(('http', '500', '502', '503', '504', '404', 'badgateway', 'service unavailable', 'upstream_http', '408'))
    network = matches(('connection reset', 'connection refused', 'connectionerror', 'name resolution', 'sslerror', 'networkerror', 'upstream_network'))
    category = f"CASE WHEN {expected} THEN NULL WHEN {auth} THEN 'upstream_auth' WHEN {timeout} THEN 'upstream_timeout' WHEN {http} THEN 'upstream_http' WHEN {network} THEN 'upstream_network' END"
    status_key = next((key for key in ('status', 'state') if key in columns), None)
    failed = f"(lower(NEW.{_quote(status_key)}) LIKE '%fail%' OR lower(NEW.{_quote(status_key)}) LIKE '%retry%' OR lower(NEW.{_quote(status_key)}) IN ('error','exhausted'))" if status_key else '1'
    source = f"COALESCE(NEW.source,'{database_key}')" if 'source' in columns else f"'{database_key}'"
    if table == 'x_processing_attempts' and conn.execute("SELECT 1 FROM sqlite_master WHERE name='tasks'").fetchone():
        source = "COALESCE((SELECT source FROM tasks WHERE id=NEW.task_id),'main')"
    stage = "COALESCE(NEW.stage,'unknown')" if 'stage' in columns else "'capture'" if table.endswith('_attempts') else f"'{table}'"
    time_key = next((key for key in ('finished_at', 'completed_at', 'updated_at', 'started_at', 'created_at') if key in columns), None)
    date = f"COALESCE(date(NEW.{_quote(time_key)},'+8 hours'),date('now','+8 hours'))" if time_key else "date('now','+8 hours')"
    changes = [f'NEW.{_quote(key)} IS NOT OLD.{_quote(key)}' for key in (*errors, 'status', 'state', 'attempts', 'attempt_count', 'retry_count') if key in columns]
    for operation in ('INSERT', 'UPDATE'):
        condition = '1' if operation == 'INSERT' else f"(NEW.{COUNTED_COLUMN}=0 OR " + ' OR '.join(changes) + ')'
        name = _quote('odaily_dependency_counter_' + table + '_' + operation.lower())
        conn.execute(f'''CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON {_quote(table)}
            WHEN {failed} AND ({category}) IS NOT NULL AND {condition}
            BEGIN
                INSERT INTO odaily_external_error_daily VALUES({date},{source},{stage},{category},1)
                ON CONFLICT(error_date,source,stage,category) DO UPDATE SET occurrences=occurrences+1;
                UPDATE {_quote(table)} SET {COUNTED_COLUMN}=1 WHERE rowid=NEW.rowid;
            END''')
    return True


def cleanup_dependency_errors(path: Path, *, tables: list[str], database_key: str,
                              now_ms: int | None = None, limit: int = 500) -> dict[str, int]:
    cutoff = day_start_ms(now_ms)
    changed = counted = 0
    with sqlite3.connect(path, timeout=2, factory=ClosingSQLiteConnection) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA busy_timeout=2000')
        conn.execute(DAILY_SCHEMA)
        conn.execute('CREATE TABLE IF NOT EXISTS odaily_error_retention_state (table_name TEXT PRIMARY KEY,day_ms INTEGER NOT NULL,last_rowid INTEGER NOT NULL)')
        existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in dict.fromkeys([*tables, 'x_task_pipeline', 'x_processing_attempts', 'x_processing_attempt_events',
                                   'pipeline_worker_heartbeats', 'odaily_observability_outbox']):
            if table not in existing:
                continue
            columns = {str(row['name']): row for row in conn.execute(f'PRAGMA table_info({_quote(table)})')}
            has_counter = _install_counters(conn, table, columns, database_key)
            direct_errors = [key for key in ERROR_KEYS if key in columns]
            if table in {'jobs', 'trigger_events', 'fact_checks', 'fact_check_runs'}:
                direct_errors.extend(key for key in ('reason', 'summary') if key in columns)
            if table == 'odaily_observability_outbox':
                direct_errors = ['payload_json']
            if not direct_errors:
                continue
            time_column = next((key for key in ('happened_at_ms', 'finished_at', 'completed_at', 'updated_at', 'last_seen_at', 'started_at', 'created_at') if key in columns), None)
            if not time_column:
                continue
            state = conn.execute('SELECT day_ms,last_rowid FROM odaily_error_retention_state WHERE table_name=?', (table,)).fetchone()
            last_rowid = int(state['last_rowid']) if state and int(state['day_ms']) == cutoff else 0
            time_expr = _quote(time_column) if time_column.endswith('_ms') else f"(julianday({_quote(time_column)})-2440587.5)*86400000"
            where_errors = ' OR '.join(f'COALESCE({_quote(key)},\'\')<>\'\'' for key in direct_errors)
            rows = conn.execute(f'SELECT rowid AS __rowid__,* FROM {_quote(table)} WHERE rowid>? AND {time_expr}<? AND ({where_errors}) ORDER BY rowid LIMIT ?',
                                (last_rowid, cutoff, max(1, min(limit, 2000)))).fetchall()
            for record in rows:
                row = dict(record)
                messages = error_messages(row)
                if not messages and table == 'odaily_observability_outbox':
                    try:
                        messages = error_messages(json.loads(row.get('payload_json') or '{}'))
                    except ValueError:
                        pass
                category = next((kind for message in messages if (kind := failure_category(message)) in EXTERNAL_CATEGORIES), None)
                if category is None:
                    continue
                stamp = row[time_column]
                dt = datetime.fromtimestamp(float(stamp) / 1000 + 8*3600, UTC) if time_column.endswith('_ms') else datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
                if not time_column.endswith('_ms'):
                    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt
                    dt = dt.astimezone(UTC) + timedelta(hours=8)
                error_date = dt.date().isoformat()
                # Attempts are authoritative. Current pipeline/queue states and projections are copies.
                canonical = table not in {'x_task_pipeline', 'x_processing_attempt_events',
                                           'pipeline_worker_heartbeats', 'local_pipeline_jobs', 'odaily_observability_outbox'}
                if canonical and not row.get(COUNTED_COLUMN):
                    source = str(row.get('source') or database_key)
                    if table == 'x_processing_attempts' and 'tasks' in existing:
                        task = conn.execute('SELECT source FROM tasks WHERE id=?', (row.get('task_id'),)).fetchone()
                        source = str(task[0]) if task else source
                    stage = str(row.get('stage') or ('capture' if table.endswith('_attempts') else table))
                    conn.execute('INSERT INTO odaily_external_error_daily VALUES(?,?,?,?,1) ON CONFLICT(error_date,source,stage,category) DO UPDATE SET occurrences=occurrences+1',
                                 (error_date, source, stage, category))
                    counted += 1
                clean = scrub_external_errors(row)
                # Legacy/current snapshots sometimes use a free-form reason as their only error field.
                fields: dict[str, Any] = {key: '' for key in direct_errors if key != 'payload_json'}
                if has_counter:
                    # Boolean dedup marker has no error type or response material.
                    fields[COUNTED_COLUMN] = 1
                for key in ('result_json', 'push_result', 'publish_json', 'metadata', 'response_excerpt', 'response_archive_error',
                            'response_ref', 'response_gzip', 'raw_output', 'summary', 'payload_json'):
                    if key in columns and clean.get(key) != row.get(key):
                        fields[key] = clean.get(key)
                for key in ('response_gzip', 'response_ref', 'response_excerpt', 'raw_output'):
                    if key in columns:
                        fields[key] = '' if columns[key]['notnull'] else None
                for key in list(fields):
                    if fields[key] is None and columns[key]['notnull']:
                        fields[key] = ''
                if fields:
                    assignments = ','.join(f'{_quote(key)}=?' for key in fields)
                    conn.execute(f'UPDATE {_quote(table)} SET {assignments} WHERE rowid=?', (*fields.values(), row['__rowid__']))
                    changed += 1
                if table == 'x_processing_attempts' and 'x_processing_attempt_events' in existing:
                    event_columns = {str(c['name']) for c in conn.execute('PRAGMA table_info(x_processing_attempt_events)')}
                    removals = [key for key in ('error_message', 'error_code', 'response_ref', 'response_excerpt', 'response_gzip', 'response_archive_error') if key in event_columns]
                    if removals:
                        conn.execute('UPDATE x_processing_attempt_events SET ' + ','.join(f'{_quote(key)}=NULL' for key in removals) + ' WHERE attempt_id=?', (row['id'],))
            new_cursor = int(rows[-1]['__rowid__']) if rows else last_rowid
            if state is None or int(state['day_ms']) != cutoff or new_cursor != last_rowid:
                conn.execute('INSERT INTO odaily_error_retention_state VALUES(?,?,?) ON CONFLICT(table_name) DO UPDATE SET day_ms=excluded.day_ms,last_rowid=excluded.last_rowid', (table, cutoff, new_cursor))
    return {'cleared': changed, 'counted': counted}


def dependency_error_totals(path: Path) -> list[dict[str, Any]]:
    from packages.common.storage import connect_sqlite_readonly
    with connect_sqlite_readonly(path, timeout_seconds=2) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='odaily_external_error_daily'").fetchone():
            return []
        return [dict(row) for row in conn.execute('SELECT * FROM odaily_external_error_daily ORDER BY error_date DESC,source,stage,category LIMIT 500')]


def cleanup_observation_errors(store: Any, *, now_ms: int | None = None, limit: int = 200) -> dict[str, int]:
    """Sweep legacy observation copies too; never aggregate them as additional failures."""
    cutoff = day_start_ms(now_ms)
    changed = 0
    delete_files: set[Path] = set()
    with store.connect() as conn:
        tables = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

        def replace_ref(ref: str | None, *, force: bool = False) -> None:
            nonlocal changed
            if not ref:
                return
            payload = store.load_evidence(ref)
            if payload is None or (not force and not external_error(payload)):
                return
            cleaned = {} if force else scrub_external_errors(payload)
            new_ref, new_path, size = store._write_evidence_file(cleaned)
            store._register_evidence(conn, new_ref, new_path, size)
            for table, field in (('system_information_items', 'evidence_ref'), ('system_information_events', 'evidence_ref'),
                                 ('system_information_decision_samples', 'evidence_ref'), ('system_review_analysis_attempts', 'raw_response_ref'),
                                 ('system_information_evidence_pins', 'evidence_ref')):
                if table in tables:
                    conn.execute(f'UPDATE {table} SET {field}=? WHERE {field}=?', (new_ref, ref))
            old = conn.execute('SELECT path FROM system_information_evidence WHERE evidence_ref=?', (ref,)).fetchone()
            if old and ref != new_ref:
                conn.execute('DELETE FROM system_information_evidence WHERE evidence_ref=?', (ref,))
                delete_files.add(Path(old[0]))
            changed += 1

        for table, time_column in (('system_information_items', 'updated_at_ms'), ('system_information_events', 'happened_at_ms')):
            key = 'external_retention:' + table
            state = conn.execute('SELECT state_value FROM system_information_state WHERE state_key=?', (key,)).fetchone()
            cursor_state = json.loads(state[0]) if state else {}
            cursor = int(cursor_state.get('cursor', 0)) if cursor_state.get('day') == cutoff else 0
            rows = conn.execute(f'SELECT rowid AS __rowid__,* FROM {table} WHERE rowid>? AND {time_column}<? ORDER BY rowid LIMIT ?',
                                (cursor, cutoff, max(1, min(limit, 2000)))).fetchall()
            for record in rows:
                row = dict(record)
                evidence = store.load_evidence(row.get('evidence_ref'))
                material = {**row, 'evidence': evidence}
                if not external_error(material):
                    continue
                if table == 'system_information_events':
                    # Response events can precede an external failure; remove those technical copies as well.
                    try:
                        details = json.loads(row.get('details_json') or '{}')
                    except ValueError:
                        details = {}
                    attempt_id = details.get('attempt_id')
                    if attempt_id is not None:
                        siblings = conn.execute("SELECT evidence_ref FROM system_information_events WHERE item_id=? AND json_valid(details_json) AND json_extract(details_json,'$.attempt_id')=?", (row['item_id'], attempt_id)).fetchall()
                        for sibling in siblings:
                            replace_ref(sibling[0], force=True)
                        conn.execute("DELETE FROM system_information_events WHERE item_id=? AND json_valid(details_json) AND json_extract(details_json,'$.attempt_id')=?", (row['item_id'], attempt_id))
                    else:
                        replace_ref(row.get('evidence_ref'))
                        conn.execute('DELETE FROM system_information_events WHERE event_id=?', (row['event_id'],))
                else:
                    replace_ref(row.get('evidence_ref'))
                    conn.execute("UPDATE system_information_items SET summary='',failure_category=NULL,fingerprint='' WHERE item_id=?", (row['item_id'],))
                    conn.execute('DELETE FROM system_information_search WHERE item_id=?', (row['item_id'],))
                    # Reindex only surviving material; deleted error terms must not be searchable.
                    from .store import _search_terms, _searchable_text
                    item = conn.execute('SELECT * FROM system_information_items WHERE item_id=?', (row['item_id'],)).fetchone()
                    cleaned_evidence = scrub_external_errors(evidence)
                    text = _searchable_text(tuple(item[k] for k in ('title', 'source', 'source_id', 'source_url')), cleaned_evidence)
                    conn.executemany('INSERT OR IGNORE INTO system_information_search VALUES(?,?)', ((term, row['item_id']) for term in _search_terms(text)))
                changed += 1
            new_cursor = int(rows[-1]['__rowid__']) if rows else cursor
            conn.execute('INSERT INTO system_information_state VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms',
                         (key, json.dumps({'day': cutoff, 'cursor': new_cursor}), now_ms or int(datetime.now(UTC).timestamp()*1000)))
        if 'system_review_runs' in tables:
            key_runs = 'external_retention:system_review_runs'
            saved = conn.execute('SELECT state_value FROM system_information_state WHERE state_key=?', (key_runs,)).fetchone()
            saved = json.loads(saved[0]) if saved else {}
            after = int(saved.get('cursor', 0)) if saved.get('day') == cutoff else 0
            rows = conn.execute("SELECT rowid AS __rowid__,run_key,error FROM system_review_runs WHERE rowid>? AND error IS NOT NULL AND julianday(COALESCE(completed_at,started_at))<julianday(?) ORDER BY rowid LIMIT ?",
                                (after, datetime.fromtimestamp(cutoff / 1000, UTC).isoformat(), limit)).fetchall()
            for row in rows:
                if failure_category(row['error']) in EXTERNAL_CATEGORIES:
                    conn.execute('UPDATE system_review_runs SET error=NULL WHERE run_key=?', (row['run_key'],))
                    changed += 1
            conn.execute('INSERT INTO system_information_state VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms',
                         (key_runs, json.dumps({'day': cutoff, 'cursor': int(rows[-1]['__rowid__']) if rows else after}), now_ms or int(datetime.now(UTC).timestamp()*1000)))
        # Prior versions persisted supplier failures as individual AI review cards.
        if 'system_review_items' in tables:
            cutoff_iso = datetime.fromtimestamp(cutoff / 1000, UTC).isoformat()
            key_state = 'external_retention:system_review_items'
            state = conn.execute('SELECT state_value FROM system_information_state WHERE state_key=?', (key_state,)).fetchone()
            state = json.loads(state[0]) if state else {}
            after = int(state.get('cursor', 0)) if state.get('day') == cutoff else 0
            cards = conn.execute('SELECT rowid AS __rowid__,* FROM system_review_items WHERE rowid>? AND julianday(last_seen_at)<julianday(?) ORDER BY rowid LIMIT ?', (after, cutoff_iso, limit)).fetchall()
            for record in cards:
                card = dict(record)
                if not external_error(card):
                    continue
                key = card['item_key']
                for attempt in conn.execute('SELECT raw_response_ref FROM system_review_analysis_attempts WHERE item_key=?', (key,)).fetchall():
                    replace_ref(attempt[0], force=True)
                conn.execute('DELETE FROM system_review_analysis_attempts WHERE item_key=?', (key,))
                conn.execute('DELETE FROM system_review_decisions WHERE item_key=?', (key,))
                conn.execute('DELETE FROM system_review_items WHERE item_key=?', (key,))
                changed += 1
            conn.execute('INSERT INTO system_information_state VALUES(?,?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at_ms=excluded.updated_at_ms',
                         (key_state, json.dumps({'day': cutoff, 'cursor': int(cards[-1]['__rowid__']) if cards else after}), now_ms or int(datetime.now(UTC).timestamp()*1000)))
    for path in delete_files:
        path.unlink(missing_ok=True)
    return {'cleared': changed}
