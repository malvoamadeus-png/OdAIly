from __future__ import annotations

import pytest

from packages.tasks.registry import TASKS, run_task_once


def test_tasks_register_expected_kinds_and_schedules() -> None:
    assert TASKS["us-market"].kinds == ("close", "premarket", "open")
    assert TASKS["gate-tradfi"].kinds == ("morning", "open")
    assert len(TASKS["us-market"].schedules) == 2
    assert len(TASKS["gate-tradfi"].schedules) == 2
    assert [schedule.kind for schedule in TASKS["us-market"].schedules] == ["close", "open"]
    assert "premarket" not in {schedule.kind for schedule in TASKS["us-market"].schedules}


def test_morning_schedules_run_at_0900_shanghai() -> None:
    us_close = next(schedule for schedule in TASKS["us-market"].schedules if schedule.kind == "close")
    gate_morning = next(schedule for schedule in TASKS["gate-tradfi"].schedules if schedule.kind == "morning")

    assert us_close.description == "close=09:00 Asia/Shanghai"
    assert gate_morning.description == "morning=09:00 Asia/Shanghai"
    assert us_close.job_id == "us-market-close-0900-cst"
    assert gate_morning.job_id == "gate-tradfi-morning-0900-cst"
    assert str(us_close.trigger.timezone) == "Asia/Shanghai"
    assert str(gate_morning.trigger.timezone) == "Asia/Shanghai"



def test_run_task_once_rejects_invalid_kind(tmp_path) -> None:
    with pytest.raises(ValueError, match="Invalid kind"):
        run_task_once(
            task_id="gate-tradfi",
            kind="close",
            config_path=None,
            paths=tmp_path,  # type: ignore[arg-type]
            dry_run_override=True,
            force=True,
        )
