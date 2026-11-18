from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Iterable

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.common.time_utils import utc_iso
from .matching import score_candidate
from .models import AnalysisResult, OdailyReference, Writer3Candidate
from .index import _loads_json, _parse_datetime


_SCHEMA = """
CREATE TABLE IF NOT EXISTS writer3_odaily_references(source_item_id text PRIMARY KEY,source_url text,title text,content text NOT NULL,published_at timestamptz,metadata_json jsonb NOT NULL,raw_payload_json jsonb NOT NULL,updated_at timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS idx_writer3_odaily_published ON writer3_odaily_references(published_at DESC);
CREATE TABLE IF NOT EXISTS writer3_telegram_confirmations(message_id bigint PRIMARY KEY,chat_id text NOT NULL,context_id bigint NOT NULL,current_source text NOT NULL,current_source_item_id text NOT NULL,current_message_text text NOT NULL,sent_at timestamptz NOT NULL,confirmed_at timestamptz,confirmed_by_id text,confirmed_by_username text,confirmed_by_name text,updated_at timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS idx_writer3_confirm_context ON writer3_telegram_confirmations(context_id);
"""


class PostgresWriter3Index:
    def __init__(self) -> None:
        from psycopg.types.json import Jsonb
        self._Jsonb = Jsonb
        self.initialize_schema()

    @staticmethod
    def initialize_schema() -> None:
        checksum=hashlib.sha256(_SCHEMA.encode()).hexdigest()
        with connect_postgres(schema="main",application_name="odaily-writer3-index-migrate") as conn:
            apply_schema_migrations(conn,schema="main",component="writer3_index",migrations=(PostgresMigration(1,"Writer3 index and Telegram confirmations",checksum,lambda c:c.execute(_SCHEMA)),))

    def upsert_references(self, references: Iterable[OdailyReference]) -> int:
        rows=list(references)
        with connect_postgres(schema="main",application_name="odaily-writer3-index-write") as conn:
            with conn.transaction():
                for item in rows:
                    conn.execute("""INSERT INTO writer3_odaily_references(source_item_id,source_url,title,content,published_at,metadata_json,raw_payload_json,updated_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_item_id) DO UPDATE SET source_url=excluded.source_url,title=excluded.title,content=excluded.content,published_at=excluded.published_at,metadata_json=excluded.metadata_json,raw_payload_json=excluded.raw_payload_json,updated_at=excluded.updated_at""",
                        (item.source_item_id,item.source_url,item.title,item.content,item.published_at,self._Jsonb(item.metadata),self._Jsonb(item.raw_payload),datetime.now(UTC)))
        return len(rows)

    def prune_before(self, cutoff: datetime) -> int:
        with connect_postgres(schema="main",application_name="odaily-writer3-index-prune") as conn:
            result=conn.execute("DELETE FROM writer3_odaily_references WHERE published_at IS NOT NULL AND published_at < %s",(cutoff,))
            return result.rowcount or 0

    def upsert_telegram_confirmation(self, *, message_id:int,chat_id:str,context_id:int,current_source:str,current_source_item_id:str,current_message_text:str,sent_at:datetime) -> None:
        with connect_postgres(schema="main",application_name="odaily-writer3-confirmation") as conn:
            conn.execute("""INSERT INTO writer3_telegram_confirmations(message_id,chat_id,context_id,current_source,current_source_item_id,current_message_text,sent_at,updated_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(message_id) DO UPDATE SET chat_id=excluded.chat_id,context_id=excluded.context_id,current_source=excluded.current_source,current_source_item_id=excluded.current_source_item_id,current_message_text=excluded.current_message_text,sent_at=excluded.sent_at,updated_at=excluded.updated_at""",
                (message_id,chat_id,context_id,current_source,current_source_item_id,current_message_text,sent_at,datetime.now(UTC)))

    def get_telegram_confirmation_by_message_id(self,message_id:int):
        with connect_postgres(schema="main",application_name="odaily-writer3-confirmation") as conn:
            row=conn.execute("SELECT message_id,chat_id,context_id,current_source,current_source_item_id,current_message_text,sent_at,confirmed_at,confirmed_by_id,confirmed_by_username,confirmed_by_name FROM writer3_telegram_confirmations WHERE message_id=%s",(message_id,)).fetchone()
        return dict(row) if row else None

    def confirm_telegram_message(self, *, message_id:int,confirmed_at:datetime,confirmed_by_id:str|None,confirmed_by_username:str|None,confirmed_by_name:str|None) -> bool:
        with connect_postgres(schema="main",application_name="odaily-writer3-confirmation") as conn:
            result=conn.execute("""UPDATE writer3_telegram_confirmations SET confirmed_at=COALESCE(confirmed_at,%s),confirmed_by_id=COALESCE(confirmed_by_id,%s),confirmed_by_username=COALESCE(confirmed_by_username,%s),confirmed_by_name=COALESCE(confirmed_by_name,%s),updated_at=%s WHERE message_id=%s""",
                (confirmed_at,confirmed_by_id,confirmed_by_username,confirmed_by_name,datetime.now(UTC),message_id))
            return bool(result.rowcount)

    def search(self, *, analysis:AnalysisResult,current_time:datetime|None,history_days:int,candidate_limit:int,exclude_source_item_id:str|None=None) -> list[Writer3Candidate]:
        upper=current_time or datetime.now(UTC); lower=upper-timedelta(days=history_days)
        with connect_postgres(schema="main",application_name="odaily-writer3-index-search") as conn:
            rows=conn.execute("SELECT * FROM writer3_odaily_references WHERE published_at >= %s AND published_at < %s ORDER BY published_at DESC",(lower,upper)).fetchall()
        references=[OdailyReference(source_item_id=str(r["source_item_id"]),source_url=r["source_url"],title=r["title"],content=str(r["content"]),published_at=r["published_at"],metadata=r["metadata_json"] or {},raw_payload=r["raw_payload_json"] or {}) for r in rows]
        scored=[]
        for reference in references:
            if exclude_source_item_id and reference.source_item_id==exclude_source_item_id: continue
            candidate=score_candidate(reference=reference,analysis=analysis,current_time=current_time)
            if candidate is not None: scored.append(candidate)
        scored.sort(key=lambda item:(item.score,item.published_at or datetime.min.replace(tzinfo=UTC)),reverse=True)
        return scored[:candidate_limit] if candidate_limit>0 else scored
