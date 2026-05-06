from __future__ import annotations

import argparse
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from packages.briefing.service import run_brief_once  # noqa: E402
from packages.common.config import BriefKind, load_settings  # noqa: E402
from packages.common.paths import ensure_runtime_dirs, get_paths  # noqa: E402
from packages.common.time_utils import EASTERN_TZ, SHANGHAI_TZ  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OdAIly US market brief worker.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(command_parser: argparse.ArgumentParser) -> None:
        command_parser.add_argument("--config", help="Path to data/config/market_brief.json.")

    run_once = subparsers.add_parser("run-once", help="Generate one market brief.")
    add_common(run_once)
    run_once.add_argument("--kind", choices=["close", "premarket", "open"], required=True)
    run_once.add_argument("--dry-run", action="store_true", help="Do not call the Push Data API.")
    run_once.add_argument("--send", action="store_true", help="Call the Push Data API even if config uses dry_run.")
    run_once.add_argument("--force", action="store_true", help="Run even on weekends.")

    worker = subparsers.add_parser("run-worker", help="Run the scheduled worker.")
    add_common(worker)

    doctor = subparsers.add_parser("doctor", help="Print configuration and schedule diagnostics.")
    add_common(doctor)
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
    settings = load_settings(args.config)
    result = run_brief_once(
        kind=args.kind,
        settings=settings,
        paths=paths,
        dry_run_override=_dry_run_override(args),
        force=args.force,
    )
    print(
        f"[odaily] kind={result.kind} status={result.status} "
        f"run_id={result.run_id} message={result.message}"
    )
    return result.exit_code


def run_worker_command(args: argparse.Namespace) -> int:
    paths = get_paths()
    settings = load_settings(args.config)
    ensure_runtime_dirs(paths)
    scheduler = BlockingScheduler(timezone=SHANGHAI_TZ)

    def add_job(kind: BriefKind, trigger: CronTrigger, job_id: str) -> None:
        scheduler.add_job(
            lambda brief_kind=kind: run_brief_once(
                kind=brief_kind,
                settings=settings,
                paths=paths,
            ),
            trigger,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    add_job("close", CronTrigger(hour=8, minute=0, timezone=SHANGHAI_TZ), "brief-close-0800-cst")
    add_job("premarket", CronTrigger(hour=4, minute=5, timezone=EASTERN_TZ), "brief-premarket-0405-et")
    add_job("open", CronTrigger(hour=9, minute=31, timezone=EASTERN_TZ), "brief-open-0931-et")
    print(
        "[odaily] worker started. close=08:00 Asia/Shanghai; "
        "premarket=04:05 America/New_York; open=09:31 America/New_York"
    )
    scheduler.start()
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    paths = get_paths()
    settings = load_settings(args.config)
    ensure_runtime_dirs(paths)
    print(f"[odaily] root={paths.root_dir}")
    print(f"[odaily] config={Path(args.config).resolve() if args.config else paths.market_brief_config_path}")
    print(f"[odaily] watchlist={','.join(settings.watchlist)}")
    print(f"[odaily] push_endpoint={settings.push_endpoint}")
    print(f"[odaily] dry_run={settings.dry_run}")
    print("[odaily] schedules close=08:00 Asia/Shanghai premarket=04:05 America/New_York open=09:31 America/New_York")
    return 0


def main() -> int:
    args = parse_args()
    try:
        if args.command == "run-once":
            return run_once_command(args)
        if args.command == "run-worker":
            return run_worker_command(args)
        if args.command == "doctor":
            return doctor_command(args)
    except Exception as exc:
        print(f"[odaily] error: {exc}", file=sys.stderr)
        return 1
    raise ValueError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
