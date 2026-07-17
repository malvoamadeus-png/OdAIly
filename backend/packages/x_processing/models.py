from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from packages.common.text import normalize_inline_text, normalize_multiline_text


NewsType = Literal["regular", "onchain", "funding", "non_mainstream_media", "ai_source", "mainstream_media"]
JudgeRoute = Literal["regular", "onchain", "funding", "non_mainstream_media", "ai_source", "discard"]
DiscardType = Literal["none", "pure_emotion", "baseless_trading_call", "daily_chatter", "non_crypto_ai", "off_topic"]
ProcessingStage = Literal[
    "judge",
    "judge_crypto",
    "judge_ai",
    "judge_jin10",
    "search",
    "write",
    "format_publish",
    "publish",
]
PublisherChannelKey = Literal["external_media", "x", "competitor", "jin10"]
PublisherCategory = Literal["policy_regulation", "people_view", "major_project_progress", "funding", "other"]
PublisherDecision = Literal["auto_publish", "manual_review", "failed"]


NON_MAINSTREAM_MEDIA_SOURCE = "non_mainstream_media"
AI_SOURCE = "ai_source"
MAINSTREAM_MEDIA_SOURCE = "mainstream_media"
JIN10_SOURCE = "jin10"
NEWS_TYPES: set[str] = {"regular", "onchain", "funding", "non_mainstream_media", "ai_source", "mainstream_media"}
JUDGE_ROUTES: set[str] = {"regular", "onchain", "funding", "non_mainstream_media", "ai_source", "discard"}
DISCARD_TYPES: set[str] = {"none", "pure_emotion", "baseless_trading_call", "daily_chatter", "non_crypto_ai", "off_topic"}
COMPETITOR_SOURCES: set[str] = {"blockbeats", "panews", "jinse"}
ODAILY_REFERENCE_SOURCE = "odaily"
SEARCH_FIRST_SOURCES: set[str] = {*COMPETITOR_SOURCES, NON_MAINSTREAM_MEDIA_SOURCE, AI_SOURCE}
PROCESSING_SOURCES: set[str] = {"x", JIN10_SOURCE, *SEARCH_FIRST_SOURCES}
WRITE_ONLY_SOURCES: set[str] = {MAINSTREAM_MEDIA_SOURCE}
WRITE_STAGE_SOURCES: set[str] = {*PROCESSING_SOURCES, *WRITE_ONLY_SOURCES}
PUBLISHER_CHANNEL_KEYS: set[str] = {"external_media", "x", "competitor", "jin10"}
PUBLISHER_CATEGORIES: set[str] = {
    "policy_regulation",
    "people_view",
    "major_project_progress",
    "funding",
    "other",
}
PUBLISHER_DECISIONS: set[str] = {"auto_publish", "manual_review", "failed"}
ACTIVE_CANDIDATE_TTL = timedelta(hours=24)


PROMPT_KEY_BY_NEWS_TYPE: dict[NewsType, str] = {
    "regular": "x_regular_writer",
    "onchain": "x_onchain_writer",
    "funding": "x_funding_writer",
    "non_mainstream_media": "mainstream_media_writer",
    "ai_source": "mainstream_media_writer",
    "mainstream_media": "mainstream_media_writer",
}


@dataclass(frozen=True, slots=True)
class StageSpec:
    claim_status: str
    processing_status: str
    success_status: str
    failure_status: str


STAGE_SPECS: dict[ProcessingStage, StageSpec] = {
    "judge": StageSpec("pending", "judging", "judged", "judge_failed"),
    "judge_crypto": StageSpec("pending", "judging", "judged", "judge_failed"),
    "judge_ai": StageSpec("pending", "judging", "judged", "judge_failed"),
    "judge_jin10": StageSpec("pending", "judging", "judged", "judge_failed"),
    "search": StageSpec("judged", "deduping", "deduped", "search_failed"),
    "write": StageSpec("deduped", "writing", "written", "write_failed"),
    "format_publish": StageSpec("written", "formatting", "publisher_pending", "format_failed"),
    "publish": StageSpec("publisher_pending", "publishing", "ready_review", "publisher_failed"),
}


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: int
    source: str
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    published_at: datetime | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_item_id", normalize_inline_text(self.source_item_id))
        object.__setattr__(self, "source_url", normalize_inline_text(self.source_url) or None if self.source_url else None)
        object.__setattr__(self, "title", normalize_inline_text(self.title) or None if self.title else None)
        object.__setattr__(self, "content", normalize_multiline_text(self.content))


@dataclass(frozen=True, slots=True)
class PipelineRecord:
    task_id: int
    news_type: NewsType | None = None
    candidate_id: int | None = None
    judge_completed_at: datetime | None = None
    search_result: dict[str, Any] = field(default_factory=dict)
    search_completed_at: datetime | None = None
    prompt_template_key: str | None = None
    prompt_version_id: int | None = None
    writer_feature_mode_enabled: bool | None = None
    draft_title: str | None = None
    draft_content: str | None = None
    write_completed_at: datetime | None = None
    final_title: str | None = None
    final_content: str | None = None
    format_completed_at: datetime | None = None
    publisher_channel: PublisherChannelKey | None = None
    publisher_model: str | None = None
    publisher_category: PublisherCategory | None = None
    publisher_decision: PublisherDecision | None = None
    publisher_reason_code: str | None = None
    publisher_output: dict[str, Any] = field(default_factory=dict)
    publisher_decided_at: datetime | None = None
    publish_completed_at: datetime | None = None
    push_result: dict[str, Any] = field(default_factory=dict)
    telegram_result: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "draft_title", normalize_inline_text(self.draft_title) or None if self.draft_title else None)
        object.__setattr__(
            self,
            "draft_content",
            normalize_multiline_text(self.draft_content) or None if self.draft_content else None,
        )
        object.__setattr__(self, "final_title", normalize_inline_text(self.final_title) or None if self.final_title else None)
        object.__setattr__(
            self,
            "final_content",
            normalize_multiline_text(self.final_content) or None if self.final_content else None,
        )


@dataclass(frozen=True, slots=True)
class PublisherSettingsRecord:
    enabled: bool
    timezone: str
    window_start_local: str
    window_end_local: str
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublisherChannelRecord:
    channel_key: PublisherChannelKey
    display_name: str
    enabled: bool
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PromptTemplateVersion:
    id: int
    template_key: str
    version_number: int
    content: str
    feature_mode_enabled: bool = False
    feature_mode_text: str = ""
    note: str | None = None
    created_at: datetime | None = None
    published_at: datetime | None = None


def render_prompt_content(prompt: PromptTemplateVersion) -> str:
    if not prompt.feature_mode_enabled:
        return prompt.content
    feature_text = prompt.feature_mode_text.strip()
    if not feature_text:
        return prompt.content
    return f"{feature_text}\n\n{prompt.content}"


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
