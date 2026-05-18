from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


CaptureMethod = Literal["html_request", "browser_render"]
CaptureStatus = Literal["success", "fetch_failed", "parse_failed", "parse_empty", "unsupported_method"]


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


@dataclass(frozen=True, slots=True)
class DiscoveredPage:
    source_item_id: str
    detail_url: str


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
