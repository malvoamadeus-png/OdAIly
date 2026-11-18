from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from packages.common.pipeline_schema import PIPELINE_MONITORING_SCHEMA_SQL
from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres, postgres_transaction

from .prompts import POLICY_VERSION


SCHEMA_SQL = PIPELINE_MONITORING_SCHEMA_SQL + """
CREATE TABLE IF NOT EXISTS odaily_reference_items (
    source_item_id text PRIMARY KEY, source_url text, title text, content text NOT NULL,
    raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    published_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS fact_checks (
    id bigserial PRIMARY KEY,
    source_item_id text NOT NULL, content_hash text NOT NULL, policy_version text NOT NULL,
    title text, content text NOT NULL, source_url text, source_locator text NOT NULL DEFAULT '', published_at timestamptz,
    status text NOT NULL DEFAULT 'pending', model text NOT NULL DEFAULT 'gpt-6-luna',
    reason text NOT NULL DEFAULT '', summary text NOT NULL DEFAULT '', original_text text NOT NULL DEFAULT '', original_url text NOT NULL DEFAULT '',
    result_json jsonb NOT NULL DEFAULT '{}'::jsonb, attempts integer NOT NULL DEFAULT 0,
    locked_by text, locked_until timestamptz, next_attempt_at timestamptz, lease_token text, last_error text,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source_item_id,content_hash,policy_version)
);
CREATE INDEX IF NOT EXISTS idx_fact_checks_source ON fact_checks(source_item_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_fact_checks_due ON fact_checks(status,next_attempt_at,locked_until);
CREATE TABLE IF NOT EXISTS fact_check_runs (
    id bigserial PRIMARY KEY, check_id bigint NOT NULL REFERENCES fact_checks(id),
    status text NOT NULL, summary text NOT NULL, result_json jsonb NOT NULL, completed_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_check_runs_check ON fact_check_runs(check_id,id DESC);
"""


def _locator(alias: str = "r") -> str:
    return f"COALESCE(NULLIF({alias}.raw_payload->>'sourceUrl',''),{alias}.source_url,'')"


def _decode(row: Any) -> dict[str, Any]:
    result = dict(row)
    value = result.pop("result_json", {})
    result["result"] = value if isinstance(value, dict) else {}
    from .repository import LABELS

    result["label"] = LABELS.get(result["status"], result["status"])
    return result


def _digest(title: str | None, content: str, locator: str) -> str:
    payload = json.dumps([title, content, locator], ensure_ascii=False).encode()
    return hashlib.sha256(payload).hexdigest()


class PostgresFactReviewRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.application_name = "odaily-fact-auditor"
        from psycopg.types.json import Jsonb

        self._Jsonb = Jsonb

    @contextmanager
    def _connect(self, *, autocommit: bool = False):
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            if autocommit:
                yield connection
            else:
                with postgres_transaction(connection):
                    yield connection

    def init_schema(self) -> None:
        with connect_postgres(schema="main", application_name=self.application_name) as connection:
            apply_schema_migrations(
                connection,
                schema="main",
                component="fact_auditor",
                migrations=(PostgresMigration(
                    1, "fact_auditor_initial", sha256(SCHEMA_SQL.encode()).hexdigest(),
                    lambda conn: conn.execute(SCHEMA_SQL),
                ), PostgresMigration(
                    2, "fact_auditor_lease_token", sha256(b"ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS lease_token text").hexdigest(),
                    lambda conn: conn.execute("ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS lease_token text"),
                ), PostgresMigration(
                    3, "fact_auditor_attempt_token", sha256(b"ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS lease_token text").hexdigest(),
                    lambda conn: conn.execute("ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS lease_token text"),
                ), PostgresMigration(
                    4, "fact_auditor_external_error_counter", sha256(b"ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS odaily_external_error_counted integer NOT NULL DEFAULT 0").hexdigest(),
                    lambda conn: conn.execute("ALTER TABLE fact_checks ADD COLUMN IF NOT EXISTS odaily_external_error_counted integer NOT NULL DEFAULT 0"),
                )),
            )

    def claim(self, *, worker_id: str, lookback_minutes: int, max_attempts: int, start_after: str = "", lock_seconds: int = 600) -> dict[str, Any] | None:
        self.init_schema()
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=lookback_minutes)
        start_boundary = datetime.fromisoformat(start_after.replace("Z", "+00:00")).astimezone(UTC) if start_after else None
        with self._connect() as connection:
            connection.execute(
                """UPDATE fact_checks SET status='failed',last_error='Worker lease expired; retry limit reached',
                summary='执行中断且已达到重试上限',locked_by=NULL,locked_until=NULL,updated_at=%s
                WHERE status='processing' AND attempts>=%s AND locked_until<%s""",
                (now, max_attempts, now),
            )
            source_rows = connection.execute(
                f"""SELECT r.source_item_id,r.title,r.content,r.source_url,r.published_at,{_locator()} AS source_locator
                FROM odaily_reference_items r
                WHERE r.content<>'' AND r.published_at >= %s AND r.published_at <= %s
                  AND (%s::timestamptz IS NULL OR r.published_at >= %s::timestamptz)
                  AND NOT EXISTS (
                    SELECT 1 FROM fact_checks c WHERE c.source_item_id=r.source_item_id
                      AND c.title IS NOT DISTINCT FROM r.title AND c.content=r.content
                      AND c.source_locator={_locator()} AND c.policy_version=%s
                  )
                ORDER BY r.published_at DESC,r.source_item_id
                FOR UPDATE OF r SKIP LOCKED LIMIT 1""",
                (cutoff, now, start_boundary, start_boundary, POLICY_VERSION),
            ).fetchone()
            if source_rows:
                digest = _digest(source_rows["title"], source_rows["content"], source_rows["source_locator"])
                connection.execute(
                    """INSERT INTO fact_checks(source_item_id,content_hash,policy_version,title,content,source_url,source_locator,published_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_item_id,content_hash,policy_version) DO NOTHING""",
                    (source_rows["source_item_id"], digest, POLICY_VERSION, source_rows["title"], source_rows["content"],
                     source_rows["source_url"], source_rows["source_locator"], source_rows["published_at"]),
                )
            candidate = connection.execute(
                f"""SELECT fc.* FROM fact_checks fc JOIN odaily_reference_items r ON r.source_item_id=fc.source_item_id
                WHERE fc.status IN ('pending','failed','processing') AND fc.attempts < %s
                  AND fc.title IS NOT DISTINCT FROM r.title AND fc.content=r.content AND fc.source_locator={_locator()}
                  AND fc.policy_version=%s
                  AND (fc.status='pending' OR (fc.status='failed' AND fc.next_attempt_at<=%s)
                    OR (fc.status='processing' AND fc.locked_until<%s))
                ORDER BY CASE WHEN fc.status='pending' THEN 0 ELSE 1 END,fc.published_at DESC
                FOR UPDATE OF fc SKIP LOCKED LIMIT 1""",
                (max_attempts, POLICY_VERSION, now, now),
            ).fetchone()
            if candidate is None:
                return None
            updated = connection.execute(
                """UPDATE fact_checks SET status='processing',locked_by=%s,locked_until=%s,attempts=attempts+1,
                lease_token=gen_random_uuid()::text,updated_at=%s
                WHERE id=%s RETURNING *""",
                (worker_id, now + timedelta(seconds=lock_seconds), now, candidate["id"]),
            ).fetchone()
        return _decode(updated)

    def save(self, task: dict[str, Any], result: dict[str, Any], *, worker_id: str) -> bool:
        self.init_schema()
        now = datetime.now(UTC)
        retry_at = now + timedelta(seconds=min(900, 60 * 2 ** task["attempts"]))
        with self._connect() as connection:
            updated = connection.execute(
                """UPDATE fact_checks SET status=%s,reason=%s,summary=%s,original_text=%s,original_url=%s,result_json=%s,
                model=%s,last_error=%s,next_attempt_at=%s,locked_by=NULL,locked_until=NULL,updated_at=%s
                WHERE id=%s AND locked_by=%s AND attempts=%s AND lease_token=%s
                  AND status='processing' AND locked_until>%s RETURNING id""",
                (result["status"], result.get("reason", ""), result.get("summary", ""), result.get("original_text", ""),
                 result.get("original_url", ""), self._Jsonb(result), result.get("model", "gpt-6-luna"), result.get("error"),
                 retry_at if result["status"] == "failed" else None, now, task["id"], worker_id,
                 task["attempts"], task["lease_token"], now),
            ).fetchone()
            if updated:
                connection.execute(
                    "INSERT INTO fact_check_runs(check_id,status,summary,result_json,completed_at) VALUES(%s,%s,%s,%s,%s)",
                    (task["id"], result["status"], result.get("summary", ""), self._Jsonb(result), now),
                )
            return bool(updated)

    def retry(self, check_id: int) -> dict[str, Any]:
        self.init_schema()
        current_id = f"""(SELECT c.id FROM fact_checks c WHERE c.source_item_id=r.source_item_id
            AND c.title IS NOT DISTINCT FROM r.title AND c.content=r.content AND c.source_locator={_locator()}
            AND c.policy_version=%s ORDER BY c.id DESC LIMIT 1)"""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT fc.id,fc.status,fc.source_item_id FROM odaily_reference_items r JOIN fact_checks fc ON fc.id={current_id} WHERE fc.id=%s",
                (POLICY_VERSION, check_id),
            ).fetchone()
            if row is None:
                raise ValueError("只能重新审查当前稿件版本")
            if row["status"] in {"pending", "processing"}:
                raise ValueError("该稿件已在等待或执行审查")
            connection.execute(
                """UPDATE fact_checks SET status='pending',attempts=0,next_attempt_at=NULL,last_error=NULL,
                locked_by=NULL,locked_until=NULL,result_json='{}'::jsonb,summary='已请求重新审查',updated_at=now() WHERE id=%s""",
                (check_id,),
            )
        return {"check_id": check_id, "source_item_id": row["source_item_id"], "status": "pending"}

    def detail(self, source_item_id: str, check_id: int | None = None) -> dict[str, Any]:
        self.init_schema()
        current_id = f"""(SELECT c.id FROM fact_checks c WHERE c.source_item_id=r.source_item_id
            AND c.title IS NOT DISTINCT FROM r.title AND c.content=r.content AND c.source_locator={_locator()}
            AND c.policy_version='{POLICY_VERSION}' ORDER BY c.id DESC LIMIT 1)"""
        with self._connect(autocommit=True) as connection:
            current = connection.execute(f"SELECT r.*,fc.id AS current_check_id FROM odaily_reference_items r LEFT JOIN fact_checks fc ON fc.id={current_id} WHERE r.source_item_id=%s", (source_item_id,)).fetchone()
            if current is None:
                raise ValueError("newsflash not found")
            target = check_id if check_id is not None else current["current_check_id"]
            row = connection.execute("SELECT * FROM fact_checks WHERE id=%s AND source_item_id=%s", (target, source_item_id)).fetchone() if target else None
            if check_id is not None and row is None:
                raise ValueError("fact review not found")
            history = connection.execute("SELECT id,status,summary,updated_at FROM fact_checks WHERE source_item_id=%s ORDER BY id DESC LIMIT 20", (source_item_id,)).fetchall()
            runs = connection.execute("SELECT id,status,summary,completed_at FROM fact_check_runs WHERE check_id=%s ORDER BY id DESC LIMIT 20", (target,)).fetchall() if target else []
        return {"check": _decode(row) if row else None,
                "published": {key: current[key] for key in ("source_item_id", "title", "content", "published_at")},
                "is_current": bool(row and row["id"] == current["current_check_id"]),
                "history": [dict(item) for item in history], "runs": [dict(item) for item in runs]}

    def linked_material(self, task: dict[str, Any]) -> dict[str, Any] | None:
        self.init_schema()
        with self._connect(autocommit=True) as connection:
            exists = connection.execute("SELECT to_regclass('main.x_task_pipeline') AS relation").fetchone()
            if not exists or not exists["relation"]:
                return None
            candidates = connection.execute(
                """SELECT t.source,t.title,t.content,t.source_url,t.metadata,p.final_title,p.publish_completed_at,p.push_result
                FROM x_task_pipeline p JOIN tasks t ON t.id=p.task_id
                WHERE p.publish_completed_at IS NOT NULL
                  AND abs(extract(epoch FROM (p.publish_completed_at-%s)))<=300""",
                (task.get("published_at"),),
            ).fetchall()
        exact = [dict(row) for row in candidates if task["source_item_id"] in _remote_ids(row["push_result"])]
        if len(exact) == 1:
            return exact[0]
        matches = [dict(row) for row in candidates if row["final_title"] == task.get("title")]
        return matches[0] if len(matches) == 1 else None

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool,
                                error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        from psycopg.types.json import Jsonb
        self.init_schema()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata)
                VALUES(%s,%s,%s,now(),CASE WHEN %s THEN now() ELSE NULL END,%s,%s)
                ON CONFLICT(component,worker_id) DO UPDATE SET status=EXCLUDED.status,last_seen_at=EXCLUDED.last_seen_at,
                last_success_at=CASE WHEN %s THEN EXCLUDED.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END,
                last_error=EXCLUDED.last_error,metadata=EXCLUDED.metadata,updated_at=now()""",
                (component, worker_id, status, success, error[:2000] if error else None, Jsonb(metadata or {}), success),
            )


def _remote_ids(value: Any, key: str = "") -> set[str]:
    if isinstance(value, str) and key == "response_text":
        try:
            return _remote_ids(json.loads(value))
        except (ValueError, TypeError):
            return set()
    if isinstance(value, dict):
        return set().union(*(_remote_ids(item, str(name)) for name, item in value.items())) if value else set()
    if isinstance(value, list):
        return set().union(*(_remote_ids(item) for item in value)) if value else set()
    return {str(value)} if value is not None and key.casefold() in {"id", "newsflashid", "newsflash_id", "source_item_id"} else set()


__all__ = ["PostgresFactReviewRepository"]
