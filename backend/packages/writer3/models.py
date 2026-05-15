from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


Writer3Status = Literal["pending", "processing", "skipped", "sent", "failed"]
Writer3EventType = Literal[
    "financing",
    "mainnet_launch",
    "testnet_launch",
    "airdrop",
    "tokenomics",
    "regulation",
    "bill",
    "lawsuit",
    "security",
    "none",
]


@dataclass(frozen=True, slots=True)
class Writer3Task:
    task_id: int | None
    source: str
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    final_content: str
    published_at: datetime | None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    context_id: int | None = None


@dataclass(frozen=True, slots=True)
class OdailyReference:
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    published_at: datetime | None
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FocusSubject:
    name: str
    subject_type: str
    aliases: list[str]


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    should_run_writer3: bool
    current_event_type: Writer3EventType
    focus_subject: FocusSubject
    context_entities: list[str]
    matter_key: str
    matter_aliases: list[str]


@dataclass(frozen=True, slots=True)
class Writer3Candidate:
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    published_at: datetime | None
    score: float
    matched_aliases: list[str]
    matched_prior_types: list[str]


@dataclass(frozen=True, slots=True)
class ContextResult:
    should_write: bool
    context_text: str
    evidence_source_item_ids: list[str]
