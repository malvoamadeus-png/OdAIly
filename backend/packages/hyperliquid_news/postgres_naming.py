from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction
from .postgres_store import address_not_blocked_sql

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hl_news_address_names (
    address text PRIMARY KEY,current_name text,kind text NOT NULL DEFAULT 'stable',coin text,side text,
    status text NOT NULL DEFAULT 'active',manual_locked boolean NOT NULL DEFAULT false,
    evidence jsonb NOT NULL DEFAULT '{}'::jsonb,coverage jsonb NOT NULL DEFAULT '{}'::jsonb,
    reason text NOT NULL DEFAULT '',model text,checked_ms bigint NOT NULL DEFAULT 0,
    next_check_ms bigint NOT NULL DEFAULT 0,inflight_token text,inflight_started_ms bigint,
    last_error text,retry_after_ms bigint NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS hl_news_address_names_due ON hl_news_address_names(manual_locked,next_check_ms,retry_after_ms);
"""
SCHEMA_CHECKSUM = sha256(SCHEMA_SQL.encode()).hexdigest()


CLEAR_GENERIC_SQL = """
UPDATE hl_news_address_names SET current_name=NULL WHERE manual_locked=false AND current_name LIKE '普通地址%';
DELETE FROM hl_news_metadata WHERE key LIKE 'address_note:%' AND payload->>'origin'='auto' AND payload->>'note' LIKE '普通地址%';
"""
CLEAR_GENERIC_CHECKSUM = sha256(CLEAR_GENERIC_SQL.encode()).hexdigest()


class PostgresNamingStore:
    def __init__(self, news_store) -> None:
        self.store = news_store

    def _connect(self):
        return self.store._connect()

    def initialize(self) -> None:
        self.initialize_schema()

    @staticmethod
    def initialize_schema() -> None:
        with connect_postgres(schema="hyperliquid_news", application_name="odaily-hyperliquid-naming-migrate") as conn:
            apply_schema_migrations(conn, schema="hyperliquid_news", component="hyperliquid_naming",
                migrations=(
                    PostgresMigration(1,"hyperliquid_naming_initial",SCHEMA_CHECKSUM,
                        lambda connection: connection.execute(SCHEMA_SQL)),
                    PostgresMigration(2,"hyperliquid_naming_clear_generic_prefix",CLEAR_GENERIC_CHECKSUM,
                        lambda connection: connection.execute(CLEAR_GENERIC_SQL)),
                ))

    @staticmethod
    def _now() -> int:
        return int(datetime.now(UTC).timestamp()*1000)

    @staticmethod
    def _json(value: Any) -> Any:
        if isinstance(value,str):
            try:
                return json.loads(value)
            except (TypeError,ValueError):
                return {}
        return value if isinstance(value,dict) else {}

    @staticmethod
    def _unique_name(conn,address: str,name: str) -> str:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('hyperliquid-address-name-allocation',0))")
        feature=re.sub(r"0x[0-9a-f]+$","",name,flags=re.I)
        length=4
        while conn.execute("SELECT 1 FROM hl_news_address_names WHERE current_name=%s AND address<>%s",(name,address)).fetchone():
            length+=1
            name=feature+address[:length]
        return name

    def sync_missing_addresses(self) -> None:
        with self._connect() as conn:
            self._sync_missing_addresses_in_tx(conn)

    @staticmethod
    def _sync_missing_addresses_in_tx(conn) -> None:
        conn.execute(
            """INSERT INTO hl_news_address_names(address)
            SELECT a.address FROM hl_news_addresses a
            WHERE NOT EXISTS (SELECT 1 FROM hl_news_address_names n WHERE n.address=a.address)
            ON CONFLICT(address) DO NOTHING"""
        )

    def claim_due(self, *, now_ms: int, limit: int=100) -> list[dict[str,Any]]:
        token=uuid.uuid4().hex
        blocked=address_not_blocked_sql("n.address")
        with self._connect() as conn:
            self._sync_missing_addresses_in_tx(conn)
            conn.execute("UPDATE hl_news_address_names SET inflight_token=NULL,inflight_started_ms=NULL WHERE inflight_started_ms IS NOT NULL AND inflight_started_ms<%s",(now_ms-3600000,))
            rows=conn.execute(f"""SELECT n.* FROM hl_news_address_names n JOIN hl_news_addresses a USING(address)
                WHERE n.manual_locked=false AND n.status<>'blocked' AND n.inflight_token IS NULL AND n.next_check_ms<=%s AND n.retry_after_ms<=%s
                AND {blocked}
                AND NOT EXISTS (
                    SELECT 1 FROM hl_news_account_pool p WHERE p.address=n.address AND (
                        p.tier='non_perp' OR (
                            p.qualified=false
                            AND EXISTS (
                                SELECT 1 FROM hl_news_normal_state ns WHERE ns.address=n.address
                                AND COALESCE((ns.history->>'fills')::bigint,0)=0
                                AND COALESCE(ns.state->'positions','{{}}'::jsonb)='{{}}'::jsonb
                            )
                            AND NOT EXISTS (SELECT 1 FROM hl_news_fills f WHERE f.address=n.address)
                            AND NOT EXISTS (SELECT 1 FROM hl_news_compact_history ch WHERE ch.address=n.address)
                            AND NOT EXISTS (SELECT 1 FROM hl_news_candidate_addresses ca WHERE ca.address_lower=n.address)
                            AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(a.sources) s WHERE s->>'kind' IN ('manual','large_observed_trade'))
                        )
                    )
                )
                ORDER BY n.next_check_ms,n.address FOR UPDATE OF n SKIP LOCKED LIMIT %s""",(now_ms,now_ms,max(1,min(limit,20)))).fetchall()
            addresses=[row["address"] for row in rows]
            if addresses:
                conn.execute("UPDATE hl_news_address_names SET inflight_token=%s,inflight_started_ms=%s WHERE address=ANY(%s) AND manual_locked=false AND inflight_token IS NULL",(token,now_ms,addresses))
            return [{**dict(row),"claim_token":token} for row in rows]

    def renew_claim(self,address: str,*,token: str,now_ms: int)->bool:
        with self._connect() as conn:
            result=conn.execute("UPDATE hl_news_address_names SET inflight_started_ms=%s WHERE address=%s AND inflight_token=%s AND manual_locked=false",(now_ms,address.lower(),token))
            return result.rowcount==1

    def save_result(self,address: str,*,token: str,result: dict[str,Any],now_ms: int,model: str,error: str|None=None)->bool:
        from .naming import POSITION_MAX_AGE_MS, REVIEW_MS, is_generic_name
        from .client import address_key
        address=address_key(address)
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM hl_news_address_names WHERE address=%s AND inflight_token=%s AND manual_locked=false FOR UPDATE",(address,token)).fetchone()
            if row is None:
                return False
            if error:
                delay=min(REVIEW_MS,3600000)
                result=conn.execute("UPDATE hl_news_address_names SET inflight_token=NULL,inflight_started_ms=NULL,last_error=%s,retry_after_ms=%s,next_check_ms=%s WHERE address=%s AND inflight_token=%s AND manual_locked=false",(str(error)[:2000],now_ms+delay,now_ms+delay,address,token))
                return result.rowcount==1
            raw_name=str(result.get("name") or "").strip()[:24]
            selected=result.get("selected_dimension")
            kind=result.get("kind") if result.get("kind") in ("stable","position") else "stable"
            coin=str(result.get("coin") or "").strip() or None
            side=result.get("side") if result.get("side") in ("long","short") else None
            if kind=="stable":
                coin=side=None
            if kind=="position":
                snapshots=[]
                for table in ("hl_news_normal_state","hl_news_state"):
                    state=conn.execute(f"SELECT observed_ms,state FROM {table} WHERE address=%s",(address,)).fetchone()
                    if state:
                        snapshots.append((int(state["observed_ms"]),self._json(state["state"]).get("positions") or {}))
                if not snapshots:
                    raise ValueError("No saved position snapshot")
                latest_ms,positions=max(snapshots,key=lambda item:item[0])
                if not 0<=now_ms-latest_ms<=POSITION_MAX_AGE_MS:
                    raise ValueError("Position snapshot is stale")
                position=positions.get(coin)
                if not position or "size" not in position or float(position["size"])==0 or ((float(position["size"])>0)!=(side=="long")):
                    conn.execute("UPDATE hl_news_address_names SET inflight_token=NULL,inflight_started_ms=NULL,next_check_ms=%s,retry_after_ms=0,last_error='Position changed while naming' WHERE address=%s AND inflight_token=%s AND manual_locked=false",(now_ms,address,token))
                    return False
            if is_generic_name(raw_name, selected_dimension=selected):
                name=None
            else:
                name=self._unique_name(conn,address,raw_name)
            if self.store.blocked(address,conn=conn):
                return False
            refs=result.get("evidence_refs",[])
            evidence_ids={item.get("evidence_id") for item in result.get("coverage",{}).get("dimensions",[])}
            if selected not in evidence_ids or any(ref not in evidence_ids for ref in refs):
                return False
            if kind=="position":
                selected_dimension=next((item for item in result["coverage"]["dimensions"] if item.get("evidence_id")==selected),{})
                if selected_dimension.get("key",selected)!="current_positions":
                    return False
            saved=conn.execute("""UPDATE hl_news_address_names SET current_name=%s,kind=%s,coin=%s,side=%s,status='active',
                evidence=%s,coverage=%s,reason=%s,model=%s,checked_ms=%s,next_check_ms=%s,inflight_token=NULL,
                inflight_started_ms=NULL,last_error=NULL,retry_after_ms=0 WHERE address=%s AND inflight_token=%s AND manual_locked=false""",
                (name,kind,coin,side,json.dumps({"selected_dimension":selected,"evidence_refs":refs}),json.dumps(result.get("coverage",{})),str(result.get("reason") or "")[:240],model,now_ms,now_ms+REVIEW_MS,address,token))
            if saved.rowcount==1:
                manual=self.store._metadata_in_transaction(conn,"address_note:"+address,{})
                if manual.get("origin")!="manual":
                    if name:
                        self.store._save_metadata_in_transaction(conn, "address_note:"+address, json.dumps({"address":address,"note":name,"origin":"auto","updated_at":datetime.fromtimestamp(now_ms/1000,UTC).isoformat()}))
                    else:
                        conn.execute("DELETE FROM hl_news_metadata WHERE key=%s AND payload->>'origin'='auto'",("address_note:"+address,))
                        if manual.get("origin")=="auto":
                            conn.execute("DELETE FROM hl_news_metadata_large WHERE key=%s",("address_note:"+address,))
            return saved.rowcount==1

    def set_manual(self,address: str,note: str,*,updated_by: str)->dict[str,Any]:
        from .client import address_key
        address=address_key(address)
        if not isinstance(note,str) or len(note.strip())>80 or any(c in note for c in "\n\r（）()"):
            raise ValueError("Note must be at most 80 characters without newlines or parentheses")
        note=note.strip(); now=datetime.now(UTC); now_ms=int(now.timestamp()*1000)
        with self._connect() as conn:
            if not conn.execute("SELECT 1 FROM hl_news_addresses WHERE address=%s",(address,)).fetchone():
                raise ValueError("Unknown monitored address")
            value={"address":address,"note":note,"origin":"manual","updated_by":updated_by,"updated_at":now.isoformat()}
            self.store._save_metadata_in_transaction(conn, "address_note:"+address, json.dumps(value))
            conn.execute("""INSERT INTO hl_news_address_names(
                address,current_name,kind,coin,side,status,manual_locked,evidence,coverage,reason,model,checked_ms,next_check_ms,inflight_token,inflight_started_ms,last_error,retry_after_ms
            ) VALUES(%s,%s,'stable',NULL,NULL,'active',true,'{}'::jsonb,'{"manual_note": true}'::jsonb,'人工备注，程序不覆盖',NULL,0,%s,NULL,NULL,NULL,0)
            ON CONFLICT(address) DO UPDATE SET
                current_name=excluded.current_name,
                kind='stable',
                coin=NULL,
                side=NULL,
                status='active',
                manual_locked=true,
                evidence='{}'::jsonb,
                coverage='{"manual_note": true}'::jsonb,
                reason='人工备注，程序不覆盖',
                model=NULL,
                checked_ms=0,
                next_check_ms=excluded.next_check_ms,
                inflight_token=NULL,
                inflight_started_ms=NULL,
                last_error=NULL,
                retry_after_ms=0""", (address, note or None, now_ms + 7 * 24 * 60 * 60 * 1000))
        return value
    def invalidate_position(self,address: str,*,coin: str|None=None,side: str|None=None,reason: str="position_evidence_invalidated",now_ms: int|None=None)->int:
        from .client import address_key
        address=address_key(address);now_ms=now_ms or self._now()
        clauses=["address=%s","manual_locked=false","kind='position'"];params=[now_ms,reason,address]
        if coin: clauses.append("coin=%s");params.append(coin)
        if side: clauses.append("side=%s");params.append(side)
        with self._connect() as conn:
            result=conn.execute("UPDATE hl_news_address_names SET status='expired',current_name=NULL,next_check_ms=%s,inflight_token=NULL,inflight_started_ms=NULL,reason=%s WHERE "+" AND ".join(clauses),params)
            if result.rowcount:
                conn.execute("DELETE FROM hl_news_metadata WHERE key=%s AND payload->>'origin'='auto'",("address_note:"+address,))
                conn.execute("DELETE FROM hl_news_metadata_large WHERE key=%s", ("address_note:"+address,))
            return result.rowcount or 0

    def get(self,address: str)->dict[str,Any]|None:
        from .naming import fallback_name
        from .client import address_key
        with self._connect() as conn:
            row=conn.execute("SELECT * FROM hl_news_address_names WHERE address=%s",(address_key(address),)).fetchone()
        if not row: return None
        value=dict(row);value["manual_locked"]=bool(value["manual_locked"]);value["display_name"]=value.get("current_name") or fallback_name(value["address"])
        return value
