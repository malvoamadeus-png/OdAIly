from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from .client import address_key
from .history import fill_key
from .models import Candidate
from .scenarios import SCENARIOS, event_scenarios


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


BLOCK_FILTER = "NOT EXISTS (SELECT 1 FROM hl_news_metadata b WHERE b.key='address_block:' || address AND json_extract(b.payload,'$.blocked')=1)"


def _candidate_scenario_triggers(*, if_not_exists: bool = False) -> str:
    statements = []
    for key, _, _, kinds, highlights, _ in SCENARIOS:
        kind_values = ','.join("'" + value.replace("'", "''") + "'" for value in kinds)
        condition = ("EXISTS (SELECT 1 FROM json_each(NEW.payload,'$.kinds') "
                     f"WHERE value IN ({kind_values}))")
        if highlights:
            highlight_values = ','.join("'" + value.replace("'", "''") + "'" for value in highlights)
            condition += (" AND EXISTS (SELECT 1 FROM json_each(NEW.payload,'$.evidence.highlights') "
                          f"WHERE value IN ({highlight_values}))")
        statements.append(f"SELECT NEW.event_key,'{key}' WHERE {condition}")
    clause = 'IF NOT EXISTS ' if if_not_exists else ''
    return f'''
        CREATE TRIGGER {clause}hl_news_candidates_scenarios_insert AFTER INSERT ON hl_news_candidates
        BEGIN
            INSERT OR IGNORE INTO hl_news_candidate_scenarios(event_key,scenario_key)
        ''' + '\nUNION ALL\n'.join(statements) + f''';
        END;
        CREATE TRIGGER {clause}hl_news_candidates_scenarios_delete AFTER DELETE ON hl_news_candidates
        BEGIN
            DELETE FROM hl_news_candidate_scenarios WHERE event_key=OLD.event_key;
        END;
    '''


def _candidate_address_triggers(*, if_not_exists: bool = False) -> str:
    clause = 'IF NOT EXISTS ' if if_not_exists else ''
    return f'''
        CREATE TRIGGER {clause}hl_news_candidates_address_insert AFTER INSERT ON hl_news_candidates
        BEGIN
            INSERT OR IGNORE INTO hl_news_candidate_timeline
                (event_key,observed_ms,coin,kinds,addresses)
            VALUES(NEW.event_key,NEW.observed_ms,NEW.coin,
                   COALESCE(json_extract(NEW.payload,'$.kinds'),'[]'),
                   COALESCE(json_extract(NEW.payload,'$.addresses'),'[]'));
            INSERT OR IGNORE INTO hl_news_candidate_addresses
                (event_key,address_lower,observed_ms)
            SELECT NEW.event_key,lower(a.value),NEW.observed_ms
            FROM json_each(NEW.payload,'$.addresses') a WHERE a.type='text';
        END;
        CREATE TRIGGER {clause}hl_news_candidates_address_delete AFTER DELETE ON hl_news_candidates
        BEGIN
            DELETE FROM hl_news_candidate_addresses WHERE event_key=OLD.event_key;
            DELETE FROM hl_news_candidate_timeline WHERE event_key=OLD.event_key;
        END;
    '''


class NewsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS hl_news_addresses (
                    address TEXT PRIMARY KEY, first_seen_ms INTEGER NOT NULL,
                    last_seen_ms INTEGER NOT NULL, sources TEXT NOT NULL,
                    last_polled_ms INTEGER NOT NULL DEFAULT 0, error TEXT);
                CREATE TABLE IF NOT EXISTS hl_news_fills (
                    address TEXT NOT NULL, fill_key TEXT NOT NULL, time_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(address, fill_key));
                CREATE INDEX IF NOT EXISTS hl_news_fills_time ON hl_news_fills(address,time_ms);
                CREATE TABLE IF NOT EXISTS hl_news_monitor_fill_keys (
                    monitor TEXT NOT NULL,address TEXT NOT NULL,fill_key TEXT NOT NULL,time_ms INTEGER NOT NULL,
                    PRIMARY KEY(monitor,address,fill_key));
                CREATE INDEX IF NOT EXISTS hl_news_monitor_fill_keys_time ON hl_news_monitor_fill_keys(monitor,address,time_ms);
                CREATE TABLE IF NOT EXISTS hl_news_compact_history (
                    address TEXT PRIMARY KEY, through_ms INTEGER NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_compact_work (
                    address TEXT PRIMARY KEY, through_ms INTEGER NOT NULL, payload TEXT NOT NULL,
                    source_count INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_compact_segments (
                    address TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                    payload TEXT NOT NULL, summary TEXT NOT NULL, PRIMARY KEY(address,start_ms));
                CREATE INDEX IF NOT EXISTS hl_news_compact_segments_end
                    ON hl_news_compact_segments(address,end_ms);
                CREATE TABLE IF NOT EXISTS hl_news_state (
                    address TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL,
                    state TEXT NOT NULL, history TEXT NOT NULL, coverage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_normal_state (
                    address TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL,
                    state TEXT NOT NULL, history TEXT NOT NULL, coverage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_normal_schedule (
                    address TEXT PRIMARY KEY, last_polled_ms INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_candidates (
                    event_key TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL,
                    coin TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS hl_news_candidates_observed
                    ON hl_news_candidates(observed_ms DESC,event_key ASC);
                CREATE TABLE IF NOT EXISTS hl_news_candidate_timeline (
                    event_key TEXT PRIMARY KEY,observed_ms INTEGER NOT NULL,coin TEXT NOT NULL,
                    kinds TEXT NOT NULL,addresses TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS hl_news_candidate_timeline_observed
                    ON hl_news_candidate_timeline(observed_ms DESC,event_key ASC);
                CREATE TABLE IF NOT EXISTS hl_news_candidate_addresses (
                    event_key TEXT NOT NULL,address_lower TEXT NOT NULL,observed_ms INTEGER NOT NULL,
                    PRIMARY KEY(event_key,address_lower));
                CREATE INDEX IF NOT EXISTS hl_news_candidate_addresses_address_time
                    ON hl_news_candidate_addresses(address_lower,observed_ms DESC,event_key ASC);
                CREATE TABLE IF NOT EXISTS hl_news_candidate_scenarios (
                    event_key TEXT NOT NULL, scenario_key TEXT NOT NULL,
                    PRIMARY KEY(event_key,scenario_key));
                CREATE INDEX IF NOT EXISTS hl_news_candidate_scenarios_key
                    ON hl_news_candidate_scenarios(scenario_key,event_key);
                CREATE TABLE IF NOT EXISTS hl_news_dispatch (
                    event_key TEXT PRIMARY KEY, signature TEXT NOT NULL, task_id INTEGER,
                    status TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS hl_news_dispatch_signature ON hl_news_dispatch(signature);
                CREATE TABLE IF NOT EXISTS hl_news_account_pool (
                    address TEXT PRIMARY KEY REFERENCES hl_news_addresses(address),
                    tier TEXT NOT NULL DEFAULT 'pending', qualified INTEGER NOT NULL DEFAULT 0,
                    next_poll_ms INTEGER NOT NULL DEFAULT 0, last_action_ms INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS hl_news_account_due ON hl_news_account_pool(qualified,next_poll_ms);
                CREATE TABLE IF NOT EXISTS hl_news_metadata (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            ''')
            scenario_version = [[s[0], s[3], s[4]] for s in SCENARIOS]
            row = conn.execute("SELECT payload FROM hl_news_metadata WHERE key='scenario_index_version'").fetchone()
            indexed_version = json.loads(row['payload']) if row else None
            if indexed_version != scenario_version:
                conn.execute('DROP TRIGGER IF EXISTS hl_news_candidates_scenarios_insert')
                conn.execute('DROP TRIGGER IF EXISTS hl_news_candidates_scenarios_delete')
                conn.executescript(_candidate_scenario_triggers())
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM hl_news_candidate_scenarios')
                mappings = []
                for candidate in conn.execute('SELECT event_key,payload FROM hl_news_candidates'):
                    event = json.loads(candidate['payload'])
                    mappings.extend((candidate['event_key'], key) for key in event_scenarios(event))
                conn.executemany('INSERT OR IGNORE INTO hl_news_candidate_scenarios VALUES(?,?)', mappings)
                conn.execute('''INSERT INTO hl_news_metadata VALUES('scenario_index_version',?)
                    ON CONFLICT(key) DO UPDATE SET payload=excluded.payload''',
                    (json.dumps(scenario_version, separators=(',', ':')),))
                conn.commit()
            else:
                triggers = {row['name'] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('hl_news_candidates_scenarios_insert','hl_news_candidates_scenarios_delete')")}
                if len(triggers) != 2:
                    conn.executescript(_candidate_scenario_triggers(if_not_exists=True))
            row = conn.execute("SELECT payload FROM hl_news_metadata WHERE key='candidate_address_index_version'").fetchone()
            address_index_version = json.loads(row['payload']) if row else None
            if address_index_version != 1:
                conn.execute('DROP TRIGGER IF EXISTS hl_news_candidates_address_insert')
                conn.execute('DROP TRIGGER IF EXISTS hl_news_candidates_address_delete')
                conn.executescript(_candidate_address_triggers())
                conn.execute('BEGIN IMMEDIATE')
                conn.execute('DELETE FROM hl_news_candidate_timeline')
                conn.execute('DELETE FROM hl_news_candidate_addresses')
                conn.execute('''INSERT OR IGNORE INTO hl_news_candidate_timeline
                    (event_key,observed_ms,coin,kinds,addresses)
                    SELECT c.event_key,c.observed_ms,c.coin,
                           COALESCE(json_extract(c.payload,'$.kinds'),'[]'),
                           COALESCE(json_extract(c.payload,'$.addresses'),'[]')
                    FROM hl_news_candidates c''')
                conn.execute('''INSERT OR IGNORE INTO hl_news_candidate_addresses
                    (event_key,address_lower,observed_ms)
                    SELECT c.event_key,lower(a.value),c.observed_ms
                    FROM hl_news_candidates c,json_each(c.payload,'$.addresses') a
                    WHERE a.type='text' ''')
                conn.execute('''INSERT INTO hl_news_metadata VALUES('candidate_address_index_version','1')
                    ON CONFLICT(key) DO UPDATE SET payload=excluded.payload''')
                conn.commit()
            else:
                triggers = {row['name'] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name IN ('hl_news_candidates_address_insert','hl_news_candidates_address_delete')")}
                if len(triggers) != 2:
                    conn.executescript(_candidate_address_triggers(if_not_exists=True))
    def discover(self, address: str, *, source: dict[str, Any], now_ms: int, limit: int | None = 500) -> bool:
        address = address_key(address)
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.blocked(address, conn=conn):
                return False
            row = conn.execute('SELECT sources FROM hl_news_addresses WHERE address=?', (address,)).fetchone()
            if row is None and limit is not None and conn.execute(f'SELECT count(*) FROM hl_news_addresses WHERE {BLOCK_FILTER}').fetchone()[0] >= limit:
                return False
            sources = json.loads(row['sources']) if row else []
            if source.get('kind') == 'leaderboard':
                sources = [s for s in sources if not (s.get('kind') == 'leaderboard' and s.get('window') == source.get('window') and s.get('side') == source.get('side'))]
            elif source.get('kind') == 'large_observed_trade':
                sources = [s for s in sources if not (s.get('kind') == source['kind'] and s.get('coin') == source.get('coin'))]
            if source not in sources:
                sources.append(source)
            conn.execute('''INSERT INTO hl_news_addresses(address,first_seen_ms,last_seen_ms,sources)
                VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET last_seen_ms=excluded.last_seen_ms,sources=excluded.sources''',
                         (address, now_ms, now_ms, encode(sources)))
        return True

    def add_manual_address(self, address: str, *, now_ms: int, updated_by: str) -> dict[str, Any]:
        address = address_key(address)
        from datetime import UTC, datetime
        source = {'kind': 'manual', 'added_by': updated_by,
                  'added_at': datetime.fromtimestamp(now_ms / 1000, UTC).isoformat()}
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.blocked(address, conn=conn):
                raise ValueError('Address is blocked; unblock it before adding it')
            row = conn.execute('SELECT first_seen_ms,last_seen_ms,sources FROM hl_news_addresses WHERE address=?',
                               (address,)).fetchone()
            created = row is None
            sources = json.loads(row['sources']) if row else []
            has_manual_source = any(item.get('kind') == 'manual' for item in sources)
            if not has_manual_source:
                sources.append(source)
            conn.execute('''INSERT INTO hl_news_addresses(address,first_seen_ms,last_seen_ms,sources)
                VALUES(?,?,?,?) ON CONFLICT(address) DO UPDATE SET
                last_seen_ms=max(hl_news_addresses.last_seen_ms,excluded.last_seen_ms),sources=excluded.sources''',
                         (address, now_ms, now_ms, encode(sources)))
        self.schedule_account(address, now_ms=now_ms)
        return {'address': address, 'created': created, 'already_manual': has_manual_source}

    def addresses(self, limit: int, *, normal_schedule: bool = False) -> list[dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            if normal_schedule:
                return [dict(r) for r in conn.execute('''SELECT a.* FROM hl_news_addresses a
                    LEFT JOIN hl_news_normal_schedule s ON s.address=a.address
                    WHERE NOT EXISTS (SELECT 1 FROM hl_news_metadata b
                        WHERE b.key='address_block:' || a.address AND json_extract(b.payload,'$.blocked')=1)
                    ORDER BY COALESCE(s.last_polled_ms,0),a.address LIMIT ?''', (limit,))]
            return [dict(r) for r in conn.execute(f'SELECT * FROM hl_news_addresses WHERE {BLOCK_FILTER} ORDER BY last_polled_ms,address LIMIT ?', (limit,))]

    @staticmethod
    def _state_table(table: str) -> str:
        if table not in ('hl_news_state', 'hl_news_normal_state'):
            raise ValueError('Unknown Hyperliquid state table')
        return table

    def load(self, address: str, *, state_table: str = 'hl_news_state') -> dict[str, Any] | None:
        state_table = self._state_table(state_table)
        with connect_sqlite(self.path) as conn:
            row = conn.execute(f'SELECT * FROM {state_table} WHERE address=?', (address,)).fetchone()
        if row is None:
            return None
        return {key: json.loads(row[key]) for key in ('state', 'history', 'coverage')}

    def fills(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            return [json.loads(row['payload']) for row in conn.execute(
                'SELECT payload FROM hl_news_fills WHERE address=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms,fill_key',
                (address, start_ms, end_ms))]

    def monitor_fill_keys(self, address: str, monitor: str, start_ms: int, end_ms: int) -> set[str]:
        with connect_sqlite(self.path) as conn:
            return {row['fill_key'] for row in conn.execute(
                'SELECT fill_key FROM hl_news_monitor_fill_keys WHERE monitor=? AND address=? AND time_ms BETWEEN ? AND ?',
                (monitor, address, start_ms, end_ms))}

    def compact_boundary(self, address: str) -> int | None:
        with connect_sqlite(self.path) as conn:
            row = conn.execute('SELECT through_ms FROM hl_news_compact_history WHERE address=?', (address,)).fetchone()
        return int(row['through_ms']) if row else None

    def compact_history(self, address: str, start_ms: int = 0, *, cuts: list[int] | None = None,
                        aggregate_rounds: bool = False) -> tuple[int, dict[str, Any]] | None:
        with connect_sqlite(self.path) as conn:
            row = conn.execute('SELECT through_ms,payload FROM hl_news_compact_history WHERE address=?',
                               (address,)).fetchone()
            if not row:
                return None
            segments = conn.execute('SELECT start_ms,end_ms,summary FROM hl_news_compact_segments WHERE address=? AND end_ms>=? ORDER BY start_ms',
                                    (address, start_ms)).fetchall()
            payloads = []
            for segment in segments:
                split = (not aggregate_rounds or cuts is None or
                         any(segment['start_ms'] < cut <= segment['end_ms'] for cut in cuts))
                if split:
                    payloads.append(conn.execute('SELECT payload FROM hl_news_compact_segments WHERE address=? AND start_ms=?',
                                                 (address, segment['start_ms'])).fetchone()['payload'])
                else:
                    payloads.append(segment['summary'])
        carry = json.loads(row['payload'])
        data = {'version': 1, 'assets': {}}
        for payload in payloads:
            for coin, asset in json.loads(payload)['assets'].items():
                target = data['assets'].setdefault(coin, {'moments': [], 'episodes': [], 'gaps': []})
                for field in ('moments', 'episodes', 'gaps'):
                    target[field].extend(asset[field])
                target.setdefault('round_summaries', []).extend(asset.get('round_summaries', []))
        for coin, asset in carry['assets'].items():
            target = data['assets'].setdefault(coin, {'moments': [], 'episodes': [], 'gaps': []})
            if not target['moments'] or target['moments'][-1]['time_ms'] != asset['moments'][-1]['time_ms']:
                target['moments'].extend(asset['moments'])
            target['episodes'].extend(asset['episodes'])
            target['expected'] = asset['expected']
            target['ordering_expected'] = asset['ordering_expected']
        return int(row['through_ms']), data

    def compact_fills(self, address: str, *, cutoff_ms: int, batch_size: int = 5000) -> dict[str, Any]:
        """Compact only behind both monitors' persisted history cursors."""
        from .compact_history import extend_compact, summarize_segment, verify_compact_history
        if batch_size < 1:
            raise ValueError('Invalid compact batch size')
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN')
            data_version = conn.execute('PRAGMA data_version').fetchone()[0]

            def lock_unchanged() -> bool:
                conn.execute('BEGIN IMMEDIATE')
                if conn.execute('PRAGMA data_version').fetchone()[0] != data_version:
                    conn.rollback()
                    return False
                return True
            normal = conn.execute('SELECT state,coverage FROM hl_news_normal_state WHERE address=?',
                                  (address,)).fetchone()
            cursors = []
            if normal:
                state, coverage = json.loads(normal['state']), json.loads(normal['coverage'])
                cursor = (state.get('observed_ms') if coverage.get('coverage') == 'not_queried_no_eligibility'
                          else state.get('history_last_queried_ms', state.get('observed_ms')))
                if not coverage.get('pagination_complete', True):
                    return {'fills': 0, 'reason': 'normal_history_incomplete'}
                if cursor is not None:
                    cursors.append(int(cursor))
            account = conn.execute('SELECT state,coverage FROM hl_news_state WHERE address=?', (address,)).fetchone()
            pool = conn.execute('SELECT qualified FROM hl_news_account_pool WHERE address=?', (address,)).fetchone()
            if account and (not pool or pool['qualified']):
                account_state = json.loads(account['state'])
                account_coverage = json.loads(account['coverage'])
                account_cursor = account_state.get('account_history_checked_ms')
                if account_cursor is None or not account_coverage.get('pagination_complete', True):
                    return {'fills': 0, 'reason': 'account_history_incomplete'}
                cursors.append(int(account_cursor))
            if not cursors:
                return {'fills': 0, 'reason': 'history_cursor_missing'}
            cutoff_ms = min(cutoff_ms, min(cursors) - 60000)
            for prefix in ('normal_pending_fills:', 'account_pending_fills:'):
                pending = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?', (prefix + address,)).fetchone()
                if pending and json.loads(pending['payload']):
                    return {'fills': 0, 'reason': 'pending_history'}
            saved = conn.execute('SELECT through_ms,payload FROM hl_news_compact_history WHERE address=?',
                                 (address,)).fetchone()
            work = None if saved else conn.execute('SELECT * FROM hl_news_compact_work WHERE address=?',
                                                   (address,)).fetchone()
            checkpoint = saved or work
            through_ms = int(checkpoint['through_ms']) if checkpoint else -1
            data = json.loads(checkpoint['payload']) if checkpoint else {'version': 1, 'assets': {}}
            if saved:
                old = conn.execute('SELECT fill_key FROM hl_news_fills WHERE address=? AND time_ms<=? LIMIT ?',
                                   (address, through_ms, batch_size)).fetchall()
                if old:
                    conn.commit()
                    if not lock_unchanged():
                        return {'fills': 0, 'phase': 'retry', 'reason': 'concurrent_write'}
                    conn.executemany('DELETE FROM hl_news_fills WHERE address=? AND fill_key=?',
                                     [(address, row['fill_key']) for row in old])
                    return {'fills': len(old), 'phase': 'cleanup', 'through_ms': through_ms}
            rows = conn.execute('''SELECT fill_key,time_ms,payload FROM hl_news_fills
                WHERE address=? AND time_ms>? AND time_ms<? ORDER BY time_ms,fill_key LIMIT ?''',
                (address, through_ms, cutoff_ms, batch_size + 1)).fetchall()
            if len(rows) > batch_size:
                boundary = rows[-1]['time_ms']
                rows = [row for row in rows if row['time_ms'] < boundary]
                if not rows:
                    raise ValueError('Compaction batch is saturated at one timestamp')
            if not rows:
                if work:
                    if through_ms >= cutoff_ms:
                        return {'fills': 0, 'reason': 'cursor_behind_work'}
                    source_count = conn.execute('SELECT count(*) FROM hl_news_fills WHERE address=? AND time_ms<=?',
                                                (address, through_ms)).fetchone()[0]
                    conn.commit()
                    if not lock_unchanged():
                        return {'fills': 0, 'phase': 'retry', 'reason': 'concurrent_write'}
                    if source_count != work['source_count']:
                        conn.execute('DELETE FROM hl_news_compact_segments WHERE address=?', (address,))
                        conn.execute('DELETE FROM hl_news_compact_work WHERE address=?', (address,))
                        return {'fills': 0, 'phase': 'rebuild', 'reason': 'source_fills_changed'}
                    conn.execute('INSERT INTO hl_news_compact_history VALUES(?,?,?)',
                                 (address, through_ms, work['payload']))
                    conn.execute('DELETE FROM hl_news_compact_work WHERE address=?', (address,))
                    return {'fills': 0, 'phase': 'activated', 'through_ms': through_ms}
                return {'fills': 0, 'through_ms': through_ms}
            conn.commit()
            fills = [json.loads(row['payload']) for row in rows]
            updated = extend_compact(data, fills)
            new_through = int(rows[-1]['time_ms'])
            # Validate the full batch independently. Cross-batch positions are
            # checked by extend_compact against the persisted per-asset carry.
            batch_only = extend_compact({'version': 1, 'assets': {}}, fills)
            verify_compact_history(fills, batch_only, [(int(rows[0]['time_ms']), new_through)])
            segment = {'version': 1, 'assets': {}}
            carry = {'version': 1, 'assets': {}}
            for coin, asset in updated['assets'].items():
                old = data['assets'].get(coin)
                old_moments = len(old['moments']) if old else 0
                old_gaps = len(old['gaps']) if old else 0
                episodes = asset['episodes']
                active = episodes[-1:] if episodes and episodes[-1]['end_ms'] is None and episodes[-1]['continuous'] else []
                finished = episodes[:-1] if active else episodes
                new_moments = asset['moments'][old_moments:]
                if new_moments or finished:
                    segment['assets'][coin] = {'moments': new_moments, 'episodes': finished,
                                               'gaps': asset['gaps'][old_gaps:]}
                carry['assets'][coin] = {'moments': asset['moments'][-1:], 'episodes': active, 'gaps': [],
                                         'expected': asset['expected'], 'ordering_expected': asset['ordering_expected']}
            segment_payload = encode(segment)
            summary_payload = encode(summarize_segment(segment))
            carry_payload = encode(carry)
            if not lock_unchanged():
                return {'fills': 0, 'phase': 'retry', 'reason': 'concurrent_write'}
            conn.execute('INSERT INTO hl_news_compact_segments(address,start_ms,end_ms,payload,summary) VALUES(?,?,?,?,?)',
                         (address, int(rows[0]['time_ms']), new_through, segment_payload, summary_payload))
            if saved:
                conn.execute('UPDATE hl_news_compact_history SET through_ms=?,payload=? WHERE address=?',
                             (new_through, carry_payload, address))
                conn.executemany('DELETE FROM hl_news_fills WHERE address=? AND fill_key=?',
                                 [(address, row['fill_key']) for row in rows])
            else:
                conn.execute('''INSERT INTO hl_news_compact_work VALUES(?,?,?,?)
                    ON CONFLICT(address) DO UPDATE SET through_ms=excluded.through_ms,
                        payload=excluded.payload,source_count=excluded.source_count''',
                    (address, new_through, carry_payload, (int(work['source_count']) if work else 0) + len(rows)))
            return {'fills': len(rows), 'phase': 'incremental' if saved else 'building', 'through_ms': new_through}

    def commit_observation(self, *, address: str, now_ms: int, state: dict[str, Any], history: dict[str, Any],
                           coverage: dict[str, Any], fills: list[dict[str, Any]], candidates: list[Candidate],
                           state_table: str = 'hl_news_state', check_compact_boundary: bool = False,
                           expected_compact_through: int | None = None) -> None:
        state_table = self._state_table(state_table)
        # The evidence, event deduplication and next baseline advance atomically.
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.event_blocked({'addresses': [address], 'observed_ms': now_ms}, conn=conn):
                return
            old = conn.execute(f'SELECT observed_ms FROM {state_table} WHERE address=?', (address,)).fetchone()
            if old and old['observed_ms'] >= now_ms:
                raise ValueError('Observation must advance monotonically')
            archive = conn.execute('SELECT through_ms FROM hl_news_compact_history WHERE address=?', (address,)).fetchone()
            actual_through = int(archive['through_ms']) if archive else None
            if check_compact_boundary and actual_through != expected_compact_through:
                raise ValueError('Compacted history changed during observation')
            if archive and any(int(fill['time']) <= int(archive['through_ms']) for fill in fills):
                raise ValueError('Late fill at or before compacted history boundary')
            for fill in fills:
                conn.execute('INSERT OR IGNORE INTO hl_news_fills VALUES(?,?,?,?)',
                             (address, fill_key(fill), int(fill['time']), encode(fill)))
            if state.get('separate_fill_dedup'):
                monitor = 'normal' if state_table == 'hl_news_normal_state' else 'account'
                conn.executemany('INSERT OR IGNORE INTO hl_news_monitor_fill_keys VALUES(?,?,?,?)',
                                 [(monitor, address, fill_key(fill), int(fill['time'])) for fill in fills])
                cursor = state.get('history_last_queried_ms' if monitor == 'normal' else 'account_history_checked_ms')
                if cursor is not None:
                    conn.execute('DELETE FROM hl_news_monitor_fill_keys WHERE monitor=? AND address=? AND time_ms<?',
                                 (monitor, address, int(cursor) - 60000))
            for event in candidates:
                if self.event_blocked(asdict(event), conn=conn):
                    continue
                conn.execute('INSERT OR IGNORE INTO hl_news_candidates VALUES(?,?,?,?)',
                             (event.key, event.observed_ms, event.coin, encode(asdict(event))))
            conn.execute(f'''INSERT INTO {state_table} VALUES(?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET
                observed_ms=excluded.observed_ms,state=excluded.state,history=excluded.history,coverage=excluded.coverage''',
                         (address, now_ms, encode(state), encode(history), encode(coverage)))
            conn.execute('UPDATE hl_news_addresses SET last_polled_ms=?,error=NULL WHERE address=?', (now_ms, address))
            if state_table == 'hl_news_normal_state':
                conn.execute('''INSERT INTO hl_news_normal_schedule VALUES(?,?)
                    ON CONFLICT(address) DO UPDATE SET last_polled_ms=excluded.last_polled_ms''', (address, now_ms))

    def record_error(self, address: str, now_ms: int, error: str, *, normal_schedule: bool = False) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute('UPDATE hl_news_addresses SET last_polled_ms=?,error=? WHERE address=?', (now_ms, error[:2000], address))
            if normal_schedule:
                conn.execute('''INSERT INTO hl_news_normal_schedule VALUES(?,?)
                    ON CONFLICT(address) DO UPDATE SET last_polled_ms=excluded.last_polled_ms''', (address, now_ms))

    def metadata(self, key: str, default: Any) -> Any:
        with connect_sqlite(self.path) as conn:
            row = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?', (key,)).fetchone()
        return json.loads(row['payload']) if row else default

    def save_global(self, key: str, value: Any, candidates: list[Candidate]) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            for event in candidates:
                if self.event_blocked(asdict(event), conn=conn):
                    continue
                conn.execute('INSERT OR IGNORE INTO hl_news_candidates VALUES(?,?,?,?)',
                             (event.key, event.observed_ms, event.coin, encode(asdict(event))))
            conn.execute('INSERT INTO hl_news_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload', (key, encode(value)))

    def recent_states(self, since_ms: int, *, state_table: str = 'hl_news_state') -> dict[str, dict[str, Any]]:
        state_table = self._state_table(state_table)
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f'SELECT * FROM {state_table} WHERE {BLOCK_FILTER} AND observed_ms>=?', (since_ms,)).fetchall()
        return {row['address']: {'state': json.loads(row['state']), 'history': json.loads(row['history'])}
                for row in rows}

    def candidates(self) -> list[dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            return [json.loads(row[0]) for row in conn.execute('SELECT payload FROM hl_news_candidates ORDER BY observed_ms,event_key')]

    def schedule_account(self, address: str, *, now_ms: int, leaderboard_qualified: bool = False) -> None:
        address = address_key(address)
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.blocked(address, conn=conn):
                return
            conn.execute("""INSERT INTO hl_news_account_pool(address,next_poll_ms,qualified) VALUES(?,?,?)
                ON CONFLICT(address) DO UPDATE SET next_poll_ms=MIN(next_poll_ms,excluded.next_poll_ms)""",
                (address, now_ms, int(leaderboard_qualified)))

    def account_due(self, now_ms: int, limit: int) -> list[dict[str, Any]]:
        # Reserve one probe for new/inactive candidates; qualified addresses get
        # the remaining budget. Ordered due times prevent hot addresses starving cold ones.
        with connect_sqlite(self.path) as conn:
            probe = conn.execute(f"""SELECT * FROM hl_news_account_pool
                WHERE {BLOCK_FILTER} AND qualified=0 AND next_poll_ms<=? ORDER BY next_poll_ms,address LIMIT 1""", (now_ms,)).fetchall()
            active = conn.execute(f"""SELECT * FROM hl_news_account_pool
                WHERE {BLOCK_FILTER} AND qualified=1 AND next_poll_ms<=? ORDER BY next_poll_ms,address LIMIT ?""",
                (now_ms, limit-len(probe))).fetchall()
            remaining = limit-len(probe)-len(active)
            extra = conn.execute(f"""SELECT * FROM hl_news_account_pool WHERE {BLOCK_FILTER} AND qualified=0 AND next_poll_ms<=?
                ORDER BY next_poll_ms,address LIMIT ? OFFSET ?""", (now_ms, remaining, len(probe))).fetchall()
        return [dict(r) for r in [*active, *probe, *extra]]

    def update_account_schedule(self, address: str, *, now_ms: int, tier: str,
                                interval_ms: int, qualified: bool, last_action_ms: int = 0) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute("""UPDATE hl_news_account_pool SET tier=?,qualified=?,next_poll_ms=?,
                last_action_ms=MAX(last_action_ms,?) WHERE address=?""",
                (tier, int(qualified), now_ms+interval_ms, last_action_ms, address))

    def account_pool_stats(self, now_ms: int) -> dict[str, Any]:
        with connect_sqlite(self.path) as conn:
            tiers = {r['tier']: r['n'] for r in conn.execute(
                'SELECT tier,count(*) AS n FROM hl_news_account_pool GROUP BY tier')}
            due = conn.execute("""SELECT count(*) AS n,MIN(next_poll_ms) AS oldest
                FROM hl_news_account_pool WHERE qualified=1 AND next_poll_ms<=?""", (now_ms,)).fetchone()
        return {'tiers': tiers, 'qualified_due': due['n'],
                'max_qualified_delay_ms': max(0, now_ms-due['oldest']) if due['oldest'] is not None else 0}

    def save_address_note(self, address: str, note: str, *, updated_by: str) -> dict[str, Any]:
        address = address_key(address)
        if not isinstance(note, str) or len(note.strip()) > 80 or any(c in note for c in '\n\r（）()'):
            raise ValueError('Note must be at most 80 characters without newlines or parentheses')
        with connect_sqlite(self.path) as conn:
            if not conn.execute('SELECT 1 FROM hl_news_addresses WHERE address=?', (address,)).fetchone():
                raise ValueError('Unknown monitored address')
        from datetime import UTC, datetime
        value = {'address': address, 'note': note.strip(), 'updated_by': updated_by,
                 'updated_at': datetime.now(UTC).isoformat()}
        self.save_global('address_note:' + address, value, [])
        return value

    def with_address_notes(self, event: dict[str, Any]) -> dict[str, Any]:
        notes = {a: self.metadata('address_note:' + address_key(a), {}).get('note', '')
                 for a in event.get('addresses', [])}
        return {**event, 'evidence': {**event['evidence'], 'address_notes': notes}}

    def blocked(self, address: str, *, conn=None) -> bool:
        if conn is None:
            with connect_sqlite(self.path) as connection:
                return self.blocked(address, conn=connection)
        row = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?',
                           ('address_block:' + address_key(address),)).fetchone()
        return bool(row and json.loads(row[0]).get('blocked'))

    def event_blocked(self, event: dict[str, Any], *, conn=None) -> bool:
        if conn is None:
            with connect_sqlite(self.path) as connection:
                return self.event_blocked(event, conn=connection)
        for address in event.get('addresses', []):
            row = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?',
                               ('address_block:' + address_key(address),)).fetchone()
            if row:
                rule = json.loads(row[0])
                if rule.get('blocked') or int(event.get('observed_ms', 0)) <= rule.get('last_blocked_ms', -1):
                    return True
        return False

    def save_address_block(self, address: str, blocked: bool, *, updated_by: str) -> dict[str, Any]:
        from datetime import UTC, datetime
        address = address_key(address)
        if not isinstance(blocked, bool):
            raise ValueError('blocked must be a boolean')
        value = {'address': address, 'blocked': blocked, 'updated_by': updated_by,
                 'updated_at': datetime.now(UTC).isoformat()}
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM hl_news_addresses WHERE address=?',(address,)).fetchone():
                raise ValueError('Unknown monitored address')
            previously_blocked = self.blocked(address, conn=conn)
            previous = conn.execute('SELECT payload FROM hl_news_metadata WHERE key=?',('address_block:'+address,)).fetchone()
            value['last_blocked_ms'] = int(datetime.now(UTC).timestamp()*1000) if blocked or previously_blocked else (json.loads(previous[0]).get('last_blocked_ms', -1) if previous else -1)
            conn.execute('INSERT INTO hl_news_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                         ('address_block:' + address, encode(value)))
            if blocked:
                conn.execute('DELETE FROM hl_news_account_pool WHERE address=?', (address,))
                conn.execute("""INSERT OR IGNORE INTO hl_news_dispatch
                    SELECT event_key,'blocked:' || event_key,NULL,'address_blocked',NULL,?,?
                    FROM hl_news_candidates c WHERE EXISTS
                    (SELECT 1 FROM json_each(c.payload,'$.addresses') WHERE lower(value)=?)""",
                    (value['updated_at'],value['updated_at'],address))
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'tasks' in tables:
                    conn.execute("""UPDATE tasks SET status='discarded',updated_at=? WHERE source='hyperliquid_news'
                        AND status NOT IN ('ready_review','auto_published','published','discarded','duplicate','expired','legacy_skipped')
                        AND EXISTS (SELECT 1 FROM json_each(tasks.raw_payload,'$.addresses') WHERE lower(value)=?)""",
                        (value['updated_at'],address))
            elif previously_blocked:
                conn.execute('DELETE FROM hl_news_state WHERE address=?',(address,))
                conn.execute('DELETE FROM hl_news_normal_state WHERE address=?',(address,))
        return value
