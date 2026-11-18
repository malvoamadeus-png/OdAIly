from __future__ import annotations

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import json
import hashlib
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from packages.common.pg_native import connect_postgres
from packages.common.storage import PostgresSettings


class HotTopicAggregationUnitOfWork:
    """Transaction-scoped native aggregation operations."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def batch_result(self, batch_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT result_json FROM batches WHERE batch_id=%s", (batch_id,)).fetchone()
        return dict(row["result_json"]) if row else None

    def content_exists(self, tweet_id: str) -> bool:
        return self.connection.execute("SELECT 1 FROM content_items WHERE tweet_id=%s", (tweet_id,)).fetchone() is not None

    def topic_state_counts(self) -> dict[str, int]:
        rows = self.connection.execute("SELECT matching_status,COUNT(*) AS n FROM topics GROUP BY matching_status").fetchall()
        return {status: int(next((row["n"] for row in rows if row["matching_status"] == status), 0)) for status in ("seed", "active", "archived")}

    def count_topics_by_status(self, status: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) AS n FROM topics WHERE matching_status=%s", (status,)).fetchone()["n"])

    def pending_brief_topic_ids(self) -> set[str]:
        return {str(row["topic_id"]) for row in self.connection.execute(
            "SELECT topic_id FROM topics WHERE matching_status='active' AND (brief_status='pending' OR brief_retry_after IS NOT NULL) ORDER BY topic_id",
        ).fetchall()}

    def insert_batch_result(self, *, batch_id: str, batch_time: str, input_count: int, committed_at: str, result: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO batches(batch_id,batch_time,input_count,committed_at,result_json) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(batch_id) DO NOTHING",
            (batch_id,batch_time,input_count,_utc_datetime(committed_at),Jsonb(result)),
        )

    def topic_state_rows(self, topic_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in topic_ids))
        if not ids:
            return []
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM topics WHERE matching_status IN ('seed','active') AND topic_id=ANY(%s) ORDER BY topic_id", (ids,),
        ).fetchall()]

    def topic_participation_rows(self, topic_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT activity_account,last_participation_at FROM topic_participations WHERE topic_id=%s ORDER BY activity_account", (topic_id,),
        ).fetchall()]

    def lifecycle_assessment_rows(self, *, topic_id: str, cutoff: datetime) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT DISTINCT cl.*,ci.tweet_id,ci.activity_account,ci.references_json,ci.created_at FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
            "AND ci.created_at>=%s ORDER BY ci.created_at,cl.claim_id",
            (topic_id,cutoff),
        ).fetchall()]

    def lifecycle_event_counts(self, *, topic_id: str, at: datetime) -> dict[str, Any]:
        return dict(self.connection.execute(
            "SELECT t.topic_id,t.started_at,t.seed_expires_at,t.last_participation_at,t.retention_tier,"
            "COUNT(DISTINCT p.activity_account) FILTER (WHERE p.last_participation_at>=%s) AS participants_24h,"
            "COUNT(DISTINCT p.activity_account) FILTER (WHERE p.last_participation_at>=%s) AS participants_6h,"
            "COUNT(DISTINCT p.activity_account) FILTER (WHERE p.last_participation_at>=%s) AS participants_1h "
            "FROM topics t LEFT JOIN topic_participations p ON p.topic_id=t.topic_id WHERE t.topic_id=%s "
            "GROUP BY t.topic_id,t.started_at,t.seed_expires_at,t.last_participation_at,t.retention_tier",
            (at-timedelta(hours=24),at-timedelta(hours=6),at-timedelta(hours=1),topic_id),
        ).fetchone() or {})

    def update_topic_lifecycle(self, *, topic_id: str, matching_status: str, visibility: str, archived_at: str | None,
                               retention_tier: str, lifecycle_refresh_at: str | None, refreshed_at: str) -> None:
        self.connection.execute(
            "UPDATE topics SET matching_status=%s,visibility=%s,archived_at=%s,retention_tier=%s,lifecycle_refresh_at=%s "
            "WHERE topic_id=%s", (matching_status,visibility,_utc_datetime(archived_at) if archived_at else None,
                                    retention_tier,_utc_datetime(lifecycle_refresh_at) if lifecycle_refresh_at else None,topic_id),
        )

    def update_topic_metrics(self, *, topic_id: str, last_participation_at: datetime | None, counts: list[int],
                             velocity: float, hotness: float) -> None:
        self.connection.execute(
            "UPDATE topics SET last_participation_at=%s,participant_count_1h=%s,participant_count_6h=%s,participant_count_24h=%s, "
            "participant_velocity=%s,hotness_score=%s WHERE topic_id=%s",
            (last_participation_at,counts[0],counts[1],counts[2],velocity,hotness,topic_id),
        )

    def insert_content_item(self, *, item: Any, fingerprint: str) -> bool:
        row = self.connection.execute(
            "INSERT INTO content_items(content_item_id,tweet_id,activity_account,author,activity_type,content_text,expanded_text,created_at,source_url,metrics_json,references_json,raw_payload_json,fingerprint) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tweet_id) DO NOTHING RETURNING content_item_id",
            (item.content_item_id,item.tweet_id,item.activity_account,item.author,item.activity_type,item.content_text,
             item.expanded_text,_utc_datetime(item.created_at),item.source_url,Jsonb(item.metrics),Jsonb(item.references),Jsonb(item.raw_payload),fingerprint),
        ).fetchone()
        return row is not None

    def insert_claim(self, *, claim: Any, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO claims(claim_id,content_item_id,claim_text,claim_kind,entities_json,action_or_issue,stance,evidence_span,confidence,information_value,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(claim_id) DO NOTHING",
            (claim.claim_id,claim.content_item_id,claim.claim_text,claim.claim_kind,Jsonb(list(claim.entities)),claim.action_or_issue,
             claim.stance,claim.evidence_span,claim.confidence,claim.information_value,_utc_datetime(created_at)),
        )

    def retrieval_source_rows(self, topic_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        rows = self.connection.execute(
            "SELECT cl.claim_id,cl.claim_text,cl.entities_json,cl.content_item_id,ci.tweet_id,ci.created_at,ci.activity_account,ci.references_json "
            "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' ORDER BY ci.created_at DESC",
            (topic_id,),
        ).fetchall()
        topic = self.connection.execute(
            "SELECT topic_id,matching_status,event_or_issue,last_evidence_at,started_at FROM topics WHERE topic_id=%s", (topic_id,),
        ).fetchone()
        return [dict(row) for row in rows], dict(topic) if topic else None

    def save_retrieval_state(self, *, topic_id: str, tokens: list[str], hard_keys: list[str], entities: list[str],
                             representative_claim_ids: list[str], recent_claim_ids: list[str], updated_at: str) -> None:
        self.connection.execute(
            "INSERT INTO retrieval_state(scope_type,scope_id,tokens_json,hard_keys_json,entities_json,representative_claim_ids_json,recent_claim_ids_json,updated_at) "
            "VALUES('topic',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(scope_type,scope_id) DO UPDATE SET "
            "tokens_json=EXCLUDED.tokens_json,hard_keys_json=EXCLUDED.hard_keys_json,entities_json=EXCLUDED.entities_json, "
            "representative_claim_ids_json=EXCLUDED.representative_claim_ids_json,recent_claim_ids_json=EXCLUDED.recent_claim_ids_json,updated_at=EXCLUDED.updated_at",
            (topic_id,Jsonb(tokens),Jsonb(hard_keys),Jsonb(entities),Jsonb(representative_claim_ids),Jsonb(recent_claim_ids),_utc_datetime(updated_at)),
        )

    def candidate_topic_profiles(self, topic_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(str(value) for value in topic_ids))
        if not ids:
            return []
        topics = self.connection.execute(
            "SELECT topic_id,working_title,canonical_subject,core_entities_json,event_or_issue FROM topics WHERE topic_id=ANY(%s) ORDER BY topic_id", (ids,),
        ).fetchall()
        profiles = []
        for topic in topics:
            evidence = self.connection.execute(
                "SELECT ci.activity_account,cl.claim_text FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                "JOIN content_items ci ON ci.content_item_id=cl.content_item_id JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND r.decision='support' ORDER BY ci.created_at DESC LIMIT 3",
                (topic["topic_id"],),
            ).fetchall()
            profiles.append({
                "topic_id":topic["topic_id"],"title":topic["working_title"],"canonical_subject":topic["canonical_subject"],
                "core_entities":topic["core_entities_json"] or [],"event_or_issue":topic["event_or_issue"],
                "evidence":[{"account":row["activity_account"],"text":row["claim_text"]} for row in evidence],
            })
        return profiles

    def create_seed(self, *, topic_id: str, claim: Any, item: Any) -> None:
        subject = " ".join(str(claim.claim_text or "").split()).strip()[:120]
        created = _utc_datetime(item.created_at)
        self.connection.execute(
            "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,brief_status,identity_revision) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'seed','hidden','pending',1) ON CONFLICT(topic_id) DO NOTHING",
            (topic_id,subject,subject,Jsonb(list(claim.entities)),claim.action_or_issue,created,created,created+timedelta(hours=24),created,created),
        )

    def insert_audit(self, *, decision_id: str, batch_id: str, decision: dict[str, Any], created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO decision_audit(decision_id,batch_id,content_item_id,claim_id,action,topic_ids_json,candidates_json,reason,decision_source,created_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(decision_id) DO NOTHING",
            (decision_id,batch_id,decision.get("content_item_id"),decision.get("claim_id"),decision.get("action", "unknown"),
             Jsonb(decision.get("topic_ids", [])),Jsonb(decision.get("candidates", [])),decision.get("reason", ""),decision.get("decision_source", "system"),_utc_datetime(created_at)),
        )

    def complete_batch(self, *, batch_id: str, batch_time: str, input_count: int, committed_at: str, result: dict[str, Any]) -> None:
        self.insert_batch_result(batch_id=batch_id,batch_time=batch_time,input_count=input_count,committed_at=committed_at,result=result)

    def topic_reconciliation_pairs(self, *, topic_ids: Iterable[str] | None = None) -> list[tuple[dict[str, Any],dict[str, Any]]]:
        ids = list(dict.fromkeys(str(value) for value in topic_ids or []))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            if ids:
                rows = connection.execute(
                    "SELECT t.topic_id,t.working_title,t.canonical_subject,t.event_or_issue,t.core_entities_json,t.matching_status,t.visibility,t.started_at,t.first_seen_at,t.last_evidence_at,t.retention_tier, "
                    "r.tokens_json,r.hard_keys_json,r.entities_json FROM topics t LEFT JOIN retrieval_state r ON r.scope_type='topic' AND r.scope_id=t.topic_id "
                    "WHERE t.topic_id=ANY(%s) AND t.matching_status IN ('seed','active') ORDER BY t.topic_id", (ids,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT t.topic_id,t.working_title,t.canonical_subject,t.event_or_issue,t.core_entities_json,t.matching_status,t.visibility,t.started_at,t.first_seen_at,t.last_evidence_at,t.retention_tier, "
                    "r.tokens_json,r.hard_keys_json,r.entities_json FROM topics t LEFT JOIN retrieval_state r ON r.scope_type='topic' AND r.scope_id=t.topic_id "
                    "WHERE t.matching_status IN ('seed','active') ORDER BY t.topic_id"
                ).fetchall()
            profiles = []
            for row in rows:
                profile=dict(row)
                claims=connection.execute(
                    "SELECT cl.claim_id,cl.claim_text,cl.claim_kind,cl.action_or_issue,cl.entities_json,cl.stance,cl.evidence_span,cl.confidence,cl.information_value,cl.created_at,ci.tweet_id,ci.activity_account,ci.created_at AS item_created_at,ci.references_json "
                    "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                    "WHERE m.topic_id=%s AND m.superseded_by IS NULL ORDER BY ci.created_at DESC LIMIT 80", (row["topic_id"],),
                ).fetchall()
                profile["claims"]=[dict(value) for value in claims]
                profile["identity"]=None
                profiles.append(profile)
        pairs=[]
        for left_index,left in enumerate(profiles):
            for right in profiles[left_index+1:]:
                pairs.append((left,right))
        return pairs

    def topic_event_profile_rows(self, topic_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]], set[str]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            topic = connection.execute(
                "SELECT * FROM topics WHERE topic_id=%s AND matching_status IN ('seed','active')", (topic_id,),
            ).fetchone()
            if not topic:
                return None, [], set()
            rows = connection.execute(
                "SELECT cl.*,ci.activity_account,ci.created_at,ci.tweet_id FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
                "GROUP BY cl.claim_id,ci.activity_account,ci.created_at,ci.tweet_id ORDER BY ci.created_at", (topic_id,),
            ).fetchall()
            participants = {str(row["activity_account"]) for row in connection.execute(
                "SELECT activity_account FROM topic_participations WHERE topic_id=%s", (topic_id,),
            ).fetchall()}
            return dict(topic), [dict(row) for row in rows], participants

    def update_topic_identity(self, *, topic_id: str, canonical_subject: str, core_entities: list[str],
                              identity_revision: int, updated_at: str, retention_tier: str | None = None) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            connection.execute(
                "UPDATE topics SET canonical_subject=%s,core_entities_json=%s,identity_revision=%s,retention_tier=COALESCE(%s,retention_tier) WHERE topic_id=%s",
                (canonical_subject,Jsonb(core_entities),identity_revision,retention_tier,topic_id),
            )

    def attach_claim(self, *, claim: Any, topic_id: str, score: float, reason: str, source: str, item: Any, at: str,
                     relations: list[str], membership_role: str, defer_participation: bool = False) -> None:
        batch_at = _utc_datetime(at)
        item_at = _utc_datetime(item.created_at)
        labels = {"progress":"事件进展","reaction":"社区反应","attribution":"责任与解释","market_effect":"市场影响","future_effect":"未来影响","mechanism":"产品机制","other":"其他讨论"}
        for relation in relations:
            thread_id = "thread-" + hashlib.sha1(f"{topic_id}\x1f{relation}".encode()).hexdigest()[:16]
            self.connection.execute(
                "INSERT INTO threads(thread_id,topic_id,working_label,current_thesis,relation_to_topic,first_seen_at,last_seen_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(topic_id,relation_to_topic) DO NOTHING",
                (thread_id,topic_id,labels.get(relation,relation),claim.claim_text[:180],relation,batch_at,batch_at),
            )
            thread_id = self.connection.execute(
                "SELECT thread_id FROM threads WHERE topic_id=%s AND relation_to_topic=%s", (topic_id,relation),
            ).fetchone()["thread_id"]
            membership_id = "membership-" + hashlib.sha1(f"{claim.claim_id}\x1f{topic_id}\x1f{thread_id}".encode()).hexdigest()[:16]
            self.connection.execute(
                "INSERT INTO memberships(membership_id,claim_id,topic_id,thread_id,topic_role,role,match_score,decision_source,decision_reason,created_at) "
                "VALUES(%s,%s,%s,%s,'primary',%s,%s,%s,%s,%s) ON CONFLICT(claim_id,topic_id,thread_id) DO NOTHING",
                (membership_id,claim.claim_id,topic_id,thread_id,membership_role,score,source,reason,batch_at),
            )
            self.connection.execute(
                "UPDATE threads th SET last_seen_at=%s,source_count=(SELECT COUNT(DISTINCT claim_id) FROM memberships WHERE thread_id=th.thread_id), "
                "independent_account_count=(SELECT COUNT(DISTINCT ci.activity_account) FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                "JOIN content_items ci ON ci.content_item_id=cl.content_item_id WHERE m.thread_id=th.thread_id) WHERE th.thread_id=%s",
                (batch_at,thread_id),
            )
        if not defer_participation:
            self.connection.execute(
                "INSERT INTO topic_participations(topic_id,activity_account,last_participation_at,last_content_item_id) VALUES(%s,%s,%s,%s) "
                "ON CONFLICT(topic_id,activity_account) DO UPDATE SET last_participation_at=GREATEST(topic_participations.last_participation_at,EXCLUDED.last_participation_at),last_content_item_id=EXCLUDED.last_content_item_id",
                (topic_id,item.activity_account,item_at,item.content_item_id),
            )
        self.connection.execute("UPDATE topics SET last_evidence_at=GREATEST(COALESCE(last_evidence_at,%s),%s) WHERE topic_id=%s", (item_at,item_at,topic_id))

    def store_claim_review(self, *, claim_id: str, topic_id: str, decision: str, confidence: float, reason: str,
                           model: str, reasoning_effort: str, input_hash: str, reviewed_at: str, error: str | None = None) -> None:
        if decision not in {"support", "context", "unrelated"}:
            raise ValueError("invalid semantic review decision")
        self.connection.execute(
            "INSERT INTO topic_claim_reviews(claim_id,topic_id,decision,confidence,reason,model,reasoning_effort,input_hash,reviewed_at,error) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(claim_id,topic_id) DO UPDATE SET decision=EXCLUDED.decision,confidence=EXCLUDED.confidence,reason=EXCLUDED.reason,model=EXCLUDED.model,reasoning_effort=EXCLUDED.reasoning_effort,input_hash=EXCLUDED.input_hash,reviewed_at=EXCLUDED.reviewed_at,error=EXCLUDED.error",
            (claim_id,topic_id,decision,min(1.0,max(0.0,float(confidence))),reason,model,reasoning_effort,input_hash,_utc_datetime(reviewed_at),error),
        )

    def rebuild_topic_participations(self, *, topic_ids: Iterable[str]) -> None:
        for topic_id in sorted(set(str(value) for value in topic_ids)):
            self.connection.execute("DELETE FROM topic_participations WHERE topic_id=%s", (topic_id,))
            rows = self.connection.execute(
                "SELECT ci.activity_account,ci.created_at,ci.content_item_id FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
                "ORDER BY ci.created_at,cl.claim_id", (topic_id,),
            ).fetchall()
            latest: dict[str, Any] = {}
            for row in rows:
                latest[str(row["activity_account"])] = row
            for account, row in latest.items():
                self.connection.execute(
                    "INSERT INTO topic_participations(topic_id,activity_account,last_participation_at,last_content_item_id) "
                    "VALUES(%s,%s,%s,%s)",
                    (topic_id, account, row["created_at"], row["content_item_id"]),
                )
            latest_evidence = self.connection.execute(
                "SELECT MAX(ci.created_at) AS latest FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL "
                "AND COALESCE(r.decision,'support') IN ('support','context')", (topic_id,),
            ).fetchone()
            self.connection.execute(
                "UPDATE topics SET last_evidence_at=%s WHERE topic_id=%s",
                (latest_evidence["latest"] if latest_evidence else None, topic_id),
            )


    def semantic_review_case(self, *, claim_id: str, topic_id: str, pending: set[tuple[str, str]]) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT cl.*,ci.activity_account,ci.created_at,ci.tweet_id,ci.source_url,"
            "t.working_title,t.canonical_subject,t.core_entities_json,t.event_or_issue,t.identity_revision "
            "FROM claims cl JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "JOIN topics t ON t.topic_id=%s WHERE cl.claim_id=%s",
            (topic_id, claim_id),
        ).fetchone()
        if row is None:
            return None
        evidence_rows = self.connection.execute(
            "SELECT cl.claim_id,cl.claim_text,ci.activity_account,ci.created_at FROM memberships m "
            "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
            "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
            "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context') "
            "ORDER BY ci.created_at DESC LIMIT 12", (topic_id,),
        ).fetchall()
        evidence = [
            {"claim_id": item["claim_id"], "account": item["activity_account"],
             "created_at": _utc_datetime(item["created_at"]).isoformat(), "text": item["claim_text"]}
            for item in evidence_rows
            if (str(item["claim_id"]), topic_id) not in pending and str(item["claim_id"]) != claim_id
        ][:8]
        entities = row["core_entities_json"] or []
        claim_entities = row["entities_json"] or []
        topic = {"topic_id": topic_id, "title": row["working_title"], "canonical_subject": row["canonical_subject"],
                 "core_entities": entities, "event_or_issue": row["event_or_issue"],
                 "identity_revision": int(row["identity_revision"] or 0), "evidence": evidence}
        claim = {"claim_id": claim_id, "account": row["activity_account"],
                 "created_at": _utc_datetime(row["created_at"]).isoformat(),
                 "tweet_id": row["tweet_id"], "source_url": row["source_url"], "kind": row["claim_kind"],
                 "entities": claim_entities, "text": row["claim_text"]}
        from .topic_aggregator import json_dumps, stable_id
        fingerprint = hashlib.sha256(json_dumps({"claim": claim, "topic": topic}).encode("utf-8")).hexdigest()
        return {"case_id": stable_id("semantic-review-case", claim_id, topic_id), "claim_id": claim_id,
                "topic_id": topic_id, "claim": claim, "topic": topic, "input_hash": fingerprint}


class HotTopicPostgresRepository:
    """Native PostgreSQL adapter for HotTopic account and collection state."""

    def __init__(self, *, schema: str = "hottopic", settings: PostgresSettings | None = None) -> None:
        self.schema = schema
        self.settings = settings
        self.application_name = "odaily-hottopic"

    @contextmanager
    def aggregation_transaction(self):
        """Open one native transaction for deterministic topic mutations."""
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                yield HotTopicAggregationUnitOfWork(connection)

    def unreviewed_claim_targets(self, *, topic_ids: Iterable[str], limit: int,
                                 exclude: set[tuple[str, str]] | None = None) -> set[tuple[str, str]]:
        ids = sorted(set(str(value) for value in topic_ids))
        if not ids or limit <= 0:
            return set()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT m.claim_id,m.topic_id FROM memberships m LEFT JOIN topic_claim_reviews r "
                "ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id WHERE m.topic_id=ANY(%s) "
                "AND m.superseded_by IS NULL AND (r.claim_id IS NULL OR r.error IS NOT NULL) "
                "ORDER BY m.created_at,m.claim_id LIMIT %s", (ids, int(limit)),
            ).fetchall()
        excluded = exclude or set()
        return {(str(row["claim_id"]), str(row["topic_id"])) for row in rows
                if (str(row["claim_id"]), str(row["topic_id"])) not in excluded}

    def semantic_review_case(self, *, claim_id: str, topic_id: str,
                             pending: set[tuple[str, str]]) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return HotTopicAggregationUnitOfWork(connection).semantic_review_case(
                claim_id=claim_id, topic_id=topic_id, pending=pending,
            )

    def cached_claim_review(self, *, claim_id: str, topic_id: str, input_hash: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT decision,confidence,reason,model,reasoning_effort,error FROM topic_claim_reviews "
                "WHERE claim_id=%s AND topic_id=%s AND input_hash=%s", (claim_id, topic_id, input_hash),
            ).fetchone()
            return dict(row) if row else None

    def count_pending_claim_reviews(self) -> int:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM memberships m JOIN topics t ON t.topic_id=m.topic_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE t.matching_status IN ('seed','active') AND m.superseded_by IS NULL "
                "AND (r.claim_id IS NULL OR r.error IS NOT NULL)"
            ).fetchone()
            return int(row["n"] if row else 0)

    def active_topic_ids(self) -> set[str]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return {str(row["topic_id"]) for row in connection.execute(
                "SELECT topic_id FROM topics WHERE matching_status IN ('seed','active') ORDER BY topic_id"
            ).fetchall()}

    def topic_identity_candidate_ids(self, terms: Iterable[str]) -> set[str]:
        values = [str(value) for value in terms if str(value)]
        if not values:
            return set()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT DISTINCT scope_id FROM retrieval_state WHERE scope_type='topic' AND "
                "(tokens_json ?| %s OR hard_keys_json ?| %s OR entities_json ?| %s)", (values, values, values),
            ).fetchall()
            return {str(row["scope_id"]) for row in rows}

    def topic_pair_review(self, left_topic_id: str, right_topic_id: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT input_hash,decision,next_attempt_at FROM topic_pair_reviews WHERE left_topic_id=%s AND right_topic_id=%s",
                (left_topic_id, right_topic_id),
            ).fetchone()
            return dict(row) if row else None

    def topic_merge_review_pending_count(self) -> int:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return int(connection.execute(
                "SELECT COUNT(*) AS n FROM topic_pair_reviews WHERE decision='failed' AND (next_attempt_at IS NULL OR next_attempt_at<=clock_timestamp())"
            ).fetchone()["n"])

    def seed_prompt_versions(self, versions: Iterable[tuple[str,int,str]], *, created_at: datetime) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for key, version, content in versions:
                    connection.execute("INSERT INTO event_tracking_prompt_versions(prompt_key,version_number,content,created_at) VALUES(%s,%s,%s,%s) ON CONFLICT(prompt_key,version_number) DO NOTHING",
                                       (key,version,content,created_at))

    def prompt(self, key: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row=connection.execute("SELECT * FROM event_tracking_prompt_versions WHERE prompt_key=%s ORDER BY version_number DESC LIMIT 1",(key,)).fetchone()
            return dict(row) if row else None

    def event_tracking_topic_snapshots(self, *, topic_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        requested=sorted(set(str(value) for value in topic_ids or [] if str(value)))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            params=[]; where="t.matching_status='active' AND t.visibility='visible'"
            if requested: where+=" AND t.topic_id=ANY(%s)";params.append(requested)
            rows=connection.execute(
                "SELECT t.*,b.title AS brief_title,b.brief,b.generated_at,a.topic_id AS assessment_topic,o.status AS observation_status "
                "FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                "LEFT JOIN event_tracking_topic_assessments a ON a.topic_id=t.topic_id LEFT JOIN event_tracking_topic_observations o ON o.topic_id=t.topic_id "
                f"WHERE {where} ORDER BY CASE WHEN a.topic_id IS NULL OR o.status IN ('pending','failed') THEN 0 ELSE 1 END,COALESCE(o.first_eligible_at,t.first_seen_at),t.hotness_score DESC,t.last_evidence_at DESC LIMIT 100",params).fetchall()
            result=[]
            for row in rows:
                topic_id=row["topic_id"]
                evidence=[dict(item) for item in connection.execute(
                    "SELECT c.claim_text,c.action_or_issue,c.stance,c.evidence_span,c.created_at,ci.activity_account,ci.expanded_text,ci.source_url "
                    "FROM topic_evidence te JOIN claims c ON c.claim_id=te.claim_id JOIN content_items ci ON ci.content_item_id=c.content_item_id "
                    "WHERE te.topic_id=%s ORDER BY c.created_at DESC LIMIT 10",(topic_id,)).fetchall()]
                participants=[item["activity_account"] for item in connection.execute("SELECT activity_account FROM topic_participations WHERE topic_id=%s ORDER BY last_participation_at DESC LIMIT 20",(topic_id,)).fetchall()]
                snapshot={"topic_id":topic_id,"title":str(row["brief_title"] or row["working_title"]),"brief":str(row["brief"] or ""),
                    "canonical_subject":str(row["canonical_subject"]),"core_entities":row["core_entities_json"] or [],"event_or_issue":str(row["event_or_issue"]),
                    "started_at":_utc_datetime(row["started_at"]).isoformat(),"first_seen_at":_utc_datetime(row["first_seen_at"]).isoformat(),
                    "last_evidence_at":_utc_datetime(row["last_evidence_at"]).isoformat() if row["last_evidence_at"] else None,
                    "hotness":float(row["hotness_score"] or 0),"participants":[str(v) for v in participants],
                    "evidence":[{"claim":str(i["claim_text"]),"action_or_issue":str(i["action_or_issue"]),"stance":str(i["stance"]),
                        "evidence":str(i["evidence_span"]),"account":str(i["activity_account"]),"text":str(i["expanded_text"]),
                        "source_url":str(i["source_url"]),"created_at":_utc_datetime(i["created_at"]).isoformat()} for i in evidence]}
                snapshot["snapshot_hash"]=hashlib.sha256(json.dumps(snapshot,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
                result.append(snapshot)
            return result

    def event_tracking_assessment(self, topic_id: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row=connection.execute("SELECT snapshot_hash,decision,next_attempt_at,actual_model FROM event_tracking_topic_assessments WHERE topic_id=%s",(topic_id,)).fetchone()
            return dict(row) if row else None

    def queue_event_tracking_observations(self, snapshots: list[dict[str,Any]], *, at: datetime) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for snap in snapshots:
                    connection.execute("INSERT INTO event_tracking_topic_observations(topic_id,first_eligible_at,last_eligible_at,status,first_queued_at,last_queued_at,updated_at) VALUES(%s,%s,%s,'pending',%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET last_eligible_at=EXCLUDED.last_eligible_at,updated_at=EXCLUDED.updated_at",
                        (snap["topic_id"],_utc_datetime(snap["first_seen_at"]),at,at,at,at))

    def update_event_tracking_observation(self, topic_id: str, *, status: str, at: datetime, reason: str | None = None,
                                          error: str | None = None, begin_attempt: bool = False) -> dict[str,Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row=connection.execute("SELECT * FROM event_tracking_topic_observations WHERE topic_id=%s FOR UPDATE",(topic_id,)).fetchone()
                if row is None:return None
                connection.execute("UPDATE event_tracking_topic_observations SET status=%s,last_queued_at=CASE WHEN %s THEN %s ELSE last_queued_at END,last_attempt_at=CASE WHEN %s THEN %s ELSE last_attempt_at END,attempts=attempts+CASE WHEN %s THEN 1 ELSE 0 END,reason=%s,last_error=%s,updated_at=%s WHERE topic_id=%s",
                    (status,begin_attempt,at,begin_attempt,at,begin_attempt,reason,error,at,topic_id))
                return dict(row)

    def event_tracking_save_assessments(self, snapshots: list[dict[str,Any]], *, decisions: dict[str,dict[str,Any]],
                                       prompt: dict[str,Any], result: Any, at: datetime) -> int:
        from .event_tracking import compact_json
        tracked=0
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for snap in snapshots:
                    current=connection.execute("SELECT t.topic_id FROM topics t WHERE t.topic_id=%s AND t.matching_status='active' AND t.visibility='visible'",(snap["topic_id"],)).fetchone()
                    if not current: continue
                    decision=decisions.get(snap["topic_id"])
                    if decision is None: continue
                    now=at
                    payload=dict(decision)
                    connection.execute(
                        "INSERT INTO event_tracking_topic_assessments(topic_id,snapshot_hash,snapshot_json,decision,tracking_type,event_identity,result_json,prompt_version_id,actual_model,usage_json,duration_ms,raw_output,attempts,last_error,next_attempt_at,judged_at,created_at,updated_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,NULL,NULL,%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET "
                        "snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,decision=EXCLUDED.decision,tracking_type=EXCLUDED.tracking_type,event_identity=EXCLUDED.event_identity,result_json=EXCLUDED.result_json,prompt_version_id=EXCLUDED.prompt_version_id,actual_model=EXCLUDED.actual_model,usage_json=EXCLUDED.usage_json,duration_ms=EXCLUDED.duration_ms,raw_output=EXCLUDED.raw_output,attempts=event_tracking_topic_assessments.attempts+1,last_error=NULL,next_attempt_at=NULL,judged_at=EXCLUDED.judged_at,updated_at=EXCLUDED.updated_at",
                        (snap["topic_id"],snap["snapshot_hash"],Jsonb(snap),decision["decision"],None if decision["tracking_type"]=="none" else decision["tracking_type"],decision["event_identity"],Jsonb(payload),int(prompt["id"]),result.model,Jsonb(result.usage),result.duration_ms,result.text[:30000],now,now,now))
                    status="tracked" if decision["decision"]=="track" else "not_tracked"
                    observation=connection.execute("SELECT * FROM event_tracking_topic_observations WHERE topic_id=%s FOR UPDATE",(snap["topic_id"],)).fetchone()
                    if observation:
                        connection.execute("UPDATE event_tracking_topic_observations SET status=%s,judged_at=%s,reason=%s,last_error=NULL,updated_at=%s WHERE topic_id=%s",
                                           (status,now,decision.get("reason"),now,snap["topic_id"]))
                    if decision["decision"]=="track":
                        self.event_tracking_link_topic(connection,snap,decision,at=now)
                        tracked+=1
                connection.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,detail_json) VALUES(%s,%s,'topic_judgment_completed',%s)",
                    ("event-audit-"+hashlib.sha1(f"topic_judgment_completed\x1f{at.isoformat()}".encode()).hexdigest()[:16],at,Jsonb({"topic_count":len(snapshots),"tracked":tracked})))
        return tracked

    def event_tracking_link_topic(self, connection: Any, snapshot: dict[str,Any], decision: dict[str,Any], *, at: datetime,
                                  allow_failed_reopen: bool=False) -> str | None:
        from .event_tracking import compact_json, stable_id
        row=connection.execute("SELECT e.* FROM event_tracking_topic_links l JOIN event_tracking_events e ON e.event_id=l.event_id WHERE l.topic_id=%s ORDER BY l.updated_at DESC LIMIT 1",(snapshot["topic_id"],)).fetchone()
        now=at; event_title=str(decision.get("event_identity") or "").strip() or snapshot["title"]
        if row and connection.execute("SELECT 1 FROM event_tracking_dismissals WHERE event_id=%s",(row["event_id"],)).fetchone(): return None
        if row is None:
            identity=stable_id("event-topic",snapshot["topic_id"])
            if connection.execute("SELECT 1 FROM event_tracking_dismissals WHERE identity_key=%s",(identity,)).fetchone():return None
            event_id=stable_id("tracking-event",identity); cycle_id=stable_id("event-cycle",event_id,now.isoformat())
            connection.execute("INSERT INTO event_tracking_events(event_id,identity_key,identity_tokens_json,title,tracking_type,reader_value,confirmed_facts_json,unconfirmed_claims_json,rationale,official_response_hypothesis_json,status,current_cycle_id,created_at,updated_at,last_hot_topic_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'discovering',%s,%s,%s,%s)",
                (event_id,identity,Jsonb([snapshot["topic_id"]]),event_title,decision["tracking_type"],decision["reader_value"],Jsonb(decision["confirmed_facts"]),Jsonb(decision["unconfirmed_claims"]),decision["reason"],Jsonb(decision.get("official_response_hypothesis") or {}),cycle_id,now,now,now))
            initial=now+timedelta(hours=int(decision.get("recommended_initial_window_hours") or 72)); hard=now+timedelta(days=7)
            connection.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES(%s,%s,'discovering',%s,%s,%s,%s,%s,%s)",(cycle_id,event_id,now,initial,min(initial,hard),hard,now,now))
            connection.execute("INSERT INTO event_tracking_audit(audit_id,at,kind,event_id,cycle_id,detail_json) VALUES(%s,%s,'event_created',%s,%s,%s)",(stable_id("event-audit","event_created",event_id,cycle_id,now.isoformat()),now,event_id,cycle_id,Jsonb({"topic_id":snapshot["topic_id"],"tracking_type":decision["tracking_type"]})))
        else:
            event_id=str(row["event_id"]); cycle_id=row["current_cycle_id"]
            connection.execute("UPDATE event_tracking_events SET updated_at=%s,last_hot_topic_at=%s,title=%s WHERE event_id=%s",(now,now,event_title,event_id))
            if row["status"] in ({"ended","discovery_failed","capacity_exhausted"} if allow_failed_reopen else {"ended"}):
                cycle_id=stable_id("event-cycle",event_id,now.isoformat()); initial=now+timedelta(hours=72); hard=now+timedelta(days=7)
                connection.execute("INSERT INTO event_tracking_cycles(cycle_id,event_id,status,started_at,initial_expires_at,expires_at,hard_expires_at,created_at,updated_at) VALUES(%s,%s,'discovering',%s,%s,%s,%s,%s,%s)",(cycle_id,event_id,now,initial,initial,hard,now,now))
                connection.execute("UPDATE event_tracking_events SET status='discovering',current_cycle_id=%s,ended_at=NULL,end_reason=NULL WHERE event_id=%s",(cycle_id,event_id))
        connection.execute("INSERT INTO event_tracking_topic_links(event_id,topic_id,snapshot_hash,snapshot_json,linked_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(event_id,topic_id) DO UPDATE SET snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,updated_at=EXCLUDED.updated_at",
            (event_id,snapshot["topic_id"],snapshot["snapshot_hash"],Jsonb(snapshot),now,now))
        return event_id

    def event_tracking_store_assessment_failure(self, snapshots: list[dict[str,Any]], *, prompt: dict[str,Any], error: Exception, at: datetime) -> None:
        message=f"{type(error).__name__}: {error}"[:2000]; retry=at+timedelta(minutes=15)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for snap in snapshots:
                    connection.execute("INSERT INTO event_tracking_topic_assessments(topic_id,snapshot_hash,snapshot_json,decision,prompt_version_id,attempts,last_error,next_attempt_at,created_at,updated_at) VALUES(%s,%s,%s,'failed',%s,1,%s,%s,%s,%s) ON CONFLICT(topic_id) DO UPDATE SET snapshot_hash=EXCLUDED.snapshot_hash,snapshot_json=EXCLUDED.snapshot_json,decision='failed',prompt_version_id=EXCLUDED.prompt_version_id,attempts=event_tracking_topic_assessments.attempts+1,last_error=EXCLUDED.last_error,next_attempt_at=EXCLUDED.next_attempt_at,updated_at=EXCLUDED.updated_at",
                        (snap["topic_id"],snap["snapshot_hash"],Jsonb(snap),int(prompt["id"]),message,retry,at,at))
                    connection.execute("UPDATE event_tracking_topic_observations SET status='failed',last_error=%s,reason=%s,last_attempt_at=%s,updated_at=%s WHERE topic_id=%s",(message,message,at,at,snap["topic_id"]))

    def queue_visible_merge_scan(self) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("UPDATE topic_merge_scan_state SET pending_new_visible=1,request_seq=request_seq+1 WHERE singleton_key='global'")

    def visible_merge_scan_state(self) -> dict[str, Any]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row=connection.execute("SELECT * FROM topic_merge_scan_state WHERE singleton_key='global'").fetchone()
            return dict(row) if row else {}

    def visible_merge_cards(self) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT t.topic_id,t.first_seen_at,b.revision,b.title,b.brief,b.content_hash,b.source_claim_ids_json "
                "FROM topics t JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                "WHERE t.matching_status='active' AND t.visibility='visible' AND t.brief_status='ready' "
                "ORDER BY t.hotness_score DESC,t.last_evidence_at DESC,t.topic_id"
            ).fetchall()]

    def visible_merge_evidence(self, topic_id: str) -> list[dict[str, str]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            brief=connection.execute("SELECT source_claim_ids_json FROM brief_revisions WHERE topic_id=%s ORDER BY revision DESC LIMIT 1",(topic_id,)).fetchone()
            cited=set(brief["source_claim_ids_json"] or []) if brief else set()
            rows=[dict(row) for row in connection.execute(
                "SELECT cl.claim_id,cl.claim_text,ci.activity_account,ci.created_at FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                "JOIN content_items ci ON ci.content_item_id=cl.content_item_id LEFT JOIN topic_claim_reviews r ON r.claim_id=cl.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' "
                "ORDER BY ci.created_at DESC",(topic_id,),
            ).fetchall()]
            return sorted([{"claim_id":r["claim_id"],"text":r["claim_text"],"account":r["activity_account"]} for r in rows],
                          key=lambda r:r["claim_id"] not in cited)[:15]

    def create_visible_merge_scan(self, *, scan_id: str, started_at: datetime, trigger: str, input_hash: str,
                                  topic_ids: list[str], request_seq: int) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("INSERT INTO topic_merge_scans(scan_id,started_at,trigger_kind,input_hash,topic_ids_json,status) VALUES(%s,%s,%s,%s,%s,'running') ON CONFLICT(scan_id) DO NOTHING",
                                   (scan_id,started_at,trigger,input_hash,Jsonb(topic_ids)))
                connection.execute("UPDATE topic_merge_scan_state SET pending_new_visible=1 WHERE singleton_key='global' AND request_seq=%s",(request_seq,))

    def complete_visible_merge_scan(self, *, scan_id: str, completed_at: datetime, coarse: list[Any], deep: list[Any],
                                    merge_ids: list[str], request_seq: int, trigger: str) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("UPDATE topic_merge_scans SET completed_at=%s,coarse_json=%s,deep_json=%s,merge_ids_json=%s,status='succeeded' WHERE scan_id=%s",
                                   (completed_at,Jsonb(coarse),Jsonb(deep),Jsonb(merge_ids),scan_id))
                connection.execute("UPDATE topic_merge_scan_state SET pending_new_visible=CASE WHEN request_seq=%s THEN 0 ELSE pending_new_visible END,next_retry_at=NULL,last_periodic_success_at=CASE WHEN %s='periodic' THEN %s ELSE last_periodic_success_at END WHERE singleton_key='global'",
                                   (request_seq,trigger,completed_at))

    def fail_visible_merge_scan(self, *, scan_id: str, failed_at: datetime, error: str, retry_at: datetime) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("UPDATE topic_merge_scans SET completed_at=%s,status='failed',error=%s WHERE scan_id=%s",(failed_at,error,scan_id))
                connection.execute("UPDATE topic_merge_scan_state SET next_retry_at=%s WHERE singleton_key='global'",(retry_at,))

    def save_topic_pair_review(self, *, left_topic_id: str, right_topic_id: str, input_hash: str,
                               decision: str, reason: str, response: dict[str, Any], reviewed_at: datetime,
                               retry_at: datetime | None) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO topic_pair_reviews(left_topic_id,right_topic_id,input_hash,decision,reason,evidence_json,model,reasoning_effort,attempts,reviewed_at,next_attempt_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s) ON CONFLICT(left_topic_id,right_topic_id) DO UPDATE SET "
                    "input_hash=EXCLUDED.input_hash,decision=EXCLUDED.decision,reason=EXCLUDED.reason,evidence_json=EXCLUDED.evidence_json,"
                    "model=EXCLUDED.model,reasoning_effort=EXCLUDED.reasoning_effort,attempts=topic_pair_reviews.attempts+1,"
                    "reviewed_at=EXCLUDED.reviewed_at,next_attempt_at=EXCLUDED.next_attempt_at",
                    (left_topic_id,right_topic_id,input_hash,decision,reason,Jsonb(response),str(response.get("model") or ""),
                     str(response.get("reasoning_effort") or ""),reviewed_at,retry_at),
                )

    def pending_brief_topic_ids(self) -> set[str]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return {str(row["topic_id"]) for row in connection.execute(
                "SELECT topic_id FROM topics WHERE matching_status='active' AND (brief_status='pending' OR brief_retry_after IS NOT NULL)"
            ).fetchall()}

    def replace_batch_result(self, *, batch_id: str, result: dict[str, Any]) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("UPDATE batches SET result_json=%s WHERE batch_id=%s", (Jsonb(result), batch_id))

    def visible_open_topic_ids(self, topic_ids: Iterable[str]) -> set[str]:
        ids = sorted(set(str(value) for value in topic_ids))
        if not ids:
            return set()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return {str(row["topic_id"]) for row in connection.execute(
                "SELECT topic_id FROM topics WHERE topic_id=ANY(%s) AND matching_status IN ('seed','active') AND visibility='visible'",
                (ids,),
            ).fetchall()}

    def consolidation_evidence(self) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT DISTINCT cl.claim_text,ci.activity_account FROM claims cl JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "JOIN memberships m ON m.claim_id=cl.claim_id AND m.superseded_by IS NULL JOIN topics t ON t.topic_id=m.topic_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE t.matching_status IN ('seed','active') AND t.visibility='visible' AND COALESCE(r.decision,'support')='support'"
            ).fetchall()]

    def refresh_topic_identity(self, *, topic_id: str, at: str) -> None:
        from .topic_aggregator import RE_CONTRACT, RE_MENTION, RE_CASHTAG, EVENT_FORMATION_SIGNALS, MECHANISM_SIGNALS, asset_labels, compact, json_dumps
        topic, rows, _participants = self.topic_event_profile_rows(topic_id)
        if not topic or not rows:
            return
        asset_accounts: dict[str,set[str]] = {}
        for row in rows:
            for value in asset_labels(row["claim_text"]):
                asset_accounts.setdefault(value,set()).add(row["activity_account"])
        strongest = max((len(accounts) for accounts in asset_accounts.values()), default=0)
        primary = {value for value,accounts in asset_accounts.items() if len(accounts)==strongest} if strongest else set()
        if strongest == 1 and len(primary)>1:
            primary=set()
        def score(row):
            text=compact(row["claim_text"]); lower=text.lower()
            launch=sum(signal in lower for signal in EVENT_FORMATION_SIGNALS)
            mechanism=sum(signal in lower for signal in MECHANISM_SIGNALS)
            market=sum(signal in lower for signal in ("流动性","内存","芯片","liquidity","absorbing","pvp"))
            named=len(RE_CASHTAG.findall(text))+len(RE_MENTION.findall(text))
            digits=1 if __import__("re").search(r"\b\d+(?:\.\d+)?[kKmMbB万亿%]?\b", RE_CONTRACT.sub("",text)) else 0
            chinese=1 if any("\u4e00"<=char<="\u9fff" for char in text) else 0
            return launch*5+mechanism*3+market+named+digits+chinese,min(len(text),240),row["claim_id"]
        identity_rows=[row for row in rows if any(value.lower() in row["claim_text"].lower() for value in primary)] or rows
        best=max(identity_rows,key=score); title=compact(best["claim_text"])[:180]
        canonical=f"${next(iter(primary))}" if len(primary)==1 else title
        entities=set();issues=set()
        for row in rows:
            entities.update(row["entities_json"] or [])
            issues.update(value for value in row["action_or_issue"].split(",") if value)
        if any(any(signal in row["claim_text"].lower() for signal in EVENT_FORMATION_SIGNALS) for row in rows): issues.add("发布/上线")
        if any(any(signal in row["claim_text"].lower() for signal in MECHANISM_SIGNALS) for row in rows): issues.add("产品机制")
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute("UPDATE topics SET working_title=%s,canonical_subject=%s,core_entities_json=%s,event_or_issue=%s,identity_revision=identity_revision+1 WHERE topic_id=%s",
                                   (title,canonical,Jsonb(sorted(entities)),",".join(sorted(issues)) or "一般讨论",topic_id))

    def consolidate_topic_pair(self, *, source_topic_id: str, target_topic_id: str, at: datetime,
                               reason: str) -> dict[str, str]:
        from .topic_aggregator import TopicAggregator
        at_text = at.isoformat()
        merge_id = "topic-merge-" + hashlib.sha1(f"{source_topic_id}\x1f{target_topic_id}".encode()).hexdigest()[:16]
        with self.aggregation_transaction() as unit:
            rows = [dict(row) for row in unit.connection.execute(
                "SELECT m.*,th.relation_to_topic,cl.*,ci.activity_account FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN threads th ON th.thread_id=m.thread_id LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context')",
                (source_topic_id,),
            ).fetchall()]
            for row in rows:
                claim = TopicAggregator._claim_from_row(row)
                relation = row["relation_to_topic"] or TopicAggregator._relations(claim)[0]
                labels = {"progress":"事件进展","reaction":"社区反应","attribution":"责任与解释","market_effect":"市场影响",
                          "future_effect":"未来影响","mechanism":"产品机制","other":"其他讨论"}
                thread_id = "thread-" + hashlib.sha1(f"{target_topic_id}\x1f{relation}".encode()).hexdigest()[:16]
                unit.connection.execute(
                    "INSERT INTO threads(thread_id,topic_id,working_label,current_thesis,relation_to_topic,first_seen_at,last_seen_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(topic_id,relation_to_topic) DO NOTHING",
                    (thread_id,target_topic_id,labels.get(relation,relation),claim.claim_text[:180],relation,at,at),
                )
                thread_id = unit.connection.execute("SELECT thread_id FROM threads WHERE topic_id=%s AND relation_to_topic=%s",
                                                    (target_topic_id,relation)).fetchone()["thread_id"]
                membership_id = "membership-" + hashlib.sha1(f"{claim.claim_id}\x1f{target_topic_id}\x1f{thread_id}".encode()).hexdigest()[:16]
                unit.connection.execute(
                    "INSERT INTO memberships(membership_id,claim_id,topic_id,thread_id,topic_role,role,match_score,decision_source,decision_reason,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,'local_merge',%s,%s) ON CONFLICT(claim_id,topic_id,thread_id) DO NOTHING",
                    (membership_id,claim.claim_id,target_topic_id,thread_id,row["topic_role"],row["role"],row["match_score"],reason,at),
                )
                review = unit.connection.execute("SELECT * FROM topic_claim_reviews WHERE claim_id=%s AND topic_id=%s", (claim.claim_id,source_topic_id)).fetchone()
                if review:
                    unit.connection.execute(
                        "INSERT INTO topic_claim_reviews(claim_id,topic_id,decision,confidence,reason,model,reasoning_effort,input_hash,reviewed_at,error) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (claim.claim_id,target_topic_id,review["decision"],review["confidence"],review["reason"],review["model"],review["reasoning_effort"],review["input_hash"],review["reviewed_at"],review["error"]),
                    )
                unit.connection.execute("UPDATE memberships SET superseded_by=%s WHERE membership_id=%s", (membership_id,row["membership_id"]))
            for row in unit.connection.execute("SELECT * FROM topic_participations WHERE topic_id=%s", (source_topic_id,)).fetchall():
                unit.connection.execute(
                    "INSERT INTO topic_participations(topic_id,activity_account,last_participation_at,last_content_item_id) VALUES(%s,%s,%s,%s) "
                    "ON CONFLICT(topic_id,activity_account) DO UPDATE SET last_participation_at=GREATEST(topic_participations.last_participation_at,EXCLUDED.last_participation_at),last_content_item_id=CASE WHEN EXCLUDED.last_participation_at>=topic_participations.last_participation_at THEN EXCLUDED.last_content_item_id ELSE topic_participations.last_content_item_id END",
                    (target_topic_id,row["activity_account"],row["last_participation_at"],row["last_content_item_id"]),
                )
            source = unit.connection.execute("SELECT * FROM topics WHERE topic_id=%s FOR UPDATE", (source_topic_id,)).fetchone()
            target = unit.connection.execute("SELECT * FROM topics WHERE topic_id=%s FOR UPDATE", (target_topic_id,)).fetchone()
            if not source or not target:
                return {"merge_id":merge_id,"source_topic_id":source_topic_id,"target_topic_id":target_topic_id,"reason":reason}
            start = min(source["started_at"],target["started_at"])
            first_seen = min(source["first_seen_at"],target["first_seen_at"])
            last_evidence = max(source["last_evidence_at"] or source["started_at"],target["last_evidence_at"] or target["started_at"])
            unit.connection.execute("UPDATE topics SET started_at=%s,first_seen_at=%s,seed_expires_at=%s,last_evidence_at=%s WHERE topic_id=%s",
                                    (start,first_seen,start+timedelta(hours=24),last_evidence,target_topic_id))
            unit.connection.execute("UPDATE topics SET matching_status='archived',visibility='hidden',archived_at=%s,participant_count_1h=0,participant_count_6h=0,participant_count_24h=0,participant_velocity=0,hotness_score=0 WHERE topic_id=%s", (at,source_topic_id))
            unit.connection.execute("INSERT INTO topic_merges(merge_id,source_topic_id,target_topic_id,reason,created_at) VALUES(%s,%s,%s,%s,%s) ON CONFLICT(merge_id) DO NOTHING",
                                    (merge_id,source_topic_id,target_topic_id,reason,at))
        return {"merge_id":merge_id,"source_topic_id":source_topic_id,"target_topic_id":target_topic_id,"reason":reason}

    def brief_snapshot(self, topic_id: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            topic = connection.execute("SELECT * FROM topics WHERE topic_id=%s", (topic_id,)).fetchone()
            if not topic:
                return None
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision),0) AS revision,MAX(generated_at) AS generated_at "
                "FROM brief_revisions WHERE topic_id=%s", (topic_id,),
            ).fetchone()
            claims = [dict(row) for row in connection.execute(
                "SELECT cl.*,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json,"
                "COALESCE(r.decision,'support') AS semantic_decision FROM memberships m "
                "JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context') "
                "GROUP BY cl.claim_id,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json,r.decision "
                "ORDER BY ci.created_at", (topic_id,),
            ).fetchall()]
            for claim in claims:
                claim["context_only"] = claim.pop("semantic_decision") == "context"
            own_ids = {row["claim_id"] for row in claims}
            own_text = " ".join(row["claim_text"] for row in claims)
            from .topic_aggregator import RE_MENTION, asset_labels
            stable = asset_labels(own_text)
            if not stable:
                stable = asset_labels(topic["working_title"]) | set(RE_MENTION.findall(topic["working_title"]))
            if not stable:
                stable = asset_labels(topic["canonical_subject"]) | set(RE_MENTION.findall(topic["canonical_subject"]))
            context: list[dict[str, Any]] = []
            if stable:
                neighbors = connection.execute(
                    "SELECT t.topic_id,r.tokens_json FROM topics t JOIN retrieval_state r ON r.scope_type='topic' AND r.scope_id=t.topic_id "
                    "WHERE t.topic_id<>%s AND t.matching_status IN ('seed','active')", (topic_id,),
                ).fetchall()
                ranked = sorted(((len(stable & set(row["tokens_json"] or [])), row["topic_id"]) for row in neighbors), reverse=True)
                for _score, neighbor_id in [row for row in ranked if row[0]][:12]:
                    for row in connection.execute(
                        "SELECT cl.*,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json "
                        "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                        "JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                        "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                        "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support') IN ('support','context') "
                        "GROUP BY cl.claim_id,ci.source_url,ci.tweet_id,ci.activity_account,ci.created_at,ci.references_json "
                        "ORDER BY ci.created_at LIMIT 3", (neighbor_id,),
                    ).fetchall():
                        if row["claim_id"] not in own_ids:
                            context.append(dict(row) | {"context_only": True})
            return {"topic": dict(topic), "claims": claims, "context_claims": context[:16],
                    "revision": int(revision["revision"] or 0), "generated_at": revision["generated_at"]}

    def write_brief(self, *, topic_id: str, expected_revision: int, revision: int, title: str, brief: str,
                    source_claim_ids: list[str], source_bindings: list[dict[str, Any]], generated_at: datetime,
                    generation_reason: str) -> bool:
        content_hash = hashlib.sha256((title + "\n" + brief).encode()).hexdigest()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                locked = connection.execute("SELECT topic_id FROM topics WHERE topic_id=%s FOR UPDATE", (topic_id,)).fetchone()
                if not locked:
                    return False
                current = connection.execute(
                    "SELECT COALESCE(MAX(revision),0) AS revision FROM brief_revisions WHERE topic_id=%s", (topic_id,),
                ).fetchone()
                if int(current["revision"] or 0) != expected_revision:
                    return False
                connection.execute(
                    "INSERT INTO brief_revisions(topic_id,revision,title,brief,source_claim_ids_json,source_bindings_json,generated_at,generation_reason,content_hash) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (topic_id,revision,title,brief,Jsonb(source_claim_ids),Jsonb(source_bindings),generated_at,generation_reason,content_hash),
                )
                for claim_id in source_claim_ids:
                    connection.execute(
                        "INSERT INTO topic_evidence(topic_id,claim_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                        (topic_id,claim_id),
                    )
                connection.execute(
                    "UPDATE topics SET visibility='visible',retention_tier='permanent',brief_status='ready',brief_error=NULL,brief_error_at=NULL,"
                    "brief_retry_count=0,brief_retry_after=NULL WHERE topic_id=%s", (topic_id,),
                )
        return True

    def record_brief_error(self, *, topic_id: str, at: datetime, error_code: str, error_message: str,
                           retryable: bool) -> dict[str, Any]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                topic = connection.execute("SELECT visibility,brief_retry_count FROM topics WHERE topic_id=%s FOR UPDATE",
                                           (topic_id,)).fetchone()
                has_revision = connection.execute("SELECT 1 FROM brief_revisions WHERE topic_id=%s LIMIT 1", (topic_id,)).fetchone() is not None
                status = "ready" if has_revision else "error"
                old_count = int(topic["brief_retry_count"] or 0) if topic else 0
                count = old_count + 1 if retryable else 0
                retry_after = at + timedelta(minutes=min(360, 2 * (2 ** (count - 1)))) if retryable else None
                connection.execute(
                    "UPDATE topics SET brief_status=%s,brief_error=%s,brief_error_at=%s,brief_retry_count=%s,brief_retry_after=%s WHERE topic_id=%s",
                    (status,error_message,at,count,retry_after.isoformat() if retry_after else None,topic_id),
                )
        return {"brief_status":"error","visibility":topic["visibility"] if topic else "hidden","error_code":error_code,
                "error_message":error_message,"retryable":retryable,"retry_count":count,
                "retry_after":retry_after.isoformat() if retry_after else None}

    def initialize(self) -> tuple[int, ...]:
        from .postgres_schema import initialize_hottopic_schema
        from packages.investment_leads.postgres_schema import initialize_investment_leads_schema

        versions = initialize_hottopic_schema(schema=self.schema, settings=self.settings)
        initialize_investment_leads_schema(schema=self.schema, settings=self.settings)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO hottopic_meta(singleton_key,deployment_started_at) VALUES('global',%s) ON CONFLICT DO NOTHING",
                    (datetime.now(UTC),),
                )
                connection.execute(
                    "INSERT INTO investment_leads_meta(singleton_key,capture_started_at) "
                    "SELECT 'global',deployment_started_at FROM hottopic_meta WHERE singleton_key='global' "
                    "ON CONFLICT(singleton_key) DO NOTHING"
                )
        return versions

    def deployment_started_at(self) -> datetime:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return connection.execute(
                "SELECT deployment_started_at FROM hottopic_meta WHERE singleton_key='global'"
            ).fetchone()["deployment_started_at"]

    def log_event(self, kind: str, detail: dict[str, Any], at: str) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO hottopic_events(at,kind,detail_json) VALUES(%s,%s,%s)",
                    (at, kind, Jsonb(detail)),
                )

    def seed_accounts(self, rows: Iterable[dict[str, str]], *, now: str) -> int:
        added = 0
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for row in rows:
                    handle = str(row.get("screen_name") or row.get("handle") or "").strip().lstrip("@")
                    if not handle:
                        continue
                    lower = handle.lower()
                    cursor = connection.execute(
                        "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,"
                        "investment_leads_enabled_at,created_at,updated_at) VALUES(%s,%s,%s,'followed',%s,%s,%s,%s) "
                        "ON CONFLICT(screen_name_lower) DO NOTHING",
                        (handle, lower, str(row.get("display_name") or ""), now, now, now, now),
                    )
                    added += cursor.rowcount
        return added

    def add_account(self, handle: str, display_name: str, *, now: str) -> dict[str, Any]:
        normalized = _normalize_handle(handle)
        lower = normalized.lower()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO hottopic_accounts(screen_name,screen_name_lower,display_name,status,next_due_at,"
                    "hot_topic_enabled,investment_leads_enabled,investment_leads_enabled_at,created_at,updated_at) "
                    "VALUES(%s,%s,%s,'followed',%s,true,true,%s,%s,%s) ON CONFLICT(screen_name_lower) DO UPDATE SET "
                    "screen_name=EXCLUDED.screen_name,display_name=EXCLUDED.display_name,status='followed',"
                    "next_due_at=EXCLUDED.next_due_at,hot_topic_enabled=true,investment_leads_enabled=true,"
                    "investment_leads_enabled_at=COALESCE(hottopic_accounts.investment_leads_enabled_at,EXCLUDED.investment_leads_enabled_at),"
                    "updated_at=EXCLUDED.updated_at",
                    (normalized, lower, display_name.strip(), _utc_datetime(now), _utc_datetime(now), _utc_datetime(now), _utc_datetime(now)),
                )
        return self.get_account(lower) or {}

    def list_accounts(self, *, query: str = "", status: str = "all") -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            clauses.append("(screen_name_lower LIKE %s OR LOWER(display_name) LIKE %s)")
            term = f"%{query.strip().lstrip('@').lower()}%"
            params.extend((term, term))
        if status != "all":
            clauses.append("status=%s")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM hottopic_accounts" + where +
                " ORDER BY CASE status WHEN 'followed' THEN 0 WHEN 'unfollowed' THEN 1 ELSE 2 END,"
                "cumulative_hot_topic_count DESC,cumulative_content_count DESC,screen_name_lower", params,
            ).fetchall()]

    def get_account(self, lower: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=%s", (lower,)).fetchone()
            return dict(row) if row else None

    def list_x_agent_accounts(
        self, *, query: str = "", module: str = "all", enabled: str | bool = "all",
        availability: str = "all", offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        column_by_module = {
            "hot_topic": "hot_topic_enabled", "market_sentiment": "market_sentiment_enabled",
            "project_promotion": "project_promotion_enabled", "investment_leads": "investment_leads_enabled",
        }
        column = column_by_module.get(module)
        if module != "all" and column is None:
            raise ValueError("不支持的 X Agent 模块筛选")
        if availability not in {"all", "suspected_invalid"}:
            raise ValueError("不支持的 X Agent 账号状态筛选")
        clauses: list[str] = []
        params: list[Any] = []
        if availability == "suspected_invalid":
            clauses.append("consecutive_not_found >= 3")
        if query.strip():
            clauses.append("(screen_name_lower LIKE %s OR LOWER(display_name) LIKE %s)")
            term = f"%{query.strip().lower()}%"
            params.extend((term, term))
        if column and enabled in {True, False, "true", "false"}:
            clauses.append(f"{column}=%s")
            params.append(enabled in {True, "true"})
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_limit = min(100, max(1, int(limit)))
        safe_offset = max(0, int(offset))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(f"SELECT COUNT(*) AS total FROM hottopic_accounts{where}", params).fetchone()["total"])
            rows = connection.execute(
                "SELECT * FROM hottopic_accounts" + where +
                " ORDER BY hot_topic_enabled DESC,market_sentiment_enabled DESC,project_promotion_enabled DESC,"
                "investment_leads_enabled DESC,screen_name_lower LIMIT %s OFFSET %s",
                [*params, safe_limit, safe_offset],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total}

    def set_account_status(self, lower: str, status: str, *, now: str) -> dict[str, Any] | None:
        if status not in {"followed", "unfollowed"}:
            raise ValueError("不支持的账号状态")
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT * FROM hottopic_accounts WHERE screen_name_lower=%s FOR UPDATE", (lower,),
                ).fetchone()
                if current is None:
                    return None
                hot_topic = status == "followed"
                market = bool(current["market_sentiment_enabled"]) if hot_topic else False
                project = bool(current["project_promotion_enabled"]) if hot_topic else False
                investment = bool(current["investment_leads_enabled"]) if hot_topic else False
                connection.execute(
                    "UPDATE hottopic_accounts SET status=%s,next_due_at=%s,hot_topic_enabled=%s,market_sentiment_enabled=%s,"
                    "project_promotion_enabled=%s,investment_leads_enabled=%s,investment_leads_enabled_at=%s,updated_at=%s "
                    "WHERE screen_name_lower=%s",
                    (status, _utc_datetime(now) if hot_topic else None, hot_topic, market, project, investment,
                     current["investment_leads_enabled_at"] if investment else None, _utc_datetime(now), lower),
                )
                if not hot_topic:
                    connection.execute(
                        "UPDATE hottopic_inbox SET processed_at=%s WHERE screen_name_lower=%s AND processed_at IS NULL",
                        (_utc_datetime(now), lower),
                    )
                    self._cancel_ineligible_investment_jobs(connection, lower, now)
                    connection.execute(
                        "UPDATE x_agent_analysis_jobs SET status='ignored',next_attempt_at=NULL,last_error=NULL,"
                        "completed_at=%s,updated_at=%s WHERE status IN ('pending','failed') AND tweet_id IN "
                        "(SELECT tweet_id FROM hottopic_inbox WHERE screen_name_lower=%s)",
                        (_utc_datetime(now), _utc_datetime(now), lower),
                    )
                row = connection.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=%s", (lower,)).fetchone()
                return dict(row) if row else None

    def set_x_agent_subscriptions(self, handles: Iterable[str], patch: dict[str, Any], *, now: str) -> list[dict[str, Any]]:
        allowed = {"hot_topic_enabled", "market_sentiment_enabled", "project_promotion_enabled", "investment_leads_enabled"}
        changes = {key: value for key, value in patch.items() if key in allowed}
        if not changes or any(not isinstance(value, bool) for value in changes.values()):
            raise ValueError("至少提供一个布尔订阅开关")
        lowered = list(dict.fromkeys(_normalize_handle(str(handle)).lower() for handle in handles))
        if not lowered or len(lowered) > 500:
            raise ValueError("至少选择一个账号，单次最多更新 500 个账号")
        now_value = _utc_datetime(now)
        updated: list[dict[str, Any]] = []
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                rows = connection.execute(
                    "SELECT * FROM hottopic_accounts WHERE screen_name_lower=ANY(%s) FOR UPDATE", (lowered,),
                ).fetchall()
                by_lower = {str(row["screen_name_lower"]): dict(row) for row in rows}
                missing = next((handle for handle in lowered if handle not in by_lower), None)
                if missing:
                    raise ValueError(f"账号不存在：@{missing}")
                for lower in lowered:
                    row = by_lower[lower]
                    values = {key: bool(row[key]) for key in allowed}
                    values.update(changes)
                    any_enabled = any(values.values())
                    investment_at = (row["investment_leads_enabled_at"] or now_value) if values["investment_leads_enabled"] else None
                    connection.execute(
                        "UPDATE hottopic_accounts SET hot_topic_enabled=%s,market_sentiment_enabled=%s,"
                        "project_promotion_enabled=%s,investment_leads_enabled=%s,investment_leads_enabled_at=%s,"
                        "status=%s,next_due_at=%s,updated_at=%s WHERE screen_name_lower=%s",
                        (values["hot_topic_enabled"], values["market_sentiment_enabled"],
                         values["project_promotion_enabled"], values["investment_leads_enabled"], investment_at,
                         "followed" if any_enabled else "unfollowed", now_value if any_enabled else None, now_value, lower),
                    )
                    if row["hot_topic_enabled"] and not values["hot_topic_enabled"]:
                        connection.execute(
                            "UPDATE hottopic_inbox SET processed_at=%s WHERE screen_name_lower=%s AND processed_at IS NULL",
                            (now_value, lower),
                        )
                    for module, column in (("market_sentiment", "market_sentiment_enabled"),
                                           ("project_promotion", "project_promotion_enabled")):
                        if row[column] and not values[column]:
                            connection.execute(
                                "UPDATE x_agent_analysis_jobs SET status='ignored',next_attempt_at=NULL,last_error=NULL,"
                                "completed_at=%s,updated_at=%s WHERE module=%s AND status IN ('pending','failed') AND tweet_id IN "
                                "(SELECT tweet_id FROM hottopic_inbox WHERE screen_name_lower=%s)",
                                (now_value, now_value, module, lower),
                            )
                    if row["investment_leads_enabled"] and not values["investment_leads_enabled"]:
                        self._cancel_ineligible_investment_jobs(connection, lower, now)
                    connection.execute(
                        "UPDATE hottopic_accounts SET last_x_agent_error=NULL WHERE screen_name_lower=%s", (lower,),
                    )
                    result = connection.execute("SELECT * FROM hottopic_accounts WHERE screen_name_lower=%s", (lower,)).fetchone()
                    updated.append(dict(result))
        return updated

    def retry_failed_x_agent_jobs(self, *, module: str = "", limit: int = 100, now: datetime | str) -> int:
        if module and module not in {"market_sentiment", "project_promotion"}:
            raise ValueError("不支持的 X Agent 模块")
        safe_limit = min(500, max(1, int(limit)))
        now_value = _utc_datetime(now)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                rows = connection.execute(
                    "SELECT id FROM x_agent_analysis_jobs WHERE status='failed' "
                    "AND (%s='' OR module=%s) ORDER BY updated_at,id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (module, module, safe_limit),
                ).fetchall()
                ids = [row["id"] for row in rows]
                if not ids:
                    return 0
                connection.execute(
                    "UPDATE x_agent_analysis_jobs SET status='pending',attempts=0,next_attempt_at=NULL,last_error=NULL,"
                    "completed_at=NULL,updated_at=%s WHERE id=ANY(%s)",
                    (now_value, ids),
                )
                return len(ids)

    def backfill_x_agent_inbox(self, *, module: str, since: datetime, limit: int, apply: bool = False) -> dict[str, int]:
        if module not in {"market_sentiment", "project_promotion"}:
            raise ValueError("不支持的 X Agent 模块")
        flag = "market_sentiment_enabled" if module == "market_sentiment" else "project_promotion_enabled"
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                f"SELECT i.tweet_id,i.payload_json FROM hottopic_inbox i JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower WHERE a.status='followed' AND a.{flag} AND i.collected_at>=%s AND NOT EXISTS(SELECT 1 FROM x_agent_analysis_jobs j WHERE j.tweet_id=i.tweet_id AND j.module=%s) ORDER BY i.collected_at,i.tweet_id LIMIT %s",
                (_utc_datetime(since), module, max(1, min(5000, int(limit) * 10))),
            ).fetchall()
        from packages.x_agent import is_relevant
        relevant = []
        for row in rows:
            source = row["payload_json"] if isinstance(row["payload_json"], dict) else json.loads(row["payload_json"])
            text = str(source.get("text") or source.get("expanded_text") or "")
            if is_relevant(module, text):
                relevant.append(str(row["tweet_id"]))
        enqueued = 0
        if apply and relevant:
            now = datetime.now(UTC)
            with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
                with connection.transaction():
                    for tweet_id in relevant[:limit]:
                        cur = connection.execute("INSERT INTO x_agent_analysis_jobs(tweet_id,module,status,created_at,updated_at) VALUES(%s,%s,'pending',%s,%s) ON CONFLICT(tweet_id,module) DO NOTHING", (tweet_id,module,now,now))
                        enqueued += cur.rowcount
        return {"considered":len(rows),"relevant":len(relevant),"enqueued":enqueued}

    def x_agent_dashboard_counts(self) -> dict[str, int]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            accounts = connection.execute(
                "SELECT COUNT(*) AS total,COUNT(*) FILTER (WHERE hot_topic_enabled) AS hot_topic_enabled,"
                "COUNT(*) FILTER (WHERE market_sentiment_enabled) AS market_sentiment_enabled,"
                "COUNT(*) FILTER (WHERE project_promotion_enabled) AS project_promotion_enabled,"
                "COUNT(*) FILTER (WHERE (last_error IS NOT NULL AND last_error NOT LIKE '%%HTTP Error 404%%' "
                "AND last_error NOT LIKE '%%\"kind\": \"not_found\"%%') OR last_x_agent_error IS NOT NULL) AS errors "
                "FROM hottopic_accounts"
            ).fetchone()
            jobs = connection.execute(
                "SELECT COUNT(*) FILTER (WHERE status IN ('pending','processing')) AS pending,"
                "COUNT(*) FILTER (WHERE status='failed') AS failed FROM x_agent_analysis_jobs"
            ).fetchone()
            return {key: int(value or 0) for key, value in {**accounts, **jobs}.items()}

    def list_market_sentiment(
        self, *, cutoff: datetime | str, query: str = "", sentiment: str = "all", offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = [_utc_datetime(cutoff)]
        if query.strip():
            clauses.append("LOWER(instrument_name || ' ' || ticker || ' ' || scope) LIKE %s")
            params.append(f"%{query.strip().lower()}%")
        if sentiment != "all":
            clauses.append("snapshot=%s")
            params.append(sentiment)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_offset, safe_limit = max(0, int(offset)), min(100, max(1, int(limit)))
        cte = """
          WITH filtered AS (
            SELECT id,instrument_key,instrument_name,ticker,scope,sentiment,reason,account_lower,posted_at,
              CASE sentiment WHEN '极度恐慌' THEN -2 WHEN '偏空/谨慎' THEN -1 WHEN '中性/分歧' THEN 0
                WHEN '偏多/乐观' THEN 1 WHEN '极度狂热' THEN 2 END AS score
            FROM x_agent_sentiment_results WHERE posted_at>=%s AND sentiment<>'证据不足'
          ), grouped AS (
            SELECT instrument_key,COUNT(*) AS source_count,COUNT(DISTINCT account_lower) AS account_count,
              MAX(posted_at) AS latest_at,AVG(score) AS average_score FROM filtered GROUP BY instrument_key
          ), snapshots AS (
            SELECT *,CASE WHEN average_score>=1.5 THEN '极度狂热' WHEN average_score>0.25 THEN '偏多/乐观'
              WHEN average_score<=-1.5 THEN '极度恐慌' WHEN average_score<-0.25 THEN '偏空/谨慎'
              ELSE '中性/分歧' END AS snapshot,CAST(ROUND(average_score*50) AS integer) AS score FROM grouped
          ), latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY instrument_key ORDER BY posted_at DESC,id DESC) AS row_number FROM filtered
          ), result_rows AS (
            SELECT l.instrument_key,l.instrument_name,l.ticker,l.scope,s.snapshot,s.score,s.source_count,s.account_count,s.latest_at,
              CASE WHEN l.sentiment=s.snapshot THEN l.reason ELSE '来源态度综合为'||s.snapshot||'，展开查看各来源。' END AS reason
            FROM snapshots s JOIN latest l ON l.instrument_key=s.instrument_key AND l.row_number=1
          )
        """
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()["count"])
            rows = connection.execute(
                f"{cte} SELECT instrument_key,instrument_name,ticker,scope,snapshot,score,source_count,account_count,latest_at,reason "
                f"FROM result_rows{where} ORDER BY account_count DESC,latest_at DESC LIMIT %s OFFSET %s",
                [*params, safe_limit, safe_offset],
            ).fetchall()
        return {"items": [{
            "instrumentKey": row["instrument_key"], "instrumentName": row["instrument_name"], "ticker": row["ticker"],
            "scope": row["scope"], "sentiment": row["snapshot"], "score": int(row["score"]),
            "mentionCount": int(row["account_count"]), "latestAt": row["latest_at"].isoformat(), "reason": row["reason"],
        } for row in rows], "total": total}

    def market_sentiment_history(self, instrument_key: str, *, limit: int = 120) -> dict[str, Any]:
        if not instrument_key.strip():
            raise ValueError("instrument_key 不能为空")
        safe_limit = min(240, max(1, int(limit)))
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT instrument_key,snapshot_at,instrument_name,ticker,sentiment,score,mention_count,source_count "
                "FROM x_agent_sentiment_snapshots WHERE instrument_key=%s ORDER BY snapshot_at DESC LIMIT %s",
                (instrument_key, safe_limit),
            ).fetchall()
        items = list(reversed(rows))
        return {
            "instrumentKey": instrument_key,
            "instrumentName": str(items[-1]["instrument_name"]) if items else "",
            "ticker": str(items[-1]["ticker"]) if items else "",
            "items": [{"snapshotAt": row["snapshot_at"].isoformat(), "sentiment": row["sentiment"],
                       "score": int(row["score"]), "mentionCount": int(row["mention_count"]),
                       "sourceCount": int(row["source_count"])} for row in items],
        }

    def market_sentiment_detail(
        self, instrument_key: str, *, cutoff: datetime | str, offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        if not instrument_key.strip():
            raise ValueError("instrument_key 不能为空")
        safe_offset, safe_limit = max(0, int(offset)), min(100, max(1, int(limit)))
        cutoff_value = _utc_datetime(cutoff)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(
                "SELECT COUNT(*) AS count FROM x_agent_sentiment_results WHERE instrument_key=%s AND posted_at>=%s",
                (instrument_key, cutoff_value),
            ).fetchone()["count"])
            rows = connection.execute(
                "SELECT tweet_id,account_lower,source_url,source_text,posted_at,sentiment,reason,actual_model,fallback_reason "
                "FROM x_agent_sentiment_results WHERE instrument_key=%s AND posted_at>=%s "
                "ORDER BY posted_at DESC,id DESC LIMIT %s OFFSET %s",
                (instrument_key, cutoff_value, safe_limit, safe_offset),
            ).fetchall()
        return {"items": [{"tweetId": row["tweet_id"], "account": row["account_lower"],
                            "sourceUrl": row["source_url"], "sourceText": row["source_text"],
                            "postedAt": row["posted_at"].isoformat(), "sentiment": row["sentiment"],
                            "reason": row["reason"], "actualModel": row["actual_model"],
                            "fallbackReason": row["fallback_reason"]} for row in rows], "total": total}

    def process_account_cleanup_jobs(self, *, job_limit: int = 1) -> dict[str, int]:
        from .service import ACCOUNT_CLEANUP_BATCH_SIZE

        result = {"processed_jobs": 0, "deleted_content_items": 0, "deleted_claims": 0}
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                jobs = connection.execute(
                    "SELECT screen_name_lower FROM hottopic_account_cleanup ORDER BY queued_at "
                    "LIMIT %s FOR UPDATE SKIP LOCKED", (max(1, int(job_limit)),),
                ).fetchall()
                for job in jobs:
                    lower = str(job["screen_name_lower"])
                    try:
                        with connection.transaction():
                            ids = [row["content_item_id"] for row in connection.execute(
                                "SELECT ci.content_item_id FROM content_items ci WHERE lower(ci.activity_account)=%s "
                                "AND NOT EXISTS (SELECT 1 FROM claims cl JOIN memberships m ON m.claim_id=cl.claim_id "
                                "JOIN topics t ON t.topic_id=m.topic_id WHERE cl.content_item_id=ci.content_item_id "
                                "AND m.superseded_by IS NULL AND t.retention_tier='permanent') "
                                "AND NOT EXISTS (SELECT 1 FROM claims cl JOIN topic_evidence te ON te.claim_id=cl.claim_id "
                                "JOIN topics t ON t.topic_id=te.topic_id WHERE cl.content_item_id=ci.content_item_id "
                                "AND t.retention_tier='permanent') ORDER BY ci.content_item_id LIMIT %s FOR UPDATE OF ci",
                                (lower, ACCOUNT_CLEANUP_BATCH_SIZE),
                            ).fetchall()]
                            if not ids:
                                connection.execute("DELETE FROM hottopic_account_cleanup WHERE screen_name_lower=%s", (lower,))
                                result["processed_jobs"] += 1
                                continue
                            claims = connection.execute(
                                "SELECT claim_id FROM claims WHERE content_item_id=ANY(%s) FOR UPDATE", (ids,),
                            ).fetchall()
                            claim_ids = [row["claim_id"] for row in claims]
                            if claim_ids:
                                connection.execute(
                                    "DELETE FROM topic_evidence WHERE claim_id=ANY(%s) AND topic_id IN "
                                    "(SELECT topic_id FROM topics WHERE retention_tier!='permanent')", (claim_ids,),
                                )
                                connection.execute("DELETE FROM memberships WHERE claim_id=ANY(%s)", (claim_ids,))
                                connection.execute("DELETE FROM decision_audit WHERE claim_id=ANY(%s)", (claim_ids,))
                                connection.execute("DELETE FROM claims WHERE claim_id=ANY(%s)", (claim_ids,))
                            connection.execute("DELETE FROM content_items WHERE content_item_id=ANY(%s)", (ids,))
                            connection.execute(
                                "DELETE FROM topic_participations WHERE lower(activity_account)=%s AND topic_id IN "
                                "(SELECT topic_id FROM topics WHERE retention_tier!='permanent')", (lower,),
                            )
                            if len(ids) < ACCOUNT_CLEANUP_BATCH_SIZE:
                                connection.execute("DELETE FROM hottopic_account_cleanup WHERE screen_name_lower=%s", (lower,))
                            result["processed_jobs"] += 1
                            result["deleted_content_items"] += len(ids)
                            result["deleted_claims"] += len(claim_ids)
                    except Exception as exc:
                        connection.execute(
                            "UPDATE hottopic_account_cleanup SET last_error=%s WHERE screen_name_lower=%s",
                            (f"{type(exc).__name__}: {exc}", lower),
                        )
        return result

    def project_promotion_detail(
        self, identity_key: str, *, window: str = "24h", cutoff: datetime | str,
        offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        if not identity_key.strip():
            raise ValueError("identity_key 不能为空")
        cutoff_value = _utc_datetime(cutoff)
        safe_offset, safe_limit = max(0, int(offset)), min(100, max(1, int(limit)))
        cte = """
          WITH named AS (
            SELECT p.*,CASE WHEN trim(project_name)='' THEN 'tweet:'||source_tweet_id ELSE lower(trim(project_name)) END AS project_name_key,
              CASE WHEN lower(trim(contract_address)) LIKE '0x%%' THEN NULLIF(lower(trim(contract_address)),'')
                ELSE NULLIF(trim(contract_address),'') END AS normalized_contract_address
            FROM x_agent_project_observations p
          ), project_ca_counts AS (
            SELECT project_name_key,COUNT(DISTINCT normalized_contract_address) AS ca_count,
              MIN(normalized_contract_address) AS only_contract_address FROM named GROUP BY project_name_key
          ), identified AS (
            SELECT n.*,CASE WHEN n.normalized_contract_address IS NOT NULL THEN 'ca:'||n.normalized_contract_address
              WHEN c.ca_count=1 THEN 'ca:'||c.only_contract_address
              WHEN c.ca_count>1 THEN 'name:'||n.project_name_key||':ca:unresolved'
              ELSE 'name:'||n.project_name_key END AS merged_identity_key
            FROM named n JOIN project_ca_counts c ON c.project_name_key=n.project_name_key
          ), selected_groups AS (
            SELECT merged_identity_key FROM identified WHERE merged_identity_key=%s
            UNION SELECT MIN(merged_identity_key) FROM identified WHERE identity_key=%s
              GROUP BY identity_key HAVING COUNT(DISTINCT merged_identity_key)=1
            UNION SELECT DISTINCT merged_identity_key FROM identified WHERE 'tweet:'||source_tweet_id=%s
          ), filtered AS (
            SELECT i.id,i.account_lower,i.source_url,i.source_text,i.source_tweet_id,i.logic,
              i.direct_promotion_json,i.operation_advice_json,i.last_mentioned_at,i.actual_model,i.fallback_reason
            FROM identified i JOIN selected_groups g ON g.merged_identity_key=i.merged_identity_key
            WHERE i.last_mentioned_at>=%s
          ), legacy_groups AS (
            SELECT source_tweet_id,string_agg(logic,'；') AS logic FROM
              (SELECT DISTINCT source_tweet_id,logic FROM filtered WHERE direct_promotion_json IS NULL AND operation_advice_json IS NULL) x GROUP BY source_tweet_id
          ), direct_groups AS (
            SELECT source_tweet_id,string_agg(DISTINCT direct_promotion_json::text,',') AS direct_promotions
            FROM filtered WHERE direct_promotion_json IS NOT NULL GROUP BY source_tweet_id
          ), operation_groups AS (
            SELECT source_tweet_id,string_agg(DISTINCT operation_advice_json::text,',') AS operation_advices
            FROM filtered WHERE operation_advice_json IS NOT NULL GROUP BY source_tweet_id
          ), latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY source_tweet_id ORDER BY last_mentioned_at DESC,id DESC) AS row_number FROM filtered
          ), result_rows AS (
            SELECT l.id,l.account_lower,l.source_url,l.source_text,l.source_tweet_id,h.logic,d.direct_promotions,o.operation_advices,
              l.last_mentioned_at,l.actual_model,l.fallback_reason FROM latest l
            LEFT JOIN legacy_groups h ON h.source_tweet_id=l.source_tweet_id
            LEFT JOIN direct_groups d ON d.source_tweet_id=l.source_tweet_id
            LEFT JOIN operation_groups o ON o.source_tweet_id=l.source_tweet_id WHERE l.row_number=1
          )
        """
        params = (identity_key, identity_key, identity_key, cutoff_value)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(f"{cte} SELECT COUNT(*) AS n FROM result_rows", params).fetchone()["n"])
            rows = connection.execute(
                f"{cte} SELECT * FROM result_rows ORDER BY last_mentioned_at DESC,id DESC LIMIT %s OFFSET %s",
                (*params, safe_limit, safe_offset),
            ).fetchall()
        def json_array(value):
            if not value:
                return []
            try:
                parsed = json.loads(f"[{value}]")
            except (TypeError, ValueError):
                return []
            return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []
        def advice(value):
            return {"action": str(value.get("action") or ""), "route": str(value.get("route") or ""),
                    "profitSource": str(value.get("profit_source") or ""), "claimedReturn": str(value.get("claimed_return") or ""),
                    "fees": str(value.get("fees") or ""), "constraints": str(value.get("constraints") or ""),
                    "risks": str(value.get("risks") or ""), "validUntil": str(value.get("valid_until") or "")}
        return {"items": [{"id": row["id"], "account": row["account_lower"], "sourceUrl": row["source_url"],
                            "sourceText": row["source_text"], "sourceTweetId": row["source_tweet_id"],
                            "legacyLogic": row["logic"], "logic": row["logic"],
                            "directPromotions": json_array(row["direct_promotions"]),
                            "operationAdvices": [advice(item) for item in json_array(row["operation_advices"])],
                            "lastMentionedAt": row["last_mentioned_at"].isoformat(), "actualModel": row["actual_model"],
                            "fallbackReason": row["fallback_reason"]} for row in rows], "total": total}

    def list_project_promotions(
        self, *, cutoff: datetime | str, query: str = "", offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        conditions: list[str] = []
        params: list[Any] = [_utc_datetime(cutoff)]
        if query.strip():
            conditions.append(
                "LOWER(project_name || ' ' || ticker || ' ' || COALESCE(legacy_logic,'') || ' ' || "
                "COALESCE(direct_promotion_json::text,'') || ' ' || COALESCE(operation_advice_json::text,'')) LIKE %s"
            )
            params.append(f"%{query.strip().lower()}%")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        safe_offset, safe_limit = max(0, int(offset)), min(100, max(1, int(limit)))
        cte = """
          WITH named AS (
            SELECT p.*,CASE WHEN trim(project_name)='' THEN 'tweet:'||source_tweet_id ELSE lower(trim(project_name)) END AS project_name_key,
              CASE WHEN lower(trim(contract_address)) LIKE '0x%%' THEN NULLIF(lower(trim(contract_address)),'')
                ELSE NULLIF(trim(contract_address),'') END AS normalized_contract_address
            FROM x_agent_project_observations p
          ), project_ca_counts AS (
            SELECT project_name_key,COUNT(DISTINCT normalized_contract_address) AS ca_count,
              MIN(normalized_contract_address) AS only_contract_address FROM named GROUP BY project_name_key
          ), identified AS (
            SELECT n.*,CASE WHEN n.normalized_contract_address IS NOT NULL THEN 'ca:'||n.normalized_contract_address
              WHEN c.ca_count=1 THEN 'ca:'||c.only_contract_address
              WHEN c.ca_count>1 THEN 'name:'||n.project_name_key||':ca:unresolved'
              ELSE 'name:'||n.project_name_key END AS merged_identity_key
            FROM named n JOIN project_ca_counts c ON c.project_name_key=n.project_name_key
          ), filtered AS (
            SELECT id,merged_identity_key AS identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,
              direct_promotion_json,operation_advice_json,account_lower,last_mentioned_at FROM identified WHERE last_mentioned_at>=%s
          ), grouped AS (
            SELECT identity_key,COUNT(*) AS source_count,COUNT(DISTINCT account_lower) AS account_count,
              MAX(last_mentioned_at) AS last_mentioned_at,MAX(NULLIF(contract_address,'')) AS contract_address
            FROM filtered GROUP BY identity_key
          ), latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number FROM filtered
          ), direct_latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
            FROM filtered WHERE direct_promotion_json IS NOT NULL
          ), operation_latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
            FROM filtered WHERE operation_advice_json IS NOT NULL
          ), legacy_latest AS (
            SELECT *,ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY last_mentioned_at DESC,id DESC) AS row_number
            FROM filtered WHERE direct_promotion_json IS NULL AND operation_advice_json IS NULL
          ), result_rows AS (
            SELECT l.identity_key,l.project_name,l.ticker,l.chain_name,
              COALESCE(NULLIF(l.contract_address,''),g.contract_address) AS contract_address,l.official_url,
              d.direct_promotion_json,o.operation_advice_json,h.logic AS legacy_logic,g.account_count,g.source_count,g.last_mentioned_at
            FROM grouped g JOIN latest l ON l.identity_key=g.identity_key AND l.row_number=1
            LEFT JOIN direct_latest d ON d.identity_key=g.identity_key AND d.row_number=1
            LEFT JOIN operation_latest o ON o.identity_key=g.identity_key AND o.row_number=1
            LEFT JOIN legacy_latest h ON h.identity_key=g.identity_key AND h.row_number=1
          )
        """
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            total = int(connection.execute(f"{cte} SELECT COUNT(*) FROM result_rows{where}", params).fetchone()["count"])
            rows = connection.execute(
                f"{cte} SELECT identity_key,project_name,ticker,chain_name,contract_address,official_url,direct_promotion_json,"
                f"operation_advice_json,legacy_logic,last_mentioned_at FROM result_rows{where} "
                "ORDER BY last_mentioned_at DESC LIMIT %s OFFSET %s",
                [*params, safe_limit, safe_offset],
            ).fetchall()
        return {"items": [{
            "identityKey": row["identity_key"], "projectName": row["project_name"], "ticker": row["ticker"],
            "chainName": row["chain_name"], "contractAddress": row["contract_address"], "officialUrl": row["official_url"],
            "directPromotion": row["direct_promotion_json"], "operationAdvice": row["operation_advice_json"],
            "legacyLogic": row["legacy_logic"], "logic": row["legacy_logic"],
            "lastMentionedAt": row["last_mentioned_at"].isoformat(),
        } for row in rows], "total": total}

    @staticmethod
    def _cancel_ineligible_investment_jobs(connection: Any, lower: str, now: str) -> None:
        connection.execute(
            "UPDATE investment_lead_tasks t SET status='ignored',next_attempt_at=NULL,lease_until=NULL,last_error=NULL,"
            "completed_at=%s,updated_at=%s WHERE status IN ('pending','failed','budget_paused') AND EXISTS("
            "SELECT 1 FROM investment_lead_observations target WHERE target.material_id=t.material_id "
            "AND target.content_hash=t.content_hash AND target.screen_name_lower=%s) AND NOT EXISTS("
            "SELECT 1 FROM investment_lead_observations other JOIN hottopic_accounts a "
            "ON a.screen_name_lower=other.screen_name_lower WHERE other.material_id=t.material_id "
            "AND other.content_hash=t.content_hash AND a.status='followed' AND a.investment_leads_enabled)",
            (now, now, lower),
        )
        connection.execute(
            "UPDATE investment_lead_identity_searches s SET status='ignored',next_attempt_at=NULL,lease_until=NULL,last_error=NULL,"
            "completed_at=%s,updated_at=%s WHERE status IN ('pending','failed','budget_paused') AND EXISTS("
            "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations target "
            "ON target.material_id=ls.material_id AND target.content_hash=ls.content_hash "
            "WHERE ls.lead_id=s.lead_id AND target.screen_name_lower=%s) AND NOT EXISTS("
            "SELECT 1 FROM investment_lead_sources ls JOIN investment_lead_observations other "
            "ON other.material_id=ls.material_id AND other.content_hash=ls.content_hash JOIN hottopic_accounts a "
            "ON a.screen_name_lower=other.screen_name_lower WHERE ls.lead_id=s.lead_id "
            "AND a.status='followed' AND a.investment_leads_enabled)",
            (now, now, lower),
        )

    def due_accounts(self, *, at: datetime | str, limit: int | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT * FROM hottopic_accounts WHERE status='followed' AND "
            "(hot_topic_enabled OR market_sentiment_enabled OR project_promotion_enabled OR investment_leads_enabled) "
            "AND (next_due_at IS NULL OR next_due_at<=%s) ORDER BY next_due_at NULLS FIRST,screen_name_lower"
        )
        params: tuple[Any, ...] = (_utc_datetime(at),)
        if limit is not None:
            sql += " LIMIT %s"
            params += (max(1, limit),)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def pending_inbox(self, *, limit: int) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT i.tweet_id,i.payload_json FROM hottopic_inbox i JOIN hottopic_accounts a "
                "ON a.screen_name_lower=i.screen_name_lower WHERE i.processed_at IS NULL AND a.hot_topic_enabled=true "
                "ORDER BY i.collected_at LIMIT %s", (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def mark_inbox_processed(self, tweet_ids: Iterable[str], *, at: datetime | str) -> None:
        ids = list(tweet_ids)
        if not ids:
            return
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE hottopic_inbox SET processed_at=%s WHERE tweet_id=ANY(%s)",
                    (_utc_datetime(at), ids),
                )

    def refresh_hot_topic_counts(self) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "WITH participation_counts AS MATERIALIZED ("
                    "SELECT lower(p.activity_account) AS account_lower,COUNT(DISTINCT p.topic_id) AS topic_count "
                    "FROM topic_participations p JOIN topics t ON t.topic_id=p.topic_id "
                    "WHERE t.retention_tier='permanent' GROUP BY lower(p.activity_account)), "
                    "desired_counts AS MATERIALIZED (SELECT a.screen_name_lower,COALESCE(pc.topic_count,0) AS topic_count "
                    "FROM hottopic_accounts a LEFT JOIN participation_counts pc ON pc.account_lower=a.screen_name_lower) "
                    "UPDATE hottopic_accounts SET cumulative_hot_topic_count=desired_counts.topic_count "
                    "FROM desired_counts WHERE desired_counts.screen_name_lower=hottopic_accounts.screen_name_lower "
                    "AND cumulative_hot_topic_count!=desired_counts.topic_count"
                )

    def yesterday_run(self, local_date: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT local_date,topic_id,status,title,error,candidate_count,excluded_analysis_count,"
                "excluded_no_action_count,attempted_at,completed_at FROM yesterday_hot_discussion_runs WHERE local_date=%s",
                (local_date,),
            ).fetchone()
            return dict(row) if row else None

    def topic_dashboard_rows(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT t.topic_id,t.working_title,t.started_at,t.first_seen_at,t.last_evidence_at,t.last_participation_at,"
                "t.participant_count_1h,t.participant_count_6h,t.participant_count_24h,t.participant_velocity,t.hotness_score,"
                "b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
                "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                "WHERE t.matching_status='active' AND t.visibility='visible' "
                "ORDER BY t.hotness_score DESC,t.last_evidence_at DESC LIMIT %s", (max(1, int(limit)),),
            ).fetchall()
            return [dict(row) for row in rows]

    def topic_detail_rows(self, topic_id: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute(
                "SELECT t.*,b.title,b.brief,b.generated_at FROM topics t LEFT JOIN brief_revisions b ON b.topic_id=t.topic_id "
                "AND b.revision=(SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                "WHERE t.topic_id=%s AND t.matching_status='active' AND t.visibility='visible'", (topic_id,),
            ).fetchone()
            if row is None:
                return None, []
            speakers = connection.execute(
                "SELECT p.activity_account,p.last_participation_at,ci.source_url FROM topic_participations p "
                "LEFT JOIN content_items ci ON ci.content_item_id=p.last_content_item_id WHERE p.topic_id=%s "
                "ORDER BY p.last_participation_at DESC", (topic_id,),
            ).fetchall()
            return dict(row), [dict(item) for item in speakers]

    def topic_health_counts(self) -> dict[str, Any]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            accounts = connection.execute(
                "SELECT COUNT(*) AS total,COUNT(*) FILTER (WHERE status='followed') AS followed,"
                "COUNT(*) FILTER (WHERE status='unfollowed') AS unfollowed,COUNT(*) FILTER (WHERE status='blacklisted') AS blacklisted,"
                "COUNT(*) FILTER (WHERE last_error IS NOT NULL AND last_error NOT LIKE '%%HTTP Error 404%%' "
                "AND last_error NOT LIKE '%%\"kind\": \"not_found\"%%') AS errors FROM hottopic_accounts"
            ).fetchone()
            topics = connection.execute(
                "SELECT matching_status,COUNT(*) AS count FROM topics GROUP BY matching_status"
            ).fetchall()
            pending = int(connection.execute(
                "SELECT COUNT(*) AS count FROM hottopic_inbox WHERE processed_at IS NULL"
            ).fetchone()["count"])
            now_row = connection.execute(
                "SELECT clock_timestamp() AS now,MIN(next_due_at) FILTER (WHERE status='followed' AND hot_topic_enabled) AS oldest_due,"
                "MIN(last_success_at) FILTER (WHERE status='followed' AND hot_topic_enabled) AS oldest_success "
                "FROM hottopic_accounts"
            ).fetchone()
            first_capture = connection.execute(
                "SELECT MIN((payload_json->>'created_at')::timestamptz) AS source_post_at,MIN(collected_at) AS captured_at "
                "FROM hottopic_inbox WHERE payload_json ? 'created_at'"
            ).fetchone()
            now = now_row["now"]
            timing = {
                "sampleCount": 0,
                "firstCaptureLagSeconds": max(0.0, (first_capture["captured_at"]-first_capture["source_post_at"]).total_seconds()) if first_capture["captured_at"] and first_capture["source_post_at"] else None,
                "oldestDueAccountLagSeconds": max(0.0, (now-now_row["oldest_due"]).total_seconds()) if now_row["oldest_due"] else None,
                "oldestSuccessfulPollAgeSeconds": max(0.0, (now-now_row["oldest_success"]).total_seconds()) if now_row["oldest_success"] else None,
                "timestampUncertainty": "source post timestamps and collector clocks are provider/client supplied; lag is approximate",
                "exclusions": "accounts without a successful poll and inbox rows without parseable source timestamps are omitted",
            }
            return {"accounts": dict(accounts), "topics": {row["matching_status"]: int(row["count"]) for row in topics},
                    "inboxPending": pending, "collectorTiming": timing}

    def prune_transient_state(self, *, at: datetime, retention_hours: int = 48, batch_size: int = 100) -> dict[str, Any]:
        """Delete one bounded batch of archived transient aggregation state."""
        if retention_hours < 24 or batch_size < 1:
            raise ValueError("invalid transient retention or batch size")
        cutoff = at - timedelta(hours=retention_hours)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                topics = [row["topic_id"] for row in connection.execute(
                    "SELECT topic_id FROM topics WHERE retention_tier='transient' AND matching_status='archived' "
                    "AND COALESCE(last_evidence_at,started_at)<%s ORDER BY topic_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (cutoff, batch_size),
                ).fetchall()]
                contents = [row["content_item_id"] for row in connection.execute(
                    "SELECT ci.content_item_id FROM content_items ci WHERE ci.created_at<%s "
                    "AND NOT EXISTS (SELECT 1 FROM claims cl JOIN memberships m ON m.claim_id=cl.claim_id JOIN topics t ON t.topic_id=m.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id AND m.superseded_by IS NULL AND t.retention_tier='permanent') "
                    "AND NOT EXISTS (SELECT 1 FROM claims cl JOIN topic_evidence te ON te.claim_id=cl.claim_id JOIN topics t ON t.topic_id=te.topic_id "
                    "WHERE cl.content_item_id=ci.content_item_id AND t.retention_tier='permanent') ORDER BY ci.content_item_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (cutoff, batch_size),
                ).fetchall()]
                claim_ids = [row["claim_id"] for row in connection.execute(
                    "SELECT claim_id FROM claims WHERE content_item_id=ANY(%s)", (contents,),
                ).fetchall()] if contents else []
                thread_ids = [row["thread_id"] for row in connection.execute(
                    "SELECT thread_id FROM threads WHERE topic_id=ANY(%s)", (topics,),
                ).fetchall()] if topics else []
                audit_ids = [row["decision_id"] for row in connection.execute(
                    "SELECT decision_id FROM decision_audit d WHERE created_at<%s AND "
                    "(claim_id IS NULL OR NOT EXISTS (SELECT 1 FROM memberships m JOIN topics t ON t.topic_id=m.topic_id "
                    "WHERE m.claim_id=d.claim_id AND m.superseded_by IS NULL AND t.retention_tier='permanent')) "
                    "ORDER BY decision_id LIMIT %s FOR UPDATE SKIP LOCKED", (cutoff, batch_size * 5),
                ).fetchall()]
                batch_ids = [row["batch_id"] for row in connection.execute(
                    "SELECT batch_id FROM batches WHERE batch_time<%s ORDER BY batch_id LIMIT %s FOR UPDATE SKIP LOCKED",
                    (cutoff.isoformat(), batch_size * 5),
                ).fetchall()]
                if topics:
                    for table, column in (("topic_pair_reviews", "left_topic_id"), ("topic_pair_reviews", "right_topic_id"),
                                          ("topic_merges", "source_topic_id"), ("topic_merges", "target_topic_id"),
                                          ("retrieval_state", "scope_id"), ("topic_evidence", "topic_id"),
                                          ("topic_claim_reviews", "topic_id"), ("topic_participations", "topic_id"),
                                          ("memberships", "topic_id"), ("threads", "topic_id"), ("topics", "topic_id")):
                        if table == "retrieval_state":
                            connection.execute("DELETE FROM retrieval_state WHERE scope_type='topic' AND scope_id=ANY(%s)", (topics,))
                        elif table in {"topic_pair_reviews", "topic_merges"}:
                            other = "right_topic_id" if column == "left_topic_id" else "left_topic_id" if column == "right_topic_id" else None
                            if other:
                                connection.execute(f"DELETE FROM {table} WHERE {column}=ANY(%s) OR {other}=ANY(%s)", (topics, topics))
                            else:
                                connection.execute(f"DELETE FROM {table} WHERE {column}=ANY(%s)", (topics,))
                        else:
                            connection.execute(f"DELETE FROM {table} WHERE {column}=ANY(%s)", (topics,))
                    if thread_ids:
                        connection.execute("DELETE FROM retrieval_state WHERE scope_type='thread' AND scope_id=ANY(%s)", (thread_ids,))
                if claim_ids:
                    connection.execute("DELETE FROM decision_audit WHERE claim_id=ANY(%s)", (claim_ids,))
                    connection.execute("DELETE FROM topic_evidence WHERE claim_id=ANY(%s)", (claim_ids,))
                    connection.execute("DELETE FROM topic_claim_reviews WHERE claim_id=ANY(%s)", (claim_ids,))
                    connection.execute("DELETE FROM memberships WHERE claim_id=ANY(%s)", (claim_ids,))
                    connection.execute("DELETE FROM claims WHERE claim_id=ANY(%s)", (claim_ids,))
                if contents:
                    connection.execute("DELETE FROM content_items WHERE content_item_id=ANY(%s)", (contents,))
                if audit_ids:
                    connection.execute("DELETE FROM decision_audit WHERE decision_id=ANY(%s)", (audit_ids,))
                if batch_ids:
                    connection.execute("DELETE FROM batches WHERE batch_id=ANY(%s)", (batch_ids,))
                more = (len(topics) == batch_size or len(contents) == batch_size
                        or len(audit_ids) == batch_size * 5 or len(batch_ids) == batch_size * 5)
            return {"deleted_topics": len(topics), "deleted_content_items": len(contents), "deleted_claims": len(claim_ids), "has_more": more}

    def load_topic_retrieval_cache(self) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT t.topic_id,t.matching_status,t.event_or_issue,t.last_evidence_at,t.started_at,"
                "r.tokens_json,r.hard_keys_json,r.entities_json FROM topics t JOIN retrieval_state r "
                "ON r.scope_type='topic' AND r.scope_id=t.topic_id ORDER BY t.topic_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def candidate_topic_profiles(self, topic_ids: Iterable[str]) -> list[dict[str, Any]]:
        with self.aggregation_transaction() as unit:
            return unit.candidate_topic_profiles(topic_ids)

    def topic_retrieval_source_rows(self, topic_id: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT cl.claim_id,cl.claim_text,cl.entities_json,cl.content_item_id,ci.tweet_id,ci.created_at,ci.activity_account,ci.references_json "
                "FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id JOIN content_items ci ON ci.content_item_id=cl.content_item_id "
                "LEFT JOIN topic_claim_reviews r ON r.claim_id=m.claim_id AND r.topic_id=m.topic_id "
                "WHERE m.topic_id=%s AND m.superseded_by IS NULL AND COALESCE(r.decision,'support')='support' ORDER BY ci.created_at DESC",
                (topic_id,),
            ).fetchall()
            topic = connection.execute(
                "SELECT topic_id,matching_status,event_or_issue,last_evidence_at,started_at FROM topics WHERE topic_id=%s", (topic_id,),
            ).fetchone()
            return [dict(row) for row in rows], dict(topic) if topic else None

    def save_topic_retrieval_state(
        self, *, topic_id: str, tokens: list[str], hard_keys: list[str], entities: list[str],
        representative_claim_ids: list[str], recent_claim_ids: list[str], updated_at: str,
    ) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO retrieval_state(scope_type,scope_id,tokens_json,hard_keys_json,entities_json,representative_claim_ids_json,recent_claim_ids_json,updated_at) "
                    "VALUES('topic',%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(scope_type,scope_id) DO UPDATE SET "
                    "tokens_json=EXCLUDED.tokens_json,hard_keys_json=EXCLUDED.hard_keys_json,entities_json=EXCLUDED.entities_json, "
                    "representative_claim_ids_json=EXCLUDED.representative_claim_ids_json,recent_claim_ids_json=EXCLUDED.recent_claim_ids_json,updated_at=EXCLUDED.updated_at",
                    (topic_id,Jsonb(tokens),Jsonb(hard_keys),Jsonb(entities),Jsonb(representative_claim_ids),Jsonb(recent_claim_ids),_utc_datetime(updated_at)),
                )

    def due_topic_ids(self, *, at: datetime, limit: int = 200) -> set[str]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT topic_id FROM topics WHERE matching_status IN ('seed','active') "
                "AND (lifecycle_refresh_at IS NULL OR lifecycle_refresh_at<=%s) "
                "ORDER BY lifecycle_refresh_at NULLS FIRST,topic_id LIMIT %s",
                (at, max(1, min(1000, int(limit)))),
            ).fetchall()
            return {str(row["topic_id"]) for row in rows}

    def topic_counts(self) -> dict[str, int]:
        tables = ("batches", "content_items", "claims", "topics", "threads", "memberships", "brief_revisions", "topic_evidence", "decision_audit")
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return {table: int(connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]) for table in tables}

    def batch_result(self, batch_id: str) -> dict[str, Any] | None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            row = connection.execute("SELECT result_json FROM batches WHERE batch_id=%s", (batch_id,)).fetchone()
            return dict(row["result_json"]) if row else None

    def save_batch_result(self, *, batch_id: str, batch_time: str, input_count: int, committed_at: str,
                          result: dict[str, Any]) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO batches(batch_id,batch_time,input_count,committed_at,result_json) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(batch_id) DO NOTHING",
                    (batch_id,batch_time,input_count,_utc_datetime(committed_at),Jsonb(result)),
                )

    def insert_decision_audit(self, *, decision_id: str, batch_id: str, decision: dict[str, Any], created_at: str) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO decision_audit(decision_id,batch_id,content_item_id,claim_id,action,topic_ids_json,candidates_json,reason,decision_source,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(decision_id) DO NOTHING",
                    (decision_id,batch_id,decision.get("content_item_id"),decision.get("claim_id"),decision.get("action", "unknown"),
                     Jsonb(decision.get("topic_ids", [])),Jsonb(decision.get("candidates", [])),decision.get("reason", ""),
                     decision.get("decision_source", "system"),_utc_datetime(created_at)),
                )

    def content_exists(self, tweet_id: str) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            return connection.execute("SELECT 1 FROM content_items WHERE tweet_id=%s", (tweet_id,)).fetchone() is not None

    def insert_content_item(self, *, item: Any, fingerprint: str) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row = connection.execute(
                    "INSERT INTO content_items(content_item_id,tweet_id,activity_account,author,activity_type,content_text,expanded_text,created_at,source_url,metrics_json,references_json,raw_payload_json,fingerprint) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tweet_id) DO NOTHING RETURNING content_item_id",
                    (item.content_item_id,item.tweet_id,item.activity_account,item.author,item.activity_type,item.content_text,
                     item.expanded_text,_utc_datetime(item.created_at),item.source_url,Jsonb(item.metrics),Jsonb(item.references),Jsonb(item.raw_payload),fingerprint),
                ).fetchone()
                return row is not None

    def insert_topic_claim(self, *, claim: Any, created_at: str) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO claims(claim_id,content_item_id,claim_text,claim_kind,entities_json,action_or_issue,stance,evidence_span,confidence,information_value,created_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(claim_id) DO NOTHING",
                    (claim.claim_id,claim.content_item_id,claim.claim_text,claim.claim_kind,Jsonb(list(claim.entities)),
                     claim.action_or_issue,claim.stance,claim.evidence_span,claim.confidence,claim.information_value,_utc_datetime(created_at)),
                )

    def create_topic_seed(self, *, topic_id: str, claim: Any, item: Any) -> None:
        subject = " ".join(str(claim.claim_text or "").split()).strip()[:120]
        created = _utc_datetime(item.created_at)
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO topics(topic_id,working_title,canonical_subject,core_entities_json,event_or_issue,started_at,first_seen_at,seed_expires_at,last_evidence_at,last_participation_at,matching_status,visibility,brief_status,identity_revision) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'seed','hidden','pending',1) ON CONFLICT(topic_id) DO NOTHING",
                    (topic_id,subject,subject,Jsonb(list(claim.entities)),claim.action_or_issue,created,created,
                     created+timedelta(hours=24),created,created),
                )

    def attach_topic_claim(
        self, *, claim: Any, topic_id: str, score: float, reason: str, source: str, item: Any, at: str,
        relations: list[str], membership_role: str, defer_participation: bool = False,
    ) -> None:
        batch_at = _utc_datetime(at)
        item_at = _utc_datetime(item.created_at)
        labels = {"progress":"事件进展","reaction":"社区反应","attribution":"责任与解释","market_effect":"市场影响","future_effect":"未来影响","mechanism":"产品机制","other":"其他讨论"}
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                for relation in relations:
                    thread_id = "thread-" + hashlib.sha1(f"{topic_id}\x1f{relation}".encode()).hexdigest()[:16]
                    connection.execute(
                        "INSERT INTO threads(thread_id,topic_id,working_label,current_thesis,relation_to_topic,first_seen_at,last_seen_at) "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(topic_id,relation_to_topic) DO NOTHING",
                        (thread_id,topic_id,labels.get(relation,relation),claim.claim_text[:180],relation,batch_at,batch_at),
                    )
                    thread_id = connection.execute(
                        "SELECT thread_id FROM threads WHERE topic_id=%s AND relation_to_topic=%s", (topic_id,relation),
                    ).fetchone()["thread_id"]
                    membership_id = "membership-" + hashlib.sha1(f"{claim.claim_id}\x1f{topic_id}\x1f{thread_id}".encode()).hexdigest()[:16]
                    connection.execute(
                        "INSERT INTO memberships(membership_id,claim_id,topic_id,thread_id,topic_role,role,match_score,decision_source,decision_reason,created_at) "
                        "VALUES(%s,%s,%s,%s,'primary',%s,%s,%s,%s,%s) ON CONFLICT(claim_id,topic_id,thread_id) DO NOTHING",
                        (membership_id,claim.claim_id,topic_id,thread_id,membership_role,score,source,reason,batch_at),
                    )
                    connection.execute(
                        "UPDATE threads th SET last_seen_at=%s,source_count=(SELECT COUNT(DISTINCT claim_id) FROM memberships WHERE thread_id=th.thread_id), "
                        "independent_account_count=(SELECT COUNT(DISTINCT ci.activity_account) FROM memberships m JOIN claims cl ON cl.claim_id=m.claim_id "
                        "JOIN content_items ci ON ci.content_item_id=cl.content_item_id WHERE m.thread_id=th.thread_id) WHERE th.thread_id=%s",
                        (batch_at,thread_id),
                    )
                if not defer_participation:
                    connection.execute(
                        "INSERT INTO topic_participations(topic_id,activity_account,last_participation_at,last_content_item_id) VALUES(%s,%s,%s,%s) "
                        "ON CONFLICT(topic_id,activity_account) DO UPDATE SET last_participation_at=GREATEST(topic_participations.last_participation_at,EXCLUDED.last_participation_at),last_content_item_id=EXCLUDED.last_content_item_id",
                        (topic_id,item.activity_account,item_at,item.content_item_id),
                    )
                connection.execute(
                    "UPDATE topics SET last_evidence_at=GREATEST(COALESCE(last_evidence_at,%s),%s) WHERE topic_id=%s",
                    (item_at,item_at,topic_id),
                )

    def export_topics(self, *, visible_only: bool = False) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            topics = connection.execute(
                "SELECT * FROM topics" + (" WHERE visibility='visible'" if visible_only else "") +
                " ORDER BY hotness_score DESC,started_at,topic_id"
            ).fetchall()
            result = []
            for row in topics:
                topic = dict(row)
                topic["core_entities"] = topic.pop("core_entities_json")
                briefs = connection.execute("SELECT * FROM brief_revisions WHERE topic_id=%s ORDER BY revision DESC", (topic["topic_id"],)).fetchall()
                topic["briefs"] = []
                for brief_row in briefs:
                    brief = dict(brief_row)
                    brief["source_claim_ids"] = brief.pop("source_claim_ids_json")
                    brief["source_bindings"] = brief.pop("source_bindings_json")
                    topic["briefs"].append(brief)
                topic["threads"] = [dict(item) for item in connection.execute(
                    "SELECT * FROM threads WHERE topic_id=%s ORDER BY first_seen_at", (topic["topic_id"],)
                ).fetchall()]
                topic["participant_accounts"] = [item["activity_account"] for item in connection.execute(
                    "SELECT activity_account FROM topic_participations WHERE topic_id=%s ORDER BY activity_account", (topic["topic_id"],)
                ).fetchall()]
                result.append(topic)
            return result

    def yesterday_topic_candidates(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            rows = connection.execute(
                "SELECT t.topic_id,t.first_seen_at,t.hotness_score,b.title,b.brief FROM topics t "
                "JOIN brief_revisions b ON b.topic_id=t.topic_id AND b.revision=("
                "SELECT MAX(x.revision) FROM brief_revisions x WHERE x.topic_id=t.topic_id) "
                "WHERE t.matching_status='active' AND t.visibility='visible' AND t.brief_status='ready' "
                "AND t.first_seen_at>=%s AND t.first_seen_at<%s "
                "ORDER BY t.hotness_score DESC,t.participant_count_24h DESC,t.first_seen_at,t.topic_id",
                (start, end),
            ).fetchall()
            return [dict(row) for row in rows]

    def claim_yesterday_run(self, *, local_date: str, topic_id: str, title: str, counts: dict[str, int], attempted_at: datetime) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row = connection.execute(
                    "INSERT INTO yesterday_hot_discussion_runs(local_date,topic_id,status,title,candidate_count,"
                    "excluded_analysis_count,excluded_no_action_count,attempted_at) VALUES(%s,%s,'processing',%s,%s,%s,%s,%s) "
                    "ON CONFLICT(local_date) DO NOTHING RETURNING local_date",
                    (local_date, topic_id, title, counts["checked"], counts["excluded_analysis"], counts["excluded_no_action"], attempted_at),
                ).fetchone()
                return row is not None

    def record_yesterday_no_candidate(self, *, local_date: str, counts: dict[str, int], at: datetime) -> bool:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                row = connection.execute(
                    "INSERT INTO yesterday_hot_discussion_runs(local_date,status,error,candidate_count,excluded_analysis_count,"
                    "excluded_no_action_count,attempted_at,completed_at) VALUES(%s,'no_candidate',NULL,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(local_date) DO NOTHING RETURNING local_date",
                    (local_date, counts["checked"], counts["excluded_analysis"], counts["excluded_no_action"], at, at),
                ).fetchone()
                return row is not None

    def finish_yesterday_run(self, *, local_date: str, status: str, error: str | None, completed_at: datetime) -> None:
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE yesterday_hot_discussion_runs SET status=%s,error=%s,completed_at=%s WHERE local_date=%s AND status='processing'",
                    (status, error, completed_at, local_date),
                )

    def store_poll(
        self, *, account_lower: str, posts: list[dict[str, Any]], error: dict[str, Any] | None,
        now: str, deployment_started_at: str, poll_delay_seconds: float, spool_id: str | None = None,
    ) -> int:
        now_value = _utc_datetime(now)
        deployment_value = _utc_datetime(deployment_started_at)
        accepted = 0
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                if spool_id:
                    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("hottopic-spool:" + spool_id,))
                    if connection.execute(
                        "SELECT 1 FROM hottopic_collect_spool_receipts WHERE spool_id=%s", (spool_id,),
                    ).fetchone():
                        return 0
                current = connection.execute(
                    "SELECT a.status,a.created_at,a.investment_leads_enabled,a.investment_leads_enabled_at,"
                    "a.last_success_at,a.hot_topic_enabled,a.market_sentiment_enabled,a.project_promotion_enabled,"
                    "m.capture_started_at "
                    "FROM hottopic_accounts a CROSS JOIN investment_leads_meta m "
                    "WHERE a.screen_name_lower=%s AND m.singleton_key='global' FOR UPDATE OF a",
                    (account_lower,),
                ).fetchone()
                if current is None or current["status"] != "followed":
                    return 0
                visible_posts = 0
                for post in posts:
                    created_at = _utc_datetime(post.get("created_at_iso"))
                    if current["investment_leads_enabled"] and post.get("investment_materials"):
                        self._persist_investment_materials(
                            connection, account_lower=account_lower, materials=post["investment_materials"],
                            observed_at=now_value, capture_started_at=current["capture_started_at"],
                            account_created_at=current["created_at"], enabled_at=current["investment_leads_enabled_at"],
                        )
                    if created_at < deployment_value:
                        continue
                    visible_posts += 1
                    if post.get("investment_only"):
                        continue
                    processed_at = None if current["hot_topic_enabled"] else now
                    inserted = connection.execute(
                        "INSERT INTO hottopic_inbox(tweet_id,screen_name_lower,payload_json,collected_at,processed_at) "
                        "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(tweet_id) DO NOTHING RETURNING tweet_id",
                        (str(post["tweet_id"]), account_lower, Jsonb(post), now, processed_at),
                    ).fetchone()
                    if inserted is None:
                        continue
                    accepted += 1
                    for module, enabled in (("market_sentiment", current["market_sentiment_enabled"]),
                                            ("project_promotion", current["project_promotion_enabled"])):
                        if enabled:
                            connection.execute(
                                "INSERT INTO x_agent_analysis_jobs(tweet_id,module,status,created_at,updated_at) "
                                "VALUES(%s,%s,'pending',%s,%s) ON CONFLICT(tweet_id,module) DO NOTHING",
                                (str(post["tweet_id"]), module, now, now),
                            )
                failures = 0 if error is None else 1
                if error is not None:
                    failures = int(connection.execute(
                        "SELECT consecutive_failures FROM hottopic_accounts WHERE screen_name_lower=%s",
                        (account_lower,),
                    ).fetchone()["consecutive_failures"]) + 1
                not_found = bool(error is not None and (
                    str(error.get("kind")) == "not_found" or error.get("http_status") == 404
                    or "HTTP Error 404" in str(error.get("error") or "")
                ))
                connection.execute(
                    "UPDATE hottopic_accounts SET next_due_at=%s,last_polled_at=%s,last_success_at=%s,last_error=%s,"
                    "consecutive_failures=%s,consecutive_not_found=CASE WHEN %s THEN consecutive_not_found+1 ELSE 0 END,"
                    "last_item_count=%s,cumulative_content_count=cumulative_content_count+%s,updated_at=%s "
                    "WHERE screen_name_lower=%s",
                    (now_value + timedelta(seconds=poll_delay_seconds), now_value,
                     now_value if error is None else current["last_success_at"], json.dumps(error, ensure_ascii=False) if error else None,
                     failures, not_found, visible_posts, accepted, now_value, account_lower),
                )
                if error is not None:
                    connection.execute(
                        "INSERT INTO hottopic_events(at,kind,detail_json) VALUES(%s,'collect_error',%s)",
                        (now, Jsonb({"account": account_lower, "error": error})),
                    )
                if spool_id:
                    connection.execute(
                        "INSERT INTO hottopic_collect_spool_receipts(spool_id,committed_at) VALUES(%s,%s)",
                        (spool_id, now_value),
                    )
        return accepted

    def claim_x_agent_jobs(
        self, *, now: datetime | str, batch_size: int = 12, max_attempts: int = 3,
        processing_lease: timedelta = timedelta(minutes=5),
    ) -> list[dict[str, Any]]:
        """Claim due work atomically; returned JSON payloads can be analyzed outside the transaction."""
        now_value = _utc_datetime(now)
        lease_cutoff = now_value - processing_lease
        claimed: list[dict[str, Any]] = []
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                connection.execute(
                    "UPDATE x_agent_analysis_jobs SET status='failed',next_attempt_at=NULL,"
                    "last_error=COALESCE(last_error,'分析 worker 租约过期'),completed_at=%s,updated_at=%s "
                    "WHERE status='processing' AND attempts>=%s AND updated_at<%s",
                    (now_value, now_value, max_attempts, lease_cutoff),
                )
                connection.execute(
                    "UPDATE x_agent_analysis_jobs SET status='pending',next_attempt_at=NULL,"
                    "last_error=COALESCE(last_error,'分析 worker 租约过期，已重新排队'),completed_at=NULL,updated_at=%s "
                    "WHERE status='processing' AND attempts<%s AND updated_at<%s",
                    (now_value, max_attempts, lease_cutoff),
                )
                rows = connection.execute(
                    "SELECT j.id,j.tweet_id,j.module,j.attempts,j.updated_at AS claim_updated_at,i.payload_json,a.market_sentiment_enabled,"
                    "a.project_promotion_enabled FROM x_agent_analysis_jobs j "
                    "JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower "
                    "WHERE j.status='pending' AND j.attempts<%s "
                    "AND (j.next_attempt_at IS NULL OR j.next_attempt_at<=%s) "
                    "ORDER BY j.created_at,j.id LIMIT %s FOR UPDATE OF j SKIP LOCKED",
                    (max_attempts, now_value, max(1, int(batch_size))),
                ).fetchall()
                for row in rows:
                    enabled = row["market_sentiment_enabled"] if row["module"] == "market_sentiment" else row["project_promotion_enabled"]
                    if not enabled:
                        connection.execute(
                            "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=%s,updated_at=%s "
                            "WHERE id=%s AND status='pending'",
                            (now_value, now_value, row["id"]),
                        )
                        continue
                    changed = connection.execute(
                        "UPDATE x_agent_analysis_jobs SET status='processing',attempts=attempts+1,updated_at=%s "
                        "WHERE id=%s AND status='pending' RETURNING attempts,updated_at",
                        (now_value, row["id"]),
                    ).fetchone()
                    if changed is None:
                        continue
                    item = dict(row)
                    item["attempts"] = changed["attempts"]
                    item["claim_updated_at"] = changed["updated_at"]
                    if not isinstance(item["payload_json"], dict):
                        item["payload_json"] = json.loads(item["payload_json"])
                    claimed.append(item)
        return claimed

    def finish_x_agent_job(
        self, job_id: int, *, module: str, source: dict[str, Any], result: dict[str, Any] | None,
        error: str | None, attempts: int, now: datetime | str,
        claim_updated_at: datetime | str | None = None,
        retry_delay: timedelta = timedelta(minutes=15), max_attempts: int = 3,
    ) -> bool:
        """Persist one completed model call, rejecting stale finishes and disabled subscriptions."""
        if module not in {"market_sentiment", "project_promotion"}:
            raise ValueError(f"unsupported X Agent module: {module}")
        now_value = _utc_datetime(now)
        account = str(source.get("account_screen_name") or "").lower()
        with connect_postgres(schema=self.schema, application_name=self.application_name, settings=self.settings) as connection:
            with connection.transaction():
                current = connection.execute(
                    "SELECT j.status,j.attempts,j.updated_at,a.screen_name_lower,a.market_sentiment_enabled,a.project_promotion_enabled "
                    "FROM x_agent_analysis_jobs j JOIN hottopic_inbox i ON i.tweet_id=j.tweet_id "
                    "JOIN hottopic_accounts a ON a.screen_name_lower=i.screen_name_lower WHERE j.id=%s FOR UPDATE OF j,a",
                    (job_id,),
                ).fetchone()
                claim_value = _utc_datetime(claim_updated_at) if claim_updated_at is not None else None
                if (current is None or current["status"] != "processing"
                        or int(current["attempts"] or 0) != int(attempts)
                        or claim_value is None or current["updated_at"] != claim_value
                        or now_value >= claim_value + timedelta(minutes=5)):
                    return False
                enabled = current["market_sentiment_enabled"] if module == "market_sentiment" else current["project_promotion_enabled"]
                if not enabled:
                    connection.execute(
                        "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=%s,updated_at=%s,last_error=NULL WHERE id=%s",
                        (now_value, now_value, job_id),
                    )
                    return True
                account = current["screen_name_lower"]
                if error is None and result is None:
                    connection.execute(
                        "UPDATE x_agent_analysis_jobs SET status='ignored',completed_at=%s,updated_at=%s,last_error=NULL WHERE id=%s",
                        (now_value, now_value, job_id),
                    )
                    connection.execute(
                        "UPDATE hottopic_accounts SET last_x_agent_analyzed_at=%s,last_x_agent_error=NULL WHERE screen_name_lower=%s",
                        (now_value, account),
                    )
                    return True
                if error is None:
                    assert result is not None
                    if module == "market_sentiment":
                        self._save_sentiment_results(connection, source, result, now_value)
                    elif module == "project_promotion":
                        self._save_project_results(connection, source, result, now_value)
                    else:
                        raise ValueError(f"unsupported X Agent module: {module}")
                    connection.execute(
                        "UPDATE x_agent_analysis_jobs SET status='succeeded',actual_model=%s,fallback_reason=%s,"
                        "last_error=NULL,completed_at=%s,updated_at=%s WHERE id=%s",
                        (result.get("actual_model", ""), result.get("fallback_reason"), now_value, now_value, job_id),
                    )
                    connection.execute(
                        "UPDATE hottopic_accounts SET last_x_agent_analyzed_at=%s,last_x_agent_error=NULL WHERE screen_name_lower=%s",
                        (now_value, account),
                    )
                    return True
                next_status = "failed" if attempts >= max_attempts else "pending"
                connection.execute(
                    "UPDATE x_agent_analysis_jobs SET status=%s,next_attempt_at=%s,last_error=%s,updated_at=%s,completed_at=%s WHERE id=%s",
                    (next_status, None if next_status == "failed" else now_value + retry_delay,
                     error, now_value, now_value if next_status == "failed" else None, job_id),
                )
                connection.execute(
                    "UPDATE hottopic_accounts SET last_x_agent_error=%s WHERE screen_name_lower=%s",
                    (error, account),
                )
                return True

    @staticmethod
    def _save_sentiment_results(connection: Any, source: dict[str, Any], result: dict[str, Any], at: datetime) -> None:
        account = str(source.get("account_screen_name") or "").lower()
        tweet_id = str(source.get("tweet_id") or "")
        source_url = str(source.get("url") or f"https://x.com/{account}/status/{tweet_id}")
        source_text = str(source.get("text") or source.get("expanded_text") or "")
        posted_at = _optional_utc_datetime(source.get("created_at_iso")) or at
        for item in result.get("items", []):
            label = str(item.get("ticker") or item["instrument_name"]).strip()
            key = f"{item['scope']}:{' '.join(label.lower().split())}"
            connection.execute(
                "INSERT INTO x_agent_sentiment_results(tweet_id,instrument_key,instrument_name,ticker,scope,sentiment,reason,"
                "account_lower,source_url,source_text,posted_at,actual_model,fallback_reason,created_at) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(tweet_id,instrument_key) DO NOTHING",
                (tweet_id, key, item["instrument_name"], item.get("ticker", ""), item["scope"], item["sentiment"],
                 item["reason"], account, source_url, source_text, posted_at,
                 result.get("actual_model", ""), result.get("fallback_reason"), at),
            )

    @staticmethod
    def _save_project_results(connection: Any, source: dict[str, Any], result: dict[str, Any], at: datetime) -> None:
        account = str(source.get("account_screen_name") or "").lower()
        tweet_id = str(source.get("tweet_id") or "")
        source_url = str(source.get("url") or f"https://x.com/{account}/status/{tweet_id}")
        source_text = str(source.get("text") or source.get("expanded_text") or "")
        posted_at = _optional_utc_datetime(source.get("created_at_iso")) or at
        for item in result.get("items", []):
            identity = HotTopicPostgresRepository._project_identity(item, tweet_id)
            direct = item.get("direct_promotion")
            operation = item.get("operation_advice")
            direct_json = json.dumps(direct, ensure_ascii=False, sort_keys=True) if direct else None
            operation_json = json.dumps(operation, ensure_ascii=False, sort_keys=True) if operation else None
            identity_payload = json.dumps(
                {"logic": " ".join(str(item["logic"]).lower().split()), "direct": direct_json, "operation": operation_json},
                ensure_ascii=False, sort_keys=True,
            )
            logic_hash = hashlib.sha256(identity_payload.encode("utf-8")).hexdigest()
            connection.execute(
                "INSERT INTO x_agent_project_observations(identity_key,project_name,ticker,chain_name,contract_address,official_url,logic,logic_hash,"
                "direct_promotion_json,operation_advice_json,account_lower,source_tweet_id,source_url,source_text,first_mentioned_at,last_mentioned_at,"
                "actual_model,fallback_reason,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(identity_key,account_lower,logic_hash) DO UPDATE SET project_name=EXCLUDED.project_name,ticker=EXCLUDED.ticker,"
                "chain_name=EXCLUDED.chain_name,contract_address=EXCLUDED.contract_address,official_url=EXCLUDED.official_url,logic=EXCLUDED.logic,"
                "direct_promotion_json=EXCLUDED.direct_promotion_json,operation_advice_json=EXCLUDED.operation_advice_json,"
                "source_tweet_id=EXCLUDED.source_tweet_id,source_url=EXCLUDED.source_url,source_text=EXCLUDED.source_text,"
                "last_mentioned_at=EXCLUDED.last_mentioned_at,actual_model=EXCLUDED.actual_model,fallback_reason=EXCLUDED.fallback_reason,updated_at=EXCLUDED.updated_at",
                (identity, item["project_name"], item.get("ticker", ""), item.get("chain", ""), item.get("contract_address", ""),
                 item.get("official_url", ""), item["logic"], logic_hash, Jsonb(direct) if direct else None,
                 Jsonb(operation) if operation else None, account, tweet_id, source_url, source_text, posted_at, posted_at,
                 result.get("actual_model", ""), result.get("fallback_reason"), at, at),
            )

    @staticmethod
    def _project_identity(item: dict[str, Any], tweet_id: str) -> str:
        name = str(item.get("project_name") or "").strip().lower()
        contract = str(item.get("contract_address") or "").strip()
        if contract:
            return f"ca:{contract.lower() if contract.lower().startswith('0x') else contract}"
        return f"name:{name}" if name else f"tweet:{tweet_id}"

    @staticmethod
    def _persist_investment_materials(
        connection: Any, *, account_lower: str, materials: list[dict[str, Any]], observed_at: datetime,
        capture_started_at: datetime, account_created_at: datetime, enabled_at: datetime | None,
    ) -> int:
        if not enabled_at:
            return 0
        enabled_time = _utc_datetime(enabled_at)
        created_time = _utc_datetime(account_created_at)
        cutoff = max(_utc_datetime(capture_started_at),
                     enabled_time - timedelta(hours=24) if enabled_time == created_time else enabled_time)
        related = [item for item in materials if item.get("role") != "source_post"]
        saved = 0
        for original in materials:
            material = dict(original)
            if material.get("role") == "source_post":
                material["related_materials"] = related[:4]
            material_id = str(material.get("material_id") or "").strip()
            if not material_id:
                continue
            if material.get("role") == "source_post":
                activity_at = ((material.get("activity_at") if material.get("activity_time_complete")
                                else material.get("created_at_iso"))
                               if material.get("relation") == "repost" else material.get("created_at_iso"))
                try:
                    if not activity_at or _utc_datetime(activity_at) < cutoff:
                        continue
                except (TypeError, ValueError):
                    continue
            identity = {
                "id": material.get("material_id"), "author": material.get("author_screen_name"),
                "text": material.get("text"), "url": material.get("url"),
                "links": material.get("links"), "attachments": material.get("attachments"),
            }
            content_hash = hashlib.sha256(json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            relation = str(material.get("relation") or "original")
            completeness = "incomplete" if not material.get("text") else "complete"
            if completeness == "incomplete":
                material["completeness"] = "incomplete"
            existing = connection.execute(
                "SELECT payload_json FROM investment_lead_materials WHERE material_id=%s AND content_hash=%s",
                (material_id, content_hash),
            ).fetchone()
            if existing is not None:
                prior = existing["payload_json"] if isinstance(existing["payload_json"], dict) else {}
                preserved = {key: prior[key] for key in ("supplement_materials", "supplement_attempted", "supplemented_urls") if key in prior}
                prior_related = prior.get("related_materials") if isinstance(prior.get("related_materials"), list) else []
                next_related = material.get("related_materials") if isinstance(material.get("related_materials"), list) else []
                merged = {(str(item.get("material_id") or ""), str(item.get("role") or "")): item
                          for item in [*prior_related, *next_related] if isinstance(item, dict)}
                material = {**prior, **material, **preserved}
                if merged:
                    material["related_materials"] = list(merged.values())[:4]
            posted_at = _optional_utc_datetime(material.get("created_at_iso"))
            activity_at = _optional_utc_datetime(material.get("activity_at"))
            connection.execute(
                "INSERT INTO investment_lead_materials(material_id,content_hash,posted_at,activity_at,activity_time_complete,"
                "relation,payload_json,first_observed_at,last_observed_at,completeness) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(material_id,content_hash) DO UPDATE SET posted_at=COALESCE(EXCLUDED.posted_at,investment_lead_materials.posted_at),"
                "activity_at=COALESCE(EXCLUDED.activity_at,investment_lead_materials.activity_at),"
                "activity_time_complete=investment_lead_materials.activity_time_complete OR EXCLUDED.activity_time_complete,"
                "relation=EXCLUDED.relation,payload_json=EXCLUDED.payload_json,last_observed_at=EXCLUDED.last_observed_at,"
                "completeness=EXCLUDED.completeness",
                (material_id, content_hash, posted_at, activity_at, bool(material.get("activity_time_complete")),
                 relation, Jsonb(material), observed_at, observed_at, completeness),
            )
            connection.execute(
                "INSERT INTO investment_lead_observations(material_id,content_hash,screen_name_lower,relation,first_observed_at,last_observed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(material_id,content_hash,screen_name_lower,relation) "
                "DO UPDATE SET last_observed_at=EXCLUDED.last_observed_at",
                (material_id, content_hash, account_lower, relation, observed_at, observed_at),
            )
            if material.get("role") == "source_post":
                cursor = connection.execute(
                    "INSERT INTO investment_lead_tasks(material_id,content_hash,stage,prompt_version,status,created_at,updated_at) "
                    "VALUES(%s,%s,'scan','investment_leads_scan_v4','pending',%s,%s) ON CONFLICT DO NOTHING RETURNING id",
                    (material_id, content_hash, observed_at, observed_at),
                )
                connection.execute(
                    "UPDATE investment_lead_tasks SET status='pending',attempts=0,next_attempt_at=NULL,lease_until=NULL,"
                    "last_error=NULL,completed_at=NULL,updated_at=%s WHERE material_id=%s AND content_hash=%s AND status='ignored'",
                    (observed_at, material_id, content_hash),
                )
                connection.execute(
                    "UPDATE investment_lead_identity_searches SET status='pending',attempts=0,next_attempt_at=NULL,lease_until=NULL,"
                    "last_error=NULL,completed_at=NULL,updated_at=%s WHERE status='ignored' AND lead_id IN "
                    "(SELECT lead_id FROM investment_lead_sources WHERE material_id=%s AND content_hash=%s)",
                    (observed_at, material_id, content_hash),
                )
                saved += int(cursor.fetchone() is not None)
        return saved


def _utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_utc_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return _utc_datetime(value)
    except (TypeError, ValueError):
        return None


def _normalize_handle(value: str) -> str:
    handle = value.strip().lstrip("@").split("/", 1)[0]
    if not handle or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in handle):
        raise ValueError("X 用户名格式无效")
    return handle
