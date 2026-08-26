from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
        },
        "required": ["is_same_event"],
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
    def list_existing_event_sources(self, *, item_ids: set[int]) -> list[EventSourceRecord]: ...
    def list_recent_event_sources(self, *, since: datetime, exclude_item_ids: set[int]) -> list[EventSourceRecord]: ...
    def list_event_sources_by_urls(self, *, source_urls: set[str], exclude_item_ids: set[int]) -> list[EventSourceRecord]: ...
    def list_event_anchors(self, *, event_ids: set[str]) -> list[EventSourceRecord]: ...
    def create_event_with_source(self, item: NewsflashItemRecord, *, needs_review: bool = False) -> str: ...
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
        started = time.monotonic()
        print(f"[odaily] competitor event phase=upsert_items count={len(items)}")
        records = self.repository.upsert_newsflash_items(items)
        if not records:
            return set()

        record_ids = {record.id for record in records}
        print(f"[odaily] competitor event phase=list_existing count={len(records)}")
        existing_assignments = self.repository.list_existing_event_sources(item_ids=record_ids)
        existing_by_item_id = {source.item.id: source for source in existing_assignments}
        new_records = [record for record in records if record.id not in existing_by_item_id]
        updated_event_ids = {source.event_id for source in existing_assignments}
        if not new_records:
            self.repository.update_event_summaries(updated_event_ids)
            print(
                "[odaily] competitor event phase=existing_only "
                f"records={len(records)} events={len(updated_event_ids)} elapsed_seconds={time.monotonic() - started:.1f}"
            )
            return updated_event_ids

        print(f"[odaily] competitor event phase=embed_new count={len(new_records)}")
        vectors = self._embed_records(new_records)
        new_record_ids = {record.id for record in new_records}
        since = min((record.published_at or record.first_seen_at or datetime.now(UTC)) for record in new_records) - timedelta(
            hours=self.settings.event_window_hours
        )
        print(f"[odaily] competitor event phase=list_recent since={since.isoformat()} exclude_count={len(new_record_ids)}")
        existing_sources = self.repository.list_recent_event_sources(
            since=since,
            exclude_item_ids=new_record_ids,
        )

        source_urls = {record.source_url for record in new_records if record.source_url}
        exact_sources = self.repository.list_event_sources_by_urls(
            source_urls={str(source_url) for source_url in source_urls},
            exclude_item_ids=new_record_ids,
        ) if source_urls else []
        exact_by_url: dict[str, EventSourceRecord] = {}
        for source in exact_sources:
            source_url = canonicalize_source_url(source.item.source_url)
            if source_url:
                exact_by_url.setdefault(source_url, source)

        recent_event_ids = {source.event_id for source in existing_sources}
        anchor_sources = self.repository.list_event_anchors(event_ids=recent_event_ids)
        known_source_item_ids = {source.item.id for source in existing_sources}
        existing_sources.extend(
            source for source in anchor_sources if source.item.id not in known_source_item_ids
        )
        print(f"[odaily] competitor event phase=embed_recent count={len(existing_sources)}")
        existing_vectors = self._embed_records([source.item for source in existing_sources])
        anchors = self._event_anchors(existing_sources)
        assignments: dict[int, EventAssignment] = {}

        print(
            "[odaily] competitor event phase=match_existing "
            f"new_count={len(new_records)} recent_count={len(existing_sources)}"
        )
        for record in new_records:
            exact = exact_by_url.get(canonicalize_source_url(record.source_url) or "")
            if exact is not None:
                assignments[record.id] = EventAssignment(
                    item_id=record.id,
                    event_id=exact.event_id,
                    role="supporting",
                    match_method="source_url_exact",
                    similarity=1.0,
                    matched_item_id=exact.item.id,
                    ai_result={},
                )
                updated_event_ids.add(exact.event_id)
                continue
            best = self._best_existing_match(record, vectors[record.id], anchors, existing_vectors)
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

        # A new item can share a source URL with another new item that already
        # matched an existing event. Resolve that exact identity before doing
        # the stricter anchor-based grouping below.
        for record in new_records:
            if record.id in assignments:
                continue
            record_url = canonicalize_source_url(record.source_url)
            if not record_url:
                continue
            exact_assigned = next(
                (
                    other
                    for other in new_records
                    if other.id != record.id
                    and assignments.get(other.id) is not None
                    and canonicalize_source_url(other.source_url) == record_url
                ),
                None,
            )
            if exact_assigned is not None:
                current = assignments[exact_assigned.id]
                assignments[record.id] = EventAssignment(
                    item_id=record.id,
                    event_id=current.event_id,
                    role="supporting",
                    match_method="source_url_exact",
                    similarity=1.0,
                    matched_item_id=exact_assigned.id,
                    ai_result={},
                )

        unassigned = {
            record.id: record
            for record in new_records
            if record.id not in assignments
        }
        groups: list[list[tuple[NewsflashItemRecord, dict[str, Any] | None]]] = []
        print(f"[odaily] competitor event phase=match_batch new_count={len(unassigned)}")
        while unassigned:
            anchor = min(unassigned.values(), key=_record_sort_key)
            del unassigned[anchor.id]
            group: list[tuple[NewsflashItemRecord, dict[str, Any] | None]] = [(anchor, None)]
            for candidate in sorted(unassigned.values(), key=_record_sort_key):
                decision = self._same_event_decision(
                    left=anchor,
                    right=candidate,
                    similarity=cosine_similarity(vectors[anchor.id], vectors[candidate.id]),
                )
                if decision is None:
                    continue
                group.append((candidate, decision))
                del unassigned[candidate.id]
            groups.append(group)

        print(f"[odaily] competitor event phase=write_assignments components={len(groups)}")
        for group in groups:
            primary, _ = group[0]
            event_id = self.repository.create_event_with_source(primary, needs_review=False)
            updated_event_ids.add(event_id)
            for record, decision in group[1:]:
                self.repository.assign_item_to_event(
                    EventAssignment(
                        item_id=record.id,
                        event_id=event_id,
                        role="supporting",
                        match_method=decision["method"] if decision else "new_event",
                        similarity=decision["similarity"] if decision else None,
                        matched_item_id=primary.id,
                        ai_result=decision["ai_result"] if decision else {},
                        needs_review=False,
                    )
                )

        for current in assignments.values():
            self.repository.assign_item_to_event(current)
        print(f"[odaily] competitor event phase=update_summaries events={len(updated_event_ids)}")
        self.repository.update_event_summaries(updated_event_ids)
        print(
            "[odaily] competitor event phase=done "
            f"records={len(records)} new_records={len(new_records)} events={len(updated_event_ids)} "
            f"elapsed_seconds={time.monotonic() - started:.1f}"
        )
        return updated_event_ids

    def _embed_records(self, records: list[NewsflashItemRecord]) -> dict[int, list[float]]:
        if not records:
            return {}
        started = time.monotonic()
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
            print(f"[odaily] competitor event embeddings request missing={len(missing)} cached={len(vectors)}")
            embedded = self.embedding_client.embed(texts)
            for (record, key, text_hash), vector in zip(missing, embedded):
                self.cache.set_embedding(cache_key=key, model=self.embedding_client.model, text_hash=text_hash, vector=vector)
                vectors[record.id] = vector
        print(
            "[odaily] competitor event embeddings ready "
            f"records={len(records)} missing={len(missing)} elapsed_seconds={time.monotonic() - started:.1f}"
        )
        return vectors

    def _best_existing_match(
        self,
        record: NewsflashItemRecord,
        vector: list[float],
        anchors: list[EventSourceRecord],
        existing_vectors: dict[int, list[float]],
    ) -> tuple[EventSourceRecord, float, str, dict[str, Any]] | None:
        best: tuple[EventSourceRecord, dict[str, Any]] | None = None
        for anchor in anchors:
            anchor_vector = existing_vectors.get(anchor.item.id)
            if anchor_vector is None:
                continue
            similarity = cosine_similarity(vector, anchor_vector)
            decision = self._same_event_decision(left=record, right=anchor.item, similarity=similarity)
            if decision is None:
                continue
            if best is None or decision["similarity"] > best[1]["similarity"]:
                best = (anchor, decision)
        if best is None:
            return None
        anchor, decision = best
        return anchor, decision["similarity"], decision["method"], decision["ai_result"]

    @staticmethod
    def _event_anchors(existing_sources: list[EventSourceRecord]) -> list[EventSourceRecord]:
        by_event: dict[str, list[EventSourceRecord]] = {}
        for source in existing_sources:
            by_event.setdefault(source.event_id, []).append(source)
        return [
            min(sources, key=lambda source: _record_sort_key(source.item))
            for sources in by_event.values()
        ]

    def _same_event_decision(
        self,
        *,
        left: NewsflashItemRecord,
        right: NewsflashItemRecord,
        similarity: float,
    ) -> dict[str, Any] | None:
        if same_source_url(left.source_url, right.source_url):
            return {"similarity": 1.0, "method": "source_url_exact", "ai_result": {}}
        if similarity >= self.settings.event_duplicate_threshold:
            return {"similarity": similarity, "method": "embedding_high", "ai_result": {}}
        if similarity < self.settings.event_ai_review_threshold or self.ai_client is None:
            return None
        try:
            raw_output = self.ai_client.generate_text(
                model=self.settings.event_review_model,
                prompt=build_event_review_prompt(left=left, right=right),
                text_format=EVENT_REVIEW_SCHEMA,
            )
            payload = parse_ai_review_output(raw_output)
        except Exception as exc:
            print(
                "[odaily] competitor event ai review skipped "
                f"left={left.source}:{left.source_item_id} right={right.source}:{right.source_item_id} "
                f"similarity={similarity:.4f} error={exc}"
            )
            return None
        is_same = payload.get("is_same_event") is True
        if not is_same:
            return None
        return {
            "similarity": similarity,
            "method": "ai_same_event",
            "ai_result": {"raw_output": raw_output},
        }

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


def build_event_review_prompt(*, left: NewsflashItemRecord, right: NewsflashItemRecord) -> str:
    return (
        "你是 Odaily 竞品快讯的同一报道判断器。\n"
        "你的唯一任务是判断两条快讯是否在报道同一份原始消息。\n"
        "只有当两条快讯可以追溯到同一篇原始文章、同一条社交媒体发言、同一份公告或报告，\n"
        "或者报道同一个主体在同一时点作出的同一项声明、动作、数据发布或事实披露时，才返回 true。\n"
        "标题写法、篇幅、语言和强调重点不同，不影响判断；同一个核心消息可以有不同角度的改写。\n"
        "以下情况必须返回 false：只是主体、行业或主题相同；不同分析、预测、评论或报道角度；\n"
        "预告、后续进展、正式结果或市场反应；一条独立报道与一条包含多项消息的综合要闻；\n"
        "两条只存在部分事实重合；无法确认来自同一份消息。\n"
        "不要把‘同一主题’或‘同一场事件’当作‘同一报道’。证据不足时返回 false。\n"
        "只输出 JSON，不输出解释。\n\n"
        "【快讯 A】\n"
        f"来源：{left.source}\n"
        f"原始链接：{left.source_url or ''}\n"
        f"标题：{left.title or ''}\n"
        f"正文：{left.content}\n\n"
        "【快讯 B】\n"
        f"来源：{right.source}\n"
        f"原始链接：{right.source_url or ''}\n"
        f"标题：{right.title or ''}\n"
        f"正文：{right.content}\n"
        'JSON格式：{"is_same_event":true|false}'
    )


def newsflash_cache_key(record: NewsflashItemRecord) -> str:
    return f"newsflash:{record.source}:{record.source_item_id}"


def canonicalize_source_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(str(value).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    host = parsed.hostname.lower() if parsed.hostname else ""
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host
    if ":" in host:
        netloc = f"[{host}]"
    if port and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def same_source_url(left: str | None, right: str | None) -> bool:
    left_url = canonicalize_source_url(left)
    right_url = canonicalize_source_url(right)
    return bool(left_url and right_url and left_url == right_url)


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
