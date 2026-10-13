from __future__ import annotations

import argparse
import json
from dataclasses import asdict, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .detector import evaluate_address, evaluate_divergence
from .history import summarize
from .models import Position, Rules, number
from .store import encode


def load_rules(raw: dict[str, Any]) -> Rules:
    default = Rules()
    unknown = set(raw) - {f.name for f in fields(default)}
    if unknown:
        raise ValueError(f'Unknown rule fields: {sorted(unknown)}')
    values = {}
    for key, value in raw.items():
        values[key] = int(value) if isinstance(getattr(default, key), int) else number(value)
    return Rules(**values)


def replay(payload: dict[str, Any]) -> dict[str, Any]:
    """Offline snapshots/fills, with persistent dedup through a temporary test DB."""
    from .client import address_key
    from .history import fill_key
    from .store import NewsStore
    rules = load_rules(payload.get('rules', {}))
    first_seen = {}
    reports = []
    with TemporaryDirectory(prefix='odaily-hl-replay-') as directory:
        store = NewsStore(Path(directory) / 'replay.sqlite')
        store.initialize()
        for observation in payload['observations']:
            stamp = int(observation['observed_ms'])
            markets = observation.get('markets', {})
            states, histories = {}, {}
            for raw_address, snapshot in observation['addresses'].items():
                address = address_key(raw_address)
                first_seen.setdefault(address, stamp)
                store.discover(address, source={'kind': 'offline_replay'}, now_ms=stamp)
                old = store.load(address)
                raw_fills = snapshot.get('fills', [])
                cached = store.fills(address, 0, stamp)
                known = {fill_key(f) for f in cached}
                merged = {fill_key(f): f for f in cached + raw_fills}
                history = summarize(list(merged.values()), start_ms=0, end_ms=stamp, coverage='offline_fixture')
                new = [f for f in raw_fills if old and fill_key(f) not in known and int(f['time']) >= old['state']['observed_ms']]
                positions = {p.coin: p for p in (Position.from_payload(p) for p in snapshot.get('positions', []))}
                events, state = evaluate_address(address=address, observed_ms=stamp, first_seen_ms=first_seen[address],
                    positions=positions, previous=old['state'] if old else None, markets=markets, history=history,
                    new_fills=new, rules=rules)
                store.commit_observation(address=address, now_ms=stamp, state=state, history=history,
                    coverage={'coverage': 'offline_fixture'}, fills=raw_fills, candidates=events)
                states[address], histories[address] = state, history
            # Fixture batches are explicitly simultaneous comparison scopes.
            events, active = evaluate_divergence(observed_ms=stamp, states=states, histories=histories,
                previous=store.metadata('divergence', {}), rules=rules)
            store.save_global('divergence', active, events)
            reports.append({'observed_ms': stamp, 'addresses': len(states)})
        return {'mode': 'offline_replay', 'rules': asdict(rules), 'observations': reports, 'candidates': store.candidates()}


def main() -> int:
    parser = argparse.ArgumentParser(description='Hyperliquid news evidence: offline replay only, no publishing.')
    parser.add_argument('--input', type=Path, required=True, help='JSON offline observations fixture.')
    parser.add_argument('--output', type=Path, required=True, help='Structured candidate report JSON.')
    args = parser.parse_args()
    report = replay(json.loads(args.input.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encode(report) + '\n', encoding='utf-8')
    print(encode({'observations': len(report['observations']), 'candidates': len(report['candidates']), 'output': str(args.output)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
