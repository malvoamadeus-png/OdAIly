from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from packages.common.config import CompetitorMonitorSettings
from packages.common.paths import get_paths
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient
from packages.x_processing.searcher import (
    DashScopeEmbeddingClient,
    SearchCache,
    SearchDocument,
    cosine_similarity,
    parse_ai_review_output,
)

from .fetchers import NewsflashItem


ODAILY_SOURCE = "odaily"
COMPETITOR_SOURCES = {"blockbeats", "panews", "jinse"}


EVENT_REVIEW_SCHEMA = {
    "type": "json_schema",
    "name": "newsflash_event_review",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_same_event": {"type": "boolean"},
            "reason": {
                "type": "string",
                "enum": ["same_event", "same_topic_different_event", "update_of_existing_event", "unrelated"],
            },
        },
        "required": ["is_same_event", "reason"],
    },
    "strict": True,
}


@dataclass(frozen=True, slots=True)
class NewsflashItemRecord:
    id: int
    source: str
    source_item_id: str
    source_url: str | None
    title: str | None
    content: str
    published_at: datetime | None
    first_seen_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_document(self) -> SearchDocument:
        return SearchDocument(
            doc_type="newsflash_item",
            doc_id=f"{self.source}:{self.source_item_id}",
            title=self.title,
            content=self.content,
            source=self.source,
            source_url=self.source_url,
            published_at=self.published_at,
            metadata={"item_id": self.id, **self.metadata},
        )


@dataclass(frozen=True, slots=True)
class EventSourceRecord:
    event_id: str
    item: NewsflashItemRecord


@dataclass(frozen=True, slots=True)
class EventAssignment:
    item_id: int
    event_id: str
    role: str
    match_method: str
    similarity: float | None = None
    matched_item_id: int | None = None
    ai_result: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = False


class NewsflashEventRepository(Protocol):
    def upsert_newsflash_items(self, items: list[NewsflashItem]) -> list[NewsflashItemRecord]: ...
    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]: ...
    def create_event_for_item(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str: ...
    def assign_item_to_event(self, assignment: EventAssignment) -> None: ...
    def update_event_summaries(self, event_ids: set[str]) -> None: ...


class NewsflashEventAggregator:
    def __init__(
        self,
        *,
        repository: NewsflashEventRepository,
        settings: CompetitorMonitorSettings,
        embedding_client: DashScopeEmbeddingClient | None = None,
        ai_client: TextGenerationClient | None = None,
        cache: SearchCache | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.embedding_client = embedding_client or self._build_embedding_client(settings)
        self.ai_client = ai_client if ai_client is not None else self._build_ai_client(settings)
        self.cache = cache or SearchCache(get_paths().searcher_cache_path)

    def assign_items(self, items: list[NewsflashItem]) -> set[str]:
        if not items:
            return set()
        records = self.repository.upsert_newsflash_items(items)
        if not records:
            return set()

        vectors = self._embed_records(records)
        since = min((record.published_at or record.first_seen_at or datetime.now(UTC)) for record in records) - timedelta(
            hours=self.settings.event_window_hours
        )
        existing_sources = self.repository.list_recent_event_sources(
            since=since,
            exclude_item_ids={record.id for record in records},
        )
        existing_vectors = self._embed_records([source.item for source in existing_sources])

        groups = _DisjointSet({record.id for record in records})
        assignments: dict[int, EventAssignment] = {}
        event_by_root: dict[int, str] = {}

        for record in records:
            best = self._best_existing_match(record, vectors[record.id], existing_sources, existing_vectors)
            if best is None:
                continue
            source, similarity, method, ai_result = best
            assignments[record.id] = EventAssignment(
                item_id=record.id,
                event_id=source.event_id,
                role="supporting",
                match_method=method,
                similarity=similarity,
                matched_item_id=source.item.id,
                ai_result=ai_result,
            )
            event_by_root[record.id] = source.event_id

        for left_index, left in enumerate(records):
            for right in records[left_index + 1 :]:
                decision = self._same_event_decision(
                    left=left,
                    right=right,
                    similarity=cosine_similarity(vectors[left.id], vectors[right.id]),
                )
                if decision is None:
                    continue
                groups.union(left.id, right.id)
                for item_id in (left.id, right.id):
                    current = assignments.get(item_id)
                    if current is None or decision["similarity"] > (current.similarity or -1.0):
                        other = right if item_id == left.id else left
                        assignments[item_id] = EventAssignment(
                            item_id=item_id,
                            event_id=current.event_id if current else "",
                            role="supporting",
                            match_method=decision["method"],
                            similarity=decision["similarity"],
                            matched_item_id=other.id,
                            ai_result=decision["ai_result"],
                        )

        updated_event_ids: set[str] = set()
        records_by_id = {record.id: record for record in records}
        for root, item_ids in groups.components().items():
            event_ids = {assignments[item_id].event_id for item_id in item_ids if assignments.get(item_id) and assignments[item_id].event_id}
            if event_ids:
                event_id = sorted(event_ids)[0]
                needs_review = len(event_ids) > 1
                primary_item_id: int | None = None
            else:
                primary = min((records_by_id[item_id] for item_id in item_ids), key=_record_sort_key)
                event_id = self.repository.create_event_for_item(primary)
                needs_review = False
                primary_item_id = primary.id
            needs_review = needs_review or self._has_chain_conflict(item_ids, records_by_id, vectors)
            event_by_root[root] = event_id
            updated_event_ids.add(event_id)
            for item_id in item_ids:
                record = records_by_id[item_id]
                current = assignments.get(item_id)
                self.repository.assign_item_to_event(
                    EventAssignment(
                        item_id=item_id,
                        event_id=event_id,
                        role="primary" if item_id == primary_item_id else "supporting",
                        match_method=current.match_method if current else "new_event",
                        similarity=current.similarity if current else None,
                        matched_item_id=current.matched_item_id if current else None,
                        ai_result=current.ai_result if current else {},
                        needs_review=needs_review or (current.needs_review if current else False),
                    )
                )
        self.repository.update_event_summaries(updated_event_ids)
        return updated_event_ids

    def _embed_records(self, records: list[NewsflashItemRecord]) -> dict[int, list[float]]:
        if not records:
            return {}
        texts: list[str] = []
        missing: list[tuple[NewsflashItemRecord, str, str]] = []
        vectors: dict[int, list[float]] = {}
        for record in records:
            document = record.to_document()
            key = newsflash_cache_key(record)
            text = document.embedding_text
            text_hash = _content_hash(text)
            cached = self.cache.get_embedding(cache_key=key, model=self.embedding_client.model, text_hash=text_hash)
            if cached is None:
                missing.append((record, key, text_hash))
                texts.append(text)
            else:
                vectors[record.id] = cached
            self.cache.upsert_document(document)
        if missing:
            embedded = self.embedding_client.embed(texts)
            for (record, key, text_hash), vector in zip(missing, embedded):
                self.cache.set_embedding(cache_key=key, model=self.embedding_client.model, text_hash=text_hash, vector=vector)
                vectors[record.id] = vector
        return vectors

    def _best_existing_match(
        self,
        record: NewsflashItemRecord,
        vector: list[float],
        existing_sources: list[EventSourceRecord],
        existing_vectors: dict[int, list[float]],
    ) -> tuple[EventSourceRecord, float, str, dict[str, Any]] | None:
        best: tuple[EventSourceRecord, float] | None = None
        for source in existing_sources:
            candidate_vector = existing_vectors.get(source.item.id)
            if candidate_vector is None:
                continue
            similarity = cosine_similarity(vector, candidate_vector)
            if best is None or similarity > best[1]:
                best = (source, similarity)
        if best is None:
            return None
        decision = self._same_event_decision(left=record, right=best[0].item, similarity=best[1])
        if decision is None:
            return None
        return best[0], decision["similarity"], decision["method"], decision["ai_result"]

    def _same_event_decision(
        self,
        *,
        left: NewsflashItemRecord,
        right: NewsflashItemRecord,
        similarity: float,
    ) -> dict[str, Any] | None:
        if similarity >= self.settings.event_duplicate_threshold:
            return {"similarity": similarity, "method": "embedding_high", "ai_result": {}}
        if similarity < self.settings.event_ai_review_threshold or self.ai_client is None:
            return None
        raw_output = self.ai_client.generate_text(
            model=self.settings.event_review_model,
            prompt=build_event_review_prompt(left=left, right=right, similarity=similarity),
            text_format=EVENT_REVIEW_SCHEMA,
        )
        payload = parse_ai_review_output(raw_output)
        is_same = bool(payload.get("is_same_event"))
        if not is_same:
            return None
        return {
            "similarity": similarity,
            "method": "ai_same_event",
            "ai_result": {"raw_output": raw_output, "reason": payload.get("reason") or "same_event"},
        }

    def _has_chain_conflict(
        self,
        item_ids: set[int],
        records_by_id: dict[int, NewsflashItemRecord],
        vectors: dict[int, list[float]],
    ) -> bool:
        if len(item_ids) < 3:
            return False
        ordered = sorted(item_ids)
        for left_index, left_id in enumerate(ordered):
            for right_id in ordered[left_index + 1 :]:
                left_vector = vectors.get(left_id)
                right_vector = vectors.get(right_id)
                if left_vector is None or right_vector is None:
                    continue
                similarity = cosine_similarity(left_vector, right_vector)
                if similarity < self.settings.event_ai_review_threshold:
                    return True
                if similarity < self.settings.event_duplicate_threshold and self.ai_client is None:
                    return True
                if similarity < self.settings.event_duplicate_threshold and self.ai_client is not None:
                    decision = self._same_event_decision(
                        left=records_by_id[left_id],
                        right=records_by_id[right_id],
                        similarity=similarity,
                    )
                    if decision is None:
                        return True
        return False

    @staticmethod
    def _build_embedding_client(settings: CompetitorMonitorSettings) -> DashScopeEmbeddingClient:
        if not settings.dashscope_api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY")
        return DashScopeEmbeddingClient(
            api_key=settings.dashscope_api_key,
            base_url=str(settings.event_embedding_base_url),
            model=settings.event_embedding_model,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )

    @staticmethod
    def _build_ai_client(settings: CompetitorMonitorSettings) -> TextGenerationClient | None:
        if not settings.openai_api_key:
            return None
        return OpenAIResponsesClient(
            api_key=settings.openai_api_key,
            base_url=str(settings.openai_base_url),
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
            api_style=settings.openai_api_style,
        )


def build_event_review_prompt(*, left: NewsflashItemRecord, right: NewsflashItemRecord, similarity: float) -> str:
    return (
        "你是 Odaily 竞品快讯事件归属器。判断两条快讯是否属于同一个新闻事件。\n"
        "同一事件要求主体、核心动作、关键结果基本一致；同一主体的新进展或不同动作不是同一事件。\n"
        "只判断事件归属，不评价标题或正文质量。只输出 JSON，不输出解释。\n\n"
        "【快讯 A】\n"
        f"标题：{left.title or ''}\n"
        f"正文：{left.content}\n\n"
        "【快讯 B】\n"
        f"标题：{right.title or ''}\n"
        f"正文：{right.content}\n"
        f"相似度：{similarity:.4f}\n\n"
        'JSON格式：{"is_same_event":true|false,"reason":"same_event|same_topic_different_event|update_of_existing_event|unrelated"}'
    )


def newsflash_cache_key(record: NewsflashItemRecord) -> str:
    return f"newsflash:{record.source}:{record.source_item_id}"


def generate_event_id() -> str:
    now_ms = int(time.time() * 1000)
    timestamp = _encode_base32(now_ms, 10)
    random_part = _encode_base32(int.from_bytes(__import__("os").urandom(10), "big"), 16)
    return f"evt_{timestamp}{random_part}"


def _encode_base32(value: int, length: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    chars = []
    for _ in range(length):
        chars.append(alphabet[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def _content_hash(text: str) -> str:
    from packages.x_processing.searcher import content_hash

    return content_hash(text)


def _record_sort_key(record: NewsflashItemRecord) -> tuple[datetime, int]:
    return (record.published_at or record.first_seen_at or datetime.max.replace(tzinfo=UTC), record.id)


class _DisjointSet:
    def __init__(self, values: set[int]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)

    def components(self) -> dict[int, set[int]]:
        result: dict[int, set[int]] = {}
        for value in self.parent:
            result.setdefault(self.find(value), set()).add(value)
        return result
