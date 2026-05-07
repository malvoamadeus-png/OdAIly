from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


NewsType = Literal["regular", "onchain", "funding"]
ProcessingStage = Literal["judge", "search", "write", "format_publish"]


NEWS_TYPES: set[str] = {"regular", "onchain", "funding"}


PROMPT_KEY_BY_NEWS_TYPE: dict[NewsType, str] = {
    "regular": "x_regular_writer",
    "onchain": "x_onchain_writer",
    "funding": "x_funding_writer",
}


@dataclass(frozen=True, slots=True)
class StageSpec:
    claim_status: str
    processing_status: str
    success_status: str
    failure_status: str


STAGE_SPECS: dict[ProcessingStage, StageSpec] = {
    "judge": StageSpec("pending", "judging", "judged", "judge_failed"),
    "search": StageSpec("judged", "deduping", "deduped", "search_failed"),
    "write": StageSpec("deduped", "writing", "written", "write_failed"),
    "format_publish": StageSpec("written", "formatting", "ready_review", "publish_failed"),
}


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    source: str
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PipelineRecord:
    task_id: int
    news_type: NewsType | None = None
    prompt_template_key: str | None = None
    prompt_version_id: int | None = None
    draft_title: str | None = None
    draft_content: str | None = None
    final_title: str | None = None
    final_content: str | None = None
    push_result: dict[str, Any] = field(default_factory=dict)
    telegram_result: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class PromptTemplateVersion:
    id: int
    template_key: str
    version_number: int
    content: str
    note: str | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class DraftBrief:
    title: str
    content: str


@dataclass(frozen=True, slots=True)
class StageRunResult:
    exit_code: int
    stage: ProcessingStage
    processed: int = 0
    failed: int = 0
    message: str = ""
