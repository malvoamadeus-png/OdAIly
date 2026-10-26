from __future__ import annotations

from pathlib import Path
from typing import Any

from packages.common.paths import get_paths
from packages.common.storage import load_storage_settings
from .store import NewsStore, encode


def run_command(args: Any) -> int:
    path = load_storage_settings().sqlite_path
    store = NewsStore(path)
    action = args.hyperliquid_action
    if action == 'init':
        from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
        repo = SQLiteXProcessingRepository(path)
        repo.init_schema()
        repo.seed_prompt_templates(root_dir=get_paths().root_dir)
        store.initialize()
        print(encode({'initialized': True}))
    elif action == 'enable-account-profit':
        import time
        store.initialize()
        existing = store.metadata('production_account_profit', None)
        if existing is not None:
            if not existing.get('enabled'):
                raise ValueError('Disabled rollout requires an explicit new rollout decision')
            print(encode(existing))
        else:
            policy = {'enabled': True, 'started_ms': int(time.time() * 1000),
                      'enabled_scenarios': ['account_profit'], 'publication_mode': 'backend_only'}
            store.save_global('production_account_profit', policy, [])
            # Preserve test seed provenance, but require live qualification and a new baseline.
            for row in store.addresses(100000):
                store.schedule_account(row['address'], now_ms=policy['started_ms'])
            print(encode(policy))
    elif action == 'enable-account-loss':
        import time
        store.initialize()
        policy = store.metadata('production_account_profit', None)
        if not policy or not policy.get('enabled'):
            raise ValueError('Enable the shared account-profit monitor first')
        if 'historical_loss' not in policy.get('enabled_scenarios', []):
            policy['enabled_scenarios'] = ['account_profit', 'historical_loss']
            policy['loss_started_ms'] = int(time.time()*1000)
            store.save_global('production_account_profit', policy, [])
            store.save_global('account_leaderboard_checked_ms', 0, [])
            # Wake existing unqualified addresses; qualified profit baselines survive.
            from packages.common.storage import connect_sqlite
            with connect_sqlite(path) as conn:
                conn.execute("UPDATE hl_news_account_pool SET next_poll_ms=MIN(next_poll_ms,?) WHERE qualified=0",
                             (policy['loss_started_ms'],))
        print(encode(policy))
    elif action in ('enable-seven-scenarios', 'enable-approved-scenarios'):
        import time
        from .scenarios import PRODUCTION_SCENARIOS
        store.initialize()
        policy = store.metadata('production_account_profit', None)
        if not policy or not policy.get('enabled'):
            raise ValueError('Enable the shared account monitor before approved production scenarios')
        now = int(time.time() * 1000)
        existing = set(policy.get('enabled_scenarios', []))
        starts = policy.setdefault('scenario_started_ms_by_direction', {})
        for scenario in set(PRODUCTION_SCENARIOS) - existing:
            starts.setdefault(scenario, now)
        policy['enabled_scenarios'] = list(PRODUCTION_SCENARIOS)
        policy.setdefault('loss_started_ms', now)
        policy.setdefault('scenario_started_ms', now)
        store.save_global('production_account_profit', policy, [])
        if not {'large_pnl','asset_win_rate','reversal','loss_addition','realized'}.issubset(existing):
            from packages.common.storage import connect_sqlite
            with connect_sqlite(path) as conn:
                conn.execute('DELETE FROM hl_news_normal_state')
        print(encode(policy))
    elif action == 'prune-tested-cases':
        from .test_cases import prune_tested_cases
        print(encode(prune_tested_cases(store)))
    elif action == 'run-account-profit':
        import time
        from .account_monitor import AccountProfitMonitor
        from .client import NewsClient
        from .dispatch import EventDispatcher
        client = NewsClient(request_weight_per_minute=300)
        monitor = AccountProfitMonitor(store, client, addresses_per_pass=args.addresses_per_pass)
        from .service import NewsMonitor
        normal_monitor = NewsMonitor(store=store, client=client, addresses_per_pass=2,
                                     leaderboard_seeds=0, markets_per_discovery=1)
        dispatcher = EventDispatcher(store)
        while True:
            cycle_started = time.monotonic()
            cycle_ms = int(time.time() * 1000)
            report = {'observed_ms': cycle_ms, 'direction': 'approved_scenarios'}
            print(encode({'event': 'account_profit_worker.pass_started', 'observed_ms': cycle_ms}), flush=True)
            stage_started = time.monotonic()
            print(encode({'event': 'account_profit_worker.stage_started', 'stage': 'account_monitor',
                          'observed_ms': int(time.time() * 1000)}), flush=True)
            try:
                report['account_monitor'] = monitor.run_once()
                print(encode({'event': 'account_profit_worker.stage_completed', 'stage': 'account_monitor',
                              'elapsed_ms': int((time.monotonic() - stage_started) * 1000),
                              'candidate_count': report['account_monitor'].get('candidates')}), flush=True)
            except Exception as exc:
                report.setdefault('failed', {})['account_monitor'] = str(exc)
                print(encode({'event': 'account_profit_worker.stage_failed', 'stage': 'account_monitor',
                              'error': str(exc), 'elapsed_ms': int((time.monotonic() - stage_started) * 1000)}), flush=True)
            stage_started = time.monotonic()
            print(encode({'event': 'account_profit_worker.stage_started', 'stage': 'normal_monitor',
                          'observed_ms': int(time.time() * 1000)}), flush=True)
            try:
                report['normal_monitor'] = normal_monitor.run_once()
                print(encode({'event': 'account_profit_worker.stage_completed', 'stage': 'normal_monitor',
                              'elapsed_ms': int((time.monotonic() - stage_started) * 1000),
                              'candidate_count': report['normal_monitor'].get('candidates')}), flush=True)
            except Exception as exc:
                report.setdefault('failed', {})['normal_monitor'] = str(exc)
                print(encode({'event': 'account_profit_worker.stage_failed', 'stage': 'normal_monitor',
                              'error': str(exc), 'elapsed_ms': int((time.monotonic() - stage_started) * 1000)}), flush=True)
            stage_started = time.monotonic()
            print(encode({'event': 'account_profit_worker.stage_started', 'stage': 'dispatch',
                          'observed_ms': int(time.time() * 1000)}), flush=True)
            try:
                report['dispatch'] = dispatcher.dispatch(limit=10)
                store.save_global('last_pass', report, [])
                print(encode({'event': 'account_profit_worker.stage_completed', 'stage': 'dispatch',
                              'elapsed_ms': int((time.monotonic() - stage_started) * 1000),
                              'result_count': len(report['dispatch']), 'results': report['dispatch']}), flush=True)
                print(encode(report), flush=True)
            except Exception as exc:
                report.setdefault('failed', {})['dispatch'] = str(exc)
                store.save_global('last_pass', report, [])
                print(encode({'event': 'account_profit_worker.stage_failed', 'stage': 'dispatch',
                              'error': str(exc), 'elapsed_ms': int((time.monotonic() - stage_started) * 1000)}), flush=True)
                print(encode(report), flush=True)
                if args.once:
                    raise
            print(encode({'event': 'account_profit_worker.pass_completed',
                          'observed_ms': int(time.time() * 1000),
                          'elapsed_ms': int((time.monotonic() - cycle_started) * 1000),
                          'failed_stages': list(report.get('failed', {}))}), flush=True)
            if args.once:
                break
            time.sleep(30)
    elif action == 'activate-prompt':
        print(encode(activate_writer_prompt(path, get_paths().root_dir, template_key=args.template_key)))
    elif action == 'preview':
        from packages.common.config import load_x_processing_settings
        from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
        from packages.x_processing.worker import XProcessingWorker
        from packages.x_processing.models import HYPERLIQUID_SOURCE
        from .preview import generate_preview
        from .eligibility import news_eligibility
        if not 1 <= len(args.task_ids) <= 10:
            raise ValueError('Preview requires 1..10 task IDs')
        repo = SQLiteXProcessingRepository(path)
        tasks = [repo.get_task(task_id) for task_id in args.task_ids]
        if any(task.source != HYPERLIQUID_SOURCE for task in tasks):
            raise ValueError('Only Hyperliquid tasks support this preview')
        if any(not news_eligibility(task.raw_payload)['eligible'] for task in tasks):
            raise ValueError('Baseline without change evidence is not eligible for a news preview')
        settings = load_x_processing_settings()
        worker = XProcessingWorker(stage='write', repository=repo, settings=settings)
        from .scenarios import prompt_key_for_event
        for task in tasks:
            from dataclasses import replace
            task = replace(task, raw_payload=store.with_address_notes(task.raw_payload))
            template_key = str(task.metadata.get('writer_template_key') or
                               prompt_key_for_event(task.raw_payload))
            prompt = repo.get_active_prompt(template_key)
            result = generate_preview(task=task, prompt=prompt, settings=settings, ai_client=worker._get_ai_client())
            store.save_global('editorial_preview:' + task.raw_payload['key'], result, [])
            print(encode({'task_id': task.id, 'prompt_template_key': template_key, **result}))
    elif action == 'enrich-account-test-cases':
        from .test_cases import enrich_account_cases
        print(encode(enrich_account_cases(store, limit=args.limit)))
    elif action == 'replay-cached-test-cases':
        from .test_cases import replay_cached_cases
        print(encode(replay_cached_cases(store, reset=args.reset)))
    elif action == 'preview-test-cases':
        from .test_cases import preview_cases
        print(encode(preview_cases(store, limit=args.limit, repository_template=args.repository_template)))
    elif action == 'mine-test-cases':
        from .test_cases import mine_cases
        print(encode(mine_cases(store, address_limit=args.address_limit, per_side=args.per_side,
                                addresses=args.addresses, coins=args.coins,
                                threshold_profile=args.threshold_profile)))
    elif action == 'collect':
        from .client import NewsClient
        from .market_cap import CoinGeckoMarketCaps, DEFAULT_MARKET_CAP_MAPPING
        from .service import NewsMonitor
        import json
        mapping = json.loads(Path(args.market_cap_mapping).read_text()) if args.market_cap_mapping else dict(DEFAULT_MARKET_CAP_MAPPING)
        report = NewsMonitor(store=store, client=NewsClient(),
            addresses_per_pass=args.addresses_per_pass, leaderboard_seeds=args.leaderboard_seeds,
            markets_per_discovery=args.markets_per_discovery,
            market_cap_client=CoinGeckoMarketCaps() if mapping else None, market_cap_mapping=mapping).run_once()
        print(encode(report))
    elif action == 'dispatch':
        from .dispatch import EventDispatcher
        print(encode(EventDispatcher(store).dispatch(limit=args.limit, event_keys=args.event_keys)))
    elif action == 'status':
        from .dashboard import dashboard
        print(encode(dashboard(store, page=args.page)))
    return 0


def activate_writer_prompt(path: Path, root: Path, *, template_key: str = 'hyperliquid_writer') -> dict[str, Any]:
    """Publish the dedicated file as a new full version without rewriting history."""
    from packages.common.storage import connect_sqlite
    from packages.x_processing.repository import PROMPT_SEEDS
    seed = PROMPT_SEEDS.get(template_key)
    if not template_key.startswith('hyperliquid_') or seed is None:
        raise ValueError('Unknown Hyperliquid writer prompt template')
    content = (root / seed[1]).read_text(encoding='utf-8')
    with connect_sqlite(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute("SELECT v.* FROM prompt_templates t JOIN prompt_template_versions v ON v.id=t.active_version_id WHERE t.template_key=? AND v.deleted_at IS NULL", (template_key,)).fetchone()
        if active is None:
            raise ValueError('Initialize Hyperliquid writer prompt before activation')
        if active['content'] == content:
            conn.commit()
            return {'version_number': active['version_number'], 'changed': False}
        version = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM prompt_template_versions WHERE template_key=?", (template_key,)).fetchone()[0]
        cursor = conn.execute("INSERT INTO prompt_template_versions(template_key,version_number,content,note,published_at) VALUES (?,?,?,?,CURRENT_TIMESTAMP)",
                              (template_key, version, content, 'approved scenario wording and factual footer'))
        conn.execute("UPDATE prompt_templates SET active_version_id=?,updated_at=CURRENT_TIMESTAMP WHERE template_key=?", (cursor.lastrowid, template_key))
        conn.commit()
        return {'version_number': version, 'changed': True}
