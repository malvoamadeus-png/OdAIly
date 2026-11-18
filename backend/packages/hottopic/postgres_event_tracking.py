"""Native PostgreSQL persistence for HotTopic event tracking.

The repository owns short PostgreSQL transactions only.  Callers perform AI
and collector work before entering the write methods, then pass the captured
snapshot or lease token back for fencing.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable

from psycopg.types.json import Jsonb as PGJsonb

from psycopg.types.json import Jsonb

from packages.common.pg_native import connect_postgres
from packages.common.storage import PostgresSettings


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def _iso(value: Any) -> str:
    return _dt(value).isoformat()


def _stable(*parts: Any) -> str:
    return hashlib.sha1("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:24]


class PostgresEventTrackingRepository:
    def __init__(self, *, schema: str = "hottopic", settings: PostgresSettings | None = None) -> None:
        self.schema = schema
        self.settings = settings
        self.application_name = "odaily-hottopic-event-tracking"
        self._publication_refresh_cursor: tuple[datetime, str] | None = None

    def _connect(self):
        return connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings)

    def _claim_work(self, work_key: str, *, now: datetime, seconds: int = 180) -> str | None:
        token = uuid.uuid4().hex
        with self._connect() as conn:
            with conn.transaction():
                row = conn.execute("INSERT INTO event_tracking_work_leases(work_key,claim_token,claimed_at,lease_until,attempts) VALUES(%s,%s,%s,%s,1) ON CONFLICT(work_key) DO UPDATE SET claim_token=EXCLUDED.claim_token,claimed_at=EXCLUDED.claimed_at,lease_until=EXCLUDED.lease_until,attempts=event_tracking_work_leases.attempts+1 WHERE event_tracking_work_leases.lease_until<=%s RETURNING claim_token", (work_key,token,now,now+timedelta(seconds=seconds),now)).fetchone()
                return str(row["claim_token"]) if row else None

    def _owns_work(self, conn: Any, work_key: str, token: str, *, now: datetime) -> bool:
        return conn.execute("SELECT 1 FROM event_tracking_work_leases WHERE work_key=%s AND claim_token=%s AND lease_until>%s FOR UPDATE", (work_key,token,now)).fetchone() is not None

    def _current_topic_snapshot_hash(self, conn: Any, topic_id: str) -> str | None:
        row=conn.execute("SELECT t.*,b.title AS brief_title,b.brief FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) WHERE t.topic_id=%s AND t.matching_status='active' AND t.visibility='visible'",(topic_id,)).fetchone()
        if not row:return None
        evidence=conn.execute("SELECT c.claim_text,c.action_or_issue,c.stance,c.evidence_span,c.created_at,ci.activity_account,ci.expanded_text,ci.source_url FROM memberships m JOIN claims c ON c.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=c.content_item_id LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' ORDER BY c.created_at DESC LIMIT 10",(topic_id,)).fetchall()
        participants=conn.execute("SELECT activity_account FROM topic_participations WHERE topic_id=%s ORDER BY last_participation_at DESC LIMIT 20",(topic_id,)).fetchall()
        snap={"topic_id":str(row["topic_id"]),"title":str(row["brief_title"] or row["working_title"]),"brief":str(row["brief"] or ""),"canonical_subject":str(row["canonical_subject"]),"core_entities":row["core_entities_json"] or [],"event_or_issue":str(row["event_or_issue"]),"started_at":_iso(row["started_at"]),"first_seen_at":_iso(row["first_seen_at"]),"last_evidence_at":_iso(row["last_evidence_at"]) if row["last_evidence_at"] else None,"hotness":float(row["hotness_score"] or 0),"participants":[str(x["activity_account"]) for x in participants],"evidence":[{"claim":str(x["claim_text"]),"action_or_issue":str(x["action_or_issue"]),"stance":str(x["stance"]),"evidence":str(x["evidence_span"]),"account":str(x["activity_account"]),"text":str(x["expanded_text"]),"source_url":str(x["source_url"]),"created_at":_iso(x["created_at"])} for x in evidence]}
        return hashlib.sha256(json.dumps(snap,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()

    def _release_work(self, work_key: str, token: str) -> None:
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("DELETE FROM event_tracking_work_leases WHERE work_key=%s AND claim_token=%s", (work_key,token))

    def task_dispatcher(self):
        from .event_tracking import PostgresEventTrackingTaskDispatcher
        return PostgresEventTrackingTaskDispatcher(self)

    def ensure_primary_task(self, *, event: dict[str, Any], cycle: dict[str, Any], update: dict[str, Any], post: dict[str, Any]) -> int:
        from packages.common.pg_native import connect_postgres
        from packages.hottopic.event_tracking import EVENT_TRACKING_SOURCE

        now = datetime.now(UTC); tweet_id = str(post["tweet_id"])
        raw_identity = update.get("identity_result_json") or {}
        if isinstance(raw_identity, str):
            raw_identity = json.loads(raw_identity)
        identity = {"decision": raw_identity.get("decision"), "entity": str(raw_identity.get("entity") or "").strip(), "role": raw_identity.get("role"), "reason": raw_identity.get("reason"), "evidence": raw_identity.get("evidence", [])[:4]}
        identity["entity"] = identity["entity"] or str(post.get("author_name") or post.get("account_name") or update["handle_lower"])
        event_meta = {"event_id":event["event_id"],"cycle_id":cycle["cycle_id"],"update_id":update["update_id"],"classification":update["classification"],"fact_summary":update.get("fact_summary") or "","difference_from_timeline":update.get("difference_text") or "","confirmed_facts":update.get("confirmed_facts_json") or [],"unconfirmed_claims":update.get("unconfirmed_claims_json") or [],"candidate_handle":post.get("account_screen_name") or post.get("author_screen_name") or update["handle_lower"],"candidate_verification":"ai_profile_assessed_as_event_actor","account_identity":identity}
        metadata = {"source_kind":EVENT_TRACKING_SOURCE,"author_display_name":identity["entity"],"author_username":post.get("author_screen_name") or post.get("account_screen_name") or update["handle_lower"],"event_tracking":event_meta}
        content = str(post.get("expanded_text") or post.get("text") or "").strip()
        if not content:
            raise ValueError("event tracking post had no content")
        with connect_postgres(schema="main", application_name="odaily-event-task-create") as conn:
            with conn.transaction():
                row = conn.execute("INSERT INTO tasks(source,source_item_id,source_url,title,content,published_at,raw_payload,metadata,status,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'judged',%s,%s) ON CONFLICT(source,source_item_id) DO NOTHING RETURNING id",(EVENT_TRACKING_SOURCE,tweet_id,post.get("url"),f"{event['title']}：账号更新",content,_dt(post.get("created_at_iso") or post.get("created_at") or now),PGJsonb({"event_tracking":event_meta,"candidate_post":post}),PGJsonb(metadata),now,now)).fetchone()
                inserted = row is not None
                if not inserted:
                    row = conn.execute("SELECT id FROM tasks WHERE source=%s AND source_item_id=%s",(EVENT_TRACKING_SOURCE,tweet_id)).fetchone()
                if row is None:
                    raise RuntimeError("event tracking task conflict disappeared")
                task_id = int(row["id"])
                if inserted:
                    conn.execute("INSERT INTO x_task_pipeline(task_id,news_type,judge_model,judge_output,judge_completed_at,updated_at) VALUES(%s,%s,'event_tracking',%s,%s,%s) ON CONFLICT(task_id) DO NOTHING",(task_id,update.get("news_type") or "regular",PGJsonb({"route":update.get("news_type") or "regular","discard_type":"none","rule_set":"event_tracking_material_progress","event_tracking":event_meta}),now,now))
                return task_id

    def primary_task_status(self, task_id: int) -> str | None:
        with connect_postgres(schema="main",application_name="odaily-event-task-status") as conn:
            row=conn.execute("SELECT status FROM tasks WHERE id=%s",(task_id,)).fetchone(); return str(row["status"]) if row else None

    def primary_task_cancelled(self, task_id: int) -> bool:
        with connect_postgres(schema="main",application_name="odaily-event-task-status") as conn:
            row=conn.execute("SELECT source,status,metadata->'event_tracking'->>'dismissed_at' AS dismissed_at FROM tasks WHERE id=%s",(task_id,)).fetchone()
            return bool(row and row["source"]=="event_tracking" and (row["status"]=="event_tracking_cancelled" or row["dismissed_at"]))

    def primary_task_detail(self, task_id: int) -> dict[str,Any] | None:
        with connect_postgres(schema="main",application_name="odaily-event-task-detail") as conn:
            row=conn.execute("SELECT t.id,t.source_item_id,t.source_url,t.status,t.updated_at,p.draft_title,p.draft_content,p.final_title,p.final_content,p.publisher_decision,p.publisher_reason_code,p.publisher_decided_at,p.publish_completed_at,p.last_error FROM tasks t LEFT JOIN x_task_pipeline p ON p.task_id=t.id WHERE t.id=%s",(task_id,)).fetchone()
        if not row:return None
        return {"taskId":int(row["id"]),"sourceItemId":row["source_item_id"],"sourceUrl":row["source_url"],"status":row["status"],"updatedAt":row["updated_at"].isoformat() if row["updated_at"] else None,"draftTitle":row["draft_title"],"draftContent":row["draft_content"],"finalTitle":row["final_title"],"finalContent":row["final_content"],"publisherDecision":row["publisher_decision"],"publisherReasonCode":row["publisher_reason_code"],"publisherDecidedAt":row["publisher_decided_at"],"publishedAt":row["publish_completed_at"],"error":row["last_error"]}

    def dismiss_primary_tasks(self, *, event_id: str, dismissed_at: str, dismissed_by: str, handle: str | None = None) -> dict[str,Any]:
        cancelled=[];published=[];terminal=[]; at=_dt(dismissed_at)
        with connect_postgres(schema="main",application_name="odaily-event-task-dismiss") as conn:
            with conn.transaction():
                rows=conn.execute("SELECT id,status,metadata FROM tasks WHERE source='event_tracking' AND metadata->'event_tracking'->>'event_id'=%s AND (%s IS NULL OR lower(metadata->'event_tracking'->>'candidate_handle')=lower(%s)) FOR UPDATE",(event_id,handle,handle)).fetchall()
                for row in rows:
                    meta=row["metadata"] if isinstance(row["metadata"],dict) else {}; et=meta.setdefault("event_tracking",{}); et["dismissed_at"]=dismissed_at; et["dismissed_by"]=dismissed_by; status=str(row["status"]); task_id=int(row["id"])
                    if status=="auto_published": published.append(task_id)
                    elif status in {"duplicate","discarded","expired","legacy_skipped"}: terminal.append(task_id)
                    else: cancelled.append(task_id); status="event_tracking_cancelled"
                    conn.execute("UPDATE tasks SET metadata=%s,status=%s,locked_by=NULL,locked_until=NULL,updated_at=%s WHERE id=%s",(PGJsonb(meta),status,at,task_id))
        return {"cancelledTaskIds":cancelled,"publishedTaskIds":published,"terminalTaskIds":terminal}

    def event_dismissed(self, event_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM event_tracking_dismissals WHERE event_id=%s",(event_id,)).fetchone() is not None

    def seed_prompt_versions(self, versions: Iterable[tuple[str, int, str]], *, created_at: datetime) -> None:
        with self._connect() as conn:
            with conn.transaction():
                for key, version, content in versions:
                    conn.execute("INSERT INTO event_tracking_prompt_versions(prompt_key,version_number,content,created_at) VALUES(%s,%s,%s,%s) ON CONFLICT(prompt_key,version_number) DO NOTHING", (key, version, content, _dt(created_at)))

    def prompt(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM event_tracking_prompt_versions WHERE prompt_key=%s ORDER BY version_number DESC LIMIT 1", (key,)).fetchone()
            return dict(row) if row else None

    def audit(self, kind: str, detail: dict[str, Any], *, at: datetime, event_id: str | None = None, cycle_id: str | None = None) -> None:
        audit_id = "event-audit-" + _stable(kind, event_id or "", cycle_id or "", at.isoformat(), json.dumps(detail, sort_keys=True, ensure_ascii=False))
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(audit_id) DO NOTHING", (audit_id, at, kind, event_id, cycle_id, Jsonb(detail)))

    def reopen_recent_legacy_discoveries(self, now: datetime) -> None:
        prompt = self.prompt("official_discovery")
        if not prompt:
            return
        from .event_tracking import EVENT_TRACKING_INITIAL_WINDOW
        cutoff = _dt(now) - EVENT_TRACKING_INITIAL_WINDOW
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT e.*,c.cycle_id AS previous_cycle_id,d.prompt_version_id FROM event_tracking_events e "
                "JOIN event_tracking_cycles c ON c.cycle_id=e.current_cycle_id "
                "JOIN event_tracking_account_discoveries d ON d.cycle_id=c.cycle_id "
                "WHERE e.status='discovery_failed' AND d.status='failed' AND c.started_at >= %s "
                "AND COALESCE(d.prompt_version_id,0) < %s "
                "AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals x WHERE x.event_id=e.event_id) "
                "AND NOT EXISTS(SELECT 1 FROM event_tracking_audit a WHERE a.event_id=e.event_id AND a.kind='legacy_discovery_reopened')",
                (cutoff, int(prompt["id"])),
            ).fetchall()]
        for row in rows:
            at = _dt(now)
            cycle_id = "tracking-cycle-" + _stable(row["event_id"], at.isoformat())
            with self._connect() as conn:
                with conn.transaction():
                    event = conn.execute("SELECT * FROM event_tracking_events WHERE event_id=%s FOR UPDATE", (row["event_id"],)).fetchone()
                    if not event or event["status"] != "discovery_failed" or event["current_cycle_id"] != row["previous_cycle_id"]:
                        continue
                    self._insert_reopened_cycle(conn, dict(event), cycle_id, at)
                    conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,'legacy_discovery_reopened',%s,%s,%s) ON CONFLICT(audit_id) DO NOTHING",
                        ("event-audit-"+_stable("legacy-discovery",row["event_id"],cycle_id),at,row["event_id"],cycle_id,Jsonb({"previous_cycle_id":row["previous_cycle_id"],"prompt_version_id":int(prompt["id"])})))

    def reopen_recent_unclear_identity_reviews(self, now: datetime) -> None:
        prompt = self.prompt("account_identity")
        if not prompt or int(prompt["version_number"]) < 2:
            return
        from .event_tracking import EVENT_TRACKING_INITIAL_WINDOW
        audit_kind = f"identity_review_prompt_{prompt['version_number']}_reopened"
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT e.*,c.cycle_id AS previous_cycle_id,d.prompt_version_id,d.actual_model,d.usage_json,d.duration_ms,"
                "d.search_tool_calls_json,d.citations_json,d.raw_output,d.attempts,a.screen_name,a.display_name,a.official_entity,a.official_relation "
                "FROM event_tracking_events e JOIN event_tracking_cycles c ON c.cycle_id=e.current_cycle_id "
                "JOIN event_tracking_account_discoveries d ON d.cycle_id=c.cycle_id "
                "JOIN event_tracking_bindings b ON b.cycle_id=c.cycle_id AND b.event_id=e.event_id "
                "JOIN event_tracking_accounts a ON a.handle_lower=b.handle_lower "
                "WHERE e.status='ended' AND c.status='ended' AND c.end_reason='identity_rejected' AND c.started_at >= %s "
                "AND b.status='released' AND b.identity_status='unclear' "
                "AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals x WHERE x.event_id=e.event_id) "
                "AND NOT EXISTS(SELECT 1 FROM event_tracking_audit x WHERE x.cycle_id=c.cycle_id AND x.kind=%s) "
                "ORDER BY c.started_at DESC,b.handle_lower", (_dt(now)-EVENT_TRACKING_INITIAL_WINDOW,audit_kind)
            ).fetchall()]
        grouped: dict[str,list[dict[str,Any]]] = {}
        for row in rows: grouped.setdefault(str(row["previous_cycle_id"]),[]).append(row)
        for previous_id, bindings in grouped.items():
            first=bindings[0]; at=_dt(now); cycle_id="tracking-cycle-"+_stable(first["event_id"],at.isoformat())
            candidates=[{"handle":str(r["screen_name"]),"display_name":str(r["display_name"] or ""),"official_entity":str(r["official_entity"] or ""),"official_relation":str(r["official_relation"] or ""),"role":"","verification_status":"pending_identity_check","candidate_relation":"legacy_unclear_identity","search_reason":"Retry the previous unclear identity decision from refreshed FXTwitter profile data"} for r in bindings]
            result={"reason":"Rechecking previously unclear account identity using refreshed FXTwitter profile data","accounts":candidates,"identity_pending":True,"identity_recheck_of_cycle_id":previous_id}
            with self._connect() as conn:
                with conn.transaction():
                    event=conn.execute("SELECT * FROM event_tracking_events WHERE event_id=%s AND current_cycle_id=%s FOR UPDATE",(first["event_id"],previous_id)).fetchone()
                    if not event or event["status"]!="ended": continue
                    self._insert_reopened_cycle(conn,dict(event),cycle_id,at)
                    conn.execute("INSERT INTO event_tracking_account_discoveries(discovery_id,event_id,cycle_id,status,attempts,prompt_version_id,actual_model,usage_json,duration_ms,search_tool_calls_json,citations_json,result_json,raw_output,error,created_at,completed_at) VALUES(%s,%s,%s,'failed',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        ("discovery-"+_stable(cycle_id),first["event_id"],cycle_id,max(1,int(first["attempts"] or 1)),first["prompt_version_id"],first["actual_model"],first["usage_json"] or {},first["duration_ms"],first["search_tool_calls_json"] or [],first["citations_json"] or [],Jsonb(result),first["raw_output"],"Retrying account identity from FXTwitter profile data; Web Search is not repeated",at,at))
                    conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(audit_id) DO NOTHING",
                        ("event-audit-"+_stable(audit_kind,first["event_id"],previous_id),at,audit_kind,first["event_id"],previous_id,Jsonb({"previous_cycle_id":previous_id,"new_cycle_id":cycle_id,"handles":[c["handle"] for c in candidates],"identity_prompt_version_id":int(prompt["id"]),"web_search_repeated":False})))

    @staticmethod
    def _insert_reopened_cycle(conn: Any, event: dict[str,Any], cycle_id: str, now: datetime) -> None:
        initial=now+timedelta(hours=72);hard=now+timedelta(days=7)
        conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES(%s,%s,'discovering',%s,%s,%s,%s,%s,%s)",(cycle_id,event["event_id"],now,initial,initial,hard,now,now))
        conn.execute("UPDATE event_tracking_events SET status='discovering',current_cycle_id=%s,ended_at=NULL,end_reason=NULL,updated_at=%s WHERE event_id=%s",(cycle_id,now,event["event_id"]))

    def topic_snapshots(self, topic_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        requested = sorted({str(value) for value in topic_ids or [] if str(value)})
        with self._connect() as conn:
            where = "t.matching_status='active' AND t.visibility='visible'"
            params: list[Any] = []
            if requested:
                where += " AND t.topic_id=ANY(%s)"
                params.append(requested)
            cursor_row = conn.execute("SELECT last_topic_id FROM event_tracking_topic_observation_cursor WHERE singleton_key='global'").fetchone()
            cursor = str(cursor_row["last_topic_id"] or "") if cursor_row else ""
            rows = conn.execute(
                "SELECT t.*,b.title AS brief_title,b.brief FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                f"WHERE {where} AND (%s='' OR t.topic_id>%s) ORDER BY t.topic_id LIMIT 100", [*params,cursor,cursor],
            ).fetchall()
            if not rows and not requested and cursor:
                rows = conn.execute("SELECT t.*,b.title AS brief_title,b.brief FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) WHERE t.matching_status='active' AND t.visibility='visible' ORDER BY t.topic_id LIMIT 100").fetchall()
            result = []
            for row in rows:
                evidence = conn.execute(
                    "SELECT c.claim_text,c.action_or_issue,c.stance,c.evidence_span,c.created_at,ci.activity_account,ci.expanded_text,ci.source_url FROM memberships m JOIN claims c ON c.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=c.content_item_id LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' ORDER BY c.created_at DESC LIMIT 10",
                    (row["topic_id"],),
                ).fetchall()
                participants = conn.execute("SELECT activity_account FROM topic_participations WHERE topic_id=%s ORDER BY last_participation_at DESC LIMIT 20", (row["topic_id"],)).fetchall()
                snapshot = {
                    "topic_id": str(row["topic_id"]), "title": str(row["brief_title"] or row["working_title"]), "brief": str(row["brief"] or ""),
                    "canonical_subject": str(row["canonical_subject"]), "core_entities": row["core_entities_json"] or [], "event_or_issue": str(row["event_or_issue"]),
                    "started_at": _iso(row["started_at"]), "first_seen_at": _iso(row["first_seen_at"]), "last_evidence_at": _iso(row["last_evidence_at"]) if row["last_evidence_at"] else None,
                    "hotness": float(row["hotness_score"] or 0), "participants": [str(item["activity_account"]) for item in participants],
                    "evidence": [{"claim": str(item["claim_text"]), "action_or_issue": str(item["action_or_issue"]), "stance": str(item["stance"]), "evidence": str(item["evidence_span"]), "account": str(item["activity_account"]), "text": str(item["expanded_text"]), "source_url": str(item["source_url"]), "created_at": _iso(item["created_at"])} for item in evidence],
                }
                snapshot["snapshot_hash"] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                result.append(snapshot)
            if result and not requested:
                with conn.transaction():
                    conn.execute("UPDATE event_tracking_topic_observation_cursor SET last_topic_id=%s WHERE singleton_key='global'",(result[-1]["topic_id"],))
            return result

    def assessment(self, topic_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT snapshot_hash,decision,next_attempt_at,actual_model,attempts FROM event_tracking_topic_assessments WHERE topic_id=%s", (topic_id,)).fetchone()
            return dict(row) if row else None

    def queue_observations(self, snapshots: list[dict[str, Any]], *, at: datetime) -> None:
        with self._connect() as conn:
            with conn.transaction():
                for snap in snapshots:
                    conn.execute("INSERT INTO event_tracking_topic_observations(topic_id,first_eligible_at,last_eligible_at,status,first_queued_at,last_queued_at,updated_at) VALUES(%s,%s,%s,'pending',%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET last_eligible_at=EXCLUDED.last_eligible_at,last_queued_at=EXCLUDED.last_queued_at,updated_at=EXCLUDED.updated_at", (snap["topic_id"], _dt(snap["first_seen_at"]), at, at, at, at))

    def mark_judging(self, topic_ids: Iterable[str], *, at: datetime) -> None:
        ids = sorted(set(topic_ids))
        if not ids:
            return
        with self._connect() as conn:
            with conn.transaction():
                conn.execute("UPDATE event_tracking_topic_observations SET status='judging',last_queued_at=%s,last_attempt_at=%s,attempts=attempts+1,last_error=NULL,updated_at=%s WHERE topic_id=ANY(%s)", (at, at, at, ids))

    def save_observations(self, snapshots: list[dict[str, Any]], decisions: dict[str, dict[str, Any]], *, prompt: dict[str, Any], result: Any, at: datetime) -> int:
        tracked = 0
        with self._connect() as conn:
            with conn.transaction():
                for snap in snapshots:
                    current = conn.execute("SELECT topic_id FROM topics WHERE topic_id=%s AND matching_status='active' AND visibility='visible' FOR UPDATE", (snap["topic_id"],)).fetchone()
                    if not current or self._current_topic_snapshot_hash(conn,snap["topic_id"]) != snap["snapshot_hash"]:
                        continue
                    decision = decisions.get(snap["topic_id"])
                    if decision is None:
                        continue
                    payload = dict(decision)
                    conn.execute("INSERT INTO event_tracking_topic_assessments(topic_id,snapshot_hash,snapshot_json,decision,tracking_type,event_identity,result_json,prompt_version_id,actual_model,usage_json,duration_ms,raw_output,attempts,judged_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,decision=EXCLUDED.decision,tracking_type=EXCLUDED.tracking_type,event_identity=EXCLUDED.event_identity,result_json=EXCLUDED.result_json,prompt_version_id=EXCLUDED.prompt_version_id,actual_model=EXCLUDED.actual_model,usage_json=EXCLUDED.usage_json,duration_ms=EXCLUDED.duration_ms,raw_output=EXCLUDED.raw_output,attempts=event_tracking_topic_assessments.attempts+1,last_error=NULL,next_attempt_at=NULL,judged_at=EXCLUDED.judged_at,updated_at=EXCLUDED.updated_at", (snap["topic_id"], snap["snapshot_hash"], Jsonb(snap), decision["decision"], None if decision["tracking_type"] == "none" else decision["tracking_type"], decision.get("event_identity"), Jsonb(payload), int(prompt["id"]), result.model, Jsonb(result.usage), result.duration_ms, result.text[:30000], at, at, at))
                    status = "tracked" if decision["decision"] == "track" else "not_tracked"
                    conn.execute("UPDATE event_tracking_topic_observations SET status=%s,judged_at=%s,reason=%s,last_error=NULL,updated_at=%s WHERE topic_id=%s", (status, at, decision.get("reason"), at, snap["topic_id"]))
                    if status == "tracked":
                        self._link_topic(conn, snap, decision, at=at)
                        tracked += 1
                conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,detail_json) VALUES(%s,%s,'topic_judgment_completed',%s) ON CONFLICT(audit_id) DO NOTHING", ("event-audit-" + _stable("topic_judgment_completed", at.isoformat(), _stable(*(s["topic_id"] for s in snapshots))), at, Jsonb({"topic_count": len(snapshots), "tracked": tracked})))
        return tracked

    def save_cached_observation(self, snapshot: dict[str, Any], assessment: dict[str, Any], *, at: datetime) -> None:
        status = "tracked" if assessment["decision"] == "track" else "not_tracked"
        result = assessment.get("result_json") or {}
        reason = result.get("reason") if isinstance(result, dict) else None
        with self._connect() as conn:
            with conn.transaction():
                current = conn.execute("SELECT 1 FROM topics WHERE topic_id=%s AND matching_status='active' AND visibility='visible' FOR UPDATE", (snapshot["topic_id"],)).fetchone()
                if not current or self._current_topic_snapshot_hash(conn,snapshot["topic_id"]) != snapshot["snapshot_hash"]:
                    return
                conn.execute("UPDATE event_tracking_topic_observations SET status=%s,judged_at=COALESCE(judged_at,%s),reason=COALESCE(%s,reason),updated_at=%s WHERE topic_id=%s", (status, at, reason, at, snapshot["topic_id"]))
                if status == "tracked":
                    self._link_topic(conn, snapshot, result, at=at)

    def save_failure(self, snapshots: list[dict[str, Any]], *, prompt: dict[str, Any], error: Exception, at: datetime) -> None:
        message = f"{type(error).__name__}: {error}"[:2000]
        retry = at + timedelta(minutes=15)
        with self._connect() as conn:
            with conn.transaction():
                for snap in snapshots:
                    conn.execute("INSERT INTO event_tracking_topic_assessments(topic_id,snapshot_hash,snapshot_json,decision,prompt_version_id,attempts,last_error,next_attempt_at,created_at,updated_at) VALUES(%s,%s,%s,'failed',%s,1,%s,%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,decision='failed',prompt_version_id=EXCLUDED.prompt_version_id,attempts=event_tracking_topic_assessments.attempts+1,last_error=EXCLUDED.last_error,next_attempt_at=EXCLUDED.next_attempt_at,updated_at=EXCLUDED.updated_at", (snap["topic_id"], snap["snapshot_hash"], Jsonb(snap), int(prompt["id"]), message, retry, at, at))
                    conn.execute("UPDATE event_tracking_topic_observations SET status='failed',last_error=%s,reason=%s,last_attempt_at=%s,updated_at=%s WHERE topic_id=%s", (message, message, at, at, snap["topic_id"]))

    def _link_topic(self, conn: Any, snap: dict[str, Any], decision: dict[str, Any], *, at: datetime) -> str:
        row = conn.execute("SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id WHERE l.topic_id=%s ORDER BY l.updated_at DESC LIMIT 1", (snap["topic_id"],)).fetchone()
        if row:
            event_id = str(row["event_id"])
            conn.execute("UPDATE event_tracking_events SET updated_at=%s,last_hot_topic_at=%s,title=%s WHERE event_id=%s", (at, at, str(decision.get("event_identity") or snap["title"]), event_id))
        else:
            identity = "event-topic-" + _stable(snap["topic_id"])
            event_id = "tracking-event-" + _stable(identity)
            cycle_id = "event-cycle-" + _stable(event_id, at.isoformat())
            initial = at + timedelta(hours=int(decision.get("recommended_initial_window_hours") or 72)); hard = at + timedelta(days=7)
            conn.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,confirmed_facts_json,unconfirmed_claims_json,rationale,official_response_hypothesis_json,status,current_cycle_id,created_at,updated_at,last_hot_topic_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'discovering',%s,%s,%s,%s)", (event_id, identity, Jsonb([snap["topic_id"]]), str(decision.get("event_identity") or snap["title"]), decision["tracking_type"], decision.get("reader_value", "none"), Jsonb(decision.get("confirmed_facts", [])), Jsonb(decision.get("unconfirmed_claims", [])), decision.get("reason", ""), Jsonb(decision.get("official_response_hypothesis") or {}), cycle_id, at, at, at))
            conn.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES(%s,%s,'discovering',%s,%s,%s,%s,%s,%s)", (cycle_id, event_id, at, initial, min(initial, hard), hard, at, at))
        conn.execute("INSERT INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,topic_id) DO UPDATE SET snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,updated_at=EXCLUDED.updated_at", (event_id, snap["topic_id"], snap["snapshot_hash"], Jsonb(snap), at, at))
        return event_id

    def topic_assessment_status(self, topic_id: str, *, first_seen_at: str | None, enabled: bool, now: datetime) -> dict[str, Any]:
        if not enabled:
            return {"status": "disabled", "reason": "event tracking is disabled"}
        with self._connect() as conn:
            row = conn.execute("SELECT o.*,a.decision,a.next_attempt_at,a.result_json FROM event_tracking_topic_observations o LEFT JOIN event_tracking_topic_assessments a ON a.topic_id=o.topic_id WHERE o.topic_id=%s", (topic_id,)).fetchone()
            if not row:
                return {"status": "pending", "reason": "等待资格判断", "firstEligibleAt": first_seen_at}
            return {"status": row["status"], "reason": row["reason"] or "", "firstEligibleAt": _iso(row["first_eligible_at"]), "firstQueuedAt": _iso(row["first_queued_at"]), "lastAttemptAt": _iso(row["last_attempt_at"]) if row["last_attempt_at"] else None, "judgedAt": _iso(row["judged_at"]) if row["judged_at"] else None, "attempts": int(row["attempts"] or 0), "nextAttemptAt": _iso(row["next_attempt_at"]) if row["next_attempt_at"] else None}

    def start_tracking_topic(self, *, tracker: Any, topic_id: str, actor: str, reason: str, tracking_type: str, reader_value: str, confirmed_facts: list[str], unconfirmed_claims: list[str], official_response_hypothesis: dict[str, Any]) -> str:
        snapshots = self.topic_snapshots([topic_id])
        if len(snapshots) != 1:
            raise ValueError("visible active HotTopic not found")
        snap = snapshots[0]
        decision = {"topic_id": topic_id, "decision": "track", "tracking_type": tracking_type, "reader_value": reader_value, "event_identity": topic_id, "confirmed_facts": confirmed_facts, "unconfirmed_claims": unconfirmed_claims, "reason": reason.strip(), "official_response_hypothesis": official_response_hypothesis, "recommended_initial_window_hours": 72, "stop_conditions": ["事件解决"], "confidence": 1.0, "operator": actor.strip()}
        result = type("Result", (), {"model": "operator_override", "usage": {}, "duration_ms": 0.0, "text": json.dumps(decision, ensure_ascii=False)})()
        prompt = self.prompt("topic_judgment")
        if prompt is None:
            raise RuntimeError("event tracking prompt is missing")
        self.queue_observations([snap], at=tracker.now())
        self.save_observations([snap], {topic_id: decision}, prompt=prompt, result=result, at=tracker.now())
        with self._connect() as conn:
            row = conn.execute("SELECT event_id FROM event_tracking_topic_links WHERE topic_id=%s ORDER BY updated_at DESC LIMIT 1", (topic_id,)).fetchone()
            if not row:
                raise RuntimeError("operator-selected topic was not linked to an event")
            return str(row["event_id"])

    def discover_official_accounts(self, *, tracker: Any, event_id: str | None = None) -> dict[str, int]:
        if not tracker.enabled:
            return {"discovered":0,"failed":0,"bound":0}
        with self._connect() as conn:
            rows=[dict(row) for row in conn.execute("SELECT c.*,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,e.unconfirmed_claims_json,e.rationale,e.official_response_hypothesis_json FROM event_tracking_cycles c JOIN event_tracking_events e ON e.event_id=c.event_id WHERE c.status='discovering' AND (%s::text IS NULL OR c.event_id=%s::text) AND NOT EXISTS(SELECT 1 FROM event_tracking_account_discoveries d WHERE d.cycle_id=c.cycle_id) ORDER BY c.created_at LIMIT 12",(event_id,event_id)).fetchall()]
        discovered=failed=bound=0
        for row in rows:
            now=tracker.now(); key=f"discover:{row['cycle_id']}"; token=self._claim_work(key,now=now)
            if not token: continue
            try:
                status,count=self._discover_one(tracker,row,token)
                discovered+=1; bound+=count; failed+=int(status!="succeeded")
            finally:
                self._release_work(key,token)
        return {"discovered":discovered,"failed":failed,"bound":bound}

    def _discover_one(self, tracker: Any, row: dict[str,Any], token: str) -> tuple[str,int]:
        prompt_version=tracker._prompt("official_discovery")
        with self._connect() as conn:
            links=conn.execute("SELECT snapshot_json FROM event_tracking_topic_links WHERE event_id=%s ORDER BY updated_at DESC LIMIT 8",(row["event_id"],)).fetchall()
        event={"event_id":row["event_id"],"title":row["title"],"tracking_type":row["tracking_type"],"reader_value":row["reader_value"],"confirmed_facts":row["confirmed_facts_json"] or [],"unconfirmed_claims":row["unconfirmed_claims_json"] or [],"rationale":row["rationale"],"official_response_hypothesis":row["official_response_hypothesis_json"] or {},"hot_topic_evidence":[r["snapshot_json"] for r in links]}
        prompt=str(prompt_version["content"]).replace("{{EVENT}}",json.dumps(event,ensure_ascii=False))
        result=None; error=None; attempts=0
        if tracker.ai is None:
            error=RuntimeError("event tracking GPT Web Search client is not configured")
        else:
            for model in dict.fromkeys((tracker.discovery_model,tracker.discovery_retry_model)):
                attempts+=1
                try:
                    result=tracker.ai.web_search_json(model=model,prompt=prompt); break
                except Exception as exc: error=exc
        payload={}; accepted=[]; identity_audit={}; retryable=False; status="failed"
        if result is not None:
            try:
                parsed=tracker._parse_discovery_output(result.text) if hasattr(tracker,"_parse_discovery_output") else None
            except Exception: parsed=None
            try:
                parsed=json.loads(result.text) if parsed is None else parsed
                if not isinstance(parsed,dict): raise ValueError("official discovery output must be a JSON object")
                candidates=tracker._candidate_accounts(parsed.get("accounts"),reason=str(parsed.get("reason") or ""))
                accepted,identity_audit,retryable=tracker._verify_candidate_identities(event,candidates)
                status="succeeded" if accepted else ("failed" if retryable else "no_official_account")
                payload={"reason":str(parsed.get("reason") or ""),"accounts":accepted or candidates,"identity_pending":retryable,"identity_verification":identity_audit}
            except Exception as exc:
                error=exc; payload={"failure_stage":"structured_output"}
        if error and result is None: payload={"failure_stage":"web_search_request"}
        now=tracker.now(); selected=[]; exhausted=False
        if accepted:
            with self._connect() as conn:
                active={str(r["handle_lower"]) for r in conn.execute("SELECT DISTINCT handle_lower FROM event_tracking_bindings WHERE status='active'").fetchall()}
            from .event_tracking import EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS
            for account in accepted[:8]:
                key=str(account["handle"]).lower()
                if key not in active and len(active)>=EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS: exhausted=True; continue
                selected.append(account); active.add(key)
            if not selected and exhausted and not retryable: status="capacity_exhausted"
        with self._connect() as conn:
            with conn.transaction():
                if not self._owns_work(conn,f"discover:{row['cycle_id']}",token,now=now): return "stale",0
                cycle=conn.execute("SELECT status FROM event_tracking_cycles WHERE cycle_id=%s FOR UPDATE",(row["cycle_id"],)).fetchone()
                if not cycle or cycle["status"]!="discovering": return "stale",0
                selected_now=now
                for account in selected:
                    handle=str(account["handle"]); lower=handle.lower()
                    identity=account.get("identity_verification") or {}
                    conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,display_name,official_entity,official_relation,official_evidence_json,status,next_due_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s,%s) ON CONFLICT(handle_lower) DO UPDATE SET screen_name=EXCLUDED.screen_name,display_name=CASE WHEN EXCLUDED.display_name='' THEN event_tracking_accounts.display_name ELSE EXCLUDED.display_name END,official_entity=EXCLUDED.official_entity,official_relation=EXCLUDED.official_relation,official_evidence_json=EXCLUDED.official_evidence_json,status='active',next_due_at=COALESCE(event_tracking_accounts.next_due_at,EXCLUDED.next_due_at),updated_at=EXCLUDED.updated_at",(lower,handle,account["display_name"],account["official_entity"],account["official_relation"],Jsonb(account),now,now,now))
                    conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,identity_result_json,discovered_at) VALUES(%s,%s,%s,%s,'active','verified',%s,%s) ON CONFLICT(cycle_id,handle_lower) DO UPDATE SET status='active',identity_status='verified',identity_result_json=EXCLUDED.identity_result_json,released_at=NULL",(row["cycle_id"],row["event_id"],lower,account["role"],Jsonb(identity),now))
                attempts=max(1,attempts)
                conn.execute("INSERT INTO event_tracking_account_discoveries(discovery_id,event_id,cycle_id,status,attempts,prompt_version_id,actual_model,usage_json,duration_ms,search_tool_calls_json,citations_json,result_json,raw_output,error,created_at,completed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(cycle_id) DO NOTHING",("discovery-"+_stable(row["cycle_id"]),row["event_id"],row["cycle_id"],status,attempts,int(prompt_version["id"]),result.model if result else None,Jsonb(result.usage if result else {}),result.duration_ms if result else None,Jsonb(result.tool_calls if result else []),Jsonb(result.citations if result else []),Jsonb(payload),result.text[:30000] if result else None,(str(error)[:2000] if error else None),now,now))
                if selected:
                    conn.execute("UPDATE event_tracking_cycles SET status='active',updated_at=%s WHERE cycle_id=%s",(now,row["cycle_id"]))
                    conn.execute("UPDATE event_tracking_events SET status='active',updated_at=%s WHERE event_id=%s",(now,row["event_id"]))
                    status="succeeded"
                elif status!="failed" or not retryable:
                    reason="capacity_exhausted" if status=="capacity_exhausted" else "discovery_failed"
                    conn.execute("UPDATE event_tracking_cycles SET status='ended',ended_at=%s,end_reason=%s,updated_at=%s WHERE cycle_id=%s",(now,reason,now,row["cycle_id"]))
                    conn.execute("UPDATE event_tracking_events SET status=%s,ended_at=%s,end_reason=%s,updated_at=%s WHERE event_id=%s",("capacity_exhausted" if status=="capacity_exhausted" else "discovery_failed",now,reason,now,row["event_id"]))
                conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,%s,%s,%s,%s)",("event-audit-"+_stable("discovery",row["cycle_id"],now.isoformat()),now,"official_accounts_discovered" if selected else "official_discovery_failed",row["event_id"],row["cycle_id"],Jsonb({"status":status,"bound":len(selected),"error":str(error)[:500] if error else None})))
        if selected and tracker.feed_writer is not None:
            try: tracker.feed_writer.upsert_auto_newsflash_event(event_id=str(row["event_id"]),title=str(row["title"]),occurred_at=now)
            except Exception as exc: tracker._audit("event_plugin_notification_failed",{"error":f"{type(exc).__name__}: {exc}"[:1000]},event_id=row["event_id"])
        return status,len(selected)

    def retry_pending_identity_discoveries(self, *, tracker: Any, limit: int = 12) -> dict[str, int]:
        # Discovery records with pending identities retain their candidate
        # payload. Reuse the same verifier; Web Search is never repeated.
        with self._connect() as conn:
            rows=[dict(r) for r in conn.execute("SELECT d.*,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,e.unconfirmed_claims_json,e.rationale,e.official_response_hypothesis_json FROM event_tracking_account_discoveries d JOIN event_tracking_events e ON e.event_id=d.event_id JOIN event_tracking_cycles c ON c.cycle_id=d.cycle_id WHERE d.result_json->>'identity_pending'='true' AND c.status IN ('discovering','active') AND e.status IN ('discovering','active') ORDER BY d.created_at LIMIT %s",(max(1,min(limit,12)),)).fetchall()]
        retried=bound=failed=deferred=0
        for row in rows:
            key=f"identity-retry:{row['discovery_id']}"; token=self._claim_work(key,now=tracker.now())
            if not token: continue
            try:
                payload=row["result_json"] if isinstance(row["result_json"],dict) else json.loads(row["result_json"])
                candidates=tracker._candidate_accounts(payload.get("accounts"),reason=str(payload.get("reason") or ""))
                event={"event_id":row["event_id"],"title":row["title"],"tracking_type":row["tracking_type"],"reader_value":row["reader_value"],"confirmed_facts":row["confirmed_facts_json"] or [],"unconfirmed_claims":row["unconfirmed_claims_json"] or [],"rationale":row["rationale"],"official_response_hypothesis":row["official_response_hypothesis_json"] or {}}
                accepted,audit,retryable=tracker._verify_candidate_identities(event,candidates); now=tracker.now()
                with self._connect() as conn:
                    with conn.transaction():
                        if not self._owns_work(conn,key,token,now=now): continue
                        cycle=conn.execute("SELECT status FROM event_tracking_cycles WHERE cycle_id=%s FOR UPDATE",(row["cycle_id"],)).fetchone()
                        if not cycle or cycle["status"] not in {"discovering","active"}: continue
                        new_payload={**payload,"identity_pending":bool(retryable),"identity_verification":audit,"accounts":accepted or candidates}
                        for account in accepted:
                            lower=account["handle"].lower(); conn.execute("INSERT INTO event_tracking_accounts(handle_lower,screen_name,display_name,official_entity,official_relation,official_evidence_json,status,next_due_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s,%s) ON CONFLICT(handle_lower) DO UPDATE SET official_entity=EXCLUDED.official_entity,official_relation=EXCLUDED.official_relation,official_evidence_json=EXCLUDED.official_evidence_json,status='active',next_due_at=COALESCE(event_tracking_accounts.next_due_at,EXCLUDED.next_due_at),updated_at=EXCLUDED.updated_at",(lower,account["handle"],account["display_name"],account["official_entity"],account["official_relation"],Jsonb(account),now,now,now))
                            conn.execute("INSERT INTO event_tracking_bindings(cycle_id,event_id,handle_lower,role,status,identity_status,identity_result_json,discovered_at) VALUES(%s,%s,%s,%s,'active','verified',%s,%s) ON CONFLICT(cycle_id,handle_lower) DO UPDATE SET status='active',identity_status='verified',identity_result_json=EXCLUDED.identity_result_json,released_at=NULL",(row["cycle_id"],row["event_id"],lower,account["role"],Jsonb(account.get("identity_verification") or {}),now))
                        conn.execute("UPDATE event_tracking_account_discoveries SET status=%s,result_json=%s,error=%s,completed_at=%s WHERE discovery_id=%s",("succeeded" if accepted else "failed",Jsonb(new_payload),"FXTwitter profile or identity review unavailable" if retryable else None,now,row["discovery_id"]))
                        if accepted:
                            conn.execute("UPDATE event_tracking_cycles SET status='active',updated_at=%s WHERE cycle_id=%s",(now,row["cycle_id"]))
                            conn.execute("UPDATE event_tracking_events SET status='active',updated_at=%s WHERE event_id=%s",(now,row["event_id"]))
                retried+=1;bound+=len(accepted);deferred+=int(retryable and not accepted);failed+=int(not accepted and not retryable)
            finally:self._release_work(key,token)
        return {"retried":retried,"bound":bound,"failed":failed,"deferred":deferred}

    def reverify_pending_account_bindings(self, **_: Any) -> dict[str, int]:
        tracker=_.get("tracker");limit=int(_.get("limit",12))
        if tracker is None:return {"verified":0,"rejected":0,"deferred":0}
        with self._connect() as conn:
            rows=[dict(r) for r in conn.execute("SELECT b.cycle_id,b.event_id,b.handle_lower,a.screen_name,a.display_name,a.official_entity,a.official_relation,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,e.unconfirmed_claims_json,e.rationale,e.official_response_hypothesis_json FROM event_tracking_bindings b JOIN event_tracking_cycles c ON c.cycle_id=b.cycle_id AND c.status='active' JOIN event_tracking_events e ON e.event_id=b.event_id AND e.status='active' JOIN event_tracking_accounts a ON a.handle_lower=b.handle_lower WHERE b.status='active' AND b.identity_status='pending' ORDER BY b.discovered_at,b.handle_lower LIMIT %s",(max(1,min(limit,12)),)).fetchall()]
        groups={}
        for row in rows:groups.setdefault(row["cycle_id"],[]).append(row)
        verified=rejected=deferred=0
        for cycle_id,bindings in groups.items():
            key=f"reverify:{cycle_id}";token=self._claim_work(key,now=tracker.now())
            if not token:continue
            try:
                first=bindings[0];event={"event_id":first["event_id"],"title":first["title"],"tracking_type":first["tracking_type"],"reader_value":first["reader_value"],"confirmed_facts":first["confirmed_facts_json"] or [],"unconfirmed_claims":first["unconfirmed_claims_json"] or [],"rationale":first["rationale"],"official_response_hypothesis":first["official_response_hypothesis_json"] or {}}
                candidates=[{"handle":r["screen_name"],"display_name":r["display_name"] or "","official_entity":r["official_entity"],"official_relation":r["official_relation"],"role":"","verification_status":"pending_identity_check","candidate_relation":"legacy_binding","search_reason":"legacy binding identity review"} for r in bindings]
                accepted,audit,retryable=tracker._verify_candidate_identities(event,candidates);now=tracker.now();accepted_by={a["handle"].lower():a for a in accepted};audits={str(a.get("handle") or "").lower():a for a in audit.get("candidates",[]) if isinstance(a,dict)}
                with self._connect() as conn:
                    with conn.transaction():
                        if not self._owns_work(conn,key,token,now=now):continue
                        for binding in bindings:
                            handle=str(binding["screen_name"]);lower=str(binding["handle_lower"]);candidate_key=handle.lower();account=accepted_by.get(candidate_key);item=audits.get(candidate_key,{"status":"unclear"})
                            if account:
                                identity=account["identity_verification"];conn.execute("UPDATE event_tracking_bindings SET identity_status='verified',identity_result_json=%s WHERE cycle_id=%s AND handle_lower=%s",(Jsonb(identity),cycle_id,lower));conn.execute("UPDATE event_tracking_accounts SET official_entity=%s,official_relation=%s,official_evidence_json=%s,updated_at=%s WHERE handle_lower=%s",(account["official_entity"],account["official_relation"],Jsonb(account),now,lower));verified+=1
                            elif item.get("status")=="profile_fetch_failed" or retryable:
                                conn.execute("UPDATE event_tracking_bindings SET identity_result_json=%s WHERE cycle_id=%s AND handle_lower=%s",(Jsonb({"status":"pending","audit":item,"identity_review":audit}),cycle_id,lower));deferred+=1
                            else:
                                status="rejected" if item.get("status")=="rejected" else "unclear";conn.execute("UPDATE event_tracking_bindings SET identity_status=%s,identity_result_json=%s,status='released',released_at=%s WHERE cycle_id=%s AND handle_lower=%s",(status,Jsonb({"status":status,"audit":item,"identity_review":audit}),now,cycle_id,lower));rejected+=1
                        remaining=conn.execute("SELECT COUNT(*) AS n FROM event_tracking_bindings WHERE cycle_id=%s AND status='active'",(cycle_id,)).fetchone()["n"]
                        if not remaining:
                            conn.execute("UPDATE event_tracking_cycles SET status='ended',ended_at=%s,end_reason='identity_rejected',updated_at=%s WHERE cycle_id=%s",(now,now,cycle_id));conn.execute("UPDATE event_tracking_events SET status='ended',ended_at=%s,end_reason='identity_rejected',updated_at=%s WHERE event_id=%s",(now,now,first["event_id"]))
            finally:self._release_work(key,token)
        return {"verified":verified,"rejected":rejected,"deferred":deferred}

    def due_accounts(self, *, now: datetime, limit: int = 12) -> list[tuple[Any,datetime]]:
        from packages.hottopic.capture import AccountRow
        with self._connect() as conn:
            rows=conn.execute("SELECT a.*,MIN(c.started_at) AS cutoff_at FROM event_tracking_accounts a JOIN event_tracking_bindings b ON b.handle_lower=a.handle_lower AND b.status='active' AND b.identity_status='verified' JOIN event_tracking_cycles c ON c.cycle_id=b.cycle_id AND c.status='active' WHERE a.status='active' AND (a.next_due_at IS NULL OR a.next_due_at<=%s) GROUP BY a.handle_lower ORDER BY COALESCE(a.next_due_at,'-infinity'::timestamptz),a.handle_lower LIMIT %s",(now,max(1,min(limit,12)))).fetchall()
        return [(AccountRow(screen_name=str(r["screen_name"]),name=str(r["display_name"]),description="",followers_count=0,statuses_count=0,protected=False,url=f"https://x.com/{r['screen_name']}"),_dt(r["cutoff_at"])) for r in rows]

    def poll_due_accounts(self, *, tracker: Any, scanner: Any, workers: int = 12) -> dict[str,int]:
        from concurrent.futures import ThreadPoolExecutor,as_completed
        if not tracker.enabled:return {"polled":0,"accepted":0,"errors":0}
        due=self.due_accounts(now=tracker.now());claimed=[]
        for account,cutoff in due:
            token=self._claim_work(f"poll:{account.screen_name.lower()}",now=tracker.now(),seconds=600)
            if token:claimed.append((account,cutoff,token))
        due=[(a,c) for a,c,_ in claimed]
        if not due:return {"polled":0,"accepted":0,"errors":0}
        accepted=errors=0
        tokens={a.screen_name.lower():token for a,_c,token in claimed}
        try:
            with ThreadPoolExecutor(max_workers=max(1,min(workers,len(due))),thread_name_prefix="event-tracking") as pool:
                futures={pool.submit(scanner,a,c):(a,c) for a,c in due}
                for future in as_completed(futures):
                    account,_=futures[future]
                    try:items,_latest,error=future.result()
                    except Exception as exc:items,error=[],{"kind":"exception","error":str(exc)}
                    accepted+=self.store_event_poll(account=account,items=items,error=error,now=tracker.now(),claim_token=tokens[account.screen_name.lower()]);errors+=int(error is not None)
            return {"polled":len(due),"accepted":accepted,"errors":errors}
        finally:
            for account,_c,token in claimed:self._release_work(f"poll:{account.screen_name.lower()}",token)

    def store_event_poll(self, *, account: Any, items: list[Any], error: dict[str,Any] | None, now: datetime, claim_token: str | None = None) -> int:
        lower=account.screen_name.lower(); own=[item for item in items if item.author_screen_name.lower()==lower]; accepted=0
        with self._connect() as conn:
            with conn.transaction():
                if claim_token and not self._owns_work(conn,f"poll:{lower}",claim_token,now=now):return 0
                row=conn.execute("SELECT consecutive_failures,last_success_at FROM event_tracking_accounts WHERE handle_lower=%s FOR UPDATE",(lower,)).fetchone()
                if not row:return 0
                for item in own:
                    cur=conn.execute("INSERT INTO event_tracking_inbox(tweet_id,handle_lower,payload_json,collected_at) VALUES(%s,%s,%s,%s) ON CONFLICT(tweet_id) DO NOTHING",(item.tweet_id,lower,Jsonb(item.__dict__),now));accepted+=cur.rowcount
                failures=0 if error is None else int(row["consecutive_failures"] or 0)+1
                delay=timedelta(minutes=10) if error is None else min(timedelta(hours=6),timedelta(minutes=10)*(2**min(failures,6)))
                if error and error.get("kind")=="rate_limited":delay=max(delay,timedelta(seconds=max(0,float(error.get("retry_after_seconds") or 0))))
                conn.execute("UPDATE event_tracking_accounts SET next_due_at=%s,last_polled_at=%s,last_success_at=%s,last_error=%s,consecutive_failures=%s,last_item_count=%s,updated_at=%s WHERE handle_lower=%s",(now+delay,now,now if error is None else row["last_success_at"],json.dumps(error,ensure_ascii=False) if error else None,failures,len(own),now,lower))
                if error:conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,detail_json) VALUES(%s,%s,'official_account_poll_failed',%s)",("event-audit-"+_stable("poll",lower,now.isoformat()),now,Jsonb({"handle":account.screen_name,"error":error})))
        return accepted

    def classify_official_updates(self, *, tracker: Any, limit: int = 30) -> dict[str,int]:
        if not tracker.enabled:return {"classified":0,"material":0,"failed":0}
        with self._connect() as conn:
            inbox=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_inbox WHERE processed_at IS NULL ORDER BY collected_at,tweet_id LIMIT %s",(max(1,limit),)).fetchall()]
        classified=material=failed=0
        for item in inbox:
            post=item["payload_json"] if isinstance(item["payload_json"],dict) else json.loads(item["payload_json"])
            with self._connect() as conn:
                bindings=[dict(r) for r in conn.execute("SELECT b.event_id,b.cycle_id,b.role,b.identity_result_json,a.official_entity,e.title,e.tracking_type,e.reader_value,e.confirmed_facts_json,e.unconfirmed_claims_json,e.rationale,e.official_response_hypothesis_json,c.started_at,c.hard_expires_at FROM event_tracking_bindings b JOIN event_tracking_cycles c ON c.cycle_id=b.cycle_id AND c.status='active' JOIN event_tracking_events e ON e.event_id=b.event_id AND e.status='active' JOIN event_tracking_accounts a ON a.handle_lower=b.handle_lower WHERE b.status='active' AND b.identity_status='verified' AND b.handle_lower=%s",(item["handle_lower"],)).fetchall()]
            if not bindings:
                with self._connect() as conn:
                    with conn.transaction():conn.execute("UPDATE event_tracking_inbox SET processed_at=%s WHERE tweet_id=%s",(tracker.now(),item["tweet_id"]))
                continue
            terminal=True
            for binding in bindings:
                outcome=self._classify_one(tracker,binding,item,post)
                classified+=outcome["classified"];material+=outcome["material"];failed+=outcome["failed"];terminal=terminal and outcome["terminal"]
            if terminal:
                with self._connect() as conn:
                    with conn.transaction():conn.execute("UPDATE event_tracking_inbox SET processed_at=%s,error=NULL WHERE tweet_id=%s",(tracker.now(),item["tweet_id"]))
        return {"classified":classified,"material":material,"failed":failed}

    def _classify_one(self, tracker: Any, binding: dict[str,Any], inbox: dict[str,Any], post: dict[str,Any]) -> dict[str,bool]:
        now=tracker.now(); update_id="event-update-"+_stable(binding["cycle_id"],inbox["tweet_id"]); key=f"classify:{update_id}"; token=self._claim_work(key,now=now,seconds=240)
        if not token:return {"classified":False,"material":False,"failed":False,"terminal":False}
        try:
            with self._connect() as conn:
                with conn.transaction():
                    if not self._owns_work(conn,key,token,now=now):return {"classified":False,"material":False,"failed":False,"terminal":False}
                    current=conn.execute("SELECT e.status AS event_status,c.status AS cycle_status,c.hard_expires_at,b.status AS binding_status,b.identity_status FROM event_tracking_events e JOIN event_tracking_cycles c ON c.event_id=e.event_id AND c.cycle_id=%s JOIN event_tracking_bindings b ON b.event_id=e.event_id AND b.cycle_id=c.cycle_id AND b.handle_lower=%s WHERE e.event_id=%s FOR UPDATE OF e,c,b",(binding["cycle_id"],inbox["handle_lower"],binding["event_id"])).fetchone()
                    dismissed=conn.execute("SELECT 1 FROM event_tracking_dismissals WHERE event_id=%s",(binding["event_id"],)).fetchone()
                    if dismissed or not current or current["event_status"]!="active" or current["cycle_status"]!="active" or current["binding_status"]!="active" or current["identity_status"]!="verified" or _dt(current["hard_expires_at"])<=now:return {"classified":False,"material":False,"failed":False,"terminal":True}
                    existing=conn.execute("SELECT * FROM event_tracking_updates WHERE update_id=%s FOR UPDATE",(update_id,)).fetchone()
                if existing and existing["status"]=="succeeded":return {"classified":False,"material":existing["classification"]=="material_progress","failed":False,"terminal":True}
                if existing and existing["status"]=="failed" and existing["next_attempt_at"] and _dt(existing["next_attempt_at"])>now:return {"classified":False,"material":False,"failed":False,"terminal":False}
                attempts=int(existing["attempts"] or 0)+1 if existing else 1
                if existing and attempts>3:return {"classified":False,"material":False,"failed":True,"terminal":True}
                with conn.transaction():
                    if existing:
                        changed=conn.execute("UPDATE event_tracking_updates SET status='processing',attempts=%s,updated_at=%s WHERE update_id=%s AND status IN ('failed','pending','processing') AND attempts=%s RETURNING update_id",(attempts,now,update_id,attempts-1)).fetchone()
                        if not changed:return {"classified":False,"material":False,"failed":False,"terminal":False}
                    else:
                        conn.execute("INSERT INTO event_tracking_updates(update_id,event_id,cycle_id,tweet_id,handle_lower,status,attempts,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,'processing',1,%s,%s) ON CONFLICT(update_id) DO NOTHING",(update_id,binding["event_id"],binding["cycle_id"],inbox["tweet_id"],inbox["handle_lower"],now,now))
                timeline=conn.execute("SELECT u.tweet_id,u.handle_lower,u.fact_summary,u.difference_text,u.confirmed_facts_json,u.unconfirmed_claims_json,u.classified_at,i.payload_json FROM event_tracking_updates u LEFT JOIN event_tracking_inbox i ON i.tweet_id=u.tweet_id WHERE u.event_id=%s AND u.classification='material_progress' AND u.status='succeeded' ORDER BY u.classified_at DESC LIMIT 8",(binding["event_id"],)).fetchall()
            prompt_version=tracker._prompt("update_materiality"); identity=binding["identity_result_json"] or {}; event={"event_id":binding["event_id"],"title":binding["title"],"tracking_type":binding["tracking_type"],"reader_value":binding["reader_value"],"confirmed_facts":binding["confirmed_facts_json"] or [],"unconfirmed_claims":binding["unconfirmed_claims_json"] or [],"rationale":binding["rationale"],"official_response_hypothesis":binding["official_response_hypothesis_json"] or {},"account_identity":{"status":"ai_profile_assessed_as_event_actor","decision":identity.get("decision"),"entity":identity.get("entity") or binding.get("official_entity"),"role":identity.get("role") or binding.get("role"),"reason":identity.get("reason"),"evidence":identity.get("evidence",[])}}
            prompt=str(prompt_version["content"]).replace("{{EVENT}}",json.dumps(event,ensure_ascii=False)).replace("{{ACCOUNT_IDENTITY}}",json.dumps(event["account_identity"],ensure_ascii=False)).replace("{{TIMELINE}}",json.dumps([dict(r) for r in timeline],ensure_ascii=False,default=str)).replace("{{POST}}",json.dumps(post,ensure_ascii=False,default=str))
            try:
                result=tracker._call_text(prompt=prompt,models=(tracker.update_model,tracker.update_fallback_model));decision=tracker._parse_update_decision(__import__("packages.hottopic.event_tracking",fromlist=["_parse_json_output"])._parse_json_output(result.text))
            except Exception as exc:
                at=tracker.now(); terminal=attempts>=3
                with self._connect() as conn:
                    with conn.transaction():
                        if self._owns_work(conn,key,token,now=at):conn.execute("UPDATE event_tracking_updates SET status='failed',attempts=%s,last_error=%s,next_attempt_at=%s,updated_at=%s WHERE update_id=%s AND status='processing' AND attempts=%s",(attempts,f"{type(exc).__name__}: {exc}"[:2000],None if terminal else at+timedelta(minutes=10*attempts),at,update_id,attempts))
                return {"classified":False,"material":False,"failed":True,"terminal":terminal}
            at=tracker.now()
            with self._connect() as conn:
                with conn.transaction():
                    if not self._owns_work(conn,key,token,now=at):return {"classified":False,"material":False,"failed":False,"terminal":False}
                    current=conn.execute("SELECT c.status AS cycle_status,c.hard_expires_at,b.status AS binding_status,b.identity_status,e.status AS event_status FROM event_tracking_cycles c JOIN event_tracking_bindings b ON b.cycle_id=c.cycle_id AND b.handle_lower=%s JOIN event_tracking_events e ON e.event_id=c.event_id WHERE c.cycle_id=%s AND e.event_id=%s FOR UPDATE OF c,b,e",(inbox["handle_lower"],binding["cycle_id"],binding["event_id"])).fetchone()
                    dismissed=conn.execute("SELECT 1 FROM event_tracking_dismissals WHERE event_id=%s",(binding["event_id"],)).fetchone()
                    if dismissed or not current or current["cycle_status"]!="active" or current["event_status"]!="active" or current["binding_status"]!="active" or current["identity_status"]!="verified" or at>=_dt(current["hard_expires_at"]):return {"classified":False,"material":False,"failed":False,"terminal":True}
                    changed=conn.execute("UPDATE event_tracking_updates SET status='succeeded',classification=%s,news_type=%s,fact_summary=%s,difference_text=%s,confirmed_facts_json=%s,unconfirmed_claims_json=%s,confidence=%s,reason=%s,prompt_version_id=%s,actual_model=%s,usage_json=%s,duration_ms=%s,raw_output=%s,last_error=NULL,next_attempt_at=NULL,classified_at=%s,updated_at=%s WHERE update_id=%s AND attempts=%s AND status='processing'",(decision["classification"],decision["news_type"],decision["fact_summary"],decision["difference_from_timeline"],Jsonb(decision["confirmed_facts"]),Jsonb(decision["unconfirmed_claims"]),decision["confidence"],decision["reason"],int(prompt_version["id"]),result.model,Jsonb(result.usage),result.duration_ms,result.text[:30000],at,at,update_id,attempts)).rowcount
                    if not changed:return {"classified":False,"material":False,"failed":False,"terminal":False}
                    if decision["classification"]=="material_progress":
                        post_at=_dt(post.get("created_at_iso") or post.get("created_at") or at);cycle=conn.execute("SELECT expires_at,hard_expires_at FROM event_tracking_cycles WHERE cycle_id=%s FOR UPDATE",(binding["cycle_id"],)).fetchone()
                        if cycle:conn.execute("UPDATE event_tracking_cycles SET last_material_progress_at=%s,expires_at=%s,updated_at=%s WHERE cycle_id=%s",(post_at,min(cycle["hard_expires_at"],max(cycle["expires_at"],post_at+timedelta(hours=24))),at,binding["cycle_id"]))
                        conn.execute("UPDATE event_tracking_events SET last_material_progress_at=%s,updated_at=%s WHERE event_id=%s",(post_at,at,binding["event_id"]))
                        conn.execute("INSERT INTO event_tracking_publication_outbox(outbox_id,update_id,event_id,cycle_id,tweet_id,status,attempts,next_attempt_at,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,'pending',0,%s,%s,%s) ON CONFLICT(update_id) DO NOTHING",("event-outbox-"+_stable(update_id),update_id,binding["event_id"],binding["cycle_id"],inbox["tweet_id"],at,at,at))
                        conn.execute("UPDATE event_tracking_bindings SET last_contributed_at=%s WHERE cycle_id=%s AND handle_lower=%s",(at,binding["cycle_id"],inbox["handle_lower"]))
            return {"classified":True,"material":decision["classification"]=="material_progress","failed":False,"terminal":True}
        finally:self._release_work(key,token)

    def dispatch_publications(self, *, tracker: Any, limit: int = 20) -> dict[str,int]:
        if not tracker.enabled:return {"submitted":0,"failed":0,"duplicate":0,"published":0}
        self.refresh_publication_statuses(tracker.dispatcher)
        now=tracker.now()
        with self._connect() as conn:
            rows=[dict(r) for r in conn.execute("SELECT o.*,u.classification,u.news_type,u.fact_summary,u.difference_text,u.confirmed_facts_json,u.unconfirmed_claims_json,u.handle_lower,u.update_id,e.title AS event_title,e.tracking_type,c.started_at AS cycle_started_at,i.payload_json,b.identity_result_json AS account_identity_json FROM event_tracking_publication_outbox o JOIN event_tracking_updates u ON u.update_id=o.update_id JOIN event_tracking_bindings b ON b.cycle_id=o.cycle_id AND b.handle_lower=u.handle_lower AND b.identity_status='verified' JOIN event_tracking_events e ON e.event_id=o.event_id JOIN event_tracking_cycles c ON c.cycle_id=o.cycle_id JOIN event_tracking_inbox i ON i.tweet_id=o.tweet_id WHERE o.cancelled_at IS NULL AND o.status IN ('pending','failed') AND (o.next_attempt_at IS NULL OR o.next_attempt_at<=%s) AND o.attempts<8 AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=o.event_id) ORDER BY o.created_at LIMIT %s",(now,max(1,limit))).fetchall()]
        submitted=failed=0
        for row in rows:
            key=f"dispatch:{row['outbox_id']}";token=self._claim_work(key,now=tracker.now(),seconds=300)
            if not token:continue
            try:
                at=tracker.now()
                with self._connect() as conn:
                    with conn.transaction():
                        if not self._owns_work(conn,key,token,now=at):continue
                        changed=conn.execute("UPDATE event_tracking_publication_outbox SET status='submitting',attempts=attempts+1,updated_at=%s WHERE outbox_id=%s AND status IN ('pending','failed') AND (next_attempt_at IS NULL OR next_attempt_at<=%s) AND cancelled_at IS NULL AND attempts<8 AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=event_tracking_publication_outbox.event_id) RETURNING attempts",(at,row["outbox_id"],at)).fetchone()
                        if not changed:continue
                        attempt=int(changed["attempts"])
                event={"event_id":row["event_id"],"title":row["event_title"],"tracking_type":row["tracking_type"]};cycle={"cycle_id":row["cycle_id"],"started_at":row["cycle_started_at"]};update={**row,"identity_result_json":row["account_identity_json"]};post=row["payload_json"] if isinstance(row["payload_json"],dict) else json.loads(row["payload_json"])
                task_id=int(row["primary_task_id"] or 0)
                try:
                    if self._event_no_longer_publishable(row): raise RuntimeError("event tracking publication became ineligible")
                    if not task_id:
                        task_id=int(tracker.dispatcher.ensure_task(event=event,cycle=cycle,update=update,post=post))
                    if self._event_no_longer_publishable(row):
                        if not tracker.dispatcher.is_event_tracking_cancelled(task_id):
                            tracker.dispatcher.dismiss_event_tasks(event_id=str(row["event_id"]),dismissed_at=tracker.now().isoformat(),dismissed_by="event_tracking_fence")
                        raise RuntimeError("event tracking publication was dismissed before submission")
                    if tracker.dispatcher.is_event_tracking_cancelled(task_id):raise RuntimeError("event tracking task was cancelled")
                    tracker.dispatcher.submit(task_id=task_id,tweet_id=str(row["tweet_id"])); error=None
                except Exception as exc:error=exc
                completed=tracker.now()
                with self._connect() as conn:
                    with conn.transaction():
                        if not self._owns_work(conn,key,token,now=completed):continue
                        if error is None:
                            changed=conn.execute("UPDATE event_tracking_publication_outbox SET status='submitted',primary_task_id=%s,submitted_at=%s,last_error=NULL,next_attempt_at=NULL,updated_at=%s WHERE outbox_id=%s AND attempts=%s AND status='submitting' AND cancelled_at IS NULL",(task_id,completed,completed,row["outbox_id"],attempt)).rowcount;submitted+=changed
                        else:
                            changed=conn.execute("UPDATE event_tracking_publication_outbox SET status='failed',primary_task_id=NULLIF(%s,0),last_error=%s,next_attempt_at=%s,updated_at=%s WHERE outbox_id=%s AND attempts=%s AND status='submitting' AND cancelled_at IS NULL",(task_id,f"{type(error).__name__}: {error}"[:2000],completed+timedelta(minutes=min(60,2**attempt)),completed,row["outbox_id"],attempt)).rowcount;failed+=changed
            finally:self._release_work(key,token)
        statuses=self.refresh_publication_statuses(tracker.dispatcher)
        return {"submitted":submitted,"failed":failed,**statuses}

    def refresh_publication_statuses(self, dispatcher: Any) -> dict[str,int]:
        cursor=self._publication_refresh_cursor
        with self._connect() as conn:
            if cursor:
                rows=[dict(r) for r in conn.execute("SELECT outbox_id,primary_task_id,status,event_id,cycle_id,updated_at FROM event_tracking_publication_outbox WHERE primary_task_id IS NOT NULL AND cancelled_at IS NULL AND status='submitted' AND (updated_at,outbox_id)>(%s,%s) ORDER BY updated_at,outbox_id LIMIT 100",cursor).fetchall()]
            else:
                rows=[]
            if not rows:
                rows=[dict(r) for r in conn.execute("SELECT outbox_id,primary_task_id,status,event_id,cycle_id,updated_at FROM event_tracking_publication_outbox WHERE primary_task_id IS NOT NULL AND cancelled_at IS NULL AND status='submitted' ORDER BY updated_at,outbox_id LIMIT 100").fetchall()]
        if rows:self._publication_refresh_cursor=(rows[-1]["updated_at"],str(rows[-1]["outbox_id"]))
        duplicate=published=0
        for row in rows:
            status=dispatcher.task_status(int(row["primary_task_id"]))
            if status not in {"duplicate","auto_published"}:continue
            target="duplicate" if status=="duplicate" else "published"
            with self._connect() as conn:
                with conn.transaction():
                    changed=conn.execute("UPDATE event_tracking_publication_outbox SET status=%s,updated_at=clock_timestamp() WHERE outbox_id=%s AND primary_task_id=%s AND status='submitted' AND cancelled_at IS NULL",(target,row["outbox_id"],row["primary_task_id"])).rowcount
            duplicate+=int(target=="duplicate" and changed>0);published+=int(target=="published" and changed>0)
        return {"duplicate":duplicate,"published":published}

    def _event_no_longer_publishable(self, row: dict[str,Any]) -> bool:
        with self._connect() as conn:
            result=conn.execute("SELECT NOT EXISTS(SELECT 1 FROM event_tracking_events e JOIN event_tracking_cycles c ON c.cycle_id=%s AND c.event_id=e.event_id JOIN event_tracking_bindings b ON b.cycle_id=c.cycle_id AND b.event_id=e.event_id AND b.handle_lower=%s WHERE e.event_id=%s AND e.status='active' AND c.status='active' AND b.status='active' AND b.identity_status='verified' AND c.expires_at>clock_timestamp() AND c.hard_expires_at>clock_timestamp()) OR EXISTS(SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=%s) AS ineligible",(row["cycle_id"],row["handle_lower"],row["event_id"],row["event_id"])).fetchone()
            return bool(result["ineligible"])

    def maintain(self, *, tracker: Any) -> dict[str,int]:
        now=tracker.now();ended=0
        with self._connect() as conn:
            rows=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_cycles WHERE status IN ('discovering','active') ORDER BY started_at").fetchall()]
        for cycle in rows:
            started=_dt(cycle["started_at"]);last=_dt(cycle["last_material_progress_at"] or started);reason=None
            if now>=_dt(cycle["hard_expires_at"]):reason="hard_cap_7d"
            elif now-last>=timedelta(hours=24):reason="silence_24h"
            elif now>=_dt(cycle["expires_at"]):reason="initial_window_expired"
            if not reason:continue
            with self._connect() as conn:
                with conn.transaction():
                    current=conn.execute("SELECT status FROM event_tracking_cycles WHERE cycle_id=%s FOR UPDATE",(cycle["cycle_id"],)).fetchone()
                    if not current or current["status"] not in {"discovering","active"}:continue
                    conn.execute("UPDATE event_tracking_cycles SET status='ended',ended_at=%s,end_reason=%s,updated_at=%s WHERE cycle_id=%s",(now,reason,now,cycle["cycle_id"]))
                    conn.execute("UPDATE event_tracking_bindings SET status='released',released_at=%s WHERE cycle_id=%s AND status='active'",(now,cycle["cycle_id"]))
                    other=conn.execute("SELECT 1 FROM event_tracking_cycles WHERE event_id=%s AND status IN ('discovering','active') LIMIT 1",(cycle["event_id"],)).fetchone()
                    if not other:conn.execute("UPDATE event_tracking_events SET status='ended',ended_at=%s,end_reason=%s,updated_at=%s WHERE event_id=%s",(now,reason,now,cycle["event_id"]))
                    conn.execute("UPDATE event_tracking_accounts SET status='inactive',next_due_at=NULL,updated_at=%s WHERE status='active' AND NOT EXISTS(SELECT 1 FROM event_tracking_bindings b WHERE b.handle_lower=event_tracking_accounts.handle_lower AND b.status='active')",(now,))
                    conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,'event_cycle_ended',%s,%s,%s)",("event-audit-"+_stable("cycle-ended",cycle["cycle_id"],now.isoformat()),now,cycle["event_id"],cycle["cycle_id"],Jsonb({"reason":reason})))
                    ended+=1
        return {"ended":ended}

    def reconcile_topic_merges(self, *, tracker: Any | None = None) -> dict[str, int]:
        with self._connect() as conn:
            merges=[dict(r) for r in conn.execute("SELECT source_topic_id,target_topic_id FROM topic_merges ORDER BY created_at,merge_id").fetchall()]
        moved=retired=0
        for merge in merges:
            source_id,target_id=merge["source_topic_id"],merge["target_topic_id"]
            with self._connect() as conn:
                source=conn.execute("SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id WHERE l.topic_id=%s ORDER BY e.updated_at DESC LIMIT 1",(source_id,)).fetchone()
                target=conn.execute("SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id WHERE l.topic_id=%s ORDER BY e.updated_at DESC LIMIT 1",(target_id,)).fetchone()
                source_links={r["topic_id"] for r in conn.execute("SELECT topic_id FROM event_tracking_topic_links WHERE event_id=%s",(source["event_id"],)).fetchall()} if source else set()
                target_links={r["topic_id"] for r in conn.execute("SELECT topic_id FROM event_tracking_topic_links WHERE event_id=%s",(target["event_id"],)).fetchall()} if target else set()
            if not source or source_links-{source_id,target_id} or target_links-{source_id,target_id}:continue
            if target and target["event_id"]==source["event_id"]:continue
            winner=source;loser=None
            if target:
                with self._connect() as conn:
                    source_published=conn.execute("SELECT COUNT(*) AS n FROM event_tracking_publication_outbox WHERE event_id=%s AND status IN ('submitted','published')",(source["event_id"],)).fetchone()["n"]
                    target_published=conn.execute("SELECT COUNT(*) AS n FROM event_tracking_publication_outbox WHERE event_id=%s AND status IN ('submitted','published')",(target["event_id"],)).fetchone()["n"]
                sr=(bool(source_published),source["status"]=="active",source["created_at"]);tr=(bool(target_published),target["status"]=="active",target["created_at"])
                winner,loser=(source,target) if sr>tr else (target,source)
            now=tracker.now() if tracker is not None else datetime.now(UTC)
            if loser and tracker is not None:
                try:
                    tracker.dispatcher.dismiss_event_tasks(event_id=str(loser["event_id"]),dismissed_at=now.isoformat(),dismissed_by="topic_merge")
                except Exception as exc:
                    self.audit("event_topic_merge_task_cancellation_failed",{"winner_event_id":winner["event_id"],"error":f"{type(exc).__name__}: {exc}"[:1000]},at=now,event_id=str(loser["event_id"]))
            with self._connect() as conn:
                with conn.transaction():
                    conn.execute("SELECT event_id FROM event_tracking_events WHERE event_id=%s FOR UPDATE",(winner["event_id"],))
                    existing=conn.execute("SELECT * FROM event_tracking_topic_links WHERE event_id=%s AND topic_id=%s FOR UPDATE",(winner["event_id"],target_id)).fetchone()
                    source_link=conn.execute("SELECT * FROM event_tracking_topic_links WHERE event_id=%s AND topic_id=%s FOR UPDATE",(winner["event_id"],source_id)).fetchone()
                    target_link=conn.execute("SELECT * FROM event_tracking_topic_links WHERE topic_id=%s ORDER BY updated_at DESC LIMIT 1",(target_id,)).fetchone()
                    if not existing:
                        snapshot=(target_link["snapshot_json"] if target_link else source_link["snapshot_json"] if source_link else {})
                        digest=(target_link["snapshot_hash"] if target_link else source_link["snapshot_hash"] if source_link else "")
                        conn.execute("INSERT INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,topic_id) DO NOTHING",(winner["event_id"],target_id,digest,Jsonb(snapshot),now,now))
                        moved+=1
                    conn.execute("DELETE FROM event_tracking_topic_links WHERE topic_id=%s",(source_id,))
                    conn.execute("DELETE FROM event_tracking_topic_links WHERE topic_id=%s AND event_id<>%s",(target_id,winner["event_id"]))
                    conn.execute("UPDATE event_tracking_events SET last_hot_topic_at=%s,updated_at=%s WHERE event_id=%s",(now,now,winner["event_id"]))
                    if loser:
                        conn.execute("UPDATE event_tracking_cycles SET status='ended',ended_at=%s,end_reason='topic_merged',updated_at=%s WHERE event_id=%s AND status IN ('discovering','active')",(now,now,loser["event_id"]))
                        conn.execute("UPDATE event_tracking_bindings SET status='released',released_at=%s WHERE event_id=%s AND status='active'",(now,loser["event_id"]))
                        conn.execute("UPDATE event_tracking_events SET status='ended',ended_at=%s,end_reason='topic_merged',updated_at=%s WHERE event_id=%s",(now,now,loser["event_id"]))
                        conn.execute("UPDATE event_tracking_publication_outbox SET cancelled_at=%s,cancelled_by='topic_merge',next_attempt_at=NULL,updated_at=%s WHERE event_id=%s AND cancelled_at IS NULL AND status NOT IN ('published','duplicate')",(now,now,loser["event_id"]))
                        conn.execute("UPDATE event_tracking_accounts SET status='inactive',next_due_at=NULL,updated_at=%s WHERE status='active' AND NOT EXISTS(SELECT 1 FROM event_tracking_bindings b WHERE b.handle_lower=event_tracking_accounts.handle_lower AND b.status='active')",(now,))
                        retired+=1
                    conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,detail_json) VALUES(%s,%s,'topic_merge_tracking_reconciled',%s,%s) ON CONFLICT(audit_id) DO NOTHING",("event-audit-"+_stable("topic-merge",source_id,target_id,winner["event_id"]),now,winner["event_id"],Jsonb({"source_topic_id":source_id,"target_topic_id":target_id})))
        return {"moved":moved,"retired":retired}

    def retry_rejected_identity_task_cancellations(self, *, tracker: Any, limit: int = 20) -> dict[str,int]:
        dismiss=getattr(tracker.dispatcher,"dismiss_event_tasks",None)
        if not callable(dismiss): return {"cancelled":0,"deferred":0}
        with self._connect() as conn:
            rows=[dict(r) for r in conn.execute("SELECT DISTINCT b.event_id,b.cycle_id,b.handle_lower FROM event_tracking_bindings b JOIN event_tracking_updates u ON u.cycle_id=b.cycle_id AND u.handle_lower=b.handle_lower JOIN event_tracking_publication_outbox o ON o.update_id=u.update_id WHERE b.status='released' AND b.identity_status IN ('rejected','unclear') AND o.cancelled_at IS NULL AND o.status IN ('pending','failed','submitting','submitted') ORDER BY b.event_id,b.cycle_id,b.handle_lower LIMIT %s",(max(1,min(limit,100)),)).fetchall()]
        cancelled=deferred=0
        for row in rows:
            now=tracker.now(); event_id=str(row["event_id"]); cycle_id=str(row["cycle_id"]); handle=str(row["handle_lower"])
            try: result=dismiss(event_id=event_id,dismissed_at=now.isoformat(),dismissed_by="identity_review",handle=handle) or {}
            except Exception as exc:
                deferred+=1; self.audit("event_account_identity_task_cancellation_failed",{"handle":handle,"error":f"{type(exc).__name__}: {exc}"[:1000]},at=now,event_id=event_id,cycle_id=cycle_id); continue
            published={int(x) for x in result.get("publishedTaskIds",[])}
            with self._connect() as conn:
                with conn.transaction():
                    if published: conn.execute("UPDATE event_tracking_publication_outbox SET status='published',updated_at=%s WHERE primary_task_id=ANY(%s) AND status='submitted' AND cancelled_at IS NULL",(now,list(published)))
                    outboxes=conn.execute("SELECT o.outbox_id,o.primary_task_id FROM event_tracking_publication_outbox o JOIN event_tracking_updates u ON u.update_id=o.update_id WHERE o.cycle_id=%s AND u.handle_lower=%s AND o.cancelled_at IS NULL AND o.status IN ('pending','failed','submitting','submitted')",(cycle_id,handle)).fetchall()
                    for outbox in outboxes:
                        if outbox["primary_task_id"] and int(outbox["primary_task_id"]) in published: continue
                        cancelled+=conn.execute("UPDATE event_tracking_publication_outbox SET cancelled_at=%s,cancelled_by='identity_review',next_attempt_at=NULL,updated_at=%s WHERE outbox_id=%s AND cancelled_at IS NULL AND status!='published'",(now,now,outbox["outbox_id"])).rowcount
                    conn.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,'event_account_identity_review_cancelled_tasks',%s,%s,%s) ON CONFLICT(audit_id) DO NOTHING",("event-audit-"+_stable("identity-cancel",event_id,cycle_id,handle,now.isoformat()),now,event_id,cycle_id,Jsonb({"handle":handle,"cancelled_task_ids":result.get("cancelledTaskIds",[]),"published_task_ids":sorted(published)})))
        return {"cancelled":cancelled,"deferred":deferred}

    def dismiss_event(self, event_id: str, *, actor: str = "", tracker: Any) -> dict[str, Any]:
        now = tracker.now()
        with self._connect() as conn:
            with conn.transaction():
                event = conn.execute("SELECT event_id,identity_key FROM event_tracking_events WHERE event_id=%s", (event_id,)).fetchone()
                if not event:
                    raise ValueError("tracking event not found")
                existing = conn.execute("SELECT 1 FROM event_tracking_dismissals WHERE event_id=%s", (event_id,)).fetchone()
                if existing:
                    already=True
                else:
                    already=False
                    conn.execute("INSERT INTO event_tracking_dismissals(event_id,identity_key,dismissed_at,dismissed_by) VALUES(%s,%s,%s,%s)", (event_id, event["identity_key"], now, actor))
                conn.execute("UPDATE event_tracking_events SET status='ended',ended_at=%s,end_reason='manual_dismissed',updated_at=%s WHERE event_id=%s", (now, now, event_id))
                conn.execute("UPDATE event_tracking_cycles SET status='ended',ended_at=%s,end_reason='manual_dismissed',updated_at=%s WHERE event_id=%s AND status IN ('discovering','active')",(now,now,event_id))
                conn.execute("UPDATE event_tracking_bindings SET status='released',released_at=%s WHERE event_id=%s AND status='active'",(now,event_id))
        task_result=tracker.dispatcher.dismiss_event_tasks(event_id=event_id,dismissed_at=now.isoformat(),dismissed_by=actor)
        published=set(int(x) for x in (task_result or {}).get("publishedTaskIds",[]));terminal=set(int(x) for x in (task_result or {}).get("terminalTaskIds",[]))
        with self._connect() as conn:
            with conn.transaction():
                outboxes=conn.execute("SELECT outbox_id,status,primary_task_id FROM event_tracking_publication_outbox WHERE event_id=%s AND cancelled_at IS NULL",(event_id,)).fetchall();cancelled=0
                for outbox in outboxes:
                    task_id=int(outbox["primary_task_id"] or 0)
                    if task_id in published or task_id in terminal:continue
                    if outbox["status"] in {"pending","failed","submitting","submitted"}:
                        conn.execute("UPDATE event_tracking_publication_outbox SET cancelled_at=%s,cancelled_by=%s,next_attempt_at=NULL,updated_at=%s WHERE outbox_id=%s",(now,actor,now,outbox["outbox_id"]));cancelled+=1
        return {"eventId": event_id, "dismissed": True, "alreadyDismissed": already, "dismissedAt": now.isoformat(), "cancelledOutbox": cancelled, "cancelledTasks": len((task_result or {}).get("cancelledTaskIds",[]))}

    def dashboard(self, *, limit: int = 100, enabled: bool = True) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("SELECT e.*,c.status AS cycle_status,c.started_at AS cycle_started_at,c.expires_at,c.hard_expires_at,c.last_material_progress_at AS cycle_last_material_progress_at,c.ended_at AS cycle_ended_at,c.end_reason AS cycle_end_reason,(SELECT COUNT(*) FROM event_tracking_bindings b WHERE b.cycle_id=c.cycle_id AND b.status='active' AND b.identity_status='verified') AS active_account_count,(SELECT COUNT(*) FROM event_tracking_updates u WHERE u.cycle_id=c.cycle_id AND u.classification='material_progress' AND u.status='succeeded') AS material_progress_count FROM event_tracking_events e LEFT JOIN event_tracking_cycles c ON c.cycle_id=e.current_cycle_id WHERE NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=e.event_id) ORDER BY CASE e.status WHEN 'active' THEN 0 WHEN 'discovering' THEN 1 ELSE 2 END,e.updated_at DESC LIMIT %s", (max(1, min(limit, 200)),)).fetchall()
            events=[]
            for row in rows:
                value=dict(row);now=datetime.now(UTC)
                remaining=max(0,int((min(value["expires_at"],value["hard_expires_at"])-now).total_seconds())) if value.get("cycle_status") in {"active","discovering"} and value.get("expires_at") else None
                events.append({"id":value["event_id"],"title":value["title"],"trackingType":value["tracking_type"],"readerValue":value["reader_value"],"status":value["status"],"createdAt":value["created_at"].isoformat(),"updatedAt":value["updated_at"].isoformat(),"lastHotTopicAt":value["last_hot_topic_at"].isoformat() if value["last_hot_topic_at"] else None,"lastMaterialProgressAt":value["last_material_progress_at"].isoformat() if value["last_material_progress_at"] else None,"endReason":value["end_reason"],"cycle":None if not value.get("current_cycle_id") else {"id":value["current_cycle_id"],"status":value.get("cycle_status"),"startedAt":value["cycle_started_at"].isoformat() if value.get("cycle_started_at") else None,"expiresAt":value["expires_at"].isoformat() if value.get("expires_at") else None,"hardExpiresAt":value["hard_expires_at"].isoformat() if value.get("hard_expires_at") else None,"lastMaterialProgressAt":value["cycle_last_material_progress_at"].isoformat() if value.get("cycle_last_material_progress_at") else None,"endedAt":value["cycle_ended_at"].isoformat() if value.get("cycle_ended_at") else None,"endReason":value.get("cycle_end_reason"),"remainingSeconds":remaining,"activeAccountCount":int(value.get("active_account_count") or 0),"materialProgressCount":int(value.get("material_progress_count") or 0)}})
            counts = conn.execute("SELECT status,COUNT(*) AS count FROM event_tracking_events GROUP BY status").fetchall()
            active=int(conn.execute("SELECT COUNT(DISTINCT handle_lower) AS n FROM event_tracking_bindings WHERE status='active' AND identity_status='verified'").fetchone()["n"])
            pending_updates=int(conn.execute("SELECT COUNT(*) AS n FROM event_tracking_updates u WHERE u.status IN ('pending','processing','failed') AND EXISTS(SELECT 1 FROM event_tracking_bindings b WHERE b.cycle_id=u.cycle_id AND b.handle_lower=u.handle_lower AND b.status='active' AND b.identity_status='verified') AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=u.event_id)").fetchone()["n"])
            pending_outbox=int(conn.execute("SELECT COUNT(*) AS n FROM event_tracking_publication_outbox o WHERE o.cancelled_at IS NULL AND o.status IN ('pending','submitting','failed') AND NOT EXISTS(SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=o.event_id)").fetchone()["n"])
            from .event_tracking import EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS
            return {"generatedAt": datetime.now(UTC).isoformat(), "enabled": enabled, "summary": {"eventsByStatus": {str(r["status"]): int(r["count"]) for r in counts}, "activeAccounts": active, "maxActiveAccounts": EVENT_TRACKING_MAX_ACTIVE_ACCOUNTS, "pendingUpdates": pending_updates, "pendingOutbox": pending_outbox}, "prompts": self.prompt_versions(include_content=False), "events": events}

    def event_detail(self, event_id: str, *, dispatcher: Any | None = None) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM event_tracking_events WHERE event_id=%s AND NOT EXISTS (SELECT 1 FROM event_tracking_dismissals d WHERE d.event_id=%s)", (event_id, event_id)).fetchone()
            if not row:
                return None
            event=dict(row);cycles=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_cycles WHERE event_id=%s ORDER BY started_at DESC",(event_id,)).fetchall()]
            topics=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_topic_links WHERE event_id=%s ORDER BY updated_at DESC",(event_id,)).fetchall()]
            account_rows=conn.execute("SELECT DISTINCT a.* FROM event_tracking_accounts a JOIN event_tracking_bindings b ON b.handle_lower=a.handle_lower WHERE b.event_id=%s ORDER BY a.screen_name",(event_id,)).fetchall()
            accounts=[]
            for account in account_rows:
                bindings=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_bindings WHERE event_id=%s AND handle_lower=%s ORDER BY discovered_at DESC",(event_id,account["handle_lower"])).fetchall()]
                accounts.append({"handle":account["screen_name"],"displayName":account["display_name"],"candidateName":account["display_name"] or account["official_entity"],"verificationStatus":next((b["identity_status"] for b in bindings if b["status"]=="active"),bindings[0]["identity_status"] if bindings else "pending"),"identitySummary":(bindings[0]["identity_result_json"] or {}).get("reason", "") if bindings else "","identityEntity":account["official_entity"],"identityEvidence":account["official_evidence_json"] or {},"identityProfile":(account["official_evidence_json"] or {}).get("identity_verification",{}).get("profile",{}),"status":account["status"],"nextDueAt":account["next_due_at"].isoformat() if account["next_due_at"] else None,"lastPolledAt":account["last_polled_at"].isoformat() if account["last_polled_at"] else None,"lastSuccessAt":account["last_success_at"].isoformat() if account["last_success_at"] else None,"lastError":account["last_error"],"bindings":[{"cycleId":b["cycle_id"],"status":b["status"],"role":b["role"],"identityStatus":b["identity_status"],"identityDecision":(b["identity_result_json"] or {}).get("decision"),"identityReason":(b["identity_result_json"] or {}).get("reason"),"identityEntity":(b["identity_result_json"] or {}).get("entity"),"identityEvidence":(b["identity_result_json"] or {}).get("evidence",[]),"identityProfile":(b["identity_result_json"] or {}).get("profile",{}),"identityError":(b["identity_result_json"] or {}).get("error",""),"discoveredAt":b["discovered_at"].isoformat(),"releasedAt":b["released_at"].isoformat() if b["released_at"] else None,"lastContributedAt":b["last_contributed_at"].isoformat() if b["last_contributed_at"] else None} for b in bindings]})
            updates=[dict(r) for r in conn.execute("SELECT u.*,i.payload_json FROM event_tracking_updates u LEFT JOIN event_tracking_inbox i ON i.tweet_id=u.tweet_id WHERE u.event_id=%s ORDER BY u.created_at DESC",(event_id,)).fetchall()]
            outboxes=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_publication_outbox WHERE event_id=%s ORDER BY created_at DESC",(event_id,)).fetchall()]
            discoveries=[dict(r) for r in conn.execute("SELECT * FROM event_tracking_account_discoveries WHERE event_id=%s ORDER BY completed_at DESC",(event_id,)).fetchall()]
        cycle_values=[{"id":c["cycle_id"],"status":c["status"],"startedAt":c["started_at"].isoformat(),"initialExpiresAt":c["initial_expires_at"].isoformat(),"expiresAt":c["expires_at"].isoformat(),"hardExpiresAt":c["hard_expires_at"].isoformat(),"lastMaterialProgressAt":c["last_material_progress_at"].isoformat() if c["last_material_progress_at"] else None,"endedAt":c["ended_at"].isoformat() if c["ended_at"] else None,"endReason":c["end_reason"]} for c in cycles]
        topic_values=[{"topicId":t["topic_id"],"snapshot":t["snapshot_json"],"linkedAt":t["linked_at"].isoformat(),"updatedAt":t["updated_at"].isoformat()} for t in topics]
        update_values=[{"id":u["update_id"],"cycleId":u["cycle_id"],"tweetId":u["tweet_id"],"handle":u["handle_lower"],"status":u["status"],"classification":u["classification"],"newsType":u["news_type"],"factSummary":u["fact_summary"],"difference":u["difference_text"],"confirmedFacts":u["confirmed_facts_json"] or [],"unconfirmedClaims":u["unconfirmed_claims_json"] or [],"confidence":u["confidence"],"reason":u["reason"],"actualModel":u["actual_model"],"usage":u["usage_json"] or {},"durationMs":u["duration_ms"],"error":u["last_error"],"classifiedAt":u["classified_at"].isoformat() if u["classified_at"] else None,"post":u["payload_json"] or {}} for u in updates]
        outbox_values=[]
        for o in outboxes:
            task=dispatcher.task_detail(int(o["primary_task_id"])) if dispatcher and o["primary_task_id"] else None;status="cancelled" if o["cancelled_at"] else str(o["status"]);task_status=str((task or {}).get("status") or "")
            if task_status=="auto_published":status="published"
            elif task_status=="duplicate":status="duplicate"
            elif task_status=="event_tracking_cancelled":status="cancelled"
            elif task_status.endswith("_failed"):status="failed"
            outbox_values.append({"id":o["outbox_id"],"updateId":o["update_id"],"tweetId":o["tweet_id"],"status":status,"taskId":o["primary_task_id"],"attempts":o["attempts"],"error":o["last_error"] or (task or {}).get("error"),"submittedAt":o["submitted_at"].isoformat() if o["submitted_at"] else None,"taskStatus":task_status or None,"sourceUrl":(task or {}).get("sourceUrl"),"title":(task or {}).get("finalTitle") or (task or {}).get("draftTitle"),"content":(task or {}).get("finalContent") or (task or {}).get("draftContent"),"contentStage":"final" if (task or {}).get("finalContent") else "draft" if (task or {}).get("draftContent") else None,"publisherDecision":(task or {}).get("publisherDecision"),"publisherReasonCode":(task or {}).get("publisherReasonCode"),"publishedAt":(task or {}).get("publishedAt"),"updatedAt":(task or {}).get("updatedAt")})
        discovery_values=[{"id":d["discovery_id"],"cycleId":d["cycle_id"],"status":d["status"],"attempts":d["attempts"],"actualModel":d["actual_model"],"usage":d["usage_json"] or {},"durationMs":d["duration_ms"],"toolCalls":d["search_tool_calls_json"] or [],"citations":d["citations_json"] or [],"result":d["result_json"] or {},"error":d["error"],"completedAt":d["completed_at"].isoformat()} for d in discoveries]
        return {"event":{"id":event["event_id"],"title":event["title"],"identityKey":event["identity_key"],"trackingType":event["tracking_type"],"readerValue":event["reader_value"],"status":event["status"],"confirmedFacts":event["confirmed_facts_json"] or [],"unconfirmedClaims":event["unconfirmed_claims_json"] or [],"rationale":event["rationale"],"officialResponseHypothesis":event["official_response_hypothesis_json"] or {},"createdAt":event["created_at"].isoformat(),"updatedAt":event["updated_at"].isoformat(),"endReason":event["end_reason"]},"cycles":cycle_values,"topics":topic_values,"accounts":accounts,"updates":update_values,"outbox":outbox_values,"discoveries":discovery_values,"promptVersions":self.prompt_versions(include_content=False)}

    def prompt_versions(self, *, include_content: bool = True) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT p.*,(SELECT COUNT(*) FROM event_tracking_topic_assessments a WHERE a.prompt_version_id=p.id)+(SELECT COUNT(*) FROM event_tracking_account_discoveries d WHERE d.prompt_version_id=p.id)+(SELECT COUNT(*) FROM event_tracking_updates u WHERE u.prompt_version_id=p.id) AS call_count FROM event_tracking_prompt_versions p ORDER BY p.prompt_key,p.version_number").fetchall()
            return [{"id": int(r["id"]), "key": r["prompt_key"], "version": int(r["version_number"]), "createdAt": r["created_at"].isoformat(), "callCount": int(r["call_count"] or 0), **({"content": r["content"]} if include_content else {})} for r in rows]
