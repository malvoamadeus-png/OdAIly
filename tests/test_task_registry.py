from __future__ import annotations

import pytest

from packages.tasks.registry import TASKS, run_task_once


def test_tasks_register_expected_kinds_and_schedules() -> None:
    assert TASKS["us-market"].kinds == ("close", "premarket", "open")
    assert TASKS["gate-tradfi"].kinds == ("morning", "open")
    assert len(TASKS["us-market"].schedules) == 3
    assert len(TASKS["gate-tradfi"].schedules) == 2


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
