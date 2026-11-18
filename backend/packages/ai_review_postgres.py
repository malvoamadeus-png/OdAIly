from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from packages.common.pg_native import PostgresMigration, apply_schema_migrations, connect_postgres
from packages.ai_review import DAILY_TOKEN_LIMIT, MAX_ANALYSIS_ATTEMPTS, RECOMMENDATION_LABELS, REVIEW_LOOKBACK_DAYS, STATUS_VALUES


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_review_items (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, item_key text UNIQUE NOT NULL, item_type text NOT NULL DEFAULT 'event',
 category text NOT NULL, status text NOT NULL DEFAULT 'pending', analysis_status text NOT NULL DEFAULT 'queued',
 title text NOT NULL, recommendation text NOT NULL DEFAULT 'consider', reason text NOT NULL DEFAULT '',
 evidence_json jsonb NOT NULL DEFAULT '[]'::jsonb, clues_json jsonb NOT NULL DEFAULT '{}'::jsonb,
 analysis_model text, input_tokens integer NOT NULL DEFAULT 0, output_tokens integer NOT NULL DEFAULT 0,
 analysis_attempts integer NOT NULL DEFAULT 0, analysis_error text, human_decision text, feedback_note text,
 decision_by text, decided_at timestamptz, source_updated_at timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS idx_ai_review_queue ON ai_review_items(item_type,analysis_status,analysis_attempts,created_at);
CREATE TABLE IF NOT EXISTS ai_review_daily_usage(usage_date text NOT NULL,kind text NOT NULL DEFAULT 'event',used_tokens bigint NOT NULL DEFAULT 0,reserved_tokens bigint NOT NULL DEFAULT 0,calls integer NOT NULL DEFAULT 0,updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(usage_date,kind));
CREATE TABLE IF NOT EXISTS ai_review_run_state(state_key text PRIMARY KEY,state_value text NOT NULL,updated_at timestamptz NOT NULL DEFAULT clock_timestamp());
"""
CHECKSUM = __import__("hashlib").sha256(SCHEMA_SQL.encode()).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _item(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["evidence"] = value.pop("evidence_json") or []
    value["clues"] = value.pop("clues_json") or {}
    for old, new in (
        ("item_key", "itemKey"), ("item_type", "type"), ("analysis_status", "analysisStatus"),
        ("analysis_model", "analysisModel"), ("input_tokens", "inputTokens"),
        ("output_tokens", "outputTokens"), ("analysis_attempts", "analysisAttempts"),
        ("analysis_error", "analysisError"), ("human_decision", "humanDecision"),
        ("feedback_note", "feedbackNote"), ("decision_by", "decisionBy"),
        ("decided_at", "decidedAt"), ("source_updated_at", "sourceUpdatedAt"),
        ("created_at", "createdAt"), ("updated_at", "updatedAt"),
    ):
        raw = value.pop(old)
        value[new] = raw.isoformat() if isinstance(raw, datetime) else raw
    value["recommendationLabel"] = RECOMMENDATION_LABELS.get(value["recommendation"], "可考虑")
    return value


class PostgresAIReviewRepository:
    def __init__(self, *, schema: str = "system_observability") -> None:
        self.schema = schema
        self.initialize()

    def connect(self):
        return connect_postgres(schema=self.schema, application_name="odaily-ai-review")

    def initialize(self) -> None:
        self.initialize_schema(self.schema)

    @staticmethod
    def initialize_schema(schema: str = "system_observability") -> None:
        def install_base(connection) -> None:
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    connection.execute(statement)

        def add_account_and_adoption_fields(connection) -> None:
            connection.execute("ALTER TABLE ai_review_items ADD COLUMN IF NOT EXISTS account_lower text")
            connection.execute("ALTER TABLE ai_review_items ADD COLUMN IF NOT EXISTS adopted_targets_json jsonb NOT NULL DEFAULT '[]'::jsonb")

        with connect_postgres(schema=schema, application_name="odaily-ai-review-migrate") as conn:
            apply_schema_migrations(
                conn,
                schema=schema,
                component="ai_review",
                migrations=(
                    PostgresMigration(1, "AI review", CHECKSUM, install_base),
                    PostgresMigration(
                        2,
                        "AI review account and adoption fields",
                        __import__("hashlib").sha256(b"account_lower adopted_targets_json").hexdigest(),
                        add_account_and_adoption_fields,
                    ),
                ),
            )

    def close(self) -> None: return None

    def insert_candidate(self, *, key: str, category: str, title: str, evidence: list[dict[str, Any]], clues: dict[str, Any] | None = None, source_updated_at: str | None = None, refresh_existing: bool = False) -> bool:
        with self.connect() as conn:
            result = conn.execute("INSERT INTO ai_review_items(item_key,item_type,category,title,evidence_json,clues_json,source_updated_at) VALUES(%s,'event',%s,%s,%s::jsonb,%s::jsonb,%s) ON CONFLICT(item_key) DO UPDATE SET title=excluded.title,evidence_json=excluded.evidence_json,clues_json=excluded.clues_json,source_updated_at=excluded.source_updated_at,updated_at=clock_timestamp() WHERE ai_review_items.item_type='event' AND %s", (key, category, title[:300], json.dumps(evidence, ensure_ascii=False), json.dumps(clues or {}, ensure_ascii=False), source_updated_at, refresh_existing))
            return bool(result.rowcount)

    def pending_analysis(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM ai_review_items WHERE item_type='event' AND analysis_status IN ('queued','failed') AND analysis_attempts<%s ORDER BY COALESCE(source_updated_at,created_at) DESC,id DESC LIMIT %s", (MAX_ANALYSIS_ATTEMPTS, limit)).fetchall()
        return [_item(row) for row in rows]

    def complete_analysis(self, item_id: int, *, recommendation: str, reason: str, model: str, input_tokens: int, output_tokens: int) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_review_items SET analysis_status='completed',recommendation=%s,reason=%s,analysis_model=%s,input_tokens=%s,output_tokens=%s,analysis_attempts=analysis_attempts+1,analysis_error=NULL,updated_at=clock_timestamp() WHERE id=%s", (recommendation, reason[:1500], model, input_tokens, output_tokens, item_id))

    def fail_analysis(self, item_id: int, error: str) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_review_items SET analysis_status='failed',analysis_error=%s,analysis_attempts=analysis_attempts+1,updated_at=clock_timestamp() WHERE id=%s", (error[:1000], item_id))

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT state_value FROM ai_review_run_state WHERE state_key=%s", (key,)).fetchone()
        return str(row["state_value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn: conn.execute("INSERT INTO ai_review_run_state(state_key,state_value) VALUES(%s,%s) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=clock_timestamp()", (key, value))

    def has_pending_analysis(self) -> bool: return bool(self.pending_analysis(1))
    def requeue_event_analyses(self) -> int:
        with self.connect() as conn: return int(conn.execute("UPDATE ai_review_items SET analysis_status='queued',analysis_attempts=0,updated_at=clock_timestamp() WHERE item_type='event' AND analysis_status='completed'").rowcount or 0)
    def history_examples(self, category: str, limit: int = 2) -> list[dict[str, str]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT category,human_decision,feedback_note FROM ai_review_items WHERE item_type='event' AND human_decision IS NOT NULL AND category=%s ORDER BY decided_at DESC LIMIT %s", (category, limit)).fetchall()
        return [{"category": str(row["category"]), "decision": str(row["human_decision"]), "note": str(row["feedback_note"] or "")[:120]} for row in rows]

    def delete_stale_event_candidates(self, since: datetime, keep_keys: set[str]) -> int:
        allowed = ("competitor_first_ai", "multiple_competitors_no_odaily")
        since_utc = since.astimezone(UTC)
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM ai_review_items WHERE item_type='event' AND ("
                "item_key NOT LIKE 'event-source:%%' OR category<>ALL(%s) OR "
                "(COALESCE(source_updated_at,created_at)>=%s AND NOT(item_key=ANY(%s))) OR "
                "(COALESCE(source_updated_at,created_at)<%s AND status='pending'))",
                (list(allowed), since_utc, list(keep_keys), since_utc),
            )
            return int(cursor.rowcount or 0)

    def update_usage(self, usage_date: str, *, reserve: int = 0, settle: tuple[int, int] | None = None) -> bool:
        with self.connect() as conn:
            with conn.transaction():
                conn.execute("INSERT INTO ai_review_daily_usage(usage_date) VALUES(%s) ON CONFLICT DO NOTHING", (usage_date,))
                row = conn.execute("SELECT used_tokens,reserved_tokens FROM ai_review_daily_usage WHERE usage_date=%s FOR UPDATE", (usage_date,)).fetchone()
                if reserve and int(row["used_tokens"])+int(row["reserved_tokens"])+reserve > DAILY_TOKEN_LIMIT: return False
                if reserve: conn.execute("UPDATE ai_review_daily_usage SET reserved_tokens=reserved_tokens+%s,updated_at=clock_timestamp() WHERE usage_date=%s", (reserve, usage_date))
                elif settle: conn.execute("UPDATE ai_review_daily_usage SET reserved_tokens=GREATEST(0,reserved_tokens-%s),used_tokens=used_tokens+%s,calls=calls+1,updated_at=clock_timestamp() WHERE usage_date=%s", (settle[0], settle[1], usage_date))
        return True

    def release_usage(self, usage_date: str, reserved: int) -> None:
        with self.connect() as conn: conn.execute("UPDATE ai_review_daily_usage SET reserved_tokens=GREATEST(0,reserved_tokens-%s) WHERE usage_date=%s", (reserved, usage_date))
    def usage(self, usage_date: str) -> dict[str, dict[str, int]]:
        with self.connect() as conn: row = conn.execute("SELECT used_tokens,reserved_tokens,calls FROM ai_review_daily_usage WHERE usage_date=%s", (usage_date,)).fetchone()
        return {"event": {"usedTokens": int(row["used_tokens"]) if row else 0, "reservedTokens": int(row["reserved_tokens"]) if row else 0, "calls": int(row["calls"]) if row else 0}}

    def get_item(self, item_key: str) -> dict[str, Any] | None:
        with self.connect() as conn: row = conn.execute("SELECT * FROM ai_review_items WHERE item_key=%s AND item_type='event'", (item_key,)).fetchone()
        return _item(row) if row else None

    def decide(self, item_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"accepted", "rejected", "deferred"}:
            raise ValueError("不支持的人工决定")
        with self.connect() as conn:
            with conn.transaction():
                row = conn.execute("SELECT * FROM ai_review_items WHERE item_key=%s AND item_type='event' FOR UPDATE", (item_key,)).fetchone()
                if row is None:
                    raise ValueError("审阅项目不存在")
                if row["status"] == "accepted" and decision == "accepted":
                    return _item(row)
                conn.execute("UPDATE ai_review_items SET status=%s,human_decision=%s,feedback_note=%s,decision_by=%s,decided_at=clock_timestamp(),updated_at=clock_timestamp() WHERE item_key=%s", (decision, decision, note[:500], actor[:200], item_key))
        item = self.get_item(item_key)
        assert item is not None
        return item

    def list_items(self, *, status: str = "all", category: str = "all", offset: int = 0, limit: int = 30) -> dict[str, Any]:
        clauses=["item_type='event'","analysis_status='completed'","recommendation='priority'"]; params=[]
        if status in STATUS_VALUES: clauses.append("status=%s"); params.append(status)
        if category not in {"","all"}: clauses.append("category=%s"); params.append(category)
        where=" WHERE "+" AND ".join(clauses)
        with self.connect() as conn:
            total=int(conn.execute("SELECT count(*) AS count FROM ai_review_items"+where,params).fetchone()["count"])
            rows=conn.execute("SELECT * FROM ai_review_items"+where+" ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,COALESCE(source_updated_at,created_at) DESC,id DESC LIMIT %s OFFSET %s",(*params,min(100,max(1,limit)),max(0,offset))).fetchall()
        return {"items":[_item(row) for row in rows],"total":total,"offset":max(0,offset),"limit":min(100,max(1,limit)),"usage":self.usage(datetime.now(UTC).date().isoformat()),"weekly":self.weekly_summary()}

    def weekly_summary(self) -> dict[str, int | float]:
        cutoff = datetime.now(UTC) - timedelta(days=REVIEW_LOOKBACK_DAYS)
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS candidates,SUM((status='accepted')::int) AS accepted,"
                "SUM((status='rejected')::int) AS rejected,SUM((status='deferred')::int) AS deferred,"
                "COALESCE(SUM(input_tokens),0) AS input_tokens,COALESCE(SUM(output_tokens),0) AS output_tokens "
                "FROM ai_review_items WHERE item_type='event' AND created_at>=%s AND analysis_status='completed' AND recommendation='priority'",
                (cutoff,),
            ).fetchone()
        candidates = int(row["candidates"] or 0)
        accepted, rejected = int(row["accepted"] or 0), int(row["rejected"] or 0)
        return {"candidates": candidates, "accepted": accepted, "rejected": rejected,
                "deferred": int(row["deferred"] or 0),
                "acceptanceRate": round(accepted / candidates, 4) if candidates else 0.0,
                "rejectionRate": round(rejected / candidates, 4) if candidates else 0.0,
                "inputTokens": int(row["input_tokens"] or 0), "outputTokens": int(row["output_tokens"] or 0)}
