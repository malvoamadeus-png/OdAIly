from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


AuditorStatus = Literal["pending", "processing", "passed", "flagged", "failed", "skipped"]
AuditorSeverity = Literal["low", "medium", "high"]
AuditorIssueType = Literal["punctuation", "grammar", "typo", "format", "other"]


@dataclass(frozen=True, slots=True)
class AuditorTask:
    id: int
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    content_hash: str
    published_at: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AuditorIssue:
    issue_type: AuditorIssueType
    location: str
    original: str
    suggested: str
    reason: str


@dataclass(frozen=True, slots=True)
class AuditorResult:
    has_issue: bool
    severity: AuditorSeverity
    issues: list[AuditorIssue]
    summary: str
