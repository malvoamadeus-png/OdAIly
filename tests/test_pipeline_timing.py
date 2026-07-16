from __future__ import annotations

from datetime import UTC, datetime, timedelta

from packages.pipeline_timing import PipelineTimingRow, build_pipeline_timing_dashboard, pipeline_stage_durations


def ts(seconds: int) -> datetime:
    return datetime(2026, 7, 16, 8, 0, tzinfo=UTC) + timedelta(seconds=seconds)


def row(
    *,
    source: str,
    status: str = "auto_published",
    metadata: dict | None = None,
    created_at: datetime | None = None,
    judge: datetime | None = None,
    search: datetime | None = None,
    write: datetime | None = None,
    format_at: datetime | None = None,
    publisher: datetime | None = None,
    publish: datetime | None = None,
) -> PipelineTimingRow:
    return PipelineTimingRow(
        task_id=1,
        source=source,
        status=status,
        created_at=created_at or ts(0),
        metadata=metadata or {},
        news_type="regular",
        publisher_decision="auto_publish",
        judge_completed_at=judge,
        search_completed_at=search,
        write_completed_at=write,
        format_completed_at=format_at,
        publisher_decided_at=publisher,
        publish_completed_at=publish,
    )


def test_stage_durations_follow_judge_first_order_for_x_and_jin10() -> None:
    item = row(source="x", judge=ts(4), search=ts(10), write=ts(18), format_at=ts(21), publisher=ts(23), publish=ts(30))

    assert pipeline_stage_durations(item) == {
        "judge": 4.0,
        "search": 6.0,
        "write": 8.0,
        "format": 3.0,
        "publisher_decision": 2.0,
        "publish_finalize": 7.0,
        "total": 30.0,
    }


def test_stage_durations_follow_search_first_order_for_competitor_and_sources() -> None:
    item = row(
        source="panews",
        search=ts(5),
        judge=ts(8),
        write=ts(15),
        format_at=ts(18),
        publisher=ts(19),
        publish=ts(24),
    )

    assert pipeline_stage_durations(item)["search"] == 5.0
    assert pipeline_stage_durations(item)["judge"] == 3.0
    assert pipeline_stage_durations(item)["write"] == 7.0
    assert pipeline_stage_durations(item)["total"] == 24.0


def test_negative_and_missing_timestamps_are_excluded_from_metrics() -> None:
    generated_at = ts(120)
    rows = [
        row(source="x", judge=ts(10), search=ts(9), write=ts(20), format_at=ts(22), publisher=ts(23), publish=ts(30)),
        row(source="x", judge=ts(6), search=None, write=None, format_at=None, publisher=None, publish=None),
        row(source="jin10", judge=ts(8), search=ts(12), write=ts(16), format_at=ts(20), publisher=ts(22), publish=ts(40)),
    ]

    payload = build_pipeline_timing_dashboard(rows, generated_at=generated_at, windows=(24,))
    window = payload["windows"][0]
    stages = {stage["stage_key"]: stage for stage in window["by_stage"]}

    assert window["overall"]["sample_count"] == 3
    assert window["overall"]["completed_count"] == 2
    assert window["overall"]["mean_seconds"] == 35.0
    assert window["overall"]["median_seconds"] == 35.0
    assert stages["search"]["count"] == 1
    assert stages["search"]["mean_seconds"] == 4.0
    assert stages["write"]["count"] == 2
