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
    elif action == 'activate-prompt':
        print(encode(activate_writer_prompt(path, get_paths().root_dir)))
    elif action == 'preview':
        from packages.common.config import load_x_processing_settings
        from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository
        from packages.x_processing.worker import XProcessingWorker
        from packages.x_processing.models import HYPERLIQUID_SOURCE
        from .preview import generate_preview
        if not 1 <= len(args.task_ids) <= 10:
            raise ValueError('Preview requires 1..10 task IDs')
        repo = SQLiteXProcessingRepository(path)
        tasks = [repo.get_task(task_id) for task_id in args.task_ids]
        if any(task.source != HYPERLIQUID_SOURCE for task in tasks):
            raise ValueError('Only Hyperliquid tasks support this preview')
        settings = load_x_processing_settings()
        worker = XProcessingWorker(stage='write', repository=repo, settings=settings)
        prompt = repo.get_active_prompt('hyperliquid_writer')
        for task in tasks:
            result = generate_preview(task=task, prompt=prompt, settings=settings, ai_client=worker._get_ai_client())
            store.save_global('editorial_preview:' + task.raw_payload['key'], result, [])
            print(encode({'task_id': task.id, **result}))
    elif action == 'collect':
        from .client import NewsClient
        from .market_cap import CoinGeckoMarketCaps
        from .service import NewsMonitor
        import json
        mapping = json.loads(Path(args.market_cap_mapping).read_text()) if args.market_cap_mapping else {}
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


def activate_writer_prompt(path: Path, root: Path) -> dict[str, Any]:
    """Publish the dedicated file as a new full version without rewriting history."""
    from packages.common.storage import connect_sqlite
    content = (root / 'docs/OdAIly代码与整体设计/Hyperliquid快讯模板.txt').read_text(encoding='utf-8')
    with connect_sqlite(path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        active = conn.execute("SELECT v.* FROM prompt_templates t JOIN prompt_template_versions v ON v.id=t.active_version_id WHERE t.template_key='hyperliquid_writer' AND v.deleted_at IS NULL").fetchone()
        if active is None:
            raise ValueError('Initialize Hyperliquid writer prompt before activation')
        if active['content'] == content:
            conn.commit()
            return {'version_number': active['version_number'], 'changed': False}
        version = conn.execute("SELECT COALESCE(MAX(version_number),0)+1 FROM prompt_template_versions WHERE template_key='hyperliquid_writer'").fetchone()[0]
        cursor = conn.execute("INSERT INTO prompt_template_versions(template_key,version_number,content,note,published_at) VALUES ('hyperliquid_writer',?,?,?,CURRENT_TIMESTAMP)",
                              (version, content, 'news language, trigger-only facts and supplemental footer'))
        conn.execute("UPDATE prompt_templates SET active_version_id=?,updated_at=CURRENT_TIMESTAMP WHERE template_key='hyperliquid_writer'", (cursor.lastrowid,))
        conn.commit()
        return {'version_number': version, 'changed': True}
