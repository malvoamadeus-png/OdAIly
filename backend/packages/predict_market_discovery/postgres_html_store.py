from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction

from .html_scan import _time, daily_filter_reason


_SCHEMA = """
CREATE TABLE IF NOT EXISTS predict_html_feed_baselines(feed text PRIMARY KEY,initialized_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS predict_html_topics (
    category_id text PRIMARY KEY,slug text NOT NULL,title text NOT NULL,created_at text,starts_at text,
    first_seen_at timestamptz NOT NULL,first_feed text NOT NULL,status text NOT NULL,reason text,
    raw_json jsonb NOT NULL,detail_json jsonb,writer_json jsonb,final_title text,final_content text,
    push_result jsonb,last_error text,updated_at timestamptz,claim_token text,claim_until timestamptz,delivery_started_at timestamptz
);
CREATE TABLE IF NOT EXISTS predict_html_scan_runs (
    id bigserial PRIMARY KEY,feed text NOT NULL,scanned_at timestamptz NOT NULL,status text NOT NULL,
    baseline boolean NOT NULL DEFAULT false,total_count integer,returned_count integer,has_next_page boolean,
    snapshot_ms bigint,new_count integer NOT NULL DEFAULT 0,error text
);
CREATE INDEX IF NOT EXISTS idx_predict_html_pending ON predict_html_topics(first_seen_at) WHERE status IN ('pending','retry_wait');
"""


class PostgresHtmlDiscoveryStore:
    def __init__(self) -> None:
        from psycopg.types.json import Jsonb
        self._Jsonb = Jsonb
        self._claims: dict[str, str] = {}
        self.initialize_schema()

    def _connect(self, name: str):
        return connect_postgres(schema="main", application_name=f"odaily-predict-html-{name}")

    @classmethod
    def initialize_schema(cls) -> None:
        checksum = hashlib.sha256(_SCHEMA.encode()).hexdigest()
        lease_checksum = hashlib.sha256(b"predict html topic claims v2").hexdigest()
        def add_leases(conn):
            conn.execute("ALTER TABLE predict_html_topics ADD COLUMN IF NOT EXISTS claim_token text")
            conn.execute("ALTER TABLE predict_html_topics ADD COLUMN IF NOT EXISTS claim_until timestamptz")
            conn.execute("ALTER TABLE predict_html_topics ADD COLUMN IF NOT EXISTS delivery_started_at timestamptz")
        with connect_postgres(schema="main", application_name="odaily-predict-html-migrate") as conn:
            apply_schema_migrations(conn, schema="main", component="predict_html",
                migrations=(PostgresMigration(1, "Predict HTML discovery", checksum,
                    lambda connection: connection.execute(_SCHEMA)),
                    PostgresMigration(2, "Predict HTML bounded claims", lease_checksum, add_leases)))

    def initialize(self) -> None:
        self.initialize_schema()

    def record_feed(self, feed: str, result: dict[str, Any], now: datetime) -> dict[str, Any]:
        now = now.astimezone(UTC)
        new_count = 0
        with self._connect("scan") as conn:
            with postgres_transaction(conn):
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("predict-html-baseline:" + feed,))
                baseline = conn.execute("SELECT 1 FROM predict_html_feed_baselines WHERE feed=%s", (feed,)).fetchone() is None
                for node in result["nodes"]:
                    created = _time(node.get("createdAt"))
                    if baseline:
                        status, reason = "baseline", "首次成功扫描"
                    elif created is None or created > now:
                        status, reason = "invalid_time", "创建时间缺失或晚于扫描时间"
                    elif created < now - timedelta(hours=48):
                        status, reason = "too_old", "类目创建超过48小时"
                    elif daily_filter_reason(node):
                        status, reason = "filtered", daily_filter_reason(node)
                    else:
                        status, reason = "pending", None
                    cur = conn.execute("""INSERT INTO predict_html_topics
                        (category_id,slug,title,created_at,starts_at,first_seen_at,first_feed,status,reason,raw_json)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(category_id) DO NOTHING""",
                        (str(node["id"]), str(node["slug"]), str(node.get("title") or ""), node.get("createdAt"),
                         node.get("startsAt"), now, feed, status, reason, self._Jsonb(node)))
                    new_count += int(cur.rowcount == 1 and status == "pending")
                conn.execute("INSERT INTO predict_html_feed_baselines(feed,initialized_at) VALUES (%s,%s) ON CONFLICT DO NOTHING", (feed, now))
                conn.execute("""INSERT INTO predict_html_scan_runs(feed,scanned_at,status,baseline,total_count,returned_count,has_next_page,snapshot_ms,new_count)
                    VALUES (%s,%s,'success',%s,%s,%s,%s,%s,%s)""",
                    (feed, now, baseline, result.get("total_count"), len(result["nodes"]), bool(result.get("has_next_page")), result.get("snapshot_ms"), new_count))
        return {"feed": feed, "baseline": baseline, "returned": len(result["nodes"]), "total": result.get("total_count"),
                "has_next_page": result.get("has_next_page"), "new_topics": new_count}

    def record_error(self, feed: str, now: datetime, error: str) -> None:
        with self._connect("error") as conn:
            conn.execute("INSERT INTO predict_html_scan_runs(feed,scanned_at,status,error) VALUES (%s,%s,'failed',%s)",
                (feed, now.astimezone(UTC), error[:1000]))

    def pending_topics(self) -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        with self._connect("pending") as conn:
            with postgres_transaction(conn):
                conn.execute("UPDATE predict_html_topics SET status='delivery_uncertain',last_error='投递期间进程中断，需人工核查',claim_token=NULL,claim_until=NULL WHERE status='delivering' AND claim_until < %s", (now,))
                conn.execute("UPDATE predict_html_topics SET status='retry_wait',claim_token=NULL,claim_until=NULL WHERE status='processing' AND claim_until < %s", (now,))
                rows = conn.execute("SELECT category_id FROM predict_html_topics WHERE status IN ('pending','retry_wait') ORDER BY first_seen_at FOR UPDATE SKIP LOCKED LIMIT 1").fetchall()
                claimed = []
                for row in rows:
                    token = uuid.uuid4().hex
                    result = conn.execute("UPDATE predict_html_topics SET status='processing',claim_token=%s,claim_until=%s,updated_at=%s WHERE category_id=%s AND status IN ('pending','retry_wait') RETURNING *", (token, now + timedelta(minutes=5), now, row["category_id"])).fetchone()
                    if result:
                        self._claims[str(row["category_id"])] = token
                        claimed.append(result)
                rows = claimed
        output = []
        for row in rows:
            item = dict(row)
            for key in ("raw_json", "detail_json", "writer_json", "push_result"):
                value = item.get(key)
                if value is not None and not isinstance(value, (dict, list)):
                    item[key] = json.loads(value)
            output.append(item)
        return output

    def update_topic(self, category_id: str, values: dict[str, Any]) -> None:
        allowed = {"status", "reason", "detail_json", "writer_json", "final_title", "final_content", "push_result", "last_error", "updated_at"}
        if not values.keys() <= allowed:
            raise ValueError("invalid update field")
        assignments, params = [], []
        for key, value in values.items():
            if key in {"detail_json", "writer_json", "push_result"} and value is not None:
                value = self._Jsonb(json.loads(value) if isinstance(value, str) else value)
            assignments.append(f"{key}=%s")
            params.append(value)
        token = self._claims.get(category_id)
        status = values.get("status")
        if token is None:
            raise RuntimeError("Predict HTML topic update requires an active claim")
        if status == "delivering":
            assignments.append("delivery_started_at=now()")
        with self._connect("update") as conn:
            if token:
                result = conn.execute("UPDATE predict_html_topics SET " + ",".join(assignments) + ",claim_token=CASE WHEN %s IN ('processing','delivering') THEN claim_token ELSE NULL END,claim_until=CASE WHEN %s IN ('processing','delivering') THEN now() + interval '5 minutes' ELSE NULL END WHERE category_id=%s AND claim_token=%s AND status IN ('processing','delivering') AND claim_until>clock_timestamp()", [*params, status or "", status or "", category_id, token])
                if not result.rowcount:
                    raise RuntimeError("Predict HTML topic claim expired")
                if status not in {"processing", "delivering"}:
                    self._claims.pop(category_id, None)

    def topic_status(self, category_id: str) -> str | None:
        with self._connect("status") as conn:
            row = conn.execute("SELECT status FROM predict_html_topics WHERE category_id=%s", (category_id,)).fetchone()
        return row["status"] if row else None
