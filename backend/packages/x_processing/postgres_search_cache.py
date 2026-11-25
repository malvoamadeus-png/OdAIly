from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from .models import ACTIVE_CANDIDATE_TTL
from .searcher import (SearchCacheMaintenanceResult, SearchDocument, _row_to_search_document,
                       content_hash, decode_embedding_row, document_cache_key, encode_embedding_blob)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS x_search_embeddings(cache_key text PRIMARY KEY,model text NOT NULL,content_hash text NOT NULL,vector_json text NOT NULL DEFAULT '',vector_blob bytea,dimensions integer,updated_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS x_search_documents(cache_key text PRIMARY KEY,doc_type text NOT NULL,doc_id text NOT NULL,source text NOT NULL,task_id bigint,candidate_id bigint,title text,content text NOT NULL,source_url text,published_at timestamptz,status text,created_at timestamptz,expires_at timestamptz,metadata_json jsonb NOT NULL,content_hash text NOT NULL,updated_at timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS idx_x_search_documents_type ON x_search_documents(doc_type,source,doc_id);
CREATE INDEX IF NOT EXISTS idx_x_search_documents_status ON x_search_documents(doc_type,status,published_at);
"""


class PostgresSearchCache:
    def __init__(self) -> None:
        from psycopg.types.json import Jsonb
        self._Jsonb = Jsonb
        self.initialize_schema()

    def _connect(self, role="cache"):
        return connect_postgres(schema="main", application_name=f"odaily-search-{role}")

    @staticmethod
    def initialize_schema() -> None:
        checksum = hashlib.sha256(_SCHEMA.encode()).hexdigest()
        with connect_postgres(schema="main", application_name="odaily-search-migrate") as conn:
            apply_schema_migrations(conn, schema="main", component="x_search_cache",
                migrations=(PostgresMigration(1, "X processing search cache", checksum,
                    lambda connection: connection.execute(_SCHEMA)),))

    def get_embedding(self, *, cache_key: str, model: str, text_hash: str) -> list[float] | None:
        with self._connect("embedding-read") as conn:
            row = conn.execute(
                "SELECT vector_json,vector_blob,dimensions FROM x_search_embeddings WHERE cache_key=%s AND model=%s AND content_hash=%s",
                (cache_key, model, text_hash),
            ).fetchone()
        if row is None:
            return None
        return decode_embedding_row(row)

    def get_embeddings(self, items: Sequence[tuple[str, str, str]]) -> dict[tuple[str, str, str], list[float]]:
        """Batch-fetch cached embedding vectors in a single PK join query."""
        unique_items = list(dict.fromkeys(items))
        if not unique_items:
            return {}
        cache_keys = [item[0] for item in unique_items]
        models = [item[1] for item in unique_items]
        hashes = [item[2] for item in unique_items]
        with self._connect("embedding-read") as conn:
            rows = conn.execute(
                """
                SELECT e.cache_key, e.model, e.content_hash, e.vector_json, e.vector_blob, e.dimensions
                FROM x_search_embeddings AS e
                JOIN unnest(%s::text[], %s::text[], %s::text[]) AS q(cache_key, model, content_hash)
                  ON e.cache_key = q.cache_key
                 AND e.model = q.model
                 AND e.content_hash = q.content_hash
                """,
                (cache_keys, models, hashes),
            ).fetchall()
        return {
            (str(row["cache_key"]), str(row["model"]), str(row["content_hash"])): decode_embedding_row(row)
            for row in rows
        }

    def set_embedding(self, *, cache_key: str, model: str, text_hash: str, vector: list[float]) -> None:
        self.set_embeddings([(cache_key, model, text_hash, vector)])

    def set_embeddings(self, items: Sequence[tuple[str, str, str, list[float]]]) -> None:
        """Batch-upsert embedding vectors in one SQL statement."""
        deduped = {
            cache_key: (cache_key, model, text_hash, vector)
            for cache_key, model, text_hash, vector in items
        }
        if not deduped:
            return
        now = datetime.now(UTC)
        entries = list(deduped.values())
        cache_keys = [entry[0] for entry in entries]
        models = [entry[1] for entry in entries]
        hashes = [entry[2] for entry in entries]
        blobs = [encode_embedding_blob(entry[3]) for entry in entries]
        dimensions = [len(entry[3]) for entry in entries]
        updated_at = [now] * len(entries)
        with self._connect("embedding-write") as conn:
            conn.execute(
                """
                INSERT INTO x_search_embeddings(cache_key,model,content_hash,vector_json,vector_blob,dimensions,updated_at)
                SELECT q.cache_key,q.model,q.content_hash,'',q.vector_blob,q.dimensions,q.updated_at
                FROM unnest(%s::text[],%s::text[],%s::text[],%s::bytea[],%s::integer[],%s::timestamptz[])
                  AS q(cache_key,model,content_hash,vector_blob,dimensions,updated_at)
                ON CONFLICT(cache_key) DO UPDATE SET
                    model=excluded.model,
                    content_hash=excluded.content_hash,
                    vector_json='',
                    vector_blob=excluded.vector_blob,
                    dimensions=excluded.dimensions,
                    updated_at=excluded.updated_at
                """,
                (cache_keys, models, hashes, blobs, dimensions, updated_at),
            )

    def upsert_document(self, document: SearchDocument) -> None: self.upsert_documents([document])

    def upsert_documents(self, documents: list[SearchDocument]) -> None:
        if not documents:
            return
        now = datetime.now(UTC)
        deduped = {document_cache_key(doc): doc for doc in documents}
        keys = list(deduped.keys())
        entries = list(deduped.values())
        with self._connect("document-write") as conn:
            conn.execute(
                """
                INSERT INTO x_search_documents(
                    cache_key,doc_type,doc_id,source,task_id,candidate_id,title,content,
                    source_url,published_at,status,created_at,expires_at,metadata_json,content_hash,updated_at
                )
                SELECT
                    q.cache_key,q.doc_type,q.doc_id,q.source,q.task_id,q.candidate_id,q.title,q.content,
                    q.source_url,q.published_at,q.status,q.created_at,q.expires_at,q.metadata_text::jsonb,q.content_hash,q.updated_at
                FROM unnest(
                    %s::text[],%s::text[],%s::text[],%s::text[],%s::bigint[],%s::bigint[],%s::text[],%s::text[],
                    %s::text[],%s::timestamptz[],%s::text[],%s::timestamptz[],%s::timestamptz[],%s::text[],%s::text[],%s::timestamptz[]
                ) AS q(
                    cache_key,doc_type,doc_id,source,task_id,candidate_id,title,content,
                    source_url,published_at,status,created_at,expires_at,metadata_text,content_hash,updated_at
                )
                ON CONFLICT(cache_key) DO UPDATE SET
                    doc_type=excluded.doc_type,
                    doc_id=excluded.doc_id,
                    source=excluded.source,
                    task_id=excluded.task_id,
                    candidate_id=excluded.candidate_id,
                    title=excluded.title,
                    content=excluded.content,
                    source_url=excluded.source_url,
                    published_at=excluded.published_at,
                    status=excluded.status,
                    created_at=excluded.created_at,
                    expires_at=excluded.expires_at,
                    metadata_json=excluded.metadata_json,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at
                WHERE (
                    x_search_documents.doc_type,
                    x_search_documents.doc_id,
                    x_search_documents.source,
                    x_search_documents.task_id,
                    x_search_documents.candidate_id,
                    x_search_documents.title,
                    x_search_documents.content,
                    x_search_documents.source_url,
                    x_search_documents.published_at,
                    x_search_documents.status,
                    x_search_documents.created_at,
                    x_search_documents.expires_at,
                    x_search_documents.metadata_json,
                    x_search_documents.content_hash
                ) IS DISTINCT FROM (
                    excluded.doc_type,
                    excluded.doc_id,
                    excluded.source,
                    excluded.task_id,
                    excluded.candidate_id,
                    excluded.title,
                    excluded.content,
                    excluded.source_url,
                    excluded.published_at,
                    excluded.status,
                    excluded.created_at,
                    excluded.expires_at,
                    excluded.metadata_json,
                    excluded.content_hash
                )
                """,
                (
                    keys,
                    [doc.doc_type for doc in entries],
                    [doc.doc_id for doc in entries],
                    [doc.source for doc in entries],
                    [doc.task_id for doc in entries],
                    [doc.candidate_id for doc in entries],
                    [doc.title for doc in entries],
                    [doc.content for doc in entries],
                    [doc.source_url for doc in entries],
                    [doc.published_at for doc in entries],
                    [doc.status for doc in entries],
                    [doc.created_at for doc in entries],
                    [doc.expires_at for doc in entries],
                    [json.dumps(doc.metadata, ensure_ascii=False, sort_keys=True) for doc in entries],
                    [content_hash(doc.embedding_text) for doc in entries],
                    [doc.updated_at or now for doc in entries],
                ),
            )

    def _list(self, sql: str, params: tuple[Any,...] = ()) -> list[SearchDocument]:
        with self._connect("document-read") as conn: rows=conn.execute(sql,params).fetchall()
        return [_row_to_search_document(row) for row in rows]

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        return self._list("SELECT * FROM x_search_documents WHERE doc_type='odaily_reference' AND (published_at IS NULL OR published_at >= %s) ORDER BY published_at DESC NULLS LAST,updated_at DESC",(since,))

    def list_active_candidate_documents(self) -> list[SearchDocument]:
        now=datetime.now(UTC)
        return self._list("SELECT * FROM x_search_documents WHERE doc_type='candidate' AND COALESCE(status,'active')='active' AND created_at>%s AND expires_at>%s ORDER BY updated_at DESC",(now-ACTIVE_CANDIDATE_TTL,now))

    def list_recent_processed_documents(self) -> list[SearchDocument]:
        now=datetime.now(UTC)
        return self._list("SELECT * FROM x_search_documents WHERE doc_type='recent_processed' AND COALESCE(status,'active')='active' AND expires_at>%s ORDER BY updated_at DESC",(now,))

    def list_notified_alert_documents(self, *, since: datetime | None = None) -> list[SearchDocument]:
        sql="SELECT * FROM x_search_documents WHERE doc_type='external_media_alert_history' AND COALESCE(status,'notified')='notified'"
        params=()
        if since is not None: sql += " AND (created_at IS NULL OR created_at >= %s)"; params=(since,)
        return self._list(sql+" ORDER BY created_at DESC NULLS LAST,updated_at DESC",params)

    def upsert_active_candidate(self, *, candidate_id: int, task_id: int, title: str | None, content: str, source: str, source_item_id: str, source_url: str | None, metadata: dict[str,Any] | None = None) -> None:
        now=datetime.now(UTC)
        self.upsert_document(SearchDocument("candidate",str(candidate_id),title,content,"candidate",source_url,task_id,candidate_id,status="active",created_at=now,updated_at=now,expires_at=now+ACTIVE_CANDIDATE_TTL,metadata={"source":source,"source_item_id":source_item_id,**(metadata or {})}))

    def _change_candidate(self, task_id: int, *, doc_type: str, status: str, expires_at: datetime, release_reason: str) -> None:
        now=datetime.now(UTC)
        with self._connect("candidate-update") as conn:
            with conn.transaction():
                rows=conn.execute("SELECT cache_key,metadata_json FROM x_search_documents WHERE doc_type='candidate' AND task_id=%s AND COALESCE(status,'active')='active' FOR UPDATE",(task_id,)).fetchall()
                for row in rows:
                    metadata=row["metadata_json"] or {}; metadata={**metadata,"released_by_task_id":task_id,"released_by_task_status":release_reason}
                    if doc_type=='recent_processed': metadata["recent_processed_expires_at"]=expires_at.isoformat()
                    conn.execute("UPDATE x_search_documents SET doc_type=%s,status=%s,expires_at=%s,metadata_json=%s,updated_at=%s WHERE cache_key=%s",(doc_type,status,expires_at,self._Jsonb(metadata),now,row["cache_key"]))

    def release_candidate_for_task(self, *, task_id: int, release_reason: str) -> None:
        now=datetime.now(UTC); self._change_candidate(task_id,doc_type='candidate',status='inactive',expires_at=now,release_reason=release_reason)

    def promote_candidate_for_task(self, *, task_id: int, retention_seconds: int, release_reason: str) -> None:
        now=datetime.now(UTC); self._change_candidate(task_id,doc_type='recent_processed',status='active',expires_at=now+timedelta(seconds=max(60,int(retention_seconds))),release_reason=release_reason)

    def prune_expired_candidates(self) -> int:
        now=datetime.now(UTC)
        with self._connect("candidate-prune") as conn:
            result=conn.execute("UPDATE x_search_documents SET status='inactive',updated_at=%s WHERE doc_type='candidate' AND COALESCE(status,'active')='active' AND (expires_at IS NULL OR expires_at<=%s OR created_at<=%s)",(now,now,now-ACTIVE_CANDIDATE_TTL-timedelta(minutes=1)))
            return result.rowcount or 0

    def mark_document_status(self, *, cache_key: str, status: str, expires_at: datetime | None = None, metadata_updates: dict[str,Any] | None = None) -> None:
        with self._connect("document-status") as conn:
            with conn.transaction():
                row=conn.execute("SELECT metadata_json FROM x_search_documents WHERE cache_key=%s FOR UPDATE",(cache_key,)).fetchone()
                if not row:return
                metadata={**(row["metadata_json"] or {}),**(metadata_updates or {})}
                conn.execute("UPDATE x_search_documents SET status=%s,expires_at=%s,metadata_json=%s,updated_at=%s WHERE cache_key=%s",(status,expires_at,self._Jsonb(metadata),datetime.now(UTC),cache_key))

    def maintain(self, *, dry_run=True, short_retention_days=2, reference_retention_days=8, convert_legacy=True, compact=False) -> SearchCacheMaintenanceResult:
        if short_retention_days<1 or reference_retention_days<short_retention_days: raise ValueError("search cache retention requires reference days >= short days >= 1")
        now=datetime.now(UTC); short=now-timedelta(days=short_retention_days); ref=now-timedelta(days=reference_retention_days)
        stale="""((doc_type IN ('task','newsflash_item','editor_plugin_query','external_media_alert') AND COALESCE(published_at,created_at,updated_at)<%s) OR (doc_type='recent_processed' AND COALESCE(expires_at,updated_at)<%s) OR (doc_type='candidate' AND COALESCE(status,'inactive')<>'active' AND COALESCE(expires_at,updated_at)<%s) OR (doc_type IN ('odaily_reference','external_media_alert_history') AND COALESCE(published_at,created_at,updated_at)<%s) OR (doc_type NOT IN ('task','newsflash_item','editor_plugin_query','external_media_alert','candidate','recent_processed','odaily_reference','external_media_alert_history') AND updated_at<%s))"""
        params=(short,now,short,ref,ref)
        with self._connect("maintenance") as conn:
            docs=conn.execute(f"SELECT count(*) n FROM x_search_documents WHERE {stale}",params).fetchone()["n"]
            embeds=conn.execute("SELECT count(*) n FROM x_search_embeddings WHERE updated_at<%s",(ref,)).fetchone()["n"]
            converted=conn.execute("SELECT count(*) n FROM x_search_embeddings WHERE vector_blob IS NULL AND vector_json<>''").fetchone()["n"] if convert_legacy else 0
            if dry_run:return SearchCacheMaintenanceResult(True,int(docs),int(embeds),int(converted),False)
            with conn.transaction():
                d=conn.execute(f"DELETE FROM x_search_documents WHERE {stale}",params).rowcount or 0
                e=conn.execute("DELETE FROM x_search_embeddings WHERE updated_at<%s",(ref,)).rowcount or 0
                rows=conn.execute("SELECT cache_key,vector_json FROM x_search_embeddings WHERE vector_blob IS NULL AND vector_json<>'' FOR UPDATE").fetchall() if convert_legacy else []
                for row in rows:
                    vector=[float(v) for v in json.loads(row["vector_json"])]
                    conn.execute("UPDATE x_search_embeddings SET vector_json='',vector_blob=%s,dimensions=%s WHERE cache_key=%s",(encode_embedding_blob(vector),len(vector),row["cache_key"]))
        return SearchCacheMaintenanceResult(False,int(d),int(e),len(rows),False)
