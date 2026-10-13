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
