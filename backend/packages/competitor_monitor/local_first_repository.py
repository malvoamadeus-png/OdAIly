from __future__ import annotations

from datetime import datetime
from typing import Any

from .events import EventAssignment, EventSourceRecord, NewsflashItemRecord
from .fetchers import NewsflashItem
from .local_state import CompetitorEventStateStore
from .repository import PostgresCompetitorMonitorRepository


class LocalFirstCompetitorMonitorRepository:
    def __init__(
        self,
        *,
        remote: PostgresCompetitorMonitorRepository,
        state_store: CompetitorEventStateStore,
    ) -> None:
        self.remote = remote
        self.state_store = state_store

    def init_schema(self) -> None:
        self.remote.init_schema()

    def list_enabled_filter_keywords(self) -> list[str]:
        return self.remote.list_enabled_filter_keywords()

    def save_items(self, items: list[NewsflashItem]) -> tuple[int, int]:
        return self.remote.save_items(items)

    def save_items_for_pipeline(self, items: list[NewsflashItem]) -> tuple[list[tuple[NewsflashItem, int]], int]:
        return self.remote.save_items_for_pipeline(items)

    def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]:
        records = self.remote.upsert_newsflash_items(items)
        self.state_store.upsert_items(records)
        return records

    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]:
        local_sources = self.state_store.list_existing_event_sources(item_ids=item_ids)
        local_item_ids = {source.item.id for source in local_sources}
        missing_item_ids = item_ids - local_item_ids
        if not missing_item_ids:
            return local_sources
        remote_sources = self.remote.list_existing_event_sources(item_ids=missing_item_ids)
        self.state_store.upsert_event_sources(remote_sources)
        return local_sources + remote_sources

    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]:
        if not self.state_store.has_recent_window(since=since):
            remote_sources = self.remote.list_recent_event_sources(since=since, exclude_item_ids=set())
            self.state_store.upsert_event_sources(remote_sources)
            self.state_store.mark_recent_window(since=since)
        return self.state_store.list_recent_event_sources(since=since, exclude_item_ids=exclude_item_ids)

    def create_event_with_source(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str:
        event_id = self.remote.create_event_with_source(item, needs_review=needs_review)
        self.state_store.upsert_items([item])
        self.state_store.assign_item_to_event(
            EventAssignment(
                item_id=item.id,
                event_id=event_id,
                role="primary",
                match_method="new_event",
                needs_review=needs_review,
            )
        )
        return event_id

    def assign_item_to_event(self, assignment: EventAssignment) -> None:
        self.remote.assign_item_to_event(assignment)
        self.state_store.assign_item_to_event(assignment)

    def update_event_summaries(self, event_ids: set[str]) -> None:
        self.remote.update_event_summaries(event_ids)

    def prune_excluded_event_sources(self, terms: list[str] | None = None) -> dict[str, int]:
        return self.remote.prune_excluded_event_sources(terms)

    def prune_orphan_events(self) -> int:
        return self.remote.prune_orphan_events()

    def repair_newsflash_timestamps(self) -> dict[str, int]:
        return self.remote.repair_newsflash_timestamps()

    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.remote.record_worker_heartbeat(
            component=component,
            worker_id=worker_id,
            status=status,
            success=success,
            error=error,
            metadata=metadata,
        )
