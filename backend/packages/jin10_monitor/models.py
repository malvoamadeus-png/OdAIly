from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from packages.common.text import normalize_inline_text, normalize_multiline_text


JIN10_SOURCE = "jin10"
DEFAULT_JIN10_ENDPOINT_URL = "https://www.jin10.com/flash_newest.js"
DEFAULT_JIN10_HEADERS = {
    "x-app-id": "bVBF4FyRTn5NJF5n",
    "x-version": "1.0.0",
    "referer": "https://www.jin10.com/",
    "origin": "https://www.jin10.com",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


@dataclass(frozen=True, slots=True)
class Jin10Settings:
    enabled: bool = False
    interval_seconds: int = 60
    endpoint_url: str = DEFAULT_JIN10_ENDPOINT_URL
    channel: str | None = None
    request_headers: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_JIN10_HEADERS))
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Jin10Item:
    source_item_id: str
    title: str
    content: str
    source_url: str | None = None
    published_at: datetime | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_item_id", normalize_inline_text(self.source_item_id))
        object.__setattr__(self, "title", normalize_inline_text(self.title))
        object.__setattr__(self, "content", normalize_multiline_text(self.content))
        object.__setattr__(self, "source_url", normalize_inline_text(self.source_url) or None if self.source_url else None)


@dataclass(frozen=True, slots=True)
class Jin10RunResult:
    status: str
    fetched: int = 0
    seeded: int = 0
    new: int = 0
    saved: int = 0
    error: str | None = None
    sample_titles: list[str] = field(default_factory=list)
