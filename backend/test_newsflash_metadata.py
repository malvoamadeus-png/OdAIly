from __future__ import annotations

import json

from packages.common.storage import connect_sqlite
from packages.newsflash_metadata import (
    ensure_newsflash_metadata_schema,
    metadata_for_reference,
    record_newsflash_delivery,
    update_newsflash_priority,
)
from packages.newsflash_priority import parse_newsflash_priority
from packages.x_processing.publisher_config import parse_publisher_decision
from packages.x_processing.title_trace import parse_structured_writer_output


def _make_reference_db(path) -> None:
    with connect_sqlite(path) as conn:
        conn.execute(
            "CREATE TABLE odaily_reference_items (source_item_id text PRIMARY KEY,title text,published_at text)"
        )


def test_metadata_schema_is_repeatable_and_push_id_wins(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    ensure_newsflash_metadata_schema(path)
    ensure_newsflash_metadata_schema(path)

    record_newsflash_delivery(
        path,
        delivery_key="task-1",
        origin_key="x",
        title="重要政策进展",
        response_text='{"data":{"newsflashId":1234}}',
        pushed_at="2026-10-20T03:00:00+00:00",
        priority_status="evaluated",
        priority_level="high",
        priority_reason="政策取得实质进展。",
        duplicate_result={"is_duplicate": False, "reason": "no_match"},
    )
    with connect_sqlite(path) as conn:
        result = metadata_for_reference(
            conn,
            source_item_id="1234",
            title="不同步标题也按 ID",
            published_at="2026-10-20T03:00:00+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )

    assert result["origin_label"] == "X"
    assert result["priority_status"] == "evaluated"
    assert result["priority_level"] == "high"
    assert result["duplicate_status"] == "clear"


def test_title_and_time_link_requires_one_candidate(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-unique",
        origin_key="competitor",
        title="交易平台上线新产品",
        response_text="ok",
        pushed_at="2026-10-20T03:00:00+00:00",
        priority_status="evaluated",
        priority_level="medium",
        priority_reason="产品动态有一定行业价值。",
    )
    with connect_sqlite(path) as conn:
        conn.execute(
            "INSERT INTO odaily_reference_items(source_item_id,title,published_at) VALUES (?,?,?)",
            ("9001", "交易平台上线新产品", "2026-10-20T03:03:00+00:00"),
        )
        result = metadata_for_reference(
            conn,
            source_item_id="9001",
            title="交易平台上线新产品",
            published_at="2026-10-20T03:03:00+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )

    assert result["origin_label"] == "竞品"
    assert result["origin_status"] == "matched"
    assert result["priority_level"] == "medium"


def test_title_and_time_link_requires_one_reference(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-duplicate-reference",
        origin_key="x",
        title="同标题多条快讯",
        response_text="no id",
        pushed_at="2026-10-20T03:00:00+00:00",
    )
    with connect_sqlite(path) as conn:
        conn.executemany(
            "INSERT INTO odaily_reference_items(source_item_id,title,published_at) VALUES (?,?,?)",
            [
                ("9004", "同标题多条快讯", "2026-10-20T03:00:10+00:00"),
                ("9005", "同标题多条快讯", "2026-10-20T03:01:00+00:00"),
            ],
        )
        result = metadata_for_reference(
            conn,
            source_item_id="9004",
            title="同标题多条快讯",
            published_at="2026-10-20T03:00:10+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )
        linked = conn.execute(
            "SELECT count(*) FROM newsflash_production_metadata WHERE odaily_id IS NOT NULL"
        ).fetchone()[0]

    assert result["origin_label"] == "来源待匹配"
    assert linked == 0


def test_duplicate_snapshot_is_persisted_and_returned_by_id(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-duplicate",
        origin_key="x",
        title="重复快讯",
        response_text='{"id":"9010"}',
        pushed_at="2026-10-20T03:00:00+00:00",
        duplicate_result={
            "is_duplicate": True,
            "duplicate_target_type": "odaily_published",
            "duplicate_target_id": "9009",
            "duplicate_target": {"target_type": "odaily_published", "target_id": "9009", "title": "原快讯"},
            "reason": "same_event",
        },
    )

    with connect_sqlite(path) as conn:
        result = metadata_for_reference(
            conn,
            source_item_id="9010",
            title="重复快讯",
            published_at="2026-10-20T03:00:00+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )

    assert result["duplicate_status"] == "duplicate"
    assert result["duplicate_target_id"] == "9009"
    assert result["duplicate_target_title"] == "原快讯"
    assert result["duplicate_reason"] == "same_event"


def test_multiple_push_ids_do_not_fall_back_to_title_match(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-multiple-ids",
        origin_key="x",
        title="多 ID 响应",
        response_text='{"data":{"id":"9006","newsflash_id":"9007"}}',
        pushed_at="2026-10-20T03:00:00+00:00",
    )
    with connect_sqlite(path) as conn:
        conn.execute(
            "INSERT INTO odaily_reference_items(source_item_id,title,published_at) VALUES (?,?,?)",
            ("9008", "多 ID 响应", "2026-10-20T03:00:20+00:00"),
        )
        result = metadata_for_reference(
            conn,
            source_item_id="9008",
            title="多 ID 响应",
            published_at="2026-10-20T03:00:20+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )
        row = conn.execute(
            "SELECT id_match_status,odaily_id FROM newsflash_production_metadata WHERE delivery_key='task-multiple-ids'"
        ).fetchone()

    assert result["origin_label"] == "来源待匹配"
    assert tuple(row) == ("ambiguous", None)


def test_metadata_schema_migrates_existing_table(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    with connect_sqlite(path) as conn:
        conn.executescript(
            """
            CREATE TABLE newsflash_production_metadata (
                delivery_key text PRIMARY KEY,
                origin_key text NOT NULL,
                odaily_id text,
                title text NOT NULL,
                pushed_at text NOT NULL,
                priority_status text NOT NULL DEFAULT 'pending',
                priority_level text,
                priority_reason text,
                created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
    ensure_newsflash_metadata_schema(path)
    ensure_newsflash_metadata_schema(path)
    with connect_sqlite(path) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(newsflash_production_metadata)")}

    assert "id_match_status" in columns


def test_ambiguous_title_time_match_is_not_assigned(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    for key, origin in (("task-a", "x"), ("task-b", "competitor")):
        record_newsflash_delivery(
            path,
            delivery_key=key,
            origin_key=origin,
            title="完全相同的标题",
            response_text=None,
            pushed_at="2026-10-20T03:00:00+00:00",
            priority_status="evaluated",
            priority_level="high",
            priority_reason="重要事件。",
        )
    with connect_sqlite(path) as conn:
        conn.execute(
            "INSERT INTO odaily_reference_items(source_item_id,title,published_at) VALUES (?,?,?)",
            ("9002", "完全相同的标题", "2026-10-20T03:00:30+00:00"),
        )
        result = metadata_for_reference(
            conn,
            source_item_id="9002",
            title="完全相同的标题",
            published_at="2026-10-20T03:00:30+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )
        linked = conn.execute(
            "SELECT count(*) FROM newsflash_production_metadata WHERE odaily_id IS NOT NULL"
        ).fetchone()[0]

    assert result["origin_label"] == "来源待匹配"
    assert result["priority_status"] == "pending"
    assert linked == 0


def test_unmatched_title_time_shows_pending_instead_of_historical(tmp_path) -> None:
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-delayed-reference",
        origin_key="x",
        title="稍晚同步的快讯",
        response_text="no id",
        pushed_at="2026-10-20T03:00:00+00:00",
    )
    with connect_sqlite(path) as conn:
        result = metadata_for_reference(
            conn,
            source_item_id="9003",
            title="稍晚同步的快讯",
            published_at="2026-10-20T03:12:00+00:00",
            publisher_kind="other_ai",
            operation_created_at=None,
        )

    assert result["origin_label"] == "来源待匹配"
    assert result["origin_status"] == "pending"
    assert result["priority_status"] == "pending"


def test_priority_failure_does_not_invalidate_publish_decision() -> None:
    for payload in (
        {"decision": "pass", "reason": "命中产品上线规则"},
        {"decision": "pass", "reason": "命中产品上线规则", "priority": "urgent"},
    ):
        result = parse_publisher_decision(json.dumps(payload))
        assert result.decision == "pass"
        assert result.reason == "命中产品上线规则"
        assert result.priority_status == "failed"
        assert result.priority_level is None


def test_priority_parser_and_post_push_update_keep_distinct_statuses(tmp_path) -> None:
    assert parse_newsflash_priority({"priority": "low", "priority_reason": "常规局部动态。"}) == (
        "low",
        "常规局部动态。",
    )
    path = tmp_path / "main.sqlite"
    _make_reference_db(path)
    record_newsflash_delivery(
        path,
        delivery_key="task-failed-priority",
        origin_key="x",
        title="事件",
        response_text='{"id":321}',
    )
    assert update_newsflash_priority(path, delivery_key="task-failed-priority", status="failed")
    with connect_sqlite(path) as conn:
        row = conn.execute(
            "SELECT priority_status,priority_level FROM newsflash_production_metadata WHERE delivery_key=?",
            ("task-failed-priority",),
        ).fetchone()
    assert tuple(row) == ("failed", None)


def test_hot_writer_priority_parse_error_does_not_break_draft() -> None:
    output = {
        "title": "交易平台上线新功能",
        "content": "交易平台今日上线了新的加密资产功能。",
        "title_strategy": "action_first",
        "title_strategy_reason": "标题突出上线动作",
        "matched_title_rules": ["action_first"],
        "feature_mode_applied": False,
        "feature_mode_reason": "",
        "priority": "not-a-level",
    }
    result = parse_structured_writer_output(
        json.dumps(output),
        known_subjects=[],
        feature_mode_enabled=False,
        include_newsflash_priority=True,
    )

    assert result.draft.title == output["title"]
    assert result.priority_status == "failed"
    assert result.trace["newsflash_priority"]["status"] == "failed"
