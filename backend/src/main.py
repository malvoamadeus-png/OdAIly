from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from packages.common.config import (  # noqa: E402
    load_external_media_alert_settings,
    load_auditor_settings,
    load_competitor_monitor_settings,
    load_gate_settings,
    load_pipeline_supervisor_settings,
    load_settings,
    load_telegram_discovery_settings,
    load_whale_watch_hyperliquid_settings,
    load_whale_watch_settings,
    load_writer3_settings,
    load_x_capture_worker_settings,
    load_x_processing_settings,
)
from packages.common.paths import ensure_runtime_dirs, get_paths  # noqa: E402
from packages.common.time_utils import SHANGHAI_TZ  # noqa: E402
from packages.x_processing.publisher_config import load_publisher_rule_config, save_publisher_rule_config  # noqa: E402


SCHEDULED_TASK_IDS = ("gate-tradfi", "us-market")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OdAIly content publishing worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--config", help="Path to task config JSON.")

    run_once = subparsers.add_parser("run-once", help="Generate one content brief.")
    add_common(run_once)
    run_once.add_argument("--task", choices=SCHEDULED_TASK_IDS, default="us-market")
    run_once.add_argument("--kind", required=True)
    run_once.add_argument("--dry-run", action="store_true", help="Do not call the Push Data API.")
    run_once.add_argument("--send", action="store_true", help="Call the Push Data API even if config uses dry_run.")
    run_once.add_argument("--force", action="store_true", help="Run even when the schedule calendar would skip.")

    worker = subparsers.add_parser("run-worker", help="Run the scheduled worker.")
    add_common(worker)

    x_init_db = subparsers.add_parser("x-init-db", help="Initialize X capture SQLite tables.")

    console_grant_admin = subparsers.add_parser(
        "console-grant-admin",
        help="Grant one email access to the local SQLite console.",
    )
    console_grant_admin.add_argument("--email", required=True, help="Admin email address.")

    console_revoke_admin = subparsers.add_parser(
        "console-revoke-admin",
        help="Revoke one email from the local SQLite console.",
    )
    console_revoke_admin.add_argument("--email", required=True, help="Admin email address.")

    console_list_admins = subparsers.add_parser(
        "console-list-admins",
        help="List console admin emails from local SQLite.",
    )

    editor_plugin_init = subparsers.add_parser(
        "editor-plugin-init-db",
        help="Initialize editor plugin SQLite tables.",
    )

    editor_plugin_grant = subparsers.add_parser(
        "editor-plugin-grant-user",
        help="Grant one email access to the editor plugin.",
    )
    editor_plugin_grant.add_argument("--email", required=True, help="Editor email address.")
    editor_plugin_grant.add_argument("--display-name", help="Optional display name shown in the plugin.")

    editor_plugin_revoke = subparsers.add_parser(
        "editor-plugin-revoke-user",
        help="Revoke one email from the editor plugin whitelist.",
    )
    editor_plugin_revoke.add_argument("--email", required=True, help="Editor email address.")

    editor_plugin_list = subparsers.add_parser(
        "editor-plugin-list-users",
        help="List editor plugin whitelist emails from local SQLite.",
    )

    editor_plugin_api = subparsers.add_parser(
        "editor-plugin-api-server",
        help="Run the editor plugin news generation HTTP server.",
    )
    editor_plugin_api.add_argument("--host", help="Bind host. Defaults to EDITOR_PLUGIN_API_HOST or 127.0.0.1.")
    editor_plugin_api.add_argument("--port", type=int, help="Bind port. Defaults to EDITOR_PLUGIN_API_PORT or 8765.")

    editor_plugin_local_feed_status = subparsers.add_parser(
        "editor-plugin-local-feed-status",
        help="Print local editor plugin feed store diagnostics using only local storage.",
    )
    editor_plugin_local_feed_status.add_argument(
        "--max-age-hours",
        type=int,
        default=None,
        help="Recent feed window for lane/kind counts. Defaults to EDITOR_PLUGIN_LOCAL_FEED_MAX_AGE_HOURS or 2.",
    )

    local_pipeline = subparsers.add_parser(
        "local-pipeline-server",
        help="Run the local SQLite-backed content pipeline server.",
    )
    local_pipeline.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    local_pipeline.add_argument("--port", type=int, default=8776, help="Bind port. Defaults to 8776.")

    local_pipeline_skip = subparsers.add_parser(
        "local-pipeline-skip-legacy",
        help="Mark pre-cutover unfinished DB tasks as legacy_skipped.",
    )
    local_pipeline_skip.add_argument("--execute", action="store_true", help="Actually update tasks. Defaults to dry-run.")

    x_worker = subparsers.add_parser("x-capture-worker", help="Run the X capture worker.")
    x_worker.add_argument("--once", action="store_true", help="Run one capture pass and exit.")

    non_mainstream_init = subparsers.add_parser(
        "non-mainstream-media-init-db",
        help="Initialize non-mainstream media SQLite tables.",
    )

    non_mainstream_worker = subparsers.add_parser(
        "non-mainstream-media-worker",
        help="Run the non-mainstream media capture worker.",
    )
    non_mainstream_worker.add_argument("--once", action="store_true", help="Run one capture pass and exit.")

    telegram_discovery_worker = subparsers.add_parser(
        "telegram-discovery-worker",
        help="Run the Telegram-first non-mainstream media discovery worker.",
    )

    external_alert_init = subparsers.add_parser(
        "external-media-alert-init-db",
        help="Initialize external media alert SQLite tables.",
    )

    external_alert_worker = subparsers.add_parser(
        "external-media-alert-worker",
        help="Run the external media alert worker.",
    )
    external_alert_worker.add_argument(
        "--stage",
        choices=["domain_judge", "search", "notify"],
        required=True,
    )
    external_alert_worker.add_argument("--once", action="store_true", help="Process one available task and exit.")

    external_alert_fetcher = subparsers.add_parser(
        "external-media-alert-fetcher",
        help="Run the external media newsflash fetcher.",
    )
    external_alert_fetcher.add_argument("--once", action="store_true", help="Run one fetch pass and exit.")

    jin10_init = subparsers.add_parser("jin10-init-db", help="Initialize Jin10 monitor SQLite tables.")

    jin10_worker = subparsers.add_parser("jin10-monitor-worker", help="Run Jin10 newsflash monitor.")
    jin10_worker.add_argument("--once", action="store_true", help="Run one Jin10 monitor pass and exit.")

    x_process_init = subparsers.add_parser("x-process-init-db", help="Initialize X processing SQLite tables.")
    x_process_init.add_argument(
        "--skip-clear-pending",
        action="store_true",
        help="Do not delete existing X pending tasks during initialization.",
    )

    subparsers.add_parser(
        "publisher-init-config",
        help="Create the default local publisher rule config if it does not exist.",
    )

    x_process_worker = subparsers.add_parser("x-process-worker", help="Run an X processing stage worker.")
    x_process_worker.add_argument(
        "--stage",
        choices=["judge", "judge_crypto", "judge_ai", "judge_jin10", "search", "write", "format_publish", "publish"],
        required=True,
    )
    x_process_worker.add_argument("--once", action="store_true", help="Process one available task and exit.")

    competitor_init = subparsers.add_parser("competitor-init-db", help="Initialize competitor monitor/searcher SQLite tables.")

    competitor_prune = subparsers.add_parser("competitor-prune-excluded-events", help="Remove excluded newsflash items from event analysis.")

    competitor_prune_orphans = subparsers.add_parser(
        "competitor-prune-orphan-events",
        help="Delete newsflash events that have no linked source items.",
    )

    competitor_repair_time = subparsers.add_parser("competitor-repair-newsflash-time", help="Repair newsflash published_at timezone interpretation.")

    competitor_worker = subparsers.add_parser("competitor-monitor-worker", help="Run competitor/Odaily newsflash capture.")
    competitor_worker.add_argument("--once", action="store_true", help="Run one competitor capture pass and exit.")

    whale_watch_init = subparsers.add_parser("whale-watch-init-db", help="Initialize whale watch SQLite tables.")

    whale_watch_worker = subparsers.add_parser("whale-watch-worker", help="Run whale onchain activity monitor.")
    whale_watch_worker.add_argument("--once", action="store_true", help="Run one whale watch pass and exit.")

    whale_watch_list_addresses = subparsers.add_parser(
        "whale-watch-list-addresses",
        help="List whale onchain addresses for audit or cleanup.",
    )
    whale_watch_list_addresses.add_argument(
        "--created-since-hours",
        type=int,
        help="Only show addresses created within the last N hours.",
    )
    whale_watch_list_addresses.add_argument(
        "--include-disabled",
        action="store_true",
        help="Include disabled addresses. Default only shows enabled rows when no time filter is given.",
    )

    whale_watch_delete_addresses = subparsers.add_parser(
        "whale-watch-delete-addresses",
        help="Delete whale onchain addresses by id or recent creation window. Defaults to dry-run.",
    )
    whale_watch_delete_addresses.add_argument(
        "--ids",
        nargs="+",
        type=int,
        help="Explicit address ids to delete.",
    )
    whale_watch_delete_addresses.add_argument(
        "--created-since-hours",
        type=int,
        help="Delete addresses created within the last N hours.",
    )
    whale_watch_delete_addresses.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete rows. Omit for dry-run.",
    )

    whale_watch_hyperliquid_worker = subparsers.add_parser(
        "whale-watch-hyperliquid-worker",
        help="Run whale Hyperliquid activity monitor.",
    )
    whale_watch_hyperliquid_worker.add_argument("--once", action="store_true", help="Run one Hyperliquid whale pass and exit.")

    supervisor = subparsers.add_parser("pipeline-supervisor", help="Run pipeline health checks and Telegram alerts.")
    supervisor.add_argument("--once", action="store_true", help="Run one supervisor pass and exit.")

    telegram_test = subparsers.add_parser("telegram-test", help="Send a Telegram test message.")
    telegram_test.add_argument("--text", default="OdAIly Telegram topic test", help="Message text to send.")
    telegram_test.add_argument("--message-thread-id", help="Telegram forum topic message_thread_id.")

    telegram_topic = subparsers.add_parser("telegram-create-topic", help="Create a Telegram forum topic and print message_thread_id.")
    telegram_topic.add_argument("--name", default="系统告警", help="Telegram forum topic name.")

    writer3_init = subparsers.add_parser("writer3-init-db", help="Initialize Writer3 SQLite tables.")

    writer3_backfill = subparsers.add_parser("writer3-backfill-odaily", help="Backfill Odaily references for Writer3.")
    writer3_backfill.add_argument("--days", type=int, default=90)

    writer3_sync = subparsers.add_parser("writer3-sync-index", help="Sync Writer3 local Odaily index from local SQLite.")
    writer3_sync.add_argument("--days", type=int, default=90)

    writer3_worker = subparsers.add_parser("writer3-worker", help="Run Writer3 Telegram topic worker.")
    writer3_worker.add_argument("--once", action="store_true", help="Process one Writer3 task and exit.")

    writer3_confirm_worker = subparsers.add_parser("writer3-confirm-worker", help="Run Writer3 Telegram confirmation button worker.")
    writer3_confirm_worker.add_argument("--once", action="store_true", help="Process one Telegram getUpdates response and exit.")
    writer3_confirm_worker.add_argument("--poll-timeout", type=int, default=20, help="Telegram getUpdates timeout in seconds.")

    writer3_reset = subparsers.add_parser("writer3-reset-task", help="Reset one Writer3 task for retry.")
    writer3_reset.add_argument("--task-id", type=int, required=True)

    auditor_init = subparsers.add_parser("auditor-init-db", help="Initialize Auditor SQLite tables.")

    auditor_worker = subparsers.add_parser("auditor-worker", help="Run Odaily published-news auditor worker.")
    auditor_worker.add_argument("--once", action="store_true", help="Process one auditor task and exit.")

    maintenance_cleanup = subparsers.add_parser(
        "maintenance-cleanup",
        help="Clean old runtime logs and trim completed payload fields. Defaults to dry-run.",
    )
    maintenance_cleanup.add_argument("--execute", action="store_true", help="Actually delete/update rows. Omit for dry-run.")
    maintenance_cleanup.add_argument("--retention-days", type=int, default=7, help="Runtime log retention in days.")
    maintenance_cleanup.add_argument("--feedback-retention-days", type=int, default=90, help="Editor feedback retention in days.")
    maintenance_cleanup.add_argument(
        "--completed-field-retention-days",
        type=int,
        default=7,
        help="Age in days before trimming payload fields from completed records.",
    )

    search_cache_maintenance = subparsers.add_parser(
        "search-cache-maintenance",
        help="Prune and optionally compact the local embedding search cache. Defaults to dry-run.",
    )
    search_cache_maintenance.add_argument("--path", help="Override the configured searcher SQLite path.")
    search_cache_maintenance.add_argument("--execute", action="store_true", help="Delete stale cache rows and convert vectors.")
    search_cache_maintenance.add_argument(
        "--compact",
        action="store_true",
        help="Checkpoint and VACUUM after cleanup. Requires --execute and temporary free disk space.",
    )
    storage_backup = subparsers.add_parser(
        "storage-backup",
        help="Create and integrity-check an online backup of the active SQLite primary database.",
    )
    storage_backup.add_argument("--destination", type=Path, required=True)
    storage_import = subparsers.add_parser(
        "storage-import-legacy",
        help="One-time import from the frozen legacy database into the SQLite primary database.",
    )
    storage_import.add_argument("--execute", action="store_true", help="Perform the import; otherwise print source/destination counts.")
    storage_import.add_argument("--truncate", action="store_true", help="Clear each destination table before importing it.")
    storage_import.add_argument("--batch-size", type=int, default=1000)
    storage_import.add_argument(
        "--sample-rows-per-table",
        type=int,
        help="Import at most this many rows per table for schema/type validation; never use for cutover.",
    )
    search_cache_maintenance.add_argument("--short-retention-days", type=int, default=2)
    search_cache_maintenance.add_argument("--reference-retention-days", type=int, default=8)

    gate_market = subparsers.add_parser(
        "gate-market",
        help="Manage or run the standalone Gate market broadcast module.",
    )
    gate_market_subparsers = gate_market.add_subparsers(dest="gate_market_action", required=True)
    gate_market_subparsers.add_parser("init-db", help="Initialize the local Gate market SQLite database.")
    gate_market_subparsers.add_parser("status", help="Print current Gate market configuration and state.")
    gate_market_run = gate_market_subparsers.add_parser("run", help="Run the Gate market worker.")
    gate_market_run.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    gate_market_mode = gate_market_subparsers.add_parser("set-mode", help="Set backend or live publishing mode.")
    gate_market_mode.add_argument("mode", choices=["backend", "live"])
    gate_market_threshold = gate_market_subparsers.add_parser(
        "set-threshold",
        help="Set one symbol threshold and reset that symbol's runtime state.",
    )
    gate_market_threshold.add_argument("symbol")
    gate_market_threshold.add_argument("threshold")
    gate_market_backtest = gate_market_subparsers.add_parser(
        "backtest",
        help="Replay historical one-minute closes using current SQLite thresholds.",
    )
    gate_market_backtest.add_argument("--days", type=int, default=90)

    subparsers.add_parser("doctor", help="Print configuration and schedule diagnostics.")
    return parser.parse_args()


def _dry_run_override(args: argparse.Namespace) -> bool | None:
    if getattr(args, "dry_run", False) and getattr(args, "send", False):
        raise ValueError("--dry-run and --send cannot be used together")
    if getattr(args, "dry_run", False):
        return True
    if getattr(args, "send", False):
        return False
    return None


def run_once_command(args: argparse.Namespace) -> int:
    from packages.tasks.registry import run_task_once

    paths = get_paths()
    result = run_task_once(
        task_id=args.task,
        kind=args.kind,
        config_path=args.config,
        paths=paths,
        dry_run_override=_dry_run_override(args),
        force=args.force,
    )
    print(
        f"[odaily] task={args.task} kind={result.kind} status={result.status} "
        f"run_id={result.run_id} message={result.message}"
    )
    return result.exit_code


def run_worker_command(args: argparse.Namespace) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler

    from packages.tasks.registry import TASKS, run_task_once

    paths = get_paths()
    ensure_runtime_dirs(paths)
    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ)

    for task_id, task in TASKS.items():
        for schedule in task.schedules:
            scheduler.add_job(
                lambda scheduled_task=task_id, scheduled_kind=schedule.kind: run_task_once(
                    task_id=scheduled_task,
                    kind=scheduled_kind,
                    config_path=None,
                    paths=paths,
                    dry_run_override=None,
                    force=False,
                ),
                schedule.trigger,
                id=schedule.job_id,
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
    descriptions = [
        f"{task_id}:{schedule.description}"
        for task_id, task in TASKS.items()
        for schedule in task.schedules
    ]
    print("[odaily] worker started. " + "; ".join(descriptions))
    scheduler.start()
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    from packages.tasks.registry import TASKS

    paths = get_paths()
    ensure_runtime_dirs(paths)
    print(f"[odaily] root={paths.root_dir}")
    market_settings = load_settings(None)
    gate_settings = load_gate_settings(None)
    print(f"[odaily] us-market config={paths.market_brief_config_path}")
    print(f"[odaily] us-market watchlist={','.join(market_settings.watchlist)}")
    print(f"[odaily] us-market push_endpoint={market_settings.push_endpoint}")
    print(f"[odaily] us-market dry_run={market_settings.dry_run}")
    print(f"[odaily] gate-tradfi config={paths.gate_tradfi_config_path}")
    print(f"[odaily] gate-tradfi tradfi_symbols={','.join(gate_settings.tradfi_symbols)}")
    print(f"[odaily] gate-tradfi futures_symbols={','.join(gate_settings.futures_symbols)}")
    print(f"[odaily] gate-tradfi push_endpoint={gate_settings.push_endpoint}")
    print(f"[odaily] gate-tradfi dry_run={gate_settings.dry_run}")
    for task_id, task in TASKS.items():
        schedules = ", ".join(schedule.description for schedule in task.schedules)
        print(f"[odaily] schedules {task_id}: {schedules}")
    return 0


def x_init_db_command(args: argparse.Namespace) -> int:
    from packages.x_capture.repository import create_x_capture_repository

    repository = create_x_capture_repository(None)
    repository.init_schema()
    print("[odaily] x-capture database schema initialized")
    return 0


def console_grant_admin_command(args: argparse.Namespace) -> int:
    from packages.common.console_auth import create_console_auth_repository

    repository = create_console_auth_repository(None)
    repository.init_schema()
    record = repository.upsert_admin(args.email)
    print(f"[odaily] console admin granted email={record.email}")
    return 0


def console_revoke_admin_command(args: argparse.Namespace) -> int:
    from packages.common.console_auth import create_console_auth_repository

    repository = create_console_auth_repository(None)
    repository.init_schema()
    removed = repository.delete_admin(args.email)
    print(f"[odaily] console admin revoked email={args.email.strip().lower()} removed={removed}")
    return 0 if removed else 1


def console_list_admins_command(args: argparse.Namespace) -> int:
    from packages.common.console_auth import create_console_auth_repository

    repository = create_console_auth_repository(None)
    repository.init_schema()
    admins = repository.list_admins()
    for admin in admins:
        print(f"{admin.email}\tcreated_at={admin.created_at}\tupdated_at={admin.updated_at}")
    print(f"[odaily] console admin count={len(admins)}")
    return 0


def editor_plugin_init_command(args: argparse.Namespace) -> int:
    from packages.common.editor_plugin_auth import create_editor_plugin_auth_repository
    from packages.pipeline_timing import create_pipeline_timing_repository
    from packages.x_capture.repository import create_x_capture_repository

    repository = create_editor_plugin_auth_repository(None)
    repository.init_schema()
    create_x_capture_repository(None).init_schema()
    create_pipeline_timing_repository(None).init_schema()
    print("[odaily] editor plugin database schema initialized")
    return 0


def editor_plugin_grant_user_command(args: argparse.Namespace) -> int:
    from packages.common.editor_plugin_auth import create_editor_plugin_auth_repository

    repository = create_editor_plugin_auth_repository(None)
    repository.init_schema()
    record = repository.upsert_user(args.email, args.display_name, enabled=True)
    print(
        "[odaily] editor plugin user granted "
        f"email={record.email} display_name={record.display_name or '-'} enabled={record.enabled}"
    )
    return 0


def editor_plugin_revoke_user_command(args: argparse.Namespace) -> int:
    from packages.common.editor_plugin_auth import create_editor_plugin_auth_repository

    repository = create_editor_plugin_auth_repository(None)
    repository.init_schema()
    removed = repository.delete_user(args.email)
    print(f"[odaily] editor plugin user revoked email={args.email.strip().lower()} removed={removed}")
    return 0 if removed else 1


def editor_plugin_list_users_command(args: argparse.Namespace) -> int:
    from packages.common.editor_plugin_auth import create_editor_plugin_auth_repository

    repository = create_editor_plugin_auth_repository(None)
    repository.init_schema()
    users = repository.list_users()
    for user in users:
        print(
            f"{user.email}\tdisplay_name={user.display_name or '-'}\tenabled={user.enabled}"
            f"\tcreated_at={user.created_at}\tupdated_at={user.updated_at}"
        )
    print(f"[odaily] editor plugin user count={len(users)}")
    return 0


def editor_plugin_api_server_command(args: argparse.Namespace) -> int:
    from packages.editor_plugin_api import run_editor_plugin_api_server

    return run_editor_plugin_api_server(
        database_url=None,
        host=args.host,
        port=args.port,
    )


def editor_plugin_local_feed_status_command(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from packages.editor_plugin_local_store import LocalEditorPluginStore

    load_dotenv()
    paths = get_paths()
    ensure_runtime_dirs(paths)
    local_feed_max_age_hours = int(os.getenv("EDITOR_PLUGIN_LOCAL_FEED_MAX_AGE_HOURS") or 2)
    max_age_hours = args.max_age_hours or local_feed_max_age_hours
    store = LocalEditorPluginStore(paths.runtime_dir / "editor_plugin_local.sqlite")
    payload = store.stats(max_age_hours=max_age_hours)
    payload["settings"] = {
        "local_feed_sync_interval_seconds": float(os.getenv("EDITOR_PLUGIN_LOCAL_FEED_SYNC_INTERVAL_SECONDS") or 30.0),
        "local_feed_max_age_hours": local_feed_max_age_hours,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


def local_pipeline_server_command(args: argparse.Namespace) -> int:
    from packages.local_pipeline import run_local_pipeline_server

    run_local_pipeline_server(
        database_url=None,
        host=args.host,
        port=args.port,
    )
    return 0


def local_pipeline_skip_legacy_command(args: argparse.Namespace) -> int:
    from packages.x_processing.repository import create_x_processing_repository

    repository = create_x_processing_repository(None)
    if not args.execute:
        count = repository.count_legacy_unfinished_tasks()
        print(
            "[odaily] local pipeline legacy skip dry-run "
            f"count={count}. Re-run with --execute to update tasks."
        )
        return 0
    count = repository.mark_legacy_unfinished_tasks_skipped()
    print(f"[odaily] local pipeline legacy tasks skipped count={count}")
    return 0


def x_capture_worker_command(args: argparse.Namespace) -> int:
    from packages.common.source_exclusions import SourceExclusionMatcher, create_source_exclusion_repository
    from packages.local_pipeline import LocalPipelineClient
    from packages.x_capture import FXTwitterClient, XCaptureWorker
    from packages.x_capture.repository import create_x_capture_repository

    settings = load_x_capture_worker_settings()
    repository = create_x_capture_repository(None)
    worker = XCaptureWorker(
        repository=repository,
        client=FXTwitterClient(),
        attempt_retention_days=settings.attempt_retention_days,
        freshness_window_seconds=settings.processing_freshness_window_seconds,
        pipeline_client=LocalPipelineClient(),
        exclusion_matcher=SourceExclusionMatcher(create_source_exclusion_repository(None)),
    )
    if args.once:
        stats = worker.run_once()
        print(f"[odaily] x-capture once completed. accounts={len(stats)}")
        return 0 if all(item.status == "success" for item in stats) else 1
    worker.run_forever()
    return 0


def non_mainstream_media_init_db_command(args: argparse.Namespace) -> int:
    from packages.non_mainstream_media import create_non_mainstream_media_repository, get_site_registry

    repository = create_non_mainstream_media_repository(None)
    repository.init_schema()
    repository.sync_sources(list(get_site_registry().values()))
    print("[odaily] non-mainstream media database schema initialized")
    return 0


def non_mainstream_media_worker_command(args: argparse.Namespace) -> int:
    from packages.common.source_exclusions import SourceExclusionMatcher, create_source_exclusion_repository
    from packages.local_pipeline import LocalPipelineClient
    from packages.non_mainstream_media import NonMainstreamMediaWorker, create_non_mainstream_media_repository

    repository = create_non_mainstream_media_repository(None)
    worker = NonMainstreamMediaWorker(
        repository=repository,
        pipeline_client=LocalPipelineClient(),
        exclusion_matcher=SourceExclusionMatcher(create_source_exclusion_repository(None)),
    )
    if args.once:
        stats = worker.run_once()
        print(f"[odaily] non-mainstream media once completed. sources={len(stats)}")
        return 0 if all(item.status == "success" for item in stats) else 1
    worker.run_forever()
    return 0


def telegram_discovery_worker_command(args: argparse.Namespace) -> int:
    from packages.common.source_exclusions import SourceExclusionMatcher, create_source_exclusion_repository
    from packages.local_pipeline import LocalPipelineClient
    from packages.non_mainstream_media import TelegramDiscoveryWorker, create_non_mainstream_media_repository

    repository = create_non_mainstream_media_repository(None)
    worker = TelegramDiscoveryWorker(
        repository=repository,
        settings=load_telegram_discovery_settings(),
        pipeline_client=LocalPipelineClient(),
        exclusion_matcher=SourceExclusionMatcher(create_source_exclusion_repository(None)),
    )
    asyncio.run(worker.run_forever())
    return 0


def external_media_alert_init_db_command(args: argparse.Namespace) -> int:
    from packages.external_media_alert import create_external_media_alert_repository
    from packages.x_processing.repository import create_x_processing_repository

    paths = get_paths()
    x_repository = create_x_processing_repository(None)
    x_repository.init_schema()
    x_repository.seed_prompt_templates(root_dir=paths.root_dir)
    repository = create_external_media_alert_repository(None)
    repository.init_schema()
    print("[odaily] external media alert database schema initialized")
    return 0


def external_media_alert_worker_command(args: argparse.Namespace) -> int:
    from packages.external_media_alert import ExternalMediaAlertWorker, create_external_media_alert_repository

    ensure_runtime_dirs(get_paths())
    repository = create_external_media_alert_repository(None)
    worker = ExternalMediaAlertWorker(
        stage=args.stage,
        repository=repository,
        settings=load_external_media_alert_settings(),
    )
    if args.once:
        result = worker.run_once()
        print(
            f"[odaily] external media alert once stage={result.stage} "
            f"processed={result.processed} failed={result.failed} message={result.message}"
        )
        return result.exit_code
    worker.run_forever()
    return 0


def external_media_alert_fetcher_command(args: argparse.Namespace) -> int:
    from packages.external_media_alert import ExternalMediaFetcher, create_external_media_alert_repository

    settings = load_external_media_alert_settings()
    repository = create_external_media_alert_repository(None)
    fetcher = ExternalMediaFetcher(
        repository=repository,
        request_timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )
    if args.once:
        stats = fetcher.run_once()
        print(
            "[odaily] external media fetcher once completed. "
            f"sources={len(stats)} saved={sum(item.saved_count for item in stats)} "
            f"duplicates={sum(item.duplicate_count for item in stats)}"
        )
        return 0 if all(item.status == "success" for item in stats) else 1
    fetcher.run_forever()
    return 0


def jin10_init_db_command(args: argparse.Namespace) -> int:
    from packages.jin10_monitor.repository import create_jin10_monitor_repository
    from packages.x_capture.repository import create_x_capture_repository
    from packages.x_processing.repository import create_x_processing_repository

    paths = get_paths()
    create_x_capture_repository(None).init_schema()
    x_repository = create_x_processing_repository(None)
    x_repository.init_schema()
    x_repository.seed_prompt_templates(root_dir=paths.root_dir)
    create_jin10_monitor_repository(None).init_schema()
    print("[odaily] Jin10 monitor database schema initialized")
    return 0


def jin10_monitor_worker_command(args: argparse.Namespace) -> int:
    from packages.common.source_exclusions import SourceExclusionMatcher, create_source_exclusion_repository
    from packages.jin10_monitor import Jin10MonitorWorker
    from packages.jin10_monitor.repository import create_jin10_monitor_repository
    from packages.local_pipeline import LocalPipelineClient

    repository = create_jin10_monitor_repository(None)
    worker = Jin10MonitorWorker(
        repository=repository,
        pipeline_client=LocalPipelineClient(),
        exclusion_matcher=SourceExclusionMatcher(create_source_exclusion_repository(None)),
    )
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] jin10 monitor once "
            f"status={result.status} fetched={result.fetched} seeded={result.seeded} "
            f"new={result.new} saved={result.saved} error={result.error or '-'}"
        )
        return 0 if result.status in {"success", "disabled"} else 1
    worker.run_forever()
    return 0


def x_process_init_db_command(args: argparse.Namespace) -> int:
    from packages.x_capture.repository import create_x_capture_repository
    from packages.x_processing.repository import create_x_processing_repository

    paths = get_paths()
    create_x_capture_repository(None).init_schema()
    repository = create_x_processing_repository(None)
    repository.init_schema()
    repository.seed_prompt_templates(root_dir=paths.root_dir)
    deleted = 0 if args.skip_clear_pending else repository.clear_old_pending_x_tasks()
    print(f"[odaily] x-processing database schema initialized. deleted_pending={deleted}")
    return 0


def publisher_init_config_command(args: argparse.Namespace) -> int:
    saved = save_publisher_rule_config(load_publisher_rule_config(), updated_by="system")
    print(f"[odaily] publisher config initialized updated_at={saved.updated_at}")
    return 0


def x_process_worker_command(args: argparse.Namespace) -> int:
    from packages.x_capture.repository import create_x_capture_repository
    from packages.x_processing import XProcessingWorker
    from packages.x_processing.repository import create_x_processing_repository

    legacy_enabled = (os.getenv("ENABLE_LEGACY_X_PROCESS_WORKERS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if not legacy_enabled:
        print(
            "[odaily] legacy x-processing worker disabled; "
            "set ENABLE_LEGACY_X_PROCESS_WORKERS=true only for an intentional rollback"
        )
        return 0

    ensure_runtime_dirs(get_paths())
    x_capture_repository = create_x_capture_repository(None)
    repository = create_x_processing_repository(None)
    worker = XProcessingWorker(
        stage=args.stage,
        repository=repository,
        settings=load_x_processing_settings(),
        x_capture_repository=x_capture_repository,
    )
    if args.once:
        result = worker.run_once()
        print(
            f"[odaily] x-processing once stage={result.stage} "
            f"processed={result.processed} failed={result.failed} message={result.message}"
        )
        return result.exit_code
    worker.run_forever()
    return 0


def competitor_init_db_command(args: argparse.Namespace) -> int:
    from packages.competitor_monitor import create_competitor_monitor_repository

    repository = create_competitor_monitor_repository(None)
    repository.init_schema()
    print("[odaily] competitor/searcher database schema initialized")
    return 0


def competitor_prune_excluded_events_command(args: argparse.Namespace) -> int:
    from packages.competitor_monitor import create_competitor_monitor_repository

    repository = create_competitor_monitor_repository(None)
    result = repository.prune_excluded_event_sources()
    print(
        "[odaily] competitor excluded events pruned "
        f"matched_items={result['matched_items']} "
        f"removed_sources={result['removed_sources']} "
        f"deleted_events={result['deleted_events']} "
        f"updated_events={result['updated_events']}"
    )
    return 0


def competitor_prune_orphan_events_command(args: argparse.Namespace) -> int:
    from packages.competitor_monitor import create_competitor_monitor_repository

    repository = create_competitor_monitor_repository(None)
    deleted = repository.prune_orphan_events()
    print(f"[odaily] competitor orphan events pruned deleted_events={deleted}")
    return 0


def competitor_repair_newsflash_time_command(args: argparse.Namespace) -> int:
    from packages.competitor_monitor import create_competitor_monitor_repository

    repository = create_competitor_monitor_repository(None)
    result = repository.repair_newsflash_timestamps()
    print(
        "[odaily] competitor newsflash timestamps repaired "
        f"updated_items={result['updated_items']} "
        f"updated_events={result['updated_events']}"
    )
    return 0


def competitor_monitor_worker_command(args: argparse.Namespace) -> int:
    from packages.common.source_exclusions import SourceExclusionMatcher, create_source_exclusion_repository
    from packages.local_pipeline import LocalPipelineClient
    from packages.competitor_monitor import (
        CompetitorMonitorWorker,
        create_competitor_monitor_repository,
    )

    repository = create_competitor_monitor_repository(None)
    worker = CompetitorMonitorWorker(
        repository=repository,
        settings=load_competitor_monitor_settings(),
        pipeline_client=LocalPipelineClient(),
        exclusion_matcher=SourceExclusionMatcher(create_source_exclusion_repository(None)),
    )
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] competitor monitor once "
            f"fetched={result.fetched} tasks={result.task_inserted} references={result.reference_inserted} "
            f"events={result.events_updated} "
            f"expired_for_tasks={result.expired_for_tasks} "
            f"event_elapsed_seconds={result.event_elapsed_seconds:.1f} "
            f"fetched_by_source={result.fetched_by_source} "
            f"expired_for_tasks_by_source={result.expired_for_tasks_by_source} "
            f"failed={result.failed_sources}"
        )
        return 0 if not result.failed_sources else 1
    worker.run_forever()
    return 0


def whale_watch_init_db_command(args: argparse.Namespace) -> int:
    from packages.whale_watch import create_whale_watch_hyperliquid_repository, create_whale_watch_repository

    create_whale_watch_repository(None).init_schema()
    create_whale_watch_hyperliquid_repository(None).init_schema()
    print("[odaily] whale watch database schema initialized")
    return 0


def whale_watch_worker_command(args: argparse.Namespace) -> int:
    from packages.whale_watch import WhaleWatchWorker, create_whale_watch_repository

    repository = create_whale_watch_repository(None)
    worker = WhaleWatchWorker(repository=repository, settings=load_whale_watch_settings())
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] whale watch once "
            f"addresses={result.addresses} chains={result.chains} pairs={result.processed_pairs} "
            f"seeded={result.seeded_pairs} detected={result.detected} inserted={result.inserted} "
            f"sent={result.sent} failed={len(result.failed)}"
        )
        return 0 if not result.failed else 1
    worker.run_forever()
    return 0


def whale_watch_list_addresses_command(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from packages.whale_watch import create_whale_watch_repository

    repository = create_whale_watch_repository(None)
    if args.created_since_hours is not None:
        since = datetime.now(UTC) - timedelta(hours=args.created_since_hours)
        addresses = repository.list_addresses_created_since(since=since)
    else:
        addresses = repository.list_addresses(include_disabled=args.include_disabled)
    for address in addresses:
        print(
            f"id={address.id}\taddress={address.address}\tlabel={address.label}\tenabled={address.enabled}"
            f"\tcreated_by={address.created_by or '-'}\tupdated_by={address.updated_by or '-'}"
            f"\tcreated_at={address.created_at}\tupdated_at={address.updated_at}"
        )
    print(f"[odaily] whale watch address count={len(addresses)}")
    return 0


def whale_watch_delete_addresses_command(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from packages.whale_watch import create_whale_watch_repository

    repository = create_whale_watch_repository(None)
    selected_ids: list[int] = []
    if args.ids:
        selected_ids.extend(args.ids)
    if args.created_since_hours is not None:
        since = datetime.now(UTC) - timedelta(hours=args.created_since_hours)
        selected_ids.extend(address.id for address in repository.list_addresses_created_since(since=since))

    unique_ids = sorted(set(selected_ids))
    if not unique_ids:
        print("[odaily] whale watch delete skipped: no matched address ids")
        return 1

    if not args.execute:
        print(
            "[odaily] whale watch delete dry-run "
            f"matched_ids={','.join(str(item) for item in unique_ids)} count={len(unique_ids)}"
        )
        return 0

    deleted = repository.delete_addresses(ids=unique_ids)
    print(f"[odaily] whale watch delete completed deleted={deleted} requested={len(unique_ids)}")
    return 0 if deleted == len(unique_ids) else 1


def whale_watch_hyperliquid_worker_command(args: argparse.Namespace) -> int:
    from packages.whale_watch import WhaleWatchHyperliquidWorker, create_whale_watch_hyperliquid_repository

    repository = create_whale_watch_hyperliquid_repository(None)
    worker = WhaleWatchHyperliquidWorker(repository=repository, settings=load_whale_watch_hyperliquid_settings())
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] whale watch hyperliquid once "
            f"addresses={result.addresses} processed={result.processed} seeded={result.seeded} "
            f"detected={result.detected} inserted={result.inserted} sent={result.sent} "
            f"suppressed={result.suppressed} failed={len(result.failed)}"
        )
        return 0 if not result.failed else 1
    worker.run_forever()
    return 0


def pipeline_supervisor_command(args: argparse.Namespace) -> int:
    from packages.pipeline_supervisor import PipelineSupervisorWorker, create_pipeline_supervisor_repository

    repository = create_pipeline_supervisor_repository(None)
    worker = PipelineSupervisorWorker(repository=repository, settings=load_pipeline_supervisor_settings())
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] pipeline supervisor once "
            f"checked={result.checked} sent={result.sent} suppressed={result.suppressed}"
        )
        return 0
    worker.run_forever()
    return 0


def telegram_test_command(args: argparse.Namespace) -> int:
    from packages.x_processing.telegram import TelegramClient

    settings = load_x_processing_settings()
    client = TelegramClient(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        message_thread_id=settings.telegram_message_thread_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )
    result = client.send_message(args.text, message_thread_id=args.message_thread_id)
    if result.ok:
        print(
            "[odaily] telegram test sent "
            f"status_code={result.status_code} message_thread_id={args.message_thread_id or settings.telegram_message_thread_id}"
        )
        return 0
    if result.skipped:
        print(f"[odaily] telegram test skipped: {result.error}", file=sys.stderr)
        return 1
    print(f"[odaily] telegram test failed: {result.error}", file=sys.stderr)
    if result.response_text:
        print(result.response_text[:1000], file=sys.stderr)
    return 1


def telegram_create_topic_command(args: argparse.Namespace) -> int:
    from packages.x_processing.telegram import TelegramClient

    settings = load_x_processing_settings()
    client = TelegramClient(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        message_thread_id=settings.telegram_message_thread_id,
        timeout_seconds=settings.telegram_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )
    result = client.create_forum_topic(args.name)
    if result.ok:
        message_thread_id = None
        if isinstance(result.response_json, dict):
            topic = result.response_json.get("result")
            if isinstance(topic, dict):
                message_thread_id = topic.get("message_thread_id")
        print(f"[odaily] telegram topic created name={args.name} message_thread_id={message_thread_id}")
        return 0 if message_thread_id is not None else 1
    if result.skipped:
        print(f"[odaily] telegram topic create skipped: {result.error}", file=sys.stderr)
        return 1
    print(f"[odaily] telegram topic create failed: {result.error}", file=sys.stderr)
    if result.response_text:
        print(result.response_text[:1000], file=sys.stderr)
    return 1


def writer3_init_db_command(args: argparse.Namespace) -> int:
    from packages.writer3 import create_writer3_repository

    repository = create_writer3_repository(None)
    repository.init_schema()
    print("[odaily] writer3 database schema initialized")
    return 0


def writer3_backfill_odaily_command(args: argparse.Namespace) -> int:
    from packages.writer3 import backfill_odaily_references, create_writer3_repository

    settings = load_writer3_settings()
    repository = create_writer3_repository(None)
    repository.init_schema()
    result = backfill_odaily_references(
        repository=repository,
        days=args.days,
        timeout_seconds=settings.request_timeout_seconds,
    )
    print(
        "[odaily] writer3 odaily backfill "
        f"days={args.days} pages={result['pages']} fetched={result['fetched']} upserted={result['upserted']}"
    )
    return 0


def writer3_sync_index_command(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime, timedelta

    from packages.writer3 import Writer3Index, create_writer3_repository

    paths = get_paths()
    ensure_runtime_dirs(paths)
    repository = create_writer3_repository(None)
    index = Writer3Index(paths.writer3_index_path)
    since = datetime.now(UTC) - timedelta(days=args.days)
    references = repository.list_odaily_references(since=since)
    upserted = index.upsert_references(references)
    pruned = index.prune_before(since)
    print(f"[odaily] writer3 index synced days={args.days} upserted={upserted} pruned={pruned}")
    return 0


def writer3_worker_command(args: argparse.Namespace) -> int:
    from packages.writer3 import Writer3Index, Writer3Worker, create_writer3_repository

    paths = get_paths()
    ensure_runtime_dirs(paths)
    repository = create_writer3_repository(None)
    index = Writer3Index(paths.writer3_index_path)
    settings = load_writer3_settings()
    worker = Writer3Worker(repository=repository, index=index, settings=settings)
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] writer3 once "
            f"processed={result.processed} sent={result.sent} skipped={result.skipped} "
            f"failed={result.failed} message={result.message}"
        )
        return result.exit_code
    worker.run_forever()
    return 0


def writer3_confirm_worker_command(args: argparse.Namespace) -> int:
    from packages.writer3 import Writer3Index, Writer3TelegramConfirmWorker

    paths = get_paths()
    ensure_runtime_dirs(paths)
    settings = load_writer3_settings()
    index = Writer3Index(paths.writer3_index_path)
    worker = Writer3TelegramConfirmWorker(index=index, settings=settings, poll_timeout_seconds=args.poll_timeout)
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] writer3 confirm once "
            f"updates={result.updates} confirmed={result.confirmed} ignored={result.ignored} "
            f"failed={result.failed} message={result.message}"
        )
        return result.exit_code
    worker.run_forever()
    return 0


def writer3_reset_task_command(args: argparse.Namespace) -> int:
    from packages.writer3 import create_writer3_repository

    repository = create_writer3_repository(None)
    changed = repository.reset_task(args.task_id)
    print(f"[odaily] writer3 reset task_id={args.task_id} changed={changed}")
    return 0 if changed else 1


def auditor_init_db_command(args: argparse.Namespace) -> int:
    from packages.auditor import create_auditor_repository

    repository = create_auditor_repository(None)
    repository.init_schema()
    print("[odaily] auditor database schema initialized")
    return 0


def auditor_worker_command(args: argparse.Namespace) -> int:
    from packages.auditor import AuditorWorker, create_auditor_repository

    repository = create_auditor_repository(None)
    settings = load_auditor_settings()
    worker = AuditorWorker(repository=repository, settings=settings)
    if args.once:
        result = worker.run_once()
        print(
            "[odaily] auditor once "
            f"processed={result.processed} passed={result.passed} flagged={result.flagged} "
            f"failed={result.failed} message={result.message}"
        )
        return result.exit_code
    worker.run_forever()
    return 0


def maintenance_cleanup_command(args: argparse.Namespace) -> int:
    from packages.maintenance import create_maintenance_repository

    if args.retention_days < 1 or args.feedback_retention_days < 1 or args.completed_field_retention_days < 1:
        raise ValueError("retention day values must be >= 1")
    repository = create_maintenance_repository(None)
    result = repository.cleanup(
        dry_run=not args.execute,
        retention_days=args.retention_days,
        feedback_retention_days=args.feedback_retention_days,
        completed_field_retention_days=args.completed_field_retention_days,
    )
    mode = "execute" if args.execute else "dry-run"
    print(
        "[odaily] maintenance cleanup "
        f"mode={mode} retention_days={result.retention_days} "
        f"feedback_retention_days={result.feedback_retention_days} "
        f"completed_field_retention_days={result.completed_field_retention_days}"
    )
    for name, count in sorted(result.deleted.items()):
        print(f"delete\t{name}\t{count}")
    for name, count in sorted(result.cleared.items()):
        print(f"clear\t{name}\t{count}")
    return 0


def search_cache_maintenance_command(args: argparse.Namespace) -> int:
    from packages.x_processing.searcher import SearchCache

    if args.compact and not args.execute:
        raise ValueError("--compact requires --execute")
    paths = get_paths()
    cache_path = Path(args.path).expanduser().resolve() if args.path else paths.searcher_cache_path
    if cache_path is None:
        raise ValueError("searcher cache path is not configured")
    before_bytes = cache_path.stat().st_size if cache_path.exists() else 0
    result = SearchCache(cache_path).maintain(
        dry_run=not args.execute,
        short_retention_days=args.short_retention_days,
        reference_retention_days=args.reference_retention_days,
        convert_legacy=True,
        compact=args.compact,
    )
    after_bytes = cache_path.stat().st_size if cache_path.exists() else 0
    print(
        "[odaily] search cache maintenance "
        f"mode={'execute' if args.execute else 'dry-run'} path={cache_path} "
        f"short_retention_days={args.short_retention_days} "
        f"reference_retention_days={args.reference_retention_days} "
        f"deleted_documents={result.deleted_documents} "
        f"deleted_embeddings={result.deleted_embeddings} "
        f"converted_embeddings={result.converted_embeddings} "
        f"compacted={result.compacted} before_bytes={before_bytes} after_bytes={after_bytes}"
    )
    return 0


def storage_backup_command(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    from packages.common.sqlite_backup import create_sqlite_backup
    from packages.common.storage import load_storage_settings

    settings = load_storage_settings()
    if settings.backend != "sqlite":
        raise ValueError("storage-backup requires ODAILY_STORAGE_BACKEND=sqlite")
    manifest = create_sqlite_backup(settings.sqlite_path, args.destination.expanduser().resolve())
    print(json.dumps(asdict(manifest), ensure_ascii=False, indent=2))
    return 0


def storage_import_legacy_command(args: argparse.Namespace) -> int:
    from packages.common.legacy_database_import import import_legacy_database

    if args.truncate and not args.execute:
        raise ValueError("--truncate requires --execute")
    results = import_legacy_database(
        execute=args.execute,
        truncate=args.truncate,
        batch_size=args.batch_size,
        sample_rows_per_table=args.sample_rows_per_table,
    )
    for item in results:
        print(
            f"{item.table}\tsource={item.source_rows}\timported={item.imported_rows}"
            f"\tdestination={item.destination_rows}\tcolumns={len(item.shared_columns)}"
        )
    print(f"[odaily] legacy database import mode={'execute' if args.execute else 'preflight'} tables={len(results)}")
    return 0


def gate_market_command(args: argparse.Namespace) -> int:
    from packages.gate_market_broadcast import (
        GateMarketBroadcastService,
        GateMarketStore,
        load_gate_market_settings,
    )

    settings = load_gate_market_settings()
    store = GateMarketStore(settings.database_path)
    store.initialize()
    action = args.gate_market_action
    if action == "init-db":
        print(f"[gate-market] initialized db={settings.database_path}")
        return 0
    if action == "status":
        print(json.dumps(store.dashboard(), ensure_ascii=False, indent=2))
        return 0
    if action == "set-mode":
        mode = store.set_mode(args.mode)
        print(f"[gate-market] mode={mode}")
        return 0
    if action == "set-threshold":
        config = store.set_threshold(args.symbol, args.threshold)
        print(
            f"[gate-market] symbol={config.symbol} threshold={config.threshold_text} "
            "state=reset"
        )
        return 0

    service = GateMarketBroadcastService(settings=settings, store=store)
    if action == "run":
        if args.once:
            print(json.dumps(service.run_once(), ensure_ascii=False, indent=2))
            return 0
        service.run_forever()
        return 0
    if action == "backtest":
        if args.days < 1 or args.days > 365:
            raise ValueError("--days must be between 1 and 365")
        print(json.dumps(service.backtest(days=args.days), ensure_ascii=False, indent=2))
        return 0
    raise ValueError(f"Unknown gate-market action: {action}")


def main() -> int:
    args = parse_args()
    try:
        if args.command == "run-once":
            return run_once_command(args)
        if args.command == "run-worker":
            return run_worker_command(args)
        if args.command == "x-init-db":
            return x_init_db_command(args)
        if args.command == "console-grant-admin":
            return console_grant_admin_command(args)
        if args.command == "console-revoke-admin":
            return console_revoke_admin_command(args)
        if args.command == "console-list-admins":
            return console_list_admins_command(args)
        if args.command == "editor-plugin-init-db":
            return editor_plugin_init_command(args)
        if args.command == "editor-plugin-grant-user":
            return editor_plugin_grant_user_command(args)
        if args.command == "editor-plugin-revoke-user":
            return editor_plugin_revoke_user_command(args)
        if args.command == "editor-plugin-list-users":
            return editor_plugin_list_users_command(args)
        if args.command == "editor-plugin-api-server":
            return editor_plugin_api_server_command(args)
        if args.command == "editor-plugin-local-feed-status":
            return editor_plugin_local_feed_status_command(args)
        if args.command == "local-pipeline-server":
            return local_pipeline_server_command(args)
        if args.command == "local-pipeline-skip-legacy":
            return local_pipeline_skip_legacy_command(args)
        if args.command == "x-capture-worker":
            return x_capture_worker_command(args)
        if args.command == "non-mainstream-media-init-db":
            return non_mainstream_media_init_db_command(args)
        if args.command == "non-mainstream-media-worker":
            return non_mainstream_media_worker_command(args)
        if args.command == "telegram-discovery-worker":
            return telegram_discovery_worker_command(args)
        if args.command == "external-media-alert-init-db":
            return external_media_alert_init_db_command(args)
        if args.command == "external-media-alert-worker":
            return external_media_alert_worker_command(args)
        if args.command == "external-media-alert-fetcher":
            return external_media_alert_fetcher_command(args)
        if args.command == "jin10-init-db":
            return jin10_init_db_command(args)
        if args.command == "jin10-monitor-worker":
            return jin10_monitor_worker_command(args)
        if args.command == "x-process-init-db":
            return x_process_init_db_command(args)
        if args.command == "publisher-init-config":
            return publisher_init_config_command(args)
        if args.command == "x-process-worker":
            return x_process_worker_command(args)
        if args.command == "competitor-init-db":
            return competitor_init_db_command(args)
        if args.command == "competitor-prune-excluded-events":
            return competitor_prune_excluded_events_command(args)
        if args.command == "competitor-prune-orphan-events":
            return competitor_prune_orphan_events_command(args)
        if args.command == "competitor-repair-newsflash-time":
            return competitor_repair_newsflash_time_command(args)
        if args.command == "competitor-monitor-worker":
            return competitor_monitor_worker_command(args)
        if args.command == "whale-watch-init-db":
            return whale_watch_init_db_command(args)
        if args.command == "whale-watch-worker":
            return whale_watch_worker_command(args)
        if args.command == "whale-watch-list-addresses":
            return whale_watch_list_addresses_command(args)
        if args.command == "whale-watch-delete-addresses":
            return whale_watch_delete_addresses_command(args)
        if args.command == "whale-watch-hyperliquid-worker":
            return whale_watch_hyperliquid_worker_command(args)
        if args.command == "pipeline-supervisor":
            return pipeline_supervisor_command(args)
        if args.command == "telegram-test":
            return telegram_test_command(args)
        if args.command == "telegram-create-topic":
            return telegram_create_topic_command(args)
        if args.command == "writer3-init-db":
            return writer3_init_db_command(args)
        if args.command == "writer3-backfill-odaily":
            return writer3_backfill_odaily_command(args)
        if args.command == "writer3-sync-index":
            return writer3_sync_index_command(args)
        if args.command == "writer3-worker":
            return writer3_worker_command(args)
        if args.command == "writer3-confirm-worker":
            return writer3_confirm_worker_command(args)
        if args.command == "writer3-reset-task":
            return writer3_reset_task_command(args)
        if args.command == "auditor-init-db":
            return auditor_init_db_command(args)
        if args.command == "auditor-worker":
            return auditor_worker_command(args)
        if args.command == "maintenance-cleanup":
            return maintenance_cleanup_command(args)
        if args.command == "search-cache-maintenance":
            return search_cache_maintenance_command(args)
        if args.command == "storage-backup":
            return storage_backup_command(args)
        if args.command == "storage-import-legacy":
            return storage_import_legacy_command(args)
        if args.command == "gate-market":
            return gate_market_command(args)
        if args.command == "doctor":
            return doctor_command(args)
    except Exception as exc:
        print(f"[odaily] error: {exc}", file=sys.stderr)
        return 1
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
