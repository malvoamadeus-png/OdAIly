from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from apscheduler.triggers.cron import CronTrigger

from packages.briefing.service import BriefRunResult, run_brief_once
from packages.common.config import (
    BriefKind,
    GateBriefKind,
    load_gate_settings,
    load_settings,
)
from packages.common.paths import AppPaths
from packages.common.time_utils import EASTERN_TZ, SHANGHAI_TZ
from packages.gate.service import GateRunResult, run_gate_once


TaskId = Literal["us-market", "gate-tradfi"]


@dataclass(frozen=True, slots=True)
class ScheduledKind:
    kind: str
    trigger: CronTrigger
    job_id: str
    description: str


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    task_id: TaskId
    display_name: str
    kinds: tuple[str, ...]
    config_help: str
    schedules: tuple[ScheduledKind, ...]
    load_config: Callable[[str | None], object]
    run_once: Callable[..., BriefRunResult | GateRunResult]


def _run_us_market(
    *,
    kind: str,
    config_path: str | None,
    paths: AppPaths,
    dry_run_override: bool | None,
    force: bool,
) -> BriefRunResult:
    return run_brief_once(
        kind=kind,  # type: ignore[arg-type]
        settings=load_settings(config_path),
        paths=paths,
        dry_run_override=dry_run_override,
        force=force,
    )


def _run_gate_tradfi(
    *,
    kind: str,
    config_path: str | None,
    paths: AppPaths,
    dry_run_override: bool | None,
    force: bool,
) -> GateRunResult:
    return run_gate_once(
        kind=kind,  # type: ignore[arg-type]
        settings=load_gate_settings(config_path),
        paths=paths,
        dry_run_override=dry_run_override,
        force=force,
    )


TASKS: dict[str, TaskDefinition] = {
    "us-market": TaskDefinition(
        task_id="us-market",
        display_name="US market crypto stock brief",
        kinds=("close", "premarket", "open"),
        config_help="data/config/market_brief.json",
        load_config=load_settings,
        run_once=_run_us_market,
        schedules=(
            ScheduledKind(
                kind="close",
                trigger=CronTrigger(hour=9, minute=0, timezone=SHANGHAI_TZ),
                job_id="us-market-close-0900-cst",
                description="close=09:00 Asia/Shanghai",
            ),
            ScheduledKind(
                kind="open",
                trigger=CronTrigger(hour=9, minute=31, timezone=EASTERN_TZ),
                job_id="us-market-open-0931-et",
                description="open=09:31 America/New_York",
            ),
        ),
    ),
    "gate-tradfi": TaskDefinition(
        task_id="gate-tradfi",
        display_name="Gate TradFi market brief",
        kinds=("morning", "open"),
        config_help="data/config/gate_tradfi.json",
        load_config=load_gate_settings,
        run_once=_run_gate_tradfi,
        schedules=(
            ScheduledKind(
                kind="morning",
                trigger=CronTrigger(hour=9, minute=0, timezone=SHANGHAI_TZ),
                job_id="gate-tradfi-morning-0900-cst",
                description="morning=09:00 Asia/Shanghai",
            ),
            ScheduledKind(
                kind="open",
                trigger=CronTrigger(hour=9, minute=31, timezone=EASTERN_TZ),
                job_id="gate-tradfi-open-0931-et",
                description="open=09:31 America/New_York",
            ),
        ),
    ),
}


def get_task(task_id: str) -> TaskDefinition:
    try:
        return TASKS[task_id]
    except KeyError as exc:
        raise ValueError(f"Unknown task: {task_id}. Valid tasks: {', '.join(TASKS)}") from exc


def run_task_once(
    *,
    task_id: str,
    kind: str,
    config_path: str | None,
    paths: AppPaths,
    dry_run_override: bool | None,
    force: bool,
) -> BriefRunResult | GateRunResult:
    task = get_task(task_id)
    if kind not in task.kinds:
        raise ValueError(f"Invalid kind for {task_id}: {kind}. Valid kinds: {', '.join(task.kinds)}")
    return task.run_once(
        kind=kind,
        config_path=config_path,
        paths=paths,
        dry_run_override=dry_run_override,
        force=force,
    )
