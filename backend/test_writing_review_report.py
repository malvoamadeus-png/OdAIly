from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from packages.writing_review.report import (
    WritingReviewItem,
    default_window,
    generate_writing_review_report,
    load_ai_written_items,
    render_markdown_report,
)


class FakeReviewClient:
    def generate_text(self, *, model, prompt, text_format=None, reasoning_effort=None):
        del model, prompt, text_format, reasoning_effort
        return json.dumps(
            {
                "verdict": "minor_edit",
                "summary": "标题可以去掉重复主体。",
                "title_suggestion": "公司增持BTC并减持ETH",
                "content_suggestion": "公司增持BTC并减持ETH。",
                "issues": [
                    {
                        "location": "title",
                        "type": "redundancy",
                        "severity": "low",
                        "original": "公司公司",
                        "suggested": "公司",
                        "reason": "主体重复。",
                    }
                ],
                "patterns": ["主体重复"],
            },
            ensure_ascii=False,
        )


def test_load_ai_written_items_prefers_final_output_and_filters_window(tmp_path):
    db_path = tmp_path / "odaily.sqlite"
    now = datetime.now(UTC).replace(microsecond=0)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT,
                title TEXT, content TEXT, created_at TEXT
            );
            CREATE TABLE x_task_pipeline (
                task_id INTEGER PRIMARY KEY, write_completed_at TEXT, writer_model TEXT,
                draft_title TEXT, draft_content TEXT, final_title TEXT, final_content TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks VALUES (1,'x','item-1',NULL,'原始标题','原始正文',?)",
            (now.isoformat(),),
        )
        conn.execute(
            "INSERT INTO x_task_pipeline VALUES (1,?,?, ?,?,?,?)",
            (now.isoformat(), "writer-model", "草稿标题", "草稿正文", "最终标题", "最终正文"),
        )
        conn.execute("INSERT INTO tasks VALUES (2,'x','item-2',NULL,'旧','旧',?)", ((now - timedelta(hours=20)).isoformat(),))
        conn.execute(
            "INSERT INTO x_task_pipeline VALUES (2,?,?, ?,?,?,?)",
            ((now - timedelta(hours=20)).isoformat(), "writer-model", "旧稿", "旧正文", None, None),
        )
        conn.commit()

    items = load_ai_written_items(db_path, since=now - timedelta(hours=12), until=now)
    assert len(items) == 1
    assert items[0].title == "最终标题"
    assert items[0].content == "最终正文"
    assert items[0].is_final is True


def test_generate_report_normalizes_issue_and_renders_markdown():
    now = datetime.now(UTC)
    item = WritingReviewItem(
        task_id=1,
        source="x",
        source_item_id="item-1",
        source_url=None,
        created_at=None,
        write_completed_at=now.isoformat(),
        writer_model="writer-model",
        original_title="原始标题",
        original_content="原始正文",
        title="公司公司增持BTC",
        content="公司增持BTC。",
        is_final=True,
    )
    report = generate_writing_review_report(
        [item],
        client=FakeReviewClient(),
        model="review-model",
        since=now - timedelta(hours=12),
        until=now,
        lookback_hours=12,
        reasoning_effort="low",
    )
    assert report.counts == {"minor_edit": 1}
    assert report.issue_counts["redundancy"] == 1
    markdown = render_markdown_report(report)
    assert "公司公司" in markdown
    assert "建议标题" in markdown


def test_default_window_is_twelve_hours():
    since, until = default_window()
    assert until - since == timedelta(hours=12)
    assert since.tzinfo is UTC
