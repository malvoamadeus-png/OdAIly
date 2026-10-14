from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from .client import address_key
from .history import fill_key
from .models import Candidate


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


BLOCK_FILTER = "NOT EXISTS (SELECT 1 FROM hl_news_metadata b WHERE b.key='address_block:' || address AND json_extract(b.payload,'$.blocked')=1)"


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
                CREATE TABLE IF NOT EXISTS hl_news_state (
                    address TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL,
                    state TEXT NOT NULL, history TEXT NOT NULL, coverage TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS hl_news_candidates (
                    event_key TEXT PRIMARY KEY, observed_ms INTEGER NOT NULL,
                    coin TEXT NOT NULL, payload TEXT NOT NULL);
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

    def addresses(self, limit: int) -> list[dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            return [dict(r) for r in conn.execute(f'SELECT * FROM hl_news_addresses WHERE {BLOCK_FILTER} ORDER BY last_polled_ms,address LIMIT ?', (limit,))]

    def load(self, address: str) -> dict[str, Any] | None:
        with connect_sqlite(self.path) as conn:
            row = conn.execute('SELECT * FROM hl_news_state WHERE address=?', (address,)).fetchone()
        if row is None:
            return None
        return {key: json.loads(row[key]) for key in ('state', 'history', 'coverage')}

    def fills(self, address: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            return [json.loads(row['payload']) for row in conn.execute(
                'SELECT payload FROM hl_news_fills WHERE address=? AND time_ms BETWEEN ? AND ? ORDER BY time_ms,fill_key',
                (address, start_ms, end_ms))]

    def commit_observation(self, *, address: str, now_ms: int, state: dict[str, Any], history: dict[str, Any],
                           coverage: dict[str, Any], fills: list[dict[str, Any]], candidates: list[Candidate]) -> None:
        # The evidence, event deduplication and next baseline advance atomically.
        with connect_sqlite(self.path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if self.event_blocked({'addresses': [address], 'observed_ms': now_ms}, conn=conn):
                return
            old = conn.execute('SELECT observed_ms FROM hl_news_state WHERE address=?', (address,)).fetchone()
            if old and old['observed_ms'] >= now_ms:
                raise ValueError('Observation must advance monotonically')
            for fill in fills:
                conn.execute('INSERT OR IGNORE INTO hl_news_fills VALUES(?,?,?,?)',
                             (address, fill_key(fill), int(fill['time']), encode(fill)))
            for event in candidates:
                if self.event_blocked(asdict(event), conn=conn):
                    continue
                conn.execute('INSERT OR IGNORE INTO hl_news_candidates VALUES(?,?,?,?)',
                             (event.key, event.observed_ms, event.coin, encode(asdict(event))))
            conn.execute('''INSERT INTO hl_news_state VALUES(?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET
                observed_ms=excluded.observed_ms,state=excluded.state,history=excluded.history,coverage=excluded.coverage''',
                         (address, now_ms, encode(state), encode(history), encode(coverage)))
            conn.execute('UPDATE hl_news_addresses SET last_polled_ms=?,error=NULL WHERE address=?', (now_ms, address))

    def record_error(self, address: str, now_ms: int, error: str) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute('UPDATE hl_news_addresses SET last_polled_ms=?,error=? WHERE address=?', (now_ms, error[:2000], address))

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

    def recent_states(self, since_ms: int) -> dict[str, dict[str, Any]]:
        with connect_sqlite(self.path) as conn:
            rows = conn.execute(f'SELECT * FROM hl_news_state WHERE {BLOCK_FILTER} AND observed_ms>=?', (since_ms,)).fetchall()
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
        return value
