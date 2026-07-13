from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from packages.common.text import normalize_inline_text, normalize_multiline_text


CaptureMethod = Literal["html_request", "browser_render"]
CaptureStatus = Literal["success", "fetch_failed", "parse_failed", "parse_empty", "unsupported_method"]
PipelineMode = Literal["write_flow", "alert_only"]
SourceGroup = Literal["external_media", "ai_source", "mixed_source"]
DiscoveryMode = Literal["direct", "telegram_primary_direct_fallback"]
MixedClassificationTarget = Literal["crypto", "ai", "discard"]


SOURCE_GROUP_EXTERNAL_MEDIA = "external_media"
SOURCE_GROUP_AI_SOURCE = "ai_source"
SOURCE_GROUP_MIXED_SOURCE = "mixed_source"
TASK_SOURCE_EXTERNAL_MEDIA = "non_mainstream_media"
TASK_SOURCE_EXTERNAL_MEDIA_ALERT = "external_media_alert"
TASK_SOURCE_AI_SOURCE = "ai_source"
TASK_SOURCE_AI_SOURCE_ALERT = "ai_source_alert"
DISCOVERY_MODE_DIRECT: DiscoveryMode = "direct"
DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK: DiscoveryMode = "telegram_primary_direct_fallback"


@dataclass(frozen=True, slots=True)
class NonMainstreamMediaSettings:
    global_interval_seconds: int = 60
    jitter_seconds: int = 5
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NonMainstreamMediaSource:
    id: int
    site_key: str
    display_name: str
    homepage_url: str
    capture_method: CaptureMethod
    pipeline_mode: PipelineMode = "write_flow"
    source_group: SourceGroup = SOURCE_GROUP_EXTERNAL_MEDIA
    discovery_mode: DiscoveryMode = DISCOVERY_MODE_DIRECT
    interval_seconds: int | None = None
    enabled: bool = True
    seeded_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SiteDefinition:
    site_key: str
    display_name: str
    homepage_url: str
    list_url: str
    capture_method: CaptureMethod
    pipeline_mode: PipelineMode
    source_group: SourceGroup = SOURCE_GROUP_EXTERNAL_MEDIA
    discovery_mode: DiscoveryMode = DISCOVERY_MODE_DIRECT
    interval_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredPage:
    source_item_id: str
    detail_url: str
    discovery_url: str | None = None
    title: str | None = None
    excerpt: str | None = None
    published_at: datetime | None = None
    published_at_raw: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_item_id", normalize_inline_text(self.source_item_id))
        object.__setattr__(self, "detail_url", normalize_inline_text(self.detail_url))
        object.__setattr__(self, "discovery_url", normalize_inline_text(self.discovery_url) or None if self.discovery_url else None)
        object.__setattr__(self, "title", normalize_inline_text(self.title) or None if self.title else None)
        object.__setattr__(self, "excerpt", normalize_multiline_text(self.excerpt) or None if self.excerpt else None)


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    source_item_id: str
    canonical_url: str
    title: str
    content: str
    published_at: datetime | None
    author_names: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    excerpt: str | None = None
    content_format: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_item_id", normalize_inline_text(self.source_item_id))
        object.__setattr__(self, "canonical_url", normalize_inline_text(self.canonical_url))
        object.__setattr__(self, "title", normalize_inline_text(self.title))
        object.__setattr__(self, "content", normalize_multiline_text(self.content))
        object.__setattr__(self, "excerpt", normalize_multiline_text(self.excerpt) or None if self.excerpt else None)


@dataclass(frozen=True, slots=True)
class MixedClassificationResult:
    target: MixedClassificationTarget
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SourceRunStats:
    source: NonMainstreamMediaSource
    status: CaptureStatus
    candidate_count: int = 0
    seeded_count: int = 0
    new_count: int = 0
    saved_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
