from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from packages.common.text import normalize_inline_text, normalize_multiline_text


AttemptStatus = Literal["success", "fetch_failed", "parse_failed", "parse_empty"]


@dataclass(frozen=True, slots=True)
class XCaptureSettings:
    global_interval_seconds: int = 30
    max_concurrency: int = 2
    jitter_seconds: int = 5
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class XCaptureAccount:
    id: int
    username: str
    username_lower: str
    display_name: str | None = None
    write_name: str | None = None
    profile_url: str | None = None
    enabled: bool = True
    is_ai_source: bool = False
    interval_seconds: int | None = None
    seeded_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def effective_interval_seconds(self, settings: XCaptureSettings) -> int:
        return self.interval_seconds or settings.global_interval_seconds


@dataclass(frozen=True, slots=True)
class TweetCandidate:
    tweet_id: str
    author_username: str
    author_display_name: str
    text: str
    created_at_raw: str | None = None
    reply_count: int = 0
    retweet_count: int = 0
    like_count: int = 0
    bookmark_count: int = 0
    view_count: int = 0
    media_urls: list[str] = field(default_factory=list)
    source: str = "fxtwitter"
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "tweet_id", normalize_inline_text(self.tweet_id))
        object.__setattr__(self, "author_username", normalize_inline_text(self.author_username))
        object.__setattr__(self, "author_display_name", normalize_inline_text(self.author_display_name))
        object.__setattr__(self, "text", normalize_multiline_text(self.text))


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    platform: str
    tweet_id: str
    author_username: str
    author_display_name: str
    url: str
    text: str
    created_at: str | None
    reply_count: int
    retweet_count: int
    like_count: int
    bookmark_count: int
    view_count: int
    media_urls: list[str]
    metadata: dict[str, Any]
    raw_payload: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", normalize_inline_text(self.platform))
        object.__setattr__(self, "tweet_id", normalize_inline_text(self.tweet_id))
        object.__setattr__(self, "author_username", normalize_inline_text(self.author_username))
        object.__setattr__(self, "author_display_name", normalize_inline_text(self.author_display_name))
        object.__setattr__(self, "url", normalize_inline_text(self.url))
        object.__setattr__(self, "text", normalize_multiline_text(self.text))


@dataclass(frozen=True, slots=True)
class TimelineAttempt:
    source: str
    status: AttemptStatus
    url: str
    error: str | None = None
    candidate_count: int = 0


@dataclass(frozen=True, slots=True)
class CaptureRunStats:
    account: XCaptureAccount
    status: AttemptStatus
    candidate_count: int = 0
    seeded_count: int = 0
    new_count: int = 0
    saved_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
