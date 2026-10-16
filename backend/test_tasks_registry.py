from __future__ import annotations

from datetime import datetime

from packages.common.time_utils import SHANGHAI_TZ
from packages.tasks.registry import TASKS


def test_okx_stock_brief_is_scheduled_for_friday_noon_in_shanghai() -> None:
    task = TASKS["okx-stock-brief"]
    schedule = task.schedules[0]
    next_run = schedule.trigger.get_next_fire_time(
        None,
        datetime(2026, 10, 15, 12, 1, tzinfo=SHANGHAI_TZ),
    )

    assert schedule.kind == "weekly"
    assert schedule.job_id == "okx-stock-brief-friday-1200-cst"
    assert next_run == datetime(2026, 10, 16, 12, 0, tzinfo=SHANGHAI_TZ)
