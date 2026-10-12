from __future__ import annotations

import json

import pytest

from packages.common.config import XProcessingSettings
from packages.x_processing.models import PipelineRecord, TaskRecord
from packages.x_processing.repository import InMemoryXProcessingRepository
from packages.x_processing.worker import XProcessingWorker, build_structured_writer_prompt


def _task() -> TaskRecord:
    # Matches the legacy production task shape: the real author is only in
    # official_post, and the materiality decision selects the onchain template.
    return TaskRecord(
        id=1, source="event_tracking", source_item_id="official-post", source_url=None,
        title="PaperTrade报价套利质疑：官方进展",
        content="We have begun tightening OI caps onchain and at the relayer level.",
        metadata={"event_tracking": {
            "official_handle": "papertrade_xyz",
            "fact_summary": "官方称已开始收紧持仓量上限。",
            "difference_from_timeline": "首次披露收紧措施。",
        }},
        raw_payload={"official_post": {
            "author_name": "papertrade.xyz", "author_screen_name": "papertrade_xyz",
        }},
        status="writing",
    )


class _Writer:
    def __init__(self, content: str) -> None:
        self.content = content

    def generate_text(self, **_kwargs: object) -> str:
        return json.dumps({
            "title": "PaperTrade开始收紧持仓量上限",
            "content": self.content,
            "title_strategy": "plain", "title_strategy_reason": "官方进展",
            "matched_title_rules": ["plain_direct"],
            "feature_mode_applied": False, "feature_mode_reason": "常规表达",
        }, ensure_ascii=False)


def _worker(content: str) -> tuple[XProcessingWorker, InMemoryXProcessingRepository]:
    repo = InMemoryXProcessingRepository()
    repo.add_task(_task())
    repo.pipelines[1] = PipelineRecord(task_id=1, news_type="onchain")
    repo.prompts["x_onchain_writer"] = repo.prompts["x_onchain_writer"].__class__(
        id=1, template_key="x_onchain_writer", version_number=1,
        content="正文开头应为“据{发言人}监测，”后接正式内容",
    )
    worker = XProcessingWorker(
        stage="write", repository=repo, settings=XProcessingSettings(dry_run=True),
        ai_client=_Writer(content),
    )
    return worker, repo


@pytest.mark.parametrize("opening", [
    "据PaperTrade监测，PaperTrade表示，",
    "据 PaperTrade 监测，PaperTrade 表示，",
])
def test_event_writer_rejects_monitoring_attribution_before_persisting(opening: str) -> None:
    worker, repo = _worker(opening + "已开始收紧持仓量上限。")
    with pytest.raises(ValueError, match="event tracking.*monitoring"):
        worker._run_write(_task())
    assert repo.pipelines[1].draft_content is None


def test_event_writer_keeps_official_attribution_and_format_only_adds_house_style() -> None:
    worker, repo = _worker("PaperTrade在X平台发文表示，已开始在链上和中继层收紧持仓量上限。")
    worker._run_write(_task())
    worker._run_format_publish(repo.tasks[1])
    assert repo.pipelines[1].final_content == (
        "Odaily星球日报讯 PaperTrade 在 X 平台发文表示，已开始在链上和中继层收紧持仓量上限。"
    )


def test_event_writer_uses_real_author_and_event_progress_for_legacy_tasks() -> None:
    _worker_instance, repo = _worker("unused")
    prompt = build_structured_writer_prompt(
        task=_task(), prompt=repo.prompts["x_onchain_writer"], known_subjects=[],
    )
    assert "发布人：papertrade.xyz" in prompt
    assert "官方账号：@papertrade_xyz" in prompt
    assert "发布人：PaperTrade报价套利质疑" not in prompt
    assert "首次披露收紧措施。" in prompt
    assert "官方称已开始收紧持仓量上限。" in prompt
    assert "覆盖" in prompt and "监测" in prompt
