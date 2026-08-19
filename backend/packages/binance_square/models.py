from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


BINANCE_SQUARE_SOURCE = "binance_square"
POLL_INTERVAL_SECONDS = 180


@dataclass(frozen=True, slots=True)
class BinanceSquareSettings:
    enabled: bool = False
    interval_seconds: int = POLL_INTERVAL_SECONDS
    worker_status: str = "stopped"
    worker_last_seen_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BinanceSquareAccount:
    id: int
    slug: str
    slug_lower: str
    profile_url: str
    square_uid: str | None = None
    display_name: str | None = None
    write_name: str | None = None
    enabled: bool = True
    seeded_at: datetime | None = None
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class BinanceSquarePost:
    post_id: str
    username: str
    display_name: str
    text: str
    published_at: str | None
    url: str
    square_uid: str | None = None
    media_urls: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BinanceSquareRunStats:
    account: BinanceSquareAccount
    status: str
    candidate_count: int = 0
    seeded_count: int = 0
    new_count: int = 0
    saved_count: int = 0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
