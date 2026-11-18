"""Explicit ownership and JSON typing for the one-time HotTopic SQLite import."""

import json
import math
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

HOT_TOPIC_IMPORT_TABLES = (
    "hottopic_meta", "hottopic_accounts", "hottopic_inbox", "hottopic_account_cleanup", "hottopic_events",
    "hottopic_migrations", "x_agent_analysis_jobs", "x_agent_sentiment_results", "x_agent_sentiment_snapshots",
    "x_agent_project_observations", "batches", "content_items", "claims", "topics", "threads", "memberships",
    "topic_participations", "retrieval_state", "brief_revisions", "topic_evidence", "decision_audit",
    "topic_merges", "topic_pair_reviews", "topic_claim_reviews", "topic_merge_scan_state", "topic_merge_scans",
    "event_tracking_prompt_versions", "event_tracking_events", "event_tracking_dismissals", "event_tracking_cycles",
    "event_tracking_topic_assessments", "event_tracking_topic_observations", "event_tracking_topic_links",
    "event_tracking_account_discoveries", "event_tracking_accounts", "event_tracking_bindings", "event_tracking_inbox",
    "event_tracking_updates", "event_tracking_publication_outbox", "event_tracking_audit",
    "yesterday_hot_discussion_runs", "investment_leads_meta", "investment_lead_maintenance_progress",
    "investment_lead_materials", "investment_lead_observations", "investment_lead_tasks", "investment_lead_events",
    "investment_leads", "investment_lead_revisions", "investment_lead_sources", "investment_lead_usage",
    "investment_lead_identity_searches", "investment_lead_scheduler_cursor", "investment_lead_replay_imports",
)

HOT_TOPIC_JSON_COLUMNS: dict[str, frozenset[str]] = {
    "hottopic_inbox": frozenset({"payload_json"}),
    "hottopic_events": frozenset({"detail_json"}),
    "x_agent_project_observations": frozenset({"direct_promotion_json", "operation_advice_json"}),
    "batches": frozenset({"result_json"}),
    "content_items": frozenset({"metrics_json", "references_json", "raw_payload_json"}),
    "claims": frozenset({"entities_json"}),
    "topics": frozenset({"core_entities_json"}),
    "retrieval_state": frozenset({"tokens_json", "hard_keys_json", "entities_json", "representative_claim_ids_json", "recent_claim_ids_json"}),
    "brief_revisions": frozenset({"source_claim_ids_json", "source_bindings_json"}),
    "decision_audit": frozenset({"topic_ids_json", "candidates_json"}),
    "topic_pair_reviews": frozenset({"evidence_json"}),
    "topic_merge_scans": frozenset({"topic_ids_json", "coarse_json", "deep_json", "merge_ids_json"}),
    "event_tracking_events": frozenset({"identity_tokens_json", "confirmed_facts_json", "unconfirmed_claims_json", "official_response_hypothesis_json"}),
    "event_tracking_dismissals": frozenset({"detail_json"}),
    "event_tracking_topic_assessments": frozenset({"snapshot_json", "result_json", "usage_json"}),
    "event_tracking_topic_links": frozenset({"snapshot_json"}),
    "event_tracking_account_discoveries": frozenset({"usage_json", "search_tool_calls_json", "citations_json", "result_json"}),
    "event_tracking_accounts": frozenset({"official_evidence_json"}),
    "event_tracking_bindings": frozenset({"identity_result_json"}),
    "event_tracking_inbox": frozenset({"payload_json"}),
    "event_tracking_updates": frozenset({"confirmed_facts_json", "unconfirmed_claims_json", "usage_json"}),
    "event_tracking_audit": frozenset({"detail_json"}),
    "investment_lead_materials": frozenset({"payload_json"}),
    "investment_lead_events": frozenset({"impact_relations_json"}),
    "investment_leads": frozenset({"assets_json", "source_claims_json", "replay_json"}),
    "investment_lead_revisions": frozenset({"assets_json", "source_claims_json", "source_materials_json"}),
    "investment_lead_identity_searches": frozenset({"context_json", "result_json"}),
}

# These are added by newer PostgreSQL schema revisions and may be absent from
# the frozen SQLite source. Omitting them from INSERT lets PostgreSQL apply the
# declared defaults; do not synthesize an event-time value during import.
HOT_TOPIC_IMPORT_BACKFILLS: dict[tuple[str, str], None] = {
    ("topics", "lifecycle_refresh_at"): None,
}

# Maintenance cursors are bounded-work hints. If absent in the frozen source,
# start them empty and let the first maintenance pass discover its keyset.
HOT_TOPIC_EMPTY_RUNTIME_TABLES = frozenset({"investment_lead_maintenance_progress"})

HOT_TOPIC_BOOLEAN_COLUMNS: dict[str, frozenset[str]] = {
    "hottopic_accounts": frozenset({"protected", "hot_topic_enabled", "market_sentiment_enabled",
                                    "project_promotion_enabled", "investment_leads_enabled"}),
    "hottopic_meta": frozenset({"retention_prune_pending"}),
    "investment_lead_materials": frozenset({"activity_time_complete"}),
}

HOT_TOPIC_TIMESTAMP_COLUMNS: dict[str, frozenset[str]] = {
    "hottopic_meta": frozenset({"deployment_started_at", "last_maintenance_at"}),
    "hottopic_accounts": frozenset({"next_due_at", "last_polled_at", "last_success_at", "investment_leads_enabled_at",
                                    "last_x_agent_analyzed_at", "created_at", "updated_at"}),
    "hottopic_inbox": frozenset({"collected_at", "processed_at"}),
    "hottopic_account_cleanup": frozenset({"queued_at"}),
    "hottopic_events": frozenset({"at"}),
    "hottopic_migrations": frozenset({"applied_at"}),
    "x_agent_analysis_jobs": frozenset({"next_attempt_at", "created_at", "updated_at", "completed_at"}),
    "x_agent_sentiment_results": frozenset({"posted_at", "created_at"}),
    "x_agent_sentiment_snapshots": frozenset({"snapshot_at"}),
    "x_agent_project_observations": frozenset({"first_mentioned_at", "last_mentioned_at", "created_at", "updated_at"}),
    "batches": frozenset({"committed_at"}),
    "content_items": frozenset({"created_at"}),
    "claims": frozenset({"created_at"}),
    "topics": frozenset({"started_at", "first_seen_at", "seed_expires_at", "last_evidence_at", "last_participation_at",
                          "archived_at", "brief_error_at", "lifecycle_refresh_at"}),
    "threads": frozenset({"first_seen_at", "last_seen_at"}),
    "memberships": frozenset({"created_at"}),
    "topic_participations": frozenset({"last_participation_at"}),
    "retrieval_state": frozenset({"updated_at"}),
    "brief_revisions": frozenset({"generated_at"}),
    "decision_audit": frozenset({"created_at"}),
    "topic_merges": frozenset({"created_at"}),
    "topic_pair_reviews": frozenset({"reviewed_at", "next_attempt_at"}),
    "topic_claim_reviews": frozenset({"reviewed_at"}),
    "topic_merge_scan_state": frozenset({"last_periodic_success_at", "next_retry_at"}),
    "topic_merge_scans": frozenset({"started_at", "completed_at"}),
    "event_tracking_prompt_versions": frozenset({"created_at"}),
    "event_tracking_events": frozenset({"created_at", "updated_at", "last_hot_topic_at", "last_material_progress_at", "ended_at"}),
    "event_tracking_dismissals": frozenset({"dismissed_at"}),
    "event_tracking_cycles": frozenset({"started_at", "initial_expires_at", "expires_at", "hard_expires_at",
                                         "last_material_progress_at", "ended_at", "created_at", "updated_at"}),
    "event_tracking_topic_assessments": frozenset({"next_attempt_at", "judged_at", "created_at", "updated_at"}),
    "event_tracking_topic_observations": frozenset({"first_eligible_at", "last_eligible_at", "first_queued_at", "last_queued_at",
                                                     "last_attempt_at", "judged_at", "updated_at"}),
    "event_tracking_topic_links": frozenset({"linked_at", "updated_at"}),
    "event_tracking_account_discoveries": frozenset({"created_at", "completed_at"}),
    "event_tracking_accounts": frozenset({"next_due_at", "last_polled_at", "last_success_at", "created_at", "updated_at"}),
    "event_tracking_bindings": frozenset({"discovered_at", "released_at", "last_contributed_at"}),
    "event_tracking_inbox": frozenset({"collected_at", "processed_at"}),
    "event_tracking_updates": frozenset({"next_attempt_at", "created_at", "classified_at", "updated_at"}),
    "event_tracking_publication_outbox": frozenset({"next_attempt_at", "created_at", "submitted_at", "cancelled_at", "updated_at"}),
    "event_tracking_audit": frozenset({"at"}),
    "yesterday_hot_discussion_runs": frozenset({"attempted_at", "completed_at"}),
    "investment_leads_meta": frozenset({"capture_started_at", "worker_last_seen_at", "last_maintenance_at", "maintenance_next_at"}),
    "investment_lead_maintenance_progress": frozenset({"cursor_time", "updated_at"}),
    "investment_lead_materials": frozenset({"posted_at", "activity_at", "first_observed_at", "last_observed_at"}),
    "investment_lead_observations": frozenset({"first_observed_at", "last_observed_at"}),
    "investment_lead_tasks": frozenset({"lease_until", "next_attempt_at", "created_at", "updated_at", "completed_at"}),
    "investment_lead_events": frozenset({"created_at"}),
    "investment_leads": frozenset({"first_seen_at", "last_substantive_at", "created_at", "updated_at"}),
    "investment_lead_replay_imports": frozenset({"imported_at"}),
    "investment_lead_revisions": frozenset({"created_at"}),
    "investment_lead_sources": frozenset({"created_at"}),
    "investment_lead_usage": frozenset({"updated_at"}),
    "investment_lead_identity_searches": frozenset({"lease_until", "next_attempt_at", "created_at", "updated_at", "completed_at"}),
}


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-standard JSON number is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key is not allowed: {key!r}")
        result[key] = value
    return result


def _validate_json_numbers(value: Any, path: str = "$") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite JSON number is not allowed at {path}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"non-finite JSON number is not allowed at {path}")
    if isinstance(value, dict):
        for key, child in value.items():
            _validate_json_numbers(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_json_numbers(child, f"{path}[{index}]")


def encode_hot_topic_import_row(
    table: str, row: dict[str, Any], destination_columns: tuple[str, ...],
) -> tuple[tuple[str, ...], tuple[Any, ...]]:
    """Return typed destination columns/values; omitted new fields use PG defaults."""
    owned = set(HOT_TOPIC_IMPORT_TABLES)
    if table not in owned:
        raise ValueError(f"table is not owned by HotTopic import: {table}")
    declared_columns = frozenset(column for owner, column in HOT_TOPIC_IMPORT_BACKFILLS if owner == table)
    unknown_columns = set(row) - set(destination_columns) - declared_columns
    if unknown_columns:
        raise ValueError(f"source columns are not in the {table} import contract: {sorted(unknown_columns)}")
    json_columns = HOT_TOPIC_JSON_COLUMNS.get(table, frozenset())
    boolean_columns = HOT_TOPIC_BOOLEAN_COLUMNS.get(table, frozenset())
    timestamp_columns = HOT_TOPIC_TIMESTAMP_COLUMNS.get(table, frozenset())
    values: dict[str, Any] = {}
    for column in destination_columns:
        if column not in row:
            continue
        value = row[column]
        if (table, column) in HOT_TOPIC_IMPORT_BACKFILLS and value is None:
            continue
        if value is not None and column in json_columns:
            if isinstance(value, str):
                raw_json = value
                value = json.loads(
                    value, parse_float=Decimal, parse_int=int,
                    parse_constant=_reject_json_constant, object_pairs_hook=_unique_json_object,
                )
                _validate_json_numbers(value)
                value = Jsonb(value, dumps=lambda _parsed, source=raw_json: source)
            else:
                _validate_json_numbers(value)
                value = Jsonb(value)
        elif value is not None and column in boolean_columns:
            if isinstance(value, bool):
                pass
            elif isinstance(value, int) and value in (0, 1):
                value = bool(value)
            elif isinstance(value, str) and value.strip().lower() in {"0", "1", "false", "true"}:
                value = value.strip().lower() in {"1", "true"}
            else:
                raise ValueError(f"invalid SQLite boolean for {table}.{column}: {value!r}")
        elif value is not None and column in timestamp_columns:
            raw = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if raw.tzinfo is None:
                raw = raw.replace(tzinfo=UTC)
            value = raw.astimezone(UTC)
        values[column] = value
    columns = tuple(values)
    return columns, tuple(values[column] for column in columns)
