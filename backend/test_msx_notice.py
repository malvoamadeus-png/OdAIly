from __future__ import annotations

from datetime import datetime
from pathlib import Path

from packages.msx_notice import MSXNotice
from packages.msx_notice_worker import MSXNoticeWorker
from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.worker import (
    build_writer_prompt,
    is_msx_task,
    is_non_x_media_writer_task,
    resolve_publisher_channel,
)

from packages.common.time_utils import SHANGHAI_TZ
from packages.msx_notice import MSXNoticeClient, html_to_text


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self) -> None:
        self.payloads = [
            {
                "code": 0,
                "data": {
                    "count": 2,
                    "list": [
                        {
                            "id": 118,
                            "subTypeName": "平台活动",
                            "actualTitle": "MSX社区双月福利季丨活动调整通知",
                            "ctime": 1790057700000,
                            "alias": "c461b8b8baee423994b591f300c93d7e",
                        }
                    ],
                },
            },
            {
                "code": 0,
                "data": {
                    "actualContent": '<p>第一段</p><p><a href="/stock-details/SAIL">交易入口</a></p>',
                },
            },
        ]
        self.requests: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict, headers: dict, timeout: float) -> FakeResponse:
        self.requests.append((url, json))
        return FakeResponse(self.payloads.pop(0))


def test_fetch_page_reads_list_and_detail_from_public_api() -> None:
    session = FakeSession()
    client = MSXNoticeClient(session=session)

    total, notices = client.fetch_page()

    assert total == 2
    assert len(notices) == 1
    notice = notices[0]
    assert notice.title == "MSX社区双月福利季丨活动调整通知"
    assert notice.category == "平台活动"
    assert notice.published_at == datetime.fromtimestamp(1790057700, SHANGHAI_TZ).isoformat()
    assert notice.detail_url.endswith("/c461b8b8baee423994b591f300c93d7e")
    assert notice.content == "第一段\n交易入口"
    assert notice.links == [{"label": "交易入口", "url": "https://msx.com/stock-details/SAIL"}]
    assert session.requests[0][1]["classKey"] == "system_msg"
    assert session.requests[1][1]["id"] == 118


def test_html_to_text_removes_markup_without_joining_paragraphs() -> None:
    assert html_to_text("<p>标题</p><ul><li>一</li><li>二</li></ul>") == "标题\n一\n二"


class FakeNoticeClient:
    def __init__(self, notices: list[MSXNotice], details: dict[int, dict] | None = None) -> None:
        self.notices = notices
        self.details = details or {}
        self.detail_calls: list[int] = []

    def list_notices(self, *, page_index: int, page_size: int):
        return len(self.notices), self.notices

    def get_detail(self, notice_id: int):
        self.detail_calls.append(notice_id)
        detail = self.details.get(notice_id)
        if isinstance(detail, Exception):
            raise detail
        if detail is None:
            raise RuntimeError(f"missing detail for {notice_id}")
        return detail


class FakePipelineClient:
    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def submit_job(self, **payload) -> None:
        self.jobs.append(payload)


def make_notice(notice_id: int, title: str) -> MSXNotice:
    return MSXNotice(
        id=notice_id,
        alias=f"alias-{notice_id}",
        title=title,
        category="上新公告",
        published_at="2026-09-23T10:00:00+08:00",
        detail_url=f"https://msx.com/zh-hans/notice-center-detail/alias-{notice_id}",
    )


def test_msx_worker_seeds_first_poll_and_enqueues_only_later_notices(tmp_path: Path) -> None:
    old_notice = make_notice(115, "MSX 上新公告｜SAIL")
    new_notice = make_notice(118, "MSX 上新公告｜VRNS")
    details = {
        118: {
            "content_html": "<p>MSX 将上线 VRNS。</p>",
            "content": "MSX 将上线 VRNS。",
            "links": [],
        }
    }
    pipeline = FakePipelineClient()
    client = FakeNoticeClient([old_notice], details)
    worker = MSXNoticeWorker(database_path=tmp_path / "odaily.sqlite", client=client, pipeline_client=pipeline)

    seeded = worker.run_once()
    assert seeded.seeded_count == 1
    assert seeded.new_count == 0
    assert pipeline.jobs == []

    client.notices = [new_notice, old_notice]
    saved = worker.run_once()
    assert saved.status == "success"
    assert saved.new_count == 1
    assert client.detail_calls == [118]
    assert pipeline.jobs == [
        {"job_type": "write_flow", "task_id": 1, "source": "msx", "source_item_id": "msx:118"}
    ]

    from packages.common.storage import connect_sqlite

    with connect_sqlite(tmp_path / "odaily.sqlite") as conn:
        row = conn.execute("SELECT * FROM tasks WHERE source='msx'").fetchone()
    assert row["source_item_id"] == "msx:118"
    assert row["source_url"] == new_notice.detail_url
    assert row["content"] == "MSX 将上线 VRNS。"


def test_msx_worker_does_not_mark_notice_seen_when_detail_fails(tmp_path: Path) -> None:
    old_notice = make_notice(115, "MSX 上新公告｜SAIL")
    notice = make_notice(118, "MSX 上新公告｜VRNS")
    client = FakeNoticeClient([old_notice], {118: RuntimeError("detail unavailable")})
    worker = MSXNoticeWorker(database_path=tmp_path / "odaily.sqlite", client=client)

    worker.run_once()
    client.notices = [notice]
    result = worker.run_once()
    assert result.status == "parse_failed"
    assert result.new_count == 0
    assert client.detail_calls == [118]


def test_msx_tasks_use_dedicated_prompt_and_external_media_channel() -> None:
    task = TaskRecord(
        id=1,
        source="msx",
        source_item_id="msx:118",
        source_url="https://msx.com/zh-hans/notice-center-detail/alias-118",
        title="MSX 上新公告｜VRNS",
        content="MSX 将上线 VRNS。",
        metadata={"category": "上新公告", "site_display_name": "MSX", "writer_template_key": "msx_notice_writer"},
    )
    prompt = build_writer_prompt(
        task=task,
        prompt=PromptTemplateVersion(id=1, template_key="msx_notice_writer", version_number=1, content="MSX rules"),
    )
    assert "【待处理MSX公告】" in prompt
    assert "公告类别：上新公告" in prompt
    assert is_msx_task(task)
    assert not is_non_x_media_writer_task(task)
    assert resolve_publisher_channel(task) == "external_media"
