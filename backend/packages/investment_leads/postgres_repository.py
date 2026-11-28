from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
import time

from packages.common.pg_native import connect_postgres
from packages.common.storage import PostgresSettings
from psycopg.types.json import Jsonb


class InvestmentLeadPostgresRepository:
    """Native PostgreSQL access for short investment lead task operations."""

    def __init__(
        self, *, schema: str = "hottopic", application_name: str = "odaily-investment-leads",
        settings: PostgresSettings | None = None,
    ) -> None:
        self.schema = schema
        self.application_name = application_name
        self.settings = settings

    def initialize(self, *, capture_started_at: datetime) -> tuple[int, ...]:
        from .postgres_schema import initialize_investment_leads_schema

        versions = initialize_investment_leads_schema(schema=self.schema, settings=self.settings)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO investment_leads_meta(singleton_key,capture_started_at) VALUES('global',%s) "
                    "ON CONFLICT(singleton_key) DO NOTHING",
                    (capture_started_at,),
                )
                connection.execute(
                    "INSERT INTO investment_lead_scheduler_cursor(stage,account_lower) VALUES('identity_search','') "
                    "ON CONFLICT(stage) DO NOTHING"
                )
        return versions

    def reclaim_stale(self, *, now: datetime, lease: timedelta, max_attempts: int) -> None:
        cutoff = now - lease
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for table in ("investment_lead_tasks", "investment_lead_identity_searches"):
                    connection.execute(
                        f"UPDATE {table} SET status='failed',lease_until=NULL,"
                        "last_error=COALESCE(last_error,'任务租约过期'),completed_at=%s,updated_at=%s "
                        "WHERE status='processing' AND attempts>=%s AND "
                        "((lease_until IS NOT NULL AND lease_until<=%s) OR (lease_until IS NULL AND updated_at<%s))",
                        (now, now, max_attempts, now, cutoff),
                    )
                    connection.execute(
                        f"UPDATE {table} SET status='pending',lease_until=NULL,"
                        "last_error=COALESCE(last_error,'任务租约过期，已重新排队'),updated_at=%s "
                        "WHERE status='processing' AND attempts<%s AND "
                        "((lease_until IS NOT NULL AND lease_until<=%s) OR (lease_until IS NULL AND updated_at<%s))",
                        (now, max_attempts, now, cutoff),
                    )

    def resume_budget_paused(self, *, now: datetime) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='pending',attempts=0,updated_at=%s WHERE status='budget_paused'",
                    (now,),
                )
                connection.execute(
                    "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,updated_at=%s WHERE status='budget_paused'",
                    (now,),
                )

    def heartbeat(self, *, now: datetime, model_error: str | None) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE investment_leads_meta SET worker_last_seen_at=%s,model_error=%s "
                    "WHERE singleton_key='global'",
                    (now, model_error),
                )

    def claim_scan_batch(
        self,
        *,
        now: datetime,
        lease: timedelta,
        limit: int,
        max_attempts: int,
        flush_seconds: int = 0,
    ) -> list[dict[str, Any]]:
        lease_until = now + lease
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                rows = connection.execute(
                    "SELECT t.id,t.material_id,t.content_hash,t.attempts,t.prompt_version,m.payload_json,t.created_at "
                    "FROM investment_lead_tasks t JOIN investment_lead_materials m "
                    "ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                    "WHERE t.stage='scan' AND t.status='pending' AND t.attempts<%s "
                    "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=%s) "
                    "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                    "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled) "
                    "ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT %s",
                    (max_attempts, now, max(1, limit)),
                ).fetchall()
                if len(rows) < limit and rows and flush_seconds > 0:
                    oldest = rows[0]["created_at"]
                    if now - oldest < timedelta(seconds=flush_seconds):
                        return []
                jobs = [dict(row) for row in rows]
                for job in jobs:
                    updated = connection.execute(
                        "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=%s,updated_at=%s "
                        "WHERE id=%s AND status='pending' RETURNING attempts,lease_until",
                        (lease_until, now, job["id"]),
                    ).fetchone()
                    if updated is None:
                        continue
                    job["attempts"] = int(updated["attempts"])
                    job["lease_until"] = updated["lease_until"]
                return [job for job in jobs if int(job["attempts"]) > 0]

    def material_is_eligible(self, material_id: str, content_hash: str) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM investment_lead_observations o "
                "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                "WHERE o.material_id=%s AND o.content_hash=%s AND a.status='followed' "
                "AND a.investment_leads_enabled) AS eligible",
                (material_id, content_hash),
            ).fetchone()
            return bool(row["eligible"])

    def claim_supplement(self, *, now: datetime, lease: timedelta, max_attempts: int) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT t.id,t.material_id,t.content_hash,t.attempts,m.payload_json "
                    "FROM investment_lead_tasks t JOIN investment_lead_materials m "
                    "ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                    "WHERE t.stage='supplement' AND t.status='pending' AND t.attempts<%s "
                    "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=%s) "
                    "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                    "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled) "
                    "ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT 1",
                    (max_attempts, now),
                ).fetchone()
                if row is None:
                    return None
                updated = connection.execute(
                    "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=%s,updated_at=%s "
                    "WHERE id=%s AND status='pending' RETURNING attempts,lease_until",
                    (now + lease, now, row["id"]),
                ).fetchone()
                if updated is None:
                    return None
                job = dict(row)
                job.update(updated)
                return job

    def save_supplement_result(
        self, *, job: dict[str, Any], material: dict[str, Any], now: datetime, usage_calls: int = 0,
    ) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_tasks WHERE id=%s AND stage='supplement' "
                    "AND status='processing' AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                eligible = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=%s AND o.content_hash=%s "
                    "AND a.status='followed' AND a.investment_leads_enabled) AS eligible",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()["eligible"]
                if not eligible:
                    connection.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=%s,updated_at=%s "
                        "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                        (now, now, job["id"], job["attempts"], job["lease_until"], now),
                    )
                    return False
                connection.execute(
                    "UPDATE investment_lead_materials SET payload_json=%s,last_observed_at=%s "
                    "WHERE material_id=%s AND content_hash=%s",
                    (Jsonb(material), now, job["material_id"], job["content_hash"]),
                )
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='succeeded',lease_until=NULL,next_attempt_at=NULL,"
                    "last_error=NULL,completed_at=%s,updated_at=%s WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (now, now, job["id"], job["attempts"], job["lease_until"], now),
                )
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='pending',attempts=0,next_attempt_at=NULL,"
                    "completed_at=NULL,updated_at=%s WHERE material_id=%s AND content_hash=%s AND stage='scan' "
                    "AND status='needs_material'",
                    (now, job["material_id"], job["content_hash"]),
                )
                if usage_calls:
                    connection.execute(
                        "INSERT INTO investment_lead_usage(usage_date,kind,calls,input_tokens,output_tokens,updated_at) "
                        "VALUES(%s,'supplement',%s,0,0,%s) ON CONFLICT(usage_date,kind) DO UPDATE SET "
                        "calls=investment_lead_usage.calls+EXCLUDED.calls,updated_at=EXCLUDED.updated_at",
                        (now.date(), usage_calls, now),
                    )
                return True

    def claim_derive_batch(
        self, *, now: datetime, lease: timedelta, limit: int, max_attempts: int,
    ) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                rows = connection.execute(
                    "SELECT t.id,t.material_id,t.content_hash,t.attempts,t.prompt_version,e.classification,e.subject,e.action,"
                    "e.change_object,e.event_time,e.impact_relations_json,e.summary,e.reason,m.payload_json "
                    "FROM investment_lead_tasks t JOIN investment_lead_events e "
                    "ON e.material_id=t.material_id AND e.content_hash=t.content_hash "
                    "AND e.id=(SELECT MAX(latest.id) FROM investment_lead_events latest "
                    "WHERE latest.material_id=t.material_id AND latest.content_hash=t.content_hash) "
                    "JOIN investment_lead_materials m ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                    "WHERE t.stage='derive' AND t.status='pending' AND t.attempts<%s "
                    "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=%s) "
                    "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                    "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled) "
                    "ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT %s",
                    (max_attempts, now, max(1, limit)),
                ).fetchall()
                jobs = []
                for row in rows:
                    updated = connection.execute(
                        "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=%s,updated_at=%s "
                        "WHERE id=%s AND status='pending' RETURNING attempts,lease_until",
                        (now + lease, now, row["id"]),
                    ).fetchone()
                    if updated is not None:
                        job = dict(row)
                        job.update(updated)
                        jobs.append(job)
                return jobs

    def related_event_candidates(
        self, *, material_id: str, content_hash: str, cutoff: datetime,
    ) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return connection.execute(
                "SELECT e.material_id,e.content_hash,e.classification,e.subject,e.action,e.change_object,e.event_time,"
                "e.impact_relations_json,e.summary,e.created_at,m.payload_json,m.posted_at,m.activity_at,m.activity_time_complete "
                "FROM investment_lead_events e JOIN investment_lead_materials m "
                "ON m.material_id=e.material_id AND m.content_hash=e.content_hash "
                "WHERE e.created_at>=%s AND e.classification IN ('driver_candidate','composable') "
                "AND NOT(e.material_id=%s AND e.content_hash=%s) "
                "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=e.material_id "
                "AND o.content_hash=e.content_hash AND a.status='followed' AND a.investment_leads_enabled) "
                "ORDER BY e.created_at DESC LIMIT 100",
                (cutoff, material_id, content_hash),
            ).fetchall()

    def recent_leads(self, *, cutoff: datetime, limit: int = 30) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT id,subject,driver_logic,conditions,assets_json,status,last_substantive_at "
                "FROM investment_leads l WHERE last_substantive_at>=%s ORDER BY last_substantive_at DESC LIMIT %s",
                (cutoff, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_task_status(self, *, job: dict[str, Any], status: str, now: datetime) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                cursor = connection.execute(
                    "UPDATE investment_lead_tasks SET status=%s,lease_until=NULL,next_attempt_at=NULL,"
                    "completed_at=%s,updated_at=%s WHERE id=%s AND status='processing' AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (status, now, now, job["id"], job["attempts"], job["lease_until"], now),
                )
                return cursor.rowcount == 1
    def claim_identity_search(self, *, now: datetime, lease: timedelta, max_attempts: int) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                cursor = connection.execute(
                    "SELECT account_lower FROM investment_lead_scheduler_cursor WHERE stage='identity_search' FOR UPDATE"
                ).fetchone()
                last_account = str(cursor["account_lower"]) if cursor else ""
                fair_account = connection.execute(
                    "SELECT a.screen_name_lower FROM investment_lead_identity_searches s "
                    "JOIN investment_lead_sources ls ON ls.lead_id=s.lead_id "
                    "JOIN investment_lead_observations o ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                    "WHERE s.status='pending' AND s.attempts<%s AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=%s) "
                    "AND a.status='followed' AND a.investment_leads_enabled "
                    "GROUP BY a.screen_name_lower ORDER BY CASE WHEN a.screen_name_lower>%s THEN 0 ELSE 1 END,"
                    "a.screen_name_lower LIMIT 1",
                    (max_attempts, now, last_account),
                ).fetchone()
                if fair_account is None:
                    return None
                account = str(fair_account["screen_name_lower"])
                row = connection.execute(
                    "SELECT s.id,s.lead_id,s.asset_key,s.asset_name,s.context_json,s.attempts "
                    "FROM investment_lead_identity_searches s WHERE s.status='pending' AND s.attempts<%s "
                    "AND (s.next_attempt_at IS NULL OR s.next_attempt_at<=%s) AND EXISTS("
                    "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations o "
                    "ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                    "WHERE ls.lead_id=s.lead_id AND a.status='followed' AND a.investment_leads_enabled "
                    "AND a.screen_name_lower=%s) ORDER BY s.created_at,s.id "
                    "FOR UPDATE OF s SKIP LOCKED LIMIT 1",
                    (max_attempts, now, account),
                ).fetchone()
                if row is None:
                    return None
                updated = connection.execute(
                    "UPDATE investment_lead_identity_searches SET status='processing',attempts=attempts+1,"
                    "lease_until=%s,updated_at=%s WHERE id=%s AND status='pending' RETURNING attempts,lease_until",
                    (now + lease, now, row["id"]),
                ).fetchone()
                if updated is None:
                    return None
                connection.execute(
                    "INSERT INTO investment_lead_scheduler_cursor(stage,account_lower) VALUES('identity_search',%s) "
                    "ON CONFLICT(stage) DO UPDATE SET account_lower=EXCLUDED.account_lower",
                    (account,),
                )
                job = dict(row)
                job.update(updated)
                return job

    def finish_identity_search(
        self, *, job: dict[str, Any], result: dict[str, Any], now: datetime,
        actual_model: str, input_tokens: int, output_tokens: int, identity_prompt_version: str,
    ) -> bool:
        identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
        resolved = bool(identity.get("ticker"))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_identity_searches WHERE id=%s AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                eligible = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations o "
                    "ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=o.screen_name_lower "
                    "WHERE ls.lead_id=%s AND a.status='followed' AND a.investment_leads_enabled) AS eligible",
                    (job["lead_id"],),
                ).fetchone()["eligible"]
                if not eligible:
                    connection.execute(
                        "UPDATE investment_lead_identity_searches SET status='ignored',lease_until=NULL,completed_at=%s,updated_at=%s "
                        "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                        (now, now, job["id"], job["attempts"], job["lease_until"], now),
                    )
                    return False
                connection.execute(
                    "UPDATE investment_lead_identity_searches SET status=%s,lease_until=NULL,next_attempt_at=NULL,"
                    "result_json=%s,last_error=NULL,completed_at=%s,updated_at=%s WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    ("succeeded" if resolved else "unresolved", Jsonb(result), now, now,
                     job["id"], job["attempts"], job["lease_until"], now),
                )
                connection.execute(
                    "INSERT INTO investment_lead_usage(usage_date,kind,calls,input_tokens,output_tokens,updated_at) "
                    "VALUES(%s,'search',1,%s,%s,%s) ON CONFLICT(usage_date,kind) DO UPDATE SET "
                    "calls=investment_lead_usage.calls+1,input_tokens=investment_lead_usage.input_tokens+EXCLUDED.input_tokens,"
                    "output_tokens=investment_lead_usage.output_tokens+EXCLUDED.output_tokens,updated_at=EXCLUDED.updated_at",
                    (now.date(), input_tokens, output_tokens, now),
                )
                if not resolved:
                    return True
                row = connection.execute("SELECT * FROM investment_leads WHERE id=%s FOR UPDATE", (job["lead_id"],)).fetchone()
                if row is None:
                    return True
                assets = row["assets_json"] if isinstance(row["assets_json"], list) else []
                changed = False
                for asset in assets:
                    if (isinstance(asset, dict) and _asset_key(str(asset.get("name") or "")) == job["asset_key"]
                            and not str(asset.get("ticker") or "").strip()):
                        asset["ticker"] = identity.get("ticker") or ""
                        asset["market"] = identity.get("market") or asset.get("market") or ""
                        changed = True
                if not changed:
                    return True
                revision = int(row["revision"]) + 1
                connection.execute(
                    "UPDATE investment_leads SET assets_json=%s,last_substantive_at=COALESCE(NULLIF(replay_json->>'sourcePostedAt','')::timestamptz,%s),"
                    "revision=%s,updated_at=%s WHERE id=%s",
                    (Jsonb(assets), now, revision, now, job["lead_id"]),
                )
                connection.execute(
                    "INSERT INTO investment_lead_revisions(lead_id,revision,subject,driver_logic,conditions,assets_json,"
                    "source_claims_json,source_materials_json,actual_model,prompt_version,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (job["lead_id"], revision, row["subject"], row["driver_logic"], row["conditions"], Jsonb(assets),
                     Jsonb(row["source_claims_json"]), Jsonb([{"type": "identity_search", "asset_name": job["asset_name"],
                         "identity": identity, "summaries": result.get("summaries", [])[:3],
                         "citations": result.get("citations", [])[:3]}]), actual_model, identity_prompt_version, now),
                )
                return True

    def fail_identity_search(self, *, job: dict[str, Any], now: datetime, message: str, max_attempts: int) -> bool:
        exhausted = int(job["attempts"]) >= max_attempts
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                cursor = connection.execute(
                    "UPDATE investment_lead_identity_searches SET status=%s,lease_until=NULL,next_attempt_at=%s,last_error=%s,"
                    "completed_at=%s,updated_at=%s WHERE id=%s AND status='processing' AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    ("failed" if exhausted else "pending", None if exhausted else now + timedelta(minutes=15),
                     message[:1200], now if exhausted else None, now, job["id"], job["attempts"], job["lease_until"], now),
                )
                return cursor.rowcount == 1

    def dashboard_rows(
        self, *, usage_date: datetime, derive_prompt_version: str,
        playbook_prompt_version: str = "investment_leads_playbook_v1",
    ) -> dict[str, Any]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            account = connection.execute(
                "SELECT COUNT(*) AS total,COUNT(*) FILTER(WHERE investment_leads_enabled) AS enabled "
                "FROM hottopic_accounts WHERE status='followed'"
            ).fetchone()
            tasks = connection.execute(
                "SELECT stage,status,COUNT(*) AS count,MIN(created_at) AS oldest FROM investment_lead_tasks GROUP BY stage,status"
            ).fetchall()
            searches = connection.execute(
                "SELECT status,COUNT(*) AS count,MIN(created_at) AS oldest FROM investment_lead_identity_searches GROUP BY status"
            ).fetchall()
            usage = connection.execute(
                "SELECT kind,calls,input_tokens,output_tokens FROM investment_lead_usage WHERE usage_date=%s",
                (usage_date.date(),),
            ).fetchall()
            meta = connection.execute(
                "SELECT capture_started_at,worker_last_seen_at,model_error FROM investment_leads_meta WHERE singleton_key='global'"
            ).fetchone()
            counts = {(str(row["stage"]), str(row["status"])): int(row["count"] or 0) for row in tasks}
            identity_counts = {str(row["status"]): int(row["count"] or 0) for row in searches}
            oldest = [str(row["oldest"]) for row in [*tasks, *searches]
                      if row["status"] in {"pending", "processing"} and row["oldest"]]
            usage_by_kind = {str(row["kind"]): row for row in usage}
            visible = "EXISTS(SELECT 1 FROM investment_lead_revisions q WHERE q.lead_id=l.id AND q.prompt_version IN ('investment_leads_derive_v4',%s,%s))"
            active = connection.execute(
                f"SELECT COUNT(*) AS n FROM investment_leads l WHERE l.status='active' AND {visible}",
                (derive_prompt_version, playbook_prompt_version),
            ).fetchone()["n"]
            unlocated = connection.execute(
                f"SELECT COUNT(*) AS n FROM investment_leads l WHERE l.status='unlocated' AND {visible}",
                (derive_prompt_version, playbook_prompt_version),
            ).fetchone()["n"]
            playbooks = connection.execute(
                f"SELECT COUNT(*) AS n FROM investment_leads l WHERE l.playbook_json <> '{{}}'::jsonb AND {visible}",
                (derive_prompt_version, playbook_prompt_version),
            ).fetchone()["n"]
            return {"account": account, "tasks": tasks, "searches": searches, "usage": usage,
                    "meta": meta, "counts": counts, "identity_counts": identity_counts,
                    "oldest": min(oldest) if oldest else None, "usage_by_kind": usage_by_kind,
                    "input_tokens": sum(int(row["input_tokens"] or 0) for row in usage),
                    "output_tokens": sum(int(row["output_tokens"] or 0) for row in usage),
                    "active": int(active or 0), "unlocated": int(unlocated or 0), "playbooks": int(playbooks or 0)}

    def list_leads_rows(
        self, *, cutoff: datetime, query: str, account: str, unlocated_only: bool,
        impact: str, offset: int, limit: int, derive_prompt_version: str,
        mode: str = "", playbook_prompt_version: str = "investment_leads_playbook_v1",
    ) -> tuple[int, list[dict[str, Any]]]:
        clauses = ["l.last_substantive_at>=%s", "EXISTS(SELECT 1 FROM investment_lead_revisions q "
                   "WHERE q.lead_id=l.id AND q.prompt_version IN ('investment_leads_derive_v4',%s,%s))"]
        params: list[Any] = [cutoff, derive_prompt_version, playbook_prompt_version]
        if unlocated_only:
            clauses.append("l.status='unlocated'")
        if mode and mode != "all":
            if mode == "playbook":
                clauses.append("l.playbook_json <> '{}'::jsonb")
            elif mode == "standard":
                clauses.append("l.playbook_json = '{}'::jsonb")
            else:
                raise ValueError("不支持的投资线索模式筛选")
        if impact:
            if impact not in {"high", "medium", "low", "unknown"}:
                raise ValueError("不支持的预期影响筛选")
            clauses.append("EXISTS(SELECT 1 FROM jsonb_array_elements(l.assets_json) a WHERE COALESCE(a->'assessment'->>'impact','unknown')=%s)")
            params.append(impact)
        if query.strip():
            clauses.append("LOWER(l.subject || ' ' || l.driver_logic || ' ' || l.conditions || ' ' || l.assets_json::text || ' ' || l.playbook_json::text) LIKE %s")
            params.append(f"%{query.strip().lower()}%")
        if account.strip():
            clauses.append("EXISTS(SELECT 1 FROM investment_lead_sources s JOIN investment_lead_observations o "
                           "ON o.material_id=s.material_id AND o.content_hash=s.content_hash WHERE s.lead_id=l.id AND o.screen_name_lower=%s)")
            params.append(account.strip().lstrip("@").lower())
        where = " WHERE " + " AND ".join(clauses)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) AS n FROM investment_leads l{where}", params).fetchone()["n"])
            rows = connection.execute(
                "SELECT l.*, (SELECT string_agg(DISTINCT o.screen_name_lower,',') FROM investment_lead_sources s "
                "JOIN investment_lead_observations o ON o.material_id=s.material_id AND o.content_hash=s.content_hash "
                "WHERE s.lead_id=l.id) AS accounts "
                f"FROM investment_leads l{where} ORDER BY l.last_substantive_at DESC,l.id LIMIT %s OFFSET %s",
                [*params, limit, offset],
            ).fetchall()
            return total, [dict(row) for row in rows]

    def set_playbook_feedback(
        self, *, lead_id: str, feedback: str, now: datetime,
    ) -> dict[str, Any] | None:
        clean_feedback = feedback.strip()
        if clean_feedback not in {"", "没必要深度剧本"}:
            raise ValueError("不支持的深度剧本反馈标记")
        feedback_at = now if clean_feedback else None
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row = connection.execute(
                    "UPDATE investment_leads SET playbook_feedback=%s,playbook_feedback_at=%s,updated_at=%s "
                    "WHERE id=%s RETURNING id,playbook_feedback,playbook_feedback_at",
                    (clean_feedback, feedback_at, now, lead_id),
                ).fetchone()
                if row is None:
                    return None
                updated_materials = connection.execute(
                    "UPDATE investment_lead_materials m SET playbook_feedback=%s "
                    "WHERE EXISTS(SELECT 1 FROM investment_lead_sources s "
                    "WHERE s.lead_id=%s AND s.material_id=m.material_id AND s.content_hash=m.content_hash)",
                    (clean_feedback, lead_id),
                ).rowcount
                return {
                    "leadId": str(row["id"]),
                    "playbookFeedback": str(row["playbook_feedback"] or ""),
                    "playbookFeedbackAt": row["playbook_feedback_at"],
                    "updatedMaterials": int(updated_materials or 0),
                }

    def lead_detail_rows(self, lead_id: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            lead = connection.execute("SELECT * FROM investment_leads WHERE id=%s", (lead_id,)).fetchone()
            if lead is None:
                return None
            sources = connection.execute(
                "SELECT s.material_id,s.content_hash,s.role,s.created_at,m.payload_json,m.posted_at,m.activity_at,"
                "m.activity_time_complete,string_agg(DISTINCT o.screen_name_lower,',') AS accounts "
                "FROM investment_lead_sources s JOIN investment_lead_materials m ON m.material_id=s.material_id "
                "AND m.content_hash=s.content_hash LEFT JOIN investment_lead_observations o "
                "ON o.material_id=s.material_id AND o.content_hash=s.content_hash WHERE s.lead_id=%s "
                "GROUP BY s.material_id,s.content_hash,s.role,s.created_at,m.payload_json,m.posted_at,m.activity_at,m.activity_time_complete "
                "ORDER BY s.created_at", (lead_id,),
            ).fetchall()
            revisions = connection.execute(
                "SELECT revision,subject,driver_logic,conditions,assets_json,source_claims_json,actual_model,created_at "
                "FROM investment_lead_revisions WHERE lead_id=%s ORDER BY revision DESC", (lead_id,),
            ).fetchall()
            searches = connection.execute(
                "SELECT asset_name,status,result_json,last_error,updated_at FROM investment_lead_identity_searches "
                "WHERE lead_id=%s ORDER BY created_at", (lead_id,),
            ).fetchall()
            return {"lead": dict(lead), "sources": [dict(row) for row in sources],
                    "revisions": [dict(row) for row in revisions], "searches": [dict(row) for row in searches]}

    def retry_failed(self, *, stage: str, limit: int, now: datetime) -> int:
        if stage and stage not in {"scan", "derive", "supplement", "playbook", "identity_search"}:
            raise ValueError("不支持的投资线索任务阶段")
        safe_limit = min(500, max(1, int(limit)))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                count = 0
                if stage != "identity_search":
                    clause = " AND stage=%s" if stage else ""
                    values = (stage, safe_limit) if stage else (safe_limit,)
                    rows = connection.execute(
                        "SELECT id FROM investment_lead_tasks WHERE status='failed'" + clause +
                        " ORDER BY updated_at LIMIT %s FOR UPDATE SKIP LOCKED", values,
                    ).fetchall()
                    ids = [row["id"] for row in rows]
                    if ids:
                        cursor = connection.execute(
                            "UPDATE investment_lead_tasks t SET status='pending',attempts=0,next_attempt_at=NULL,lease_until=NULL,"
                            "last_error=NULL,completed_at=NULL,updated_at=%s WHERE id=ANY(%s) AND EXISTS("
                            "SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                            "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                            "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled)",
                            (now, ids),
                        )
                        count += cursor.rowcount
                if stage in {"", "identity_search"} and (not stage or count < safe_limit):
                    search_limit = safe_limit - count if not stage else safe_limit
                    rows = connection.execute(
                        "SELECT s.id FROM investment_lead_identity_searches s WHERE s.status='failed' "
                        "AND EXISTS(SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations o "
                        "ON o.material_id=ls.material_id AND o.content_hash=ls.content_hash JOIN hottopic_accounts a "
                        "ON a.screen_name_lower=o.screen_name_lower WHERE ls.lead_id=s.lead_id AND a.status='followed' "
                        "AND a.investment_leads_enabled) ORDER BY s.updated_at LIMIT %s FOR UPDATE OF s SKIP LOCKED",
                        (search_limit,),
                    ).fetchall()
                    ids = [row["id"] for row in rows]
                    if ids:
                        cursor = connection.execute(
                            "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,"
                            "lease_until=NULL,last_error=NULL,completed_at=NULL,updated_at=%s WHERE id=ANY(%s)", (now, ids),
                        )
                        count += cursor.rowcount
                return count

    def save_derive_result(
        self, *, job: dict[str, Any], leads: list[dict[str, Any]], related_events: list[dict[str, Any]],
        actual_model: str, fallback_reason: str | None, input_tokens: int, output_tokens: int,
        now: datetime, derive_prompt_version: str, identity_prompt_version: str, max_attempts: int,
        playbook_prompt_version: str = "investment_leads_playbook_v1",
    ) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_tasks WHERE id=%s AND stage='derive' AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                eligible = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=%s AND o.content_hash=%s "
                    "AND a.status='followed' AND a.investment_leads_enabled) AS eligible",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()["eligible"]
                if not eligible:
                    connection.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=%s,updated_at=%s "
                        "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                        (now, now, job["id"], job["attempts"], job["lease_until"], now),
                    )
                    return False
                linked = {(job["material_id"], job["content_hash"])}
                linked.update((str(item.get("material_id") or ""), str(item.get("content_hash") or "")) for item in related_events)
                result_count = 0
                has_high_impact = False
                for lead in leads:
                    sources = [pair for pair in linked if pair[0] in (lead.get("source_material_ids") or [])]
                    if not sources:
                        sources = [(job["material_id"], job["content_hash"])]
                    self._save_lead(connection, lead, sources, actual_model, now, derive_prompt_version, identity_prompt_version)
                    result_count += 1
                    for asset in lead.get("assets") or []:
                        if isinstance(asset, dict) and isinstance(asset.get("assessment"), dict) and asset["assessment"].get("impact") == "high":
                            has_high_impact = True
                if has_high_impact and playbook_prompt_version:
                    connection.execute(
                        "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                        "VALUES(%s,%s,'playbook',%s,'pending',%s,%s) ON CONFLICT(material_id,content_hash,stage,prompt_version) DO NOTHING",
                        (job["material_id"], job["content_hash"], playbook_prompt_version, now, now),
                    )
                status = "succeeded" if result_count else "no_candidate"
                connection.execute(
                    "UPDATE investment_lead_tasks SET status=%s,lease_until=NULL,next_attempt_at=NULL,actual_model=%s,"
                    "fallback_reason=%s,input_tokens=%s,output_tokens=%s,last_error=NULL,completed_at=%s,updated_at=%s "
                    "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (status, actual_model, fallback_reason, input_tokens, output_tokens, now, now,
                     job["id"], job["attempts"], job["lease_until"], now),
                )
                return True

    def claim_playbook_batch(
        self, *, now: datetime, lease: timedelta, limit: int, max_attempts: int,
    ) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                rows = connection.execute(
                    "SELECT t.id,t.material_id,t.content_hash,t.attempts,t.prompt_version,e.classification,e.playbook_trigger,"
                    "e.subject,e.action,e.change_object,e.event_time,e.impact_relations_json,e.summary,e.reason,m.payload_json "
                    "FROM investment_lead_tasks t JOIN investment_lead_events e "
                    "ON e.material_id=t.material_id AND e.content_hash=t.content_hash "
                    "AND e.id=(SELECT MAX(latest.id) FROM investment_lead_events latest "
                    "WHERE latest.material_id=t.material_id AND latest.content_hash=t.content_hash) "
                    "JOIN investment_lead_materials m ON m.material_id=t.material_id AND m.content_hash=t.content_hash "
                    "WHERE t.stage='playbook' AND t.status='pending' AND t.attempts<%s "
                    "AND (t.next_attempt_at IS NULL OR t.next_attempt_at<=%s) "
                    "AND EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=t.material_id "
                    "AND o.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled) "
                    "ORDER BY t.created_at,t.id FOR UPDATE OF t SKIP LOCKED LIMIT %s",
                    (max_attempts, now, max(1, limit)),
                ).fetchall()
                jobs = []
                for row in rows:
                    updated = connection.execute(
                        "UPDATE investment_lead_tasks SET status='processing',attempts=attempts+1,lease_until=%s,updated_at=%s "
                        "WHERE id=%s AND status='pending' RETURNING attempts,lease_until",
                        (now + lease, now, row["id"]),
                    ).fetchone()
                    if updated is not None:
                        job = dict(row)
                        job.update(updated)
                        jobs.append(job)
                return jobs

    def recent_playbooks(self, *, cutoff: datetime, limit: int = 50) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT id,subject,driver_logic,source_claims_json,playbook_json,last_substantive_at "
                "FROM investment_leads WHERE playbook_json <> '{}'::jsonb AND last_substantive_at>=%s "
                "ORDER BY last_substantive_at DESC LIMIT %s",
                (cutoff, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def attach_playbook_source(self, *, job: dict[str, Any], lead_id: str, now: datetime) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_tasks WHERE id=%s AND stage='playbook' AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                connection.execute(
                    "INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                    "VALUES(%s,%s,%s,'evidence',%s) ON CONFLICT(lead_id,material_id,content_hash,role) DO NOTHING",
                    (lead_id, job["material_id"], job["content_hash"], now),
                )
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='succeeded',lease_until=NULL,next_attempt_at=NULL,"
                    "last_error=NULL,completed_at=%s,updated_at=%s WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (now, now, job["id"], job["attempts"], job["lease_until"], now),
                )
                return True

    def save_playbook_result(
        self, *, job: dict[str, Any], lead: dict[str, Any], playbook: dict[str, Any],
        actual_model: str, fallback_reason: str | None, input_tokens: int, output_tokens: int,
        now: datetime, playbook_prompt_version: str,
    ) -> bool:
        from uuid import uuid4

        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_tasks WHERE id=%s AND stage='playbook' AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                eligible = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=%s AND o.content_hash=%s "
                    "AND a.status='followed' AND a.investment_leads_enabled) AS eligible",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()["eligible"]
                if not eligible:
                    connection.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=%s,updated_at=%s "
                        "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                        (now, now, job["id"], job["attempts"], job["lease_until"], now),
                    )
                    return False
                existing_source = connection.execute(
                    "SELECT l.* FROM investment_leads l JOIN investment_lead_sources s ON s.lead_id=l.id "
                    "WHERE s.material_id=%s AND s.content_hash=%s ORDER BY l.last_substantive_at DESC LIMIT 1 FOR UPDATE OF l",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()
                assets = lead.get("assets") or []
                claims = lead.get("source_claims") or []
                sources = [(job["material_id"], job["content_hash"])]
                if existing_source is not None:
                    lead_id = str(existing_source["id"])
                    old_assets = existing_source["assets_json"] if isinstance(existing_source["assets_json"], list) else []
                    merged_assets = list(old_assets)
                    seen_names = {str(a.get("name") or "").casefold() for a in merged_assets if isinstance(a, dict)}
                    for asset in assets:
                        if isinstance(asset, dict) and str(asset.get("name") or "").casefold() not in seen_names:
                            merged_assets.append(asset)
                            seen_names.add(str(asset.get("name") or "").casefold())
                    revision = int(existing_source["revision"]) + 1
                    status = "active" if merged_assets else str(existing_source["status"] or "active")
                    connection.execute(
                        "UPDATE investment_leads SET assets_json=%s,playbook_json=%s,last_substantive_at=%s,"
                        "revision=%s,status=%s,updated_at=%s WHERE id=%s",
                        (Jsonb(merged_assets), Jsonb(playbook), now, revision, status, now, lead_id),
                    )
                    connection.execute(
                        "INSERT INTO investment_lead_revisions(lead_id,revision,subject,driver_logic,conditions,assets_json,"
                        "source_claims_json,source_materials_json,actual_model,prompt_version,created_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (lead_id, revision, existing_source["subject"], lead["driver_logic"], lead.get("conditions") or existing_source["conditions"],
                         Jsonb(merged_assets), Jsonb(existing_source["source_claims_json"]), Jsonb(sources),
                         actual_model, playbook_prompt_version, now),
                    )
                else:
                    lead_id = str(uuid4())
                    status = "active" if assets else "unlocated"
                    connection.execute(
                        "INSERT INTO investment_leads(id,subject,driver_logic,conditions,assets_json,source_claims_json,playbook_json,"
                        "first_seen_at,last_substantive_at,revision,status,created_at,updated_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)",
                        (lead_id, lead["subject"], lead["driver_logic"], lead.get("conditions") or "",
                         Jsonb(assets), Jsonb(claims), Jsonb(playbook), now, now, status, now, now),
                    )
                    connection.execute(
                        "INSERT INTO investment_lead_revisions(lead_id,revision,subject,driver_logic,conditions,assets_json,"
                        "source_claims_json,source_materials_json,actual_model,prompt_version,created_at) "
                        "VALUES(%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (lead_id, lead["subject"], lead["driver_logic"], lead.get("conditions") or "",
                         Jsonb(assets), Jsonb(claims), Jsonb(sources), actual_model, playbook_prompt_version, now),
                    )
                    connection.execute(
                        "INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                        "VALUES(%s,%s,%s,'evidence',%s) ON CONFLICT(lead_id,material_id,content_hash,role) DO NOTHING",
                        (lead_id, job["material_id"], job["content_hash"], now),
                    )
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='succeeded',lease_until=NULL,next_attempt_at=NULL,actual_model=%s,"
                    "fallback_reason=%s,input_tokens=%s,output_tokens=%s,last_error=NULL,completed_at=%s,updated_at=%s "
                    "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (actual_model, fallback_reason, input_tokens, output_tokens, now, now,
                     job["id"], job["attempts"], job["lease_until"], now),
                )
                return True

    @staticmethod
    def _save_lead(connection: Any, lead: dict[str, Any], sources: list[tuple[str, str]], actual_model: str,
                    now: datetime, derive_prompt_version: str, identity_prompt_version: str) -> str:
        from uuid import uuid4

        lead_id = str(lead.get("matched_lead_id") or "")
        row = connection.execute("SELECT * FROM investment_leads WHERE id=%s FOR UPDATE", (lead_id,)).fetchone() if lead_id else None
        assets = lead.get("assets") or []
        claims = lead.get("source_claims") or []
        if row is None:
            lead_id = str(uuid4())
            status = "active" if assets else "unlocated"
            connection.execute(
                "INSERT INTO investment_leads(id,subject,driver_logic,conditions,assets_json,source_claims_json,first_seen_at,"
                "last_substantive_at,revision,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s)",
                (lead_id, lead["subject"], lead["driver_logic"], lead.get("conditions") or "", Jsonb(assets), Jsonb(claims),
                 now, now, status, now, now),
            )
            revision, substantive = 1, True
        else:
            old = dict(row)
            substantive = (old["subject"] != lead["subject"] or old["driver_logic"] != lead["driver_logic"]
                           or old["conditions"] != (lead.get("conditions") or "") or old["assets_json"] != assets
                           or not connection.execute(
                               "SELECT 1 FROM investment_lead_revisions WHERE lead_id=%s AND prompt_version=%s LIMIT 1",
                               (lead_id, derive_prompt_version),
                           ).fetchone())
            revision = int(old["revision"]) + int(substantive)
            status = "active" if assets else "unlocated"
            if substantive:
                connection.execute(
                    "UPDATE investment_leads SET subject=%s,driver_logic=%s,conditions=%s,assets_json=%s,source_claims_json=%s,"
                    "last_substantive_at=%s,revision=%s,status=%s,updated_at=%s,replay_json='{}'::jsonb WHERE id=%s",
                    (lead["subject"], lead["driver_logic"], lead.get("conditions") or "", Jsonb(assets), Jsonb(claims),
                     now, revision, status, now, lead_id),
                )
        if row is None or substantive:
            connection.execute(
                "INSERT INTO investment_lead_revisions(lead_id,revision,subject,driver_logic,conditions,assets_json,"
                "source_claims_json,source_materials_json,actual_model,prompt_version,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (lead_id, revision, lead["subject"], lead["driver_logic"], lead.get("conditions") or "", Jsonb(assets),
                 Jsonb(claims), Jsonb(sources), actual_model, derive_prompt_version, now),
            )
        for material_id, content_hash in sources:
            if material_id and content_hash:
                connection.execute(
                    "INSERT INTO investment_lead_sources(lead_id,material_id,content_hash,role,created_at) "
                    "VALUES(%s,%s,%s,'evidence',%s) ON CONFLICT DO NOTHING",
                    (lead_id, material_id, content_hash, now),
                )
        for asset in assets:
            if not isinstance(asset, dict) or not str(asset.get("name") or "").strip() or str(asset.get("ticker") or "").strip():
                continue
            asset_name = str(asset["name"]).strip()
            normalized_name = " ".join(asset_name.casefold().split())
            asset_key = sha256(normalized_name.encode("utf-8")).hexdigest() if normalized_name else ""
            context = {"subject": lead["subject"], "driver_logic": lead["driver_logic"],
                       "asset_direction": asset.get("direction") or "", "asset_relation": asset.get("relation") or ""}
            connection.execute(
                "INSERT INTO investment_lead_identity_searches(lead_id,asset_key,asset_name,context_json,status,created_at,updated_at) "
                "VALUES(%s,%s,%s,%s,'pending',%s,%s) ON CONFLICT(lead_id,asset_key) DO NOTHING",
                (lead_id, asset_key, asset_name, Jsonb(context), now, now),
            )
            connection.execute(
                "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,lease_until=NULL,"
                "last_error=NULL,completed_at=NULL,updated_at=%s WHERE lead_id=%s AND asset_key=%s AND status='ignored'",
                (now, lead_id, asset_key),
            )
        return lead_id

    def fail_scan_task(
        self, *, task_id: int, now: datetime, message: str, attempts: int,
        lease_until: datetime, max_attempts: int,
    ) -> bool:
        exhausted = attempts >= max_attempts
        next_attempt_at = None if exhausted else now + timedelta(minutes=15)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                cursor = connection.execute(
                    "UPDATE investment_lead_tasks SET status=%s,lease_until=NULL,next_attempt_at=%s,last_error=%s,"
                    "completed_at=%s,updated_at=%s WHERE id=%s AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    ("failed" if exhausted else "pending", next_attempt_at, message[:1200], now if exhausted else None,
                     now, task_id, attempts, lease_until, now),
                )
                return cursor.rowcount == 1

    def record_usage(
        self, *, kind: str, usage_date: datetime, calls: int, input_tokens: int,
        output_tokens: int, updated_at: datetime,
    ) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO investment_lead_usage(usage_date,kind,calls,input_tokens,output_tokens,updated_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(usage_date,kind) DO UPDATE SET "
                    "calls=investment_lead_usage.calls+EXCLUDED.calls,"
                    "input_tokens=investment_lead_usage.input_tokens+EXCLUDED.input_tokens,"
                    "output_tokens=investment_lead_usage.output_tokens+EXCLUDED.output_tokens,updated_at=EXCLUDED.updated_at",
                    (usage_date.date(), kind, calls, input_tokens, output_tokens, updated_at),
                )

    def save_scan_result(
        self, *, job: dict[str, Any], result: dict[str, Any], actual_model: str,
        fallback_reason: str | None, input_tokens: int, output_tokens: int, now: datetime,
        max_attempts: int,
    ) -> bool:
        classification = str(result.get("classification") or "incomplete")
        if classification not in {"driver_candidate", "composable", "no_candidate", "incomplete"}:
            classification = "incomplete"
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                eligible = connection.execute(
                    "SELECT EXISTS(SELECT 1 FROM investment_lead_observations o JOIN hottopic_accounts a "
                    "ON a.screen_name_lower=o.screen_name_lower WHERE o.material_id=%s AND o.content_hash=%s "
                    "AND a.status='followed' AND a.investment_leads_enabled) AS eligible",
                    (job["material_id"], job["content_hash"]),
                ).fetchone()["eligible"]
                current = connection.execute(
                    "SELECT 1 FROM investment_lead_tasks WHERE id=%s AND status='processing' "
                    "AND attempts=%s AND lease_until=%s AND lease_until>%s FOR UPDATE",
                    (job["id"], job["attempts"], job["lease_until"], now),
                ).fetchone()
                if current is None:
                    return False
                if not eligible:
                    connection.execute(
                        "UPDATE investment_lead_tasks SET status='ignored',lease_until=NULL,completed_at=%s,updated_at=%s "
                        "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                        (now, now, job["id"], job["attempts"], job["lease_until"], now),
                    )
                    return False
                connection.execute(
                    "UPDATE investment_lead_materials SET completeness=%s WHERE material_id=%s AND content_hash=%s",
                    ("incomplete" if classification == "incomplete" else "complete", job["material_id"], job["content_hash"]),
                )
                playbook_trigger = str(result.get("playbook_trigger") or "")
                if playbook_trigger not in {"anomaly_behavior", "major_catalyst"} or classification != "driver_candidate":
                    playbook_trigger = ""
                connection.execute(
                    "INSERT INTO investment_lead_events(material_id,content_hash,classification,playbook_trigger,subject,action,change_object,event_time,"
                    "impact_relations_json,summary,reason,actual_model,prompt_version,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(material_id,content_hash,prompt_version) DO UPDATE SET classification=EXCLUDED.classification,"
                    "playbook_trigger=EXCLUDED.playbook_trigger,"
                    "subject=EXCLUDED.subject,action=EXCLUDED.action,change_object=EXCLUDED.change_object,event_time=EXCLUDED.event_time,"
                    "impact_relations_json=EXCLUDED.impact_relations_json,summary=EXCLUDED.summary,reason=EXCLUDED.reason,"
                    "actual_model=EXCLUDED.actual_model,created_at=EXCLUDED.created_at",
                    (job["material_id"], job["content_hash"], classification, playbook_trigger, result.get("subject") or "",
                     result.get("action") or "", result.get("change_object") or "", result.get("event_time") or "",
                     Jsonb(result.get("impact_relations") or []), result.get("summary") or "", result.get("reason") or "",
                     actual_model, job["prompt_version"], now),
                )
                status = "no_candidate" if classification == "no_candidate" else "needs_material" if classification == "incomplete" else "succeeded"
                connection.execute(
                    "UPDATE investment_lead_tasks SET status=%s,lease_until=NULL,next_attempt_at=NULL,actual_model=%s,"
                    "fallback_reason=%s,input_tokens=%s,output_tokens=%s,last_error=NULL,completed_at=%s,updated_at=%s "
                    "WHERE id=%s AND attempts=%s AND lease_until=%s AND lease_until>%s",
                    (status, actual_model, fallback_reason, input_tokens, output_tokens, now, now,
                     job["id"], job["attempts"], job["lease_until"], now),
                )
                if classification in {"driver_candidate", "composable"}:
                    connection.execute(
                        "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                        "VALUES(%s,%s,'derive',%s,'pending',%s,%s) ON CONFLICT(material_id,content_hash,stage,prompt_version) DO NOTHING",
                        (job["material_id"], job["content_hash"], job["derive_prompt_version"], now, now),
                    )
                if playbook_trigger and job.get("playbook_prompt_version"):
                    connection.execute(
                        "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                        "VALUES(%s,%s,'playbook',%s,'pending',%s,%s) ON CONFLICT(material_id,content_hash,stage,prompt_version) DO NOTHING",
                        (job["material_id"], job["content_hash"], job["playbook_prompt_version"], now, now),
                    )
                if classification == "incomplete":
                    row = connection.execute(
                        "SELECT payload_json FROM investment_lead_materials WHERE material_id=%s AND content_hash=%s",
                        (job["material_id"], job["content_hash"]),
                    ).fetchone()
                    material = row["payload_json"] if row else {}
                    if not material.get("supplement_attempted"):
                        connection.execute(
                            "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                            "VALUES(%s,%s,'supplement','investment_leads_supplement_v1','pending',%s,%s) "
                            "ON CONFLICT(material_id,content_hash,stage,prompt_version) DO NOTHING",
                            (job["material_id"], job["content_hash"], now, now),
                        )
                return True

    def maintain(
        self, *, now: datetime, cutoff: datetime, row_limit: int = 100,
        transaction_rows: int = 25, budget_seconds: float = 0.05,
    ) -> dict[str, int]:
        started = time.monotonic()
        counts = {"tasks": 0, "identity_searches": 0, "events": 0, "materials": 0, "continued": 0}
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            meta = connection.execute(
                "SELECT last_maintenance_at,maintenance_next_at FROM investment_leads_meta WHERE singleton_key='global'"
            ).fetchone()
            if meta:
                if meta["maintenance_next_at"] and now < meta["maintenance_next_at"]:
                    return counts
                if not meta["maintenance_next_at"] and meta["last_maintenance_at"] and now - meta["last_maintenance_at"] < timedelta(hours=1):
                    return counts

            for table, counter in (
                ("investment_lead_tasks", "tasks"),
                ("investment_lead_identity_searches", "identity_searches"),
            ):
                if time.monotonic() - started >= budget_seconds:
                    counts["continued"] = 1
                    break
                active_statuses = ("pending", "failed", "budget_paused")
                where = "status = ANY(%s) AND created_at < %s"
                rows = connection.execute(
                    f"SELECT id FROM {table} WHERE {where} ORDER BY created_at,id LIMIT %s",
                    (list(active_statuses), cutoff, row_limit + 1),
                ).fetchall()
                for start in range(0, min(len(rows), row_limit), transaction_rows):
                    chunk = rows[start:start + transaction_rows]
                    with connection.transaction():
                        cursor = connection.execute(
                            f"UPDATE {table} SET status='expired',lease_until=NULL,"
                            "last_error=COALESCE(last_error,'超过材料保留期限'),completed_at=%s,updated_at=%s "
                            "WHERE id = ANY(%s) AND status = ANY(%s) AND created_at < %s",
                            (now, now, [row["id"] for row in chunk], list(active_statuses), cutoff),
                        )
                        counts[counter] += cursor.rowcount
                counts["continued"] |= int(len(rows) > row_limit)

            for stage, table, time_column in (
                ("events", "investment_lead_events", "created_at"),
                ("materials", "investment_lead_materials", "last_observed_at"),
            ):
                if time.monotonic() - started >= budget_seconds:
                    counts["continued"] = 1
                    break
                progress = connection.execute(
                    "SELECT cursor_time,cursor_id,cursor_value FROM investment_lead_maintenance_progress WHERE stage=%s",
                    (stage,),
                ).fetchone()
                cursor_time = progress["cursor_time"] if progress else None
                cursor_id = progress["cursor_id"] if progress else None
                cursor_value = progress["cursor_value"] if progress else None
                columns = "id,created_at" if stage == "events" else "material_id,content_hash,last_observed_at"
                if stage == "events":
                    cursor_clause = "" if cursor_time is None else "AND (created_at,id)>(%s,%s)"
                    query = f"SELECT {columns} FROM {table} WHERE {time_column}<%s {cursor_clause} ORDER BY created_at,id LIMIT %s"
                    params = (cutoff, row_limit + 1) if cursor_time is None else (cutoff, cursor_time, int(cursor_id), row_limit + 1)
                else:
                    cursor_clause = "" if cursor_time is None else "AND (last_observed_at,material_id,content_hash)>(%s,%s,%s)"
                    query = f"SELECT {columns} FROM {table} WHERE {time_column}<%s {cursor_clause} ORDER BY last_observed_at,material_id,content_hash LIMIT %s"
                    params = (cutoff, row_limit + 1) if cursor_time is None else (cutoff, cursor_time, cursor_id, cursor_value, row_limit + 1)
                rows = connection.execute(query, params).fetchall()
                processed = 0
                for start in range(0, min(len(rows), row_limit), transaction_rows):
                    if time.monotonic() - started >= budget_seconds:
                        counts["continued"] = 1
                        break
                    chunk = rows[start:start + transaction_rows]
                    with connection.transaction():
                        for item in chunk:
                            if stage == "events":
                                cursor = connection.execute(
                                    "DELETE FROM investment_lead_events e WHERE id=%s AND created_at<%s "
                                    "AND NOT EXISTS(SELECT 1 FROM investment_lead_sources s "
                                    "WHERE s.material_id=e.material_id AND s.content_hash=e.content_hash)",
                                    (item["id"], cutoff),
                                )
                                counts["events"] += cursor.rowcount
                                next_time, next_id, next_value = item["created_at"], str(item["id"]), ""
                            else:
                                cursor = connection.execute(
                                    "DELETE FROM investment_lead_materials m WHERE material_id=%s AND content_hash=%s "
                                    "AND last_observed_at<%s AND NOT EXISTS(SELECT 1 FROM investment_lead_sources s "
                                    "WHERE s.material_id=m.material_id AND s.content_hash=m.content_hash)",
                                    (item["material_id"], item["content_hash"], cutoff),
                                )
                                counts["materials"] += cursor.rowcount
                                next_time, next_id, next_value = item["last_observed_at"], item["material_id"], item["content_hash"]
                        connection.execute(
                            "INSERT INTO investment_lead_maintenance_progress(stage,cursor_time,cursor_id,cursor_value,updated_at) "
                            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(stage) DO UPDATE SET cursor_time=EXCLUDED.cursor_time,"
                            "cursor_id=EXCLUDED.cursor_id,cursor_value=EXCLUDED.cursor_value,updated_at=EXCLUDED.updated_at",
                            (stage, next_time, next_id, next_value, now),
                        )
                    processed += len(chunk)
                if len(rows) <= row_limit and processed == len(rows):
                    with connection.transaction():
                        connection.execute("DELETE FROM investment_lead_maintenance_progress WHERE stage=%s", (stage,))
                else:
                    counts["continued"] = 1

            next_at = now + timedelta(seconds=10) if counts["continued"] else None
            with connection.transaction():
                connection.execute(
                    "UPDATE investment_leads_meta SET last_maintenance_at=CASE WHEN %s::timestamptz IS NULL THEN %s ELSE last_maintenance_at END,"
                    "maintenance_next_at=%s WHERE singleton_key='global'",
                    (next_at, now, next_at),
                )
        return counts


def _asset_key(name: str) -> str:
    normalized = " ".join(name.casefold().split())
    return sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""
