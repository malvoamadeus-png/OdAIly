from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from packages.common.config import load_gate_settings, load_settings  # noqa: E402
from packages.common.config import load_x_processing_settings  # noqa: E402
from packages.common.paths import ensure_runtime_dirs, get_paths  # noqa: E402
from packages.common.time_utils import SHANGHAI_TZ  # noqa: E402
from packages.tasks.registry import TASKS, run_task_once  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OdAIly content publishing worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--config", help="Path to task config JSON.")

    run_once = subparsers.add_parser("run-once", help="Generate one content brief.")
    add_common(run_once)
    run_once.add_argument("--task", choices=sorted(TASKS), default="us-market")
    run_once.add_argument("--kind", required=True)
    run_once.add_argument("--dry-run", action="store_true", help="Do not call the Push Data API.")
    run_once.add_argument("--send", action="store_true", help="Call the Push Data API even if config uses dry_run.")
    run_once.add_argument("--force", action="store_true", help="Run even on weekends.")

    worker = subparsers.add_parser("run-worker", help="Run the scheduled worker.")
    add_common(worker)

    x_init_db = subparsers.add_parser("x-init-db", help="Initialize X capture Postgres tables.")
    x_init_db.add_argument("--database-url", help="Override SUPABASE_DB_URL/DATABASE_URL.")

    x_worker = subparsers.add_parser("x-capture-worker", help="Run the X capture worker.")
    x_worker.add_argument("--database-url", help="Override SUPABASE_DB_URL/DATABASE_URL.")
    x_worker.add_argument("--once", action="store_true", help="Run one capture pass and exit.")

    x_process_init = subparsers.add_parser("x-process-init-db", help="Initialize X processing Postgres tables.")
    x_process_init.add_argument("--database-url", help="Override SUPABASE_DB_URL/DATABASE_URL.")
    x_process_init.add_argument(
        "--skip-clear-pending",
        action="store_true",
        help="Do not delete existing X pending tasks during initialization.",
    )

    x_process_worker = subparsers.add_parser("x-process-worker", help="Run an X processing stage worker.")
    x_process_worker.add_argument("--database-url", help="Override SUPABASE_DB_URL/DATABASE_URL.")
    x_process_worker.add_argument(
        "--stage",
        choices=["judge", "search", "write", "format_publish"],
        required=True,
    )
    x_process_worker.add_argument("--once", action="store_true", help="Process one available task and exit.")

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
    from packages.x_capture.repository import PostgresXCaptureRepository

    repository = PostgresXCaptureRepository(args.database_url)
    repository.init_schema()
    print("[odaily] x-capture database schema initialized")
    return 0


def x_capture_worker_command(args: argparse.Namespace) -> int:
    from packages.x_capture import FXTwitterClient, XCaptureWorker
    from packages.x_capture.repository import PostgresXCaptureRepository

    repository = PostgresXCaptureRepository(args.database_url)
    worker = XCaptureWorker(
        repository=repository,
        client=FXTwitterClient(),
    )
    if args.once:
        stats = worker.run_once()
        print(f"[odaily] x-capture once completed. accounts={len(stats)}")
        return 0 if all(item.status == "success" for item in stats) else 1
    worker.run_forever()
    return 0


def x_process_init_db_command(args: argparse.Namespace) -> int:
    from packages.x_processing.repository import PostgresXProcessingRepository

    paths = get_paths()
    repository = PostgresXProcessingRepository(args.database_url)
    repository.init_schema()
    repository.seed_prompt_templates(root_dir=paths.root_dir)
    deleted = 0 if args.skip_clear_pending else repository.clear_old_pending_x_tasks()
    print(f"[odaily] x-processing database schema initialized. deleted_pending={deleted}")
    return 0


def x_process_worker_command(args: argparse.Namespace) -> int:
    from packages.x_processing import PostgresXProcessingRepository, XProcessingWorker

    repository = PostgresXProcessingRepository(args.database_url)
    worker = XProcessingWorker(
        stage=args.stage,
        repository=repository,
        settings=load_x_processing_settings(),
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


def main() -> int:
    args = parse_args()
    try:
        if args.command == "run-once":
            return run_once_command(args)
        if args.command == "run-worker":
            return run_worker_command(args)
        if args.command == "x-init-db":
            return x_init_db_command(args)
        if args.command == "x-capture-worker":
            return x_capture_worker_command(args)
        if args.command == "x-process-init-db":
            return x_process_init_db_command(args)
        if args.command == "x-process-worker":
            return x_process_worker_command(args)
        if args.command == "doctor":
            return doctor_command(args)
    except Exception as exc:
        print(f"[odaily] error: {exc}", file=sys.stderr)
        return 1
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
