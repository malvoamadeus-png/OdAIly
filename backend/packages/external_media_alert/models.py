from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


AlertStage = Literal["domain_judge", "search", "notify"]
DomainRoute = Literal["crypto"]
DomainJudgeRoute = Literal["crypto", "discard"]
DomainDiscardReason = Literal["none", "non_crypto", "market_analysis"]
ExternalMediaCaptureMethod = Literal["rss", "html_request"]
ExternalMediaCaptureStatus = Literal["success", "fetch_failed", "parse_failed", "parse_empty"]


ALERT_TASK_SOURCE = "external_media_alert"
MAINSTREAM_MEDIA_TASK_SOURCE = "mainstream_media"
DOMAIN_WORKER_TASK_SOURCES = (ALERT_TASK_SOURCE, MAINSTREAM_MEDIA_TASK_SOURCE)
ALERT_PROMPT_KEY = "external_media_alert_domain_judge"


@dataclass(frozen=True, slots=True)
class StageSpec:
    claim_status: str
    processing_status: str
    success_status: str
    failure_status: str


STAGE_SPECS: dict[AlertStage, StageSpec] = {
    "domain_judge": StageSpec("pending", "classifying", "classified", "domain_failed"),
    "search": StageSpec("classified", "deduping", "deduped", "search_failed"),
    "notify": StageSpec("deduped", "notifying", "notified", "notify_failed"),
}


@dataclass(frozen=True, slots=True)
class ExternalMediaAlertPipelineRecord:
    task_id: int
    domain_route: DomainRoute | None = None
    discard_reason: str | None = None
    prompt_template_key: str | None = None
    prompt_version_id: int | None = None
    domain_model: str | None = None
    domain_output: dict[str, Any] = field(default_factory=dict)
    search_result: dict[str, Any] = field(default_factory=dict)
    telegram_result: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class StageRunResult:
    exit_code: int
    stage: AlertStage
    processed: int = 0
    failed: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class ExternalMediaSourceDefinition:
    site_key: str
    display_name: str
    homepage_url: str
    feed_url: str | None = None
    list_url: str | None = None
    capture_method: ExternalMediaCaptureMethod = "rss"


@dataclass(frozen=True, slots=True)
class MediaNewsflashItem:
    source: str
    title: str
    content: str
    source_url: str | None = None
    published_at: datetime | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MediaSourceRunResult:
    source: ExternalMediaSourceDefinition
    status: ExternalMediaCaptureStatus
    candidate_count: int = 0
    saved_count: int = 0
    duplicate_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
