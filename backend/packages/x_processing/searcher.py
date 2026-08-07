from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import requests

from packages.common.time_utils import utc_iso

from .models import ACTIVE_CANDIDATE_TTL

try:
    import numpy as np
except ImportError:  # pragma: no cover - optional runtime acceleration
    np = None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_embedding_blob(vector: list[float]) -> bytes:
    if not vector:
        return b""
    return struct.pack(f"<{len(vector)}f", *vector)


def decode_embedding_blob(payload: bytes, dimensions: int) -> list[float]:
    if dimensions < 0 or len(payload) != dimensions * 4:
        raise ValueError("invalid float32 embedding payload")
    if dimensions == 0:
        return []
    return list(struct.unpack(f"<{dimensions}f", payload))


def normalize_for_embedding(*, title: str | None, content: str) -> str:
    parts: list[str] = []
    if title and title.strip():
        parts.append(f"标题：{title.strip()}")
    parts.append(f"正文：{content.strip()}")
    return "\n".join(parts).strip()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


COMPARE_URL_SUFFIX_PATTERN = re.compile(r"[?#].*$")
TITLE_KEY_PATTERN = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


class EmbeddingClient(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DashScopeEmbeddingClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float,
        max_attempts: int,
        backoff_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.backoff_seconds = max(0.0, backoff_seconds)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        batch_size = 10
        vectors: list[list[float]] = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._embed_batch(texts[start : start + batch_size]))
        return vectors

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts}
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/embeddings",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout_seconds,
                )
                if response.status_code >= 400:
                    message = response.text.strip().replace("\n", " ")[:500]
                    raise RuntimeError(
                        f"{response.status_code} Client Error from DashScope embeddings: {message}"
                    )
                return extract_embeddings(response.json())
            except Exception as exc:
                last_error = exc
                if attempt < self.max_attempts and self.backoff_seconds > 0:
                    time.sleep(self.backoff_seconds * attempt)
        raise RuntimeError(str(last_error) if last_error else "embedding request failed")


def is_dashscope_arrearage_error(error: Exception | str) -> bool:
    message = str(error).strip().lower()
    if not message:
        return False
    return "dashscope" in message and "arrearage" in message


def extract_embeddings(payload: dict[str, Any]) -> list[list[float]]:
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("embedding response missing data")
    ordered = sorted(
        (item for item in data if isinstance(item, dict)),
        key=lambda item: int(item.get("index", 0)),
    )
    vectors: list[list[float]] = []
    for item in ordered:
        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise ValueError("embedding item missing embedding")
        vectors.append([float(value) for value in embedding])
    if not vectors:
        raise ValueError("embedding response returned no vectors")
    return vectors


@dataclass(frozen=True, slots=True)
class SearchDocument:
    doc_type: str
    doc_id: str
    title: str | None
    content: str
    source: str
    source_url: str | None = None
    task_id: int | None = None
    candidate_id: int | None = None
    published_at: datetime | None = None
    status: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def embedding_text(self) -> str:
        return normalize_for_embedding(title=self.title, content=self.content)


@dataclass(frozen=True, slots=True)
class SearchMatch:
    document: SearchDocument
    similarity: float


@dataclass(frozen=True, slots=True)
class SearchDecision:
    is_duplicate: bool
    duplicate_target_type: str
    duplicate_target_id: str | None
    reason: str
    similarity: float
    candidate_id: int | None = None
    raw_ai_output: str | None = None

    def to_result(self) -> dict[str, Any]:
        return {
            "is_duplicate": self.is_duplicate,
            "duplicate_target_type": self.duplicate_target_type,
            "duplicate_target_id": self.duplicate_target_id,
            "reason": self.reason,
            "similarity": self.similarity,
            "candidate_id": self.candidate_id,
            "raw_ai_output": self.raw_ai_output,
        }


@dataclass(frozen=True, slots=True)
class SearchCacheMaintenanceResult:
    dry_run: bool
    deleted_documents: int
    deleted_embeddings: int
    converted_embeddings: int
    compacted: bool


def normalize_compare_url(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip().lower()
    text = COMPARE_URL_SUFFIX_PATTERN.sub("", text)
    return text.rstrip("/")


def normalize_title_key(value: str | None) -> str:
    if not value:
        return ""
    lowered = unicodedata.normalize("NFKC", value).strip().lower()
    normalized = TITLE_KEY_PATTERN.sub("", lowered)
    return normalized or re.sub(r"\s+", " ", lowered)


def exact_duplicate_decision(
    *,
    query: SearchDocument,
    documents: list[SearchDocument],
    target_type: str,
    compare_doc_id: bool = False,
) -> SearchDecision | None:
    query_url = normalize_compare_url(query.source_url)
    query_title = normalize_title_key(query.title)
    for document in documents:
        if query_url and normalize_compare_url(document.source_url) == query_url:
            return _duplicate_decision_for_document(
                document=document,
                target_type=target_type,
                similarity=1.0,
            )
        if compare_doc_id and query.doc_id and document.doc_id == query.doc_id:
            return _duplicate_decision_for_document(
                document=document,
                target_type=target_type,
                similarity=1.0,
            )
        if query_title and query_title == normalize_title_key(document.title):
            return _duplicate_decision_for_document(
                document=document,
                target_type=target_type,
                similarity=1.0,
            )
    return None


def _duplicate_decision_for_document(
    *,
    document: SearchDocument,
    target_type: str,
    similarity: float,
) -> SearchDecision:
    return SearchDecision(
        is_duplicate=True,
        duplicate_target_type=target_type,
        duplicate_target_id=document.doc_id,
        reason="same_event",
        similarity=similarity,
        candidate_id=document.candidate_id if target_type == "inflight_candidate" else None,
    )


class SearchCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_or_rebuild()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_or_rebuild(self) -> None:
        try:
            self._init_schema()
        except sqlite3.DatabaseError:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            self._init_schema()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS embeddings (
                    cache_key text PRIMARY KEY,
                    model text NOT NULL,
                    content_hash text NOT NULL,
                    vector_json text NOT NULL DEFAULT '',
                    vector_blob blob,
                    dimensions integer,
                    updated_at text NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    cache_key text PRIMARY KEY,
                    doc_type text NOT NULL,
                    doc_id text NOT NULL,
                    source text NOT NULL,
                    task_id integer,
                    candidate_id integer,
                    title text,
                    content text NOT NULL,
                    source_url text,
                    published_at text,
                    status text,
                    created_at text,
                    expires_at text,
                    metadata_json text NOT NULL,
                    content_hash text NOT NULL,
                    updated_at text NOT NULL
                )
                """
            )
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(embeddings)").fetchall()
            }
            if "vector_blob" not in existing_columns:
                conn.execute("ALTER TABLE embeddings ADD COLUMN vector_blob blob")
            if "dimensions" not in existing_columns:
                conn.execute("ALTER TABLE embeddings ADD COLUMN dimensions integer")
            existing_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(documents)").fetchall()
            }
            if "status" not in existing_columns:
                conn.execute("ALTER TABLE documents ADD COLUMN status text")
            if "created_at" not in existing_columns:
                conn.execute("ALTER TABLE documents ADD COLUMN created_at text")
            if "expires_at" not in existing_columns:
                conn.execute("ALTER TABLE documents ADD COLUMN expires_at text")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_search_documents_type ON documents(doc_type, source, doc_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_search_documents_status ON documents(doc_type, status, published_at)")
            conn.commit()

    def get_embedding(self, *, cache_key: str, model: str, text_hash: str) -> list[float] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT vector_json, vector_blob, dimensions
                FROM embeddings
                WHERE cache_key = ? AND model = ? AND content_hash = ?
                """,
                (cache_key, model, text_hash),
            ).fetchone()
        if row is None:
            return None
        if row["vector_blob"] is not None and row["dimensions"] is not None:
            return decode_embedding_blob(bytes(row["vector_blob"]), int(row["dimensions"]))
        return [float(value) for value in json.loads(str(row["vector_json"]))]

    def set_embedding(self, *, cache_key: str, model: str, text_hash: str, vector: list[float]) -> None:
        vector_blob = encode_embedding_blob(vector)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO embeddings (
                    cache_key, model, content_hash, vector_json, vector_blob, dimensions, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model = excluded.model,
                    content_hash = excluded.content_hash,
                    vector_json = excluded.vector_json,
                    vector_blob = excluded.vector_blob,
                    dimensions = excluded.dimensions,
                    updated_at = excluded.updated_at
                """,
                (cache_key, model, text_hash, "", vector_blob, len(vector), datetime.now(UTC).isoformat()),
            )
            conn.commit()

    def maintain(
        self,
        *,
        dry_run: bool = True,
        short_retention_days: int = 2,
        reference_retention_days: int = 8,
        convert_legacy: bool = True,
        compact: bool = False,
    ) -> SearchCacheMaintenanceResult:
        if short_retention_days < 1 or reference_retention_days < short_retention_days:
            raise ValueError("search cache retention requires reference days >= short days >= 1")
        now = datetime.now(UTC)
        short_cutoff = (now - timedelta(days=short_retention_days)).isoformat()
        reference_cutoff = (now - timedelta(days=reference_retention_days)).isoformat()
        stale_document_where = """
            (
                doc_type IN ('task', 'newsflash_item', 'editor_plugin_query', 'external_media_alert')
                AND julianday(COALESCE(published_at, created_at, updated_at)) < julianday(?)
            )
            OR (
                doc_type = 'recent_processed'
                AND julianday(COALESCE(expires_at, updated_at)) < julianday(?)
            )
            OR (
                doc_type = 'candidate'
                AND COALESCE(status, 'inactive') <> 'active'
                AND julianday(COALESCE(expires_at, updated_at)) < julianday(?)
            )
            OR (
                doc_type IN ('odaily_reference', 'external_media_alert_history')
                AND julianday(COALESCE(published_at, created_at, updated_at)) < julianday(?)
            )
            OR (
                doc_type NOT IN (
                    'task', 'newsflash_item', 'editor_plugin_query', 'external_media_alert',
                    'candidate', 'recent_processed', 'odaily_reference', 'external_media_alert_history'
                )
                AND julianday(updated_at) < julianday(?)
            )
        """
        document_params = (short_cutoff, now.isoformat(), short_cutoff, reference_cutoff, reference_cutoff)
        # Some producers intentionally use a query-specific cache key that differs
        # from the document key. Keep all vectors for the longest active search
        # window instead of assuming a join between both tables.
        stale_embedding_where = "julianday(updated_at) < julianday(?)"
        embedding_params = (reference_cutoff,)

        with self._connect() as conn:
            deleted_documents = int(
                conn.execute(
                    f"SELECT count(*) FROM documents WHERE {stale_document_where}",
                    document_params,
                ).fetchone()[0]
            )
            deleted_embeddings = int(
                conn.execute(
                    f"SELECT count(*) FROM embeddings WHERE {stale_embedding_where}",
                    embedding_params,
                ).fetchone()[0]
            )
            converted_embeddings = 0
            if convert_legacy:
                converted_embeddings = int(
                    conn.execute(
                        """
                        SELECT count(*)
                        FROM embeddings
                        WHERE vector_blob IS NULL
                          AND vector_json <> ''
                        """
                    ).fetchone()[0]
                )
            if dry_run:
                return SearchCacheMaintenanceResult(
                    dry_run=True,
                    deleted_documents=deleted_documents,
                    deleted_embeddings=deleted_embeddings,
                    converted_embeddings=converted_embeddings,
                    compacted=False,
                )

            conn.execute("BEGIN IMMEDIATE")
            document_cursor = conn.execute(
                f"DELETE FROM documents WHERE {stale_document_where}",
                document_params,
            )
            embedding_cursor = conn.execute(
                f"DELETE FROM embeddings WHERE {stale_embedding_where}",
                embedding_params,
            )
            converted_embeddings = self._convert_legacy_embeddings(conn) if convert_legacy else 0
            conn.commit()
            deleted_documents = int(document_cursor.rowcount or 0)
            deleted_embeddings = int(embedding_cursor.rowcount or 0)

        if compact:
            with self._connect() as conn:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.execute("VACUUM")
        return SearchCacheMaintenanceResult(
            dry_run=False,
            deleted_documents=deleted_documents,
            deleted_embeddings=deleted_embeddings,
            converted_embeddings=converted_embeddings,
            compacted=compact,
        )

    @staticmethod
    def _convert_legacy_embeddings(conn: sqlite3.Connection, *, batch_size: int = 250) -> int:
        converted = 0
        while True:
            rows = conn.execute(
                """
                SELECT cache_key, vector_json
                FROM embeddings
                WHERE vector_blob IS NULL
                  AND vector_json <> ''
                LIMIT ?
                """,
                (batch_size,),
            ).fetchall()
            if not rows:
                return converted
            updates: list[tuple[bytes, int, str]] = []
            for row in rows:
                vector = [float(value) for value in json.loads(str(row["vector_json"]))]
                updates.append((encode_embedding_blob(vector), len(vector), str(row["cache_key"])))
            conn.executemany(
                """
                UPDATE embeddings
                SET vector_json = '', vector_blob = ?, dimensions = ?
                WHERE cache_key = ?
                """,
                updates,
            )
            converted += len(updates)

    def upsert_document(self, document: SearchDocument) -> None:
        self.upsert_documents([document])

    def upsert_documents(self, documents: list[SearchDocument]) -> None:
        if not documents:
            return
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO documents (
                    cache_key, doc_type, doc_id, source, task_id, candidate_id, title, content,
                    source_url, published_at, status, created_at, expires_at, metadata_json, content_hash, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    doc_type = excluded.doc_type,
                    doc_id = excluded.doc_id,
                    source = excluded.source,
                    task_id = excluded.task_id,
                    candidate_id = excluded.candidate_id,
                    title = excluded.title,
                    content = excluded.content,
                    source_url = excluded.source_url,
                    published_at = excluded.published_at,
                    status = excluded.status,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json,
                    content_hash = excluded.content_hash,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        document_cache_key(document),
                        document.doc_type,
                        document.doc_id,
                        document.source,
                        document.task_id,
                        document.candidate_id,
                        document.title,
                        document.content,
                        document.source_url,
                        utc_iso(document.published_at),
                        document.status,
                        utc_iso(document.created_at),
                        utc_iso(document.expires_at),
                        json.dumps(document.metadata, ensure_ascii=False, sort_keys=True),
                        content_hash(document.embedding_text),
                        utc_iso(document.updated_at or now),
                    )
                    for document in documents
                ],
            )
            conn.commit()

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        return self._list_documents(
            """
            SELECT *
            FROM documents
            WHERE doc_type = 'odaily_reference'
              AND (published_at IS NULL OR julianday(published_at) >= julianday(?))
            ORDER BY published_at IS NULL ASC, julianday(published_at) DESC, julianday(updated_at) DESC
            """,
            (utc_iso(since),),
        )

    def list_active_candidate_documents(self) -> list[SearchDocument]:
        now = datetime.now(UTC)
        created_after = (now - ACTIVE_CANDIDATE_TTL).isoformat()
        return self._list_documents(
            """
            SELECT *
            FROM documents
            WHERE doc_type = 'candidate'
              AND COALESCE(status, 'active') = 'active'
              AND julianday(created_at) > julianday(?)
              AND julianday(expires_at) > julianday(?)
            ORDER BY julianday(updated_at) DESC
            """,
            (created_after, now.isoformat()),
        )

    def list_recent_processed_documents(self) -> list[SearchDocument]:
        now = datetime.now(UTC)
        return self._list_documents(
            """
            SELECT *
            FROM documents
            WHERE doc_type = 'recent_processed'
              AND COALESCE(status, 'active') = 'active'
              AND julianday(expires_at) > julianday(?)
            ORDER BY julianday(updated_at) DESC
            """,
            (now.isoformat(),),
        )

    def upsert_active_candidate(
        self,
        *,
        candidate_id: int,
        task_id: int,
        title: str | None,
        content: str,
        source: str,
        source_item_id: str,
        source_url: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.upsert_document(
            SearchDocument(
                doc_type="candidate",
                doc_id=str(candidate_id),
                title=title,
                content=content,
                source="candidate",
                source_url=source_url,
                task_id=task_id,
                candidate_id=candidate_id,
                status="active",
                created_at=now,
                updated_at=now,
                expires_at=now + ACTIVE_CANDIDATE_TTL,
                metadata={"source": source, "source_item_id": source_item_id, **(metadata or {})},
            )
        )

    def release_candidate_for_task(
        self,
        *,
        task_id: int,
        release_reason: str,
    ) -> None:
        now = datetime.now(UTC)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cache_key, metadata_json
                FROM documents
                WHERE doc_type = 'candidate'
                  AND task_id = ?
                  AND COALESCE(status, 'active') = 'active'
                """,
                (task_id,),
            ).fetchall()
            for row in rows:
                metadata = json.loads(str(row["metadata_json"])) if row["metadata_json"] else {}
                metadata.update(
                    {
                        "released_by_task_id": task_id,
                        "released_by_task_status": release_reason,
                    }
                )
                conn.execute(
                    """
                    UPDATE documents
                    SET status = 'inactive',
                        expires_at = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE cache_key = ?
                    """,
                    (
                        now.isoformat(),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        now.isoformat(),
                        row["cache_key"],
                    ),
                )
            conn.commit()

    def promote_candidate_for_task(
        self,
        *,
        task_id: int,
        retention_seconds: int,
        release_reason: str,
    ) -> None:
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=max(60, int(retention_seconds)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cache_key, metadata_json
                FROM documents
                WHERE doc_type = 'candidate'
                  AND task_id = ?
                  AND COALESCE(status, 'active') = 'active'
                """,
                (task_id,),
            ).fetchall()
            for row in rows:
                metadata = json.loads(str(row["metadata_json"])) if row["metadata_json"] else {}
                metadata.update(
                    {
                        "released_by_task_id": task_id,
                        "released_by_task_status": release_reason,
                        "recent_processed_expires_at": expires_at.isoformat(),
                    }
                )
                conn.execute(
                    """
                    UPDATE documents
                    SET doc_type = 'recent_processed',
                        status = 'active',
                        expires_at = ?,
                        metadata_json = ?,
                        updated_at = ?
                    WHERE cache_key = ?
                    """,
                    (
                        expires_at.isoformat(),
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                        now.isoformat(),
                        row["cache_key"],
                    ),
                )
            conn.commit()

    def prune_expired_candidates(self) -> int:
        now = datetime.now(UTC)
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE documents
                SET status = 'inactive',
                    updated_at = ?
                WHERE doc_type = 'candidate'
                  AND COALESCE(status, 'active') = 'active'
                  AND (
                    expires_at IS NULL
                    OR julianday(expires_at) <= julianday(?)
                    OR julianday(created_at) <= julianday(?)
                  )
                """,
                (
                    utc_iso(now),
                    utc_iso(now),
                    utc_iso(now - ACTIVE_CANDIDATE_TTL - timedelta(minutes=1)),
                ),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]:
        if since is None:
            return self._list_documents(
                """
                SELECT *
                FROM documents
                WHERE doc_type = 'external_media_alert_history'
                  AND COALESCE(status, 'notified') = 'notified'
                ORDER BY created_at IS NULL ASC, created_at DESC, updated_at DESC
                """,
                (),
            )
        return self._list_documents(
            """
            SELECT *
            FROM documents
            WHERE doc_type = 'external_media_alert_history'
              AND COALESCE(status, 'notified') = 'notified'
              AND (created_at IS NULL OR julianday(created_at) >= julianday(?))
            ORDER BY created_at IS NULL ASC, julianday(created_at) DESC, julianday(updated_at) DESC
            """,
            (utc_iso(since),),
        )

    def mark_document_status(
        self,
        *,
        cache_key: str,
        status: str,
        expires_at: datetime | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM documents WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
            if row is None:
                return
            metadata = json.loads(str(row["metadata_json"])) if row["metadata_json"] else {}
            if metadata_updates:
                metadata.update(metadata_updates)
            conn.execute(
                """
                UPDATE documents
                SET status = ?,
                    expires_at = ?,
                    metadata_json = ?,
                    updated_at = ?
                WHERE cache_key = ?
                """,
                (
                    status,
                    utc_iso(expires_at),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                    cache_key,
                ),
            )
            conn.commit()

    def _list_documents(self, sql: str, params: tuple[Any, ...]) -> list[SearchDocument]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_search_document(row) for row in rows]


class CachedEmbeddingService:
    def __init__(self, *, client: EmbeddingClient, cache: SearchCache) -> None:
        self.client = client
        self.cache = cache

    def embed_one(self, *, cache_key: str, text: str) -> list[float]:
        text_hash = content_hash(text)
        cached = self.cache.get_embedding(cache_key=cache_key, model=self.client.model, text_hash=text_hash)
        if cached is not None:
            return cached
        vector = self.client.embed([text])[0]
        self.cache.set_embedding(cache_key=cache_key, model=self.client.model, text_hash=text_hash, vector=vector)
        return vector

    def embed_documents(self, documents: list[SearchDocument]) -> list[tuple[SearchDocument, list[float]]]:
        self.cache.upsert_documents(documents)
        results: list[tuple[SearchDocument, list[float]]] = []
        missing: list[tuple[SearchDocument, str, str]] = []
        for document in documents:
            key = document_cache_key(document)
            text = document.embedding_text
            text_hash = content_hash(text)
            cached = self.cache.get_embedding(cache_key=key, model=self.client.model, text_hash=text_hash)
            if cached is None:
                missing.append((document, key, text_hash))
            else:
                results.append((document, cached))
        if missing:
            vectors = self.client.embed([document.embedding_text for document, _key, _hash in missing])
            for (document, key, text_hash), vector in zip(missing, vectors):
                self.cache.set_embedding(cache_key=key, model=self.client.model, text_hash=text_hash, vector=vector)
                results.append((document, vector))
        return results


def document_cache_key(document: SearchDocument) -> str:
    if document.doc_type == "candidate" and document.candidate_id is not None:
        return f"candidate:{document.candidate_id}"
    if document.task_id is not None:
        return f"task:{document.task_id}"
    if document.candidate_id is not None:
        return f"candidate:{document.candidate_id}"
    return f"{document.doc_type}:{document.source}:{document.doc_id}"


def _parse_dt(value: Any) -> datetime | None:
    text = str(value).strip() if value not in (None, "") else ""
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _row_to_search_document(row: sqlite3.Row) -> SearchDocument:
    metadata_json = str(row["metadata_json"]) if row["metadata_json"] is not None else "{}"
    return SearchDocument(
        doc_type=str(row["doc_type"]),
        doc_id=str(row["doc_id"]),
        title=row["title"],
        content=str(row["content"]),
        source=str(row["source"]),
        source_url=row["source_url"],
        task_id=row["task_id"],
        candidate_id=row["candidate_id"],
        published_at=_parse_dt(row["published_at"]),
        status=row["status"],
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
        expires_at=_parse_dt(row["expires_at"]),
        metadata=json.loads(metadata_json) if metadata_json else {},
    )


def top_match(query_vector: list[float], documents: list[tuple[SearchDocument, list[float]]]) -> SearchMatch | None:
    if np is not None and query_vector and documents:
        match = _top_match_numpy(query_vector, documents)
        if match is not None:
            return match
    best: SearchMatch | None = None
    for document, vector in documents:
        similarity = cosine_similarity(query_vector, vector)
        if best is None or similarity > best.similarity:
            best = SearchMatch(document=document, similarity=similarity)
    return best


def _top_match_numpy(
    query_vector: list[float],
    documents: list[tuple[SearchDocument, list[float]]],
) -> SearchMatch | None:
    dimensions = len(query_vector)
    if dimensions == 0:
        return None
    if any(len(vector) != dimensions for _document, vector in documents):
        return None
    try:
        query = np.asarray(query_vector, dtype=np.float64)
        matrix = np.asarray([vector for _document, vector in documents], dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        return None
    query_norm = float(np.linalg.norm(query))
    document_norms = np.linalg.norm(matrix, axis=1)
    denominator = document_norms * query_norm
    if query_norm == 0.0:
        return SearchMatch(document=documents[0][0], similarity=0.0)
    similarities = np.divide(
        matrix @ query,
        denominator,
        out=np.zeros_like(document_norms, dtype=np.float64),
        where=denominator != 0,
    )
    best_index = int(np.argmax(similarities))
    return SearchMatch(document=documents[best_index][0], similarity=float(similarities[best_index]))


AI_REVIEW_SCHEMA = {
    "type": "json_schema",
    "name": "search_duplicate_review",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_duplicate": {"type": "boolean"},
            "duplicate_target_type": {
                "type": "string",
                "enum": ["odaily_published", "inflight_candidate", "recent_processed", "none"],
            },
            "duplicate_target_id": {"type": "string"},
            "reason": {
                "type": "string",
                "enum": ["same_event", "same_topic_different_event", "update_of_existing_event", "unrelated"],
            },
        },
        "required": ["is_duplicate", "duplicate_target_type", "duplicate_target_id", "reason"],
    },
    "strict": True,
}


def build_ai_review_prompt(*, query: SearchDocument, match: SearchMatch) -> str:
    return (
        "duplicate_target_type may be recent_processed when the candidate was recently processed.\n"
        "你是 Odaily 快讯搜索者。判断两条材料是否是同一个新闻事件。\n"
        "同一事件要求主体、核心动作、关键结果基本一致；同一主体的新进展或不同动作不是重复。\n"
        "只输出 JSON，不输出解释。\n\n"
        "【新材料】\n"
        f"标题：{query.title or ''}\n"
        f"正文：{query.content}\n\n"
        "【候选材料】\n"
        f"类型：{match.document.doc_type}\n"
        f"ID：{match.document.doc_id}\n"
        f"标题：{match.document.title or ''}\n"
        f"正文：{match.document.content}\n"
        f"相似度：{match.similarity:.4f}\n\n"
        'JSON格式：{"is_duplicate":true|false,"duplicate_target_type":"odaily_published|inflight_candidate|none",'
        '"duplicate_target_id":"string","reason":"same_event|same_topic_different_event|update_of_existing_event|unrelated"}'
    )


def parse_ai_review_output(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("search AI output must be a JSON object")
    return payload
