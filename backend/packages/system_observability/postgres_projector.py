from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.sql import Identifier

from .news import duplicate_target, json_object, news_title, task_admission
from .postgres_schema import acknowledge_source_state_events, claim_source_state_events
from .postgres_store import PostgresSystemInformationStore, _now_ms
from .projector import (
    STATUS_LABELS,
    TABLE_SPECS,
    TableSpec,
    _first,
    _generic_snapshot,
    _pipeline_event_status,
    _result,
    _stage,
    _task_reason,
)
from .registry import SOURCE_TO_BUSINESS
from .store import timestamp_ms


IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
LOGICAL_TO_PHYSICAL_SCHEMA = {
    "main": "main",
    "hyperliquid": "hyperliquid_news",
    "hot": "hottopic",
    "meme": "meme",
    "gate": "gate",
    "stock": "stock",
    "queue": "local_pipeline",
    "business": "business_news",
}
PHYSICAL_TO_LOGICAL_SCHEMA = {value: key for key, value in LOGICAL_TO_PHYSICAL_SCHEMA.items()}


class PostgresSourceStateProjector:
    """Project committed native source-state outbox rows into observability."""

    def __init__(
        self,
        store: PostgresSystemInformationStore,
        *,
        outbox_schema: str = "main",
        table_specs: tuple[TableSpec, ...] = TABLE_SPECS,
        batch_size: int = 500,
        identity_columns: dict[tuple[str, str], tuple[tuple[str, str], ...]] | None = None,
    ) -> None:
        if not IDENTIFIER.fullmatch(outbox_schema):
            raise ValueError("unsupported outbox schema")
        self.store = store
        self.outbox_schema = outbox_schema
        self.table_specs = table_specs
        self.batch_size = max(1, min(batch_size, 5000))
        self.specs = {
            (LOGICAL_TO_PHYSICAL_SCHEMA.get(spec.database_key, spec.database_key), spec.table): spec
            for spec in table_specs
        }
        business_inbox = self.specs.get(("main", "business_message_inbox"))
        if business_inbox is not None:
            self.specs.setdefault(("business_news", "business_message_inbox"), business_inbox)
        self.identity_columns = {
            ("main", "tasks"): (("id", "bigint"),),
            ("main", "x_task_pipeline"): (("task_id", "bigint"),),
            ("main", "jin10_settings"): (("singleton_key", "text"),),
            ("main", "non_mainstream_media_settings"): (("singleton_key", "text"),),
            ("main", "non_mainstream_media_sources"): (("id", "bigint"),),
            ("main", "whale_watch_addresses"): (("id", "bigint"),),
            ("main", "whale_watch_hyperliquid_addresses"): (("id", "bigint"),),
            ("main", "whale_watch_hyperliquid_settings"): (("singleton_key", "text"),),
            ("main", "hl_news_addresses"): (("address", "text"),),
            ("main", "prompt_templates"): (("template_key", "text"),),
            ("main", "publisher_settings"): (("singleton_key", "text"),),
            ("main", "publisher_channels"): (("channel_key", "text"),),
            ("main", "publisher_rule_config"): (("singleton_key", "text"),),
            ("main", "x_capture_settings"): (("singleton_key", "text"),),
            ("main", "binance_square_settings"): (("singleton_key", "text"),),
            ("main", "newsflash_items"): (("id", "bigint"),),
            ("main", "newsflash_event_sources"): (("id", "bigint"),),
            ("main", "auditor_checks"): (("id", "bigint"),),
            ("main", "fact_checks"): (("id", "bigint"),),
            ("main", "fact_check_runs"): (("id", "bigint"),),
            ("main", "editor_plugin_generation_logs"): (("id", "bigint"),),
            ("main", "prompt_template_versions"): (("id", "bigint"),),
            ("main", "whale_watch_activities"): (("id", "bigint"),),
            ("main", "whale_watch_hyperliquid_activities"): (("id", "bigint"),),
            ("main", "x_capture_attempts"): (("id", "bigint"),),
            ("main", "binance_square_attempts"): (("id", "bigint"),),
            ("main", "business_message_inbox"): (("id", "bigint"),),
            ("business_news", "newsflash_items"): (("id", "bigint"),),
            ("business_news", "business_message_inbox"): (("id", "bigint"),),
            ("hyperliquid_news", "hl_news_addresses"): (("address", "text"),),
            ("hyperliquid_news", "hl_news_candidates"): (("event_key", "text"),),
            ("hyperliquid_news", "hl_news_dispatch"): (("event_key", "text"),),
            ("main", "newsflash_events"): (("event_id", "text"),),
            ("hottopic", "hottopic_events"): (("id", "bigint"),),
            ("hottopic", "event_tracking_events"): (("event_id", "text"),),
            ("hottopic", "event_tracking_publication_outbox"): (("outbox_id", "text"),),
            ("hottopic", "investment_leads"): (("id", "text"),),
            ("meme", "meme_grok_jobs"): (("trigger_key", "text"),),
            ("gate", "settings"): (("key", "text"),),
            ("gate", "symbol_config"): (("symbol", "text"),),
            ("gate", "templates"): (("template_key", "text"),),
            ("stock", "settings"): (("key", "text"),),
            ("stock", "instruments"): (("symbol", "text"),),
            ("local_pipeline", "local_pipeline_jobs"): (("id", "bigint"),),
            **(identity_columns or {}),
        }
        self.identity_columns[("main", "x_capture_attempts")] = (("id", "bigint"),)
        for (schema, table), spec in self.specs.items():
            if table == "x_capture_attempts":
                self.identity_columns[(schema, table)] = (("id", "bigint"),)

    def run_once(self, *, now_ms: int | None = None) -> dict[str, int]:
        now = now_ms or _now_ms()
        self._run_retention(now)
        imported = changed = 0
        with self.store.connect() as conn:
            with conn.transaction():
                # Outbox sequence IDs are allocated before commit. Serialize
                # projectors so a delayed lower ID cannot read stale source
                # state and overwrite a later committed projection.
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", ("system-information-source-projector",))
                events = claim_source_state_events(conn, limit=self.batch_size, outbox_schema=self.outbox_schema)
                entries = [self._project_event(conn, event, now) for event in events]
                # A task and its pipeline row share one item. Acquire item locks in
                # the same order across workers, while appending timeline events in
                # their original outbox order below.
                revisions: dict[str, int] = {}
                for entry in sorted(entries, key=lambda value: (str(value["snapshot"].get("item_id") or ""), int(value["event_id"].rsplit(":", 1)[-1]))):
                    snapshot = entry["snapshot"]
                    item_id = str(snapshot["item_id"])
                    if item_id not in revisions:
                        current = conn.execute("SELECT source_revision FROM system_information_items WHERE item_id=%s FOR UPDATE", (item_id,)).fetchone()
                        revisions[item_id] = int(current["source_revision"] or 0) if current else 0
                    revisions[item_id] += 1
                    snapshot["source_revision"] = revisions[item_id]
                    changed += int(self.store._upsert_snapshot(conn, snapshot, entry["evidence"], now))
                    entry["snapshot"]["item_id"] = snapshot["item_id"]
                    imported += 1
                for entry in entries:
                    snapshot = entry["snapshot"]
                    if not entry.get("skip_timeline"):
                        self.store._append_event(
                            conn,
                            observed=now,
                            item_id=str(snapshot["item_id"]),
                            event_id=entry["event_id"],
                            event_type="source_state_change",
                            stage=entry["stage"],
                            status=entry["status"],
                            result=entry["result"],
                            happened_at_ms=entry["happened_at_ms"],
                            summary=entry["summary"],
                            reason=entry["reason"],
                            error_code=entry["error_code"],
                            evidence=entry["evidence"],
                            details=entry["details"],
                        )
                acknowledged = acknowledge_source_state_events(
                    conn, [int(event["event_id"]) for event in events], outbox_schema=self.outbox_schema,
                )
                if acknowledged != len(events):
                    raise RuntimeError("source-state outbox acknowledgement count changed inside projection transaction")
        return {"read": imported, "changed": changed}

    def _run_retention(self, now_ms: int) -> None:
        last = int(self.store.get_state("postgres_retention_last_ms", "0") or 0)
        if now_ms - last < 60_000:
            return
        try:
            items = self.store.trim_information_items(now_ms=now_ms, limit=5000)
            self.store.set_state("information_item_retention_last_result", json.dumps(items, separators=(",", ":")))
            self.store.set_state("information_item_retention_error", "")
        except Exception as exc:
            self.store.set_state("information_item_retention_error", f"{type(exc).__name__}: {exc}"[:500])
        try:
            with self.store.connect() as conn:
                from .postgres_schema import prune_projected_source_events
                removed = prune_projected_source_events(conn, outbox_schema=self.outbox_schema)
            self.store.set_state("source_outbox_retention_last_result", removed)
            self.store.set_state("source_outbox_retention_error", "")
        except Exception as exc:
            self.store.set_state("source_outbox_retention_error", f"{type(exc).__name__}: {exc}"[:500])
        try:
            errors = self.store.cleanup_external_errors(now_ms=now_ms, limit=500)
            self.store.set_state("postgres_external_error_retention_last_result", json.dumps(errors, separators=(",", ":")))
            self.store.set_state("postgres_external_error_retention_error", "")
        except Exception as exc:
            self.store.set_state("postgres_external_error_retention_error", f"{type(exc).__name__}: {exc}"[:500])
        try:
            with self.store.connect() as conn:
                task_table = conn.execute("SELECT to_regclass('main.tasks') AS relation").fetchone()["relation"]
            if task_table:
                task_evidence = self.store.trim_task_evidence(now_ms=now_ms, limit=200)
                self.store.set_state("task_evidence_retention_last_result", json.dumps(task_evidence, separators=(",", ":")))
                self.store.set_state("task_evidence_retention_error", "")
        except Exception as exc:
            self.store.set_state("task_evidence_retention_error", f"{type(exc).__name__}: {exc}"[:500])
        try:
            events = self.store.prune_task_status_events(now_ms=now_ms, limit=1000)
            self.store.set_state("status_retention_last_result", json.dumps(events, separators=(",", ":")))
            self.store.set_state("status_retention_error", "")
        except Exception as exc:
            self.store.set_state("status_retention_error", f"{type(exc).__name__}: {exc}"[:500])
        try:
            evidence = self.store.cleanup_expired_evidence(now_ms=now_ms, retain_days=90, limit=200, dry_run=False)
            self.store.set_state("evidence_gc_last_result", json.dumps(evidence, separators=(",", ":")))
            self.store.set_state("evidence_gc_error", "")
        except Exception as exc:
            self.store.set_state("evidence_gc_error", f"{type(exc).__name__}: {exc}"[:500])
        self.store.set_state("postgres_retention_last_ms", now_ms)

    def _project_event(self, conn, event: dict[str, Any], now: int) -> dict[str, Any]:
        schema = str(event["source_schema"])
        table = str(event["source_table"])
        row_id = str(event["source_row_id"])
        spec = self.specs.get((schema, table))
        logical_schema = PHYSICAL_TO_LOGICAL_SCHEMA.get(schema, schema)
        payload = event["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            payload = {}
        operation = str(event["operation"]).upper()
        row = self._current_row(conn, schema, table, row_id, operation)
        # A delayed DELETE may be projected after the same primary key has
        # already been reinserted. Preserve the delete event in the timeline,
        # but build the current snapshot from the live row.
        if operation == "DELETE" and row is None:
            row = self._current_row(conn, schema, table, row_id, "UPDATE")
        deleted = operation == "DELETE" and row is None and table != "x_task_pipeline"
        if row is None:
            row = dict(payload)
            row.setdefault("id", row_id)
        if table == "x_task_pipeline":
            task = self._current_row(conn, "main", "tasks", row_id, "UPDATE")
            if task is not None:
                pipeline = {f"pipeline_{key}": value for key, value in row.items()}
                row = {**task, **pipeline}
            snapshot_table = "tasks"
        else:
            snapshot_table = table
        if table == "tasks":
            pipeline = self._task_pipeline(conn, row_id)
            row.update({f"pipeline_{key}": value for key, value in pipeline.items()})
        if spec is None:
            spec = TableSpec(logical_schema, table, "unmapped_source", table)
        elif spec.database_key != logical_schema:
            spec = TableSpec(logical_schema, spec.table, spec.business_key, spec.source, spec.record_kind)
        snapshot_spec = spec if snapshot_table == table else TableSpec(spec.database_key, snapshot_table, spec.business_key, spec.source, spec.record_kind)
        snapshot, _ = _generic_snapshot(snapshot_spec, Path(schema), row, now, 0)
        identity_row_id = row_id
        event_time = timestamp_ms(event["occurred_at"]) or now
        if snapshot["created_at_ms"] is None:
            snapshot["created_at_ms"] = event_time
        if snapshot["updated_at_ms"] is None:
            snapshot["updated_at_ms"] = event_time
        if snapshot_table != "tasks" and table != "x_capture_attempts":
            snapshot["item_id"] = f"{logical_schema}:{table}:{row_id}"
        if table == "x_capture_attempts":
            account = str(_first(row, "username_lower") or row_id)
            snapshot["source_identity_row_id"] = account
            attempt_status = str(row.get("status") or "unknown").casefold()
            if attempt_status in {"success", "ok"}:
                snapshot["result"] = "capture_success"
                snapshot["stage"] = "采集"
                snapshot["summary"] = "采集成功"
                snapshot["failure_category"] = None
            snapshot.update({
                "item_id": f"main:x_capture_attempts:{account}",
                "source_id": account,
                "source_row_id": row_id,
                "source_name": account,
                "title": account,
            })
        snapshot["source_schema"] = schema
        snapshot["source_database"] = schema
        snapshot["source_row_id"] = identity_row_id
        snapshot["source_table"] = snapshot_table
        snapshot["legacy_history"] = False
        snapshot["source_revision"] = 0
        if table == "x_capture_attempts":
            snapshot["only_if_newer"] = True
        operation_name = operation.lower()
        if snapshot_table == "tasks":
            self._project_task(snapshot, row, row_id)
        elif table == "newsflash_items":
            # This table is the source archive. The task row owns the news
            # workflow and is the only record that belongs in the news view.
            snapshot["news_visible"] = False
            snapshot["status"] = "collected"
            snapshot["result"] = "processing"
            snapshot["stage"] = "已收集"
            snapshot["admission"] = "unassessed"
            snapshot["summary"] = "原始来源记录，处理状态见关联任务"
            matching_tasks = self._matching_newsflash_tasks(conn, row)
            if len(matching_tasks) == 1:
                task = matching_tasks[0]
                snapshot["parent_item_id"] = self._task_item_id(task)
        if deleted:
            snapshot.update({"status": "deleted", "result": "unknown", "news_visible": False})
        identity_table = "tasks" if table == "x_task_pipeline" else table
        source_identity = conn.execute(
            "SELECT item_id FROM system_information_source_identity WHERE source_schema=%s AND source_table=%s AND source_row_id=%s",
            (schema, identity_table, identity_row_id),
        ).fetchone()
        previous_evidence = None
        if source_identity:
            snapshot["item_id"] = str(source_identity["item_id"])
        if table == "x_capture_attempts":
            snapshot["item_id"] = f"main:x_capture_attempts:{snapshot.get('source_id') or row_id}"
            snapshot["source_identity_row_id"] = str(snapshot.get('source_id') or row_id)
            if deleted:
                previous = conn.execute("SELECT * FROM system_information_items WHERE item_id=%s", (snapshot["item_id"],)).fetchone()
                if previous:
                    prior = dict(previous)
                    previous_evidence = self.store._load_evidence_in_connection(conn, str(prior["evidence_ref"])) if prior.get("evidence_ref") else None
                    snapshot.update({key: prior.get(key) for key in (
                        "business_key", "source", "source_id", "task_id", "title", "summary", "source_url", "stage",
                        "failure_category", "created_at_ms", "legacy_history", "news_visible", "admission", "parent_item_id",
                        "source_name", "source_updated_at_ms",
                    )})
                    snapshot["duplicate_target"] = prior.get("duplicate_json")
                    snapshot["status"] = "deleted"
                    snapshot["result"] = "unknown"
                    snapshot["updated_at_ms"] = timestamp_ms(event["occurred_at"]) or now
                    snapshot["news_visible"] = False
        changed_fields = [f"{key}={value}" for key, value in payload.items() if value is not None]
        pipeline = {
            key.removeprefix("pipeline_"): value
            for key, value in row.items()
            if key.startswith("pipeline_")
        }
        label = spec.source
        fallback_status = str(payload.get("status") or payload.get("state") or snapshot["status"])
        event_status = "deleted" if operation == "DELETE" else _pipeline_event_status(payload, fallback_status) if table == "x_task_pipeline" else fallback_status
        reason = _task_reason(pipeline, event_status) if snapshot_table == "tasks" else _first(payload, "last_error", "error_message", "failure_reason", "status_reason", "reason")
        event_result = "unknown" if operation == "DELETE" else _result(event_status, snapshot["business_key"], payload)
        summary = str(reason or ("记录已删除" if operation == "DELETE" else "状态变更：" + event_status))
        from .postgres_sources import BUSINESS_SOURCES
        compact_spec = next((source for source in BUSINESS_SOURCES if (source.schema, source.table) == (schema, table)), None)
        evidence_row = row
        if compact_spec:
            fields = {*compact_spec.row_id_columns, *compact_spec.payload_columns, "created_at", "updated_at", "source_url"}
            evidence_row = {key: value for key, value in row.items() if key in fields}
        return {
            "snapshot": snapshot,
            "evidence": {
                "recorded_scope": "transactional_source_outbox",
                "operation": operation_name,
                "source_table": table,
                "source_row_id": row_id,
                "payload": payload,
                "current_row": evidence_row,
                **({"previous_evidence": previous_evidence} if previous_evidence is not None else {}),
            },
            "event_id": f"source-state:{schema}:{int(event['event_id'])}",
            "skip_timeline": table == "x_capture_attempts",
            "happened_at_ms": timestamp_ms(row.get("finished_at")) if table == "x_capture_attempts" else timestamp_ms(event["occurred_at"]) or now,
            "status": event_status,
            "result": event_result,
            "stage": _stage(event_status),
            "summary": (summary or label)[:1200],
            "reason": str(reason)[:1200] if reason else None,
            "error_code": str(_first(payload, "error_code") or "unknown") if reason else None,
            "details": {"operation": operation, "source_schema": schema, "source_table": table, "source_row_id": row_id,
                        "changed_fields": changed_fields[:100]},
        }

    def _current_row(self, conn, schema: str, table: str, row_id: str, operation: str) -> dict[str, Any] | None:
        if not IDENTIFIER.fullmatch(schema) or not IDENTIFIER.fullmatch(table):
            raise ValueError("unsupported source identity")
        if operation == "DELETE":
            return None
        keys = self.identity_columns.get((schema, table), (("id", "bigint"),))
        try:
            values = json.loads(row_id) if len(keys) > 1 else [row_id]
        except (TypeError, ValueError):
            values = [row_id]
        if len(values) != len(keys):
            raise ValueError(f"invalid source row identity for {schema}.{table}")
        where = " AND ".join(Identifier(column).as_string(conn) + "=%s::" + cast for column, cast in keys)
        result = conn.execute(
            "SELECT to_jsonb(source_row) AS row FROM "
            + Identifier(schema, table).as_string(conn)
            + " AS source_row WHERE " + where + " LIMIT 1",
            tuple(values),
        ).fetchone()
        value = result["row"] if result else None
        if not isinstance(value, dict):
            return None
        if table == "x_capture_attempts":
            source_relation = Identifier(schema, table).as_string(conn)
            if not value.get("username_lower") and value.get("account_id") is not None:
                account_relation = Identifier(schema, "x_capture_accounts").as_string(conn)
                account = conn.execute(
                    "SELECT username_lower FROM " + account_relation + " WHERE id=%s LIMIT 1",
                    (value["account_id"],),
                ).fetchone()
                if account and account["username_lower"]:
                    value = {**value, "username_lower": account["username_lower"]}
            latest = conn.execute(
                "SELECT to_jsonb(attempt) AS row, first_seen.started_at AS first_started_at "
                "FROM " + source_relation + " attempt CROSS JOIN LATERAL (SELECT min(started_at) AS started_at FROM "
                + source_relation + " WHERE username_lower=%s) first_seen WHERE attempt.username_lower=%s "
                "ORDER BY attempt.finished_at DESC,attempt.id DESC LIMIT 1",
                (str(value["username_lower"]), str(value["username_lower"])),
            ).fetchone() if value.get("username_lower") else None
            latest_value = latest["row"] if latest else None
            if isinstance(latest_value, dict):
                latest_value = dict(latest_value)
                latest_value["started_at"] = latest["first_started_at"]
                return latest_value
            return None
        return dict(value)

    def _task_pipeline(self, conn, task_id: str) -> dict[str, Any]:
        exists = conn.execute("SELECT to_regclass('main.x_task_pipeline') AS relation").fetchone()["relation"]
        if not exists:
            return {}
        if not str(task_id).isdigit():
            return {}
        row = conn.execute("SELECT to_jsonb(p) AS row FROM main.x_task_pipeline p WHERE task_id=%s", (int(task_id),)).fetchone()
        value = row["row"] if row else None
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _task_item_id(row: dict[str, Any]) -> str:
        source = str(row.get("source") or "unknown")
        source_item_id = str(row.get("source_item_id") or row.get("id") or "")
        return f"task:{source}:{source_item_id}"

    @classmethod
    def _matching_newsflash_tasks(cls, conn, row: dict[str, Any]) -> list[dict[str, Any]]:
        source = str(row.get("source") or "")
        source_item_id = str(row.get("source_item_id") or "")
        if not source or not source_item_id:
            return []
        if not conn.execute("SELECT to_regclass('main.tasks') AS relation").fetchone()["relation"]:
            return []
        return [dict(task) for task in conn.execute(
            "SELECT id,source,source_item_id FROM main.tasks "
            "WHERE source=%s AND source_item_id=%s ORDER BY id LIMIT 2",
            (source, source_item_id),
        ).fetchall()]

    @staticmethod
    def _project_task(snapshot: dict[str, Any], row: dict[str, Any], row_id: str) -> None:
        source = str(row.get("source") or "unknown")
        status = str(row.get("status") or "unknown")
        label, result = STATUS_LABELS.get(status, (status.replace("_", " "), "failed" if status.endswith("failed") else "unknown"))
        pipeline = {key.removeprefix("pipeline_"): value for key, value in row.items() if key.startswith("pipeline_")}
        metadata = json_object(row.get("metadata"))
        error = pipeline.get("last_error")
        reason = _task_reason(pipeline, status)
        admission = task_admission(status, pipeline, source)
        created = timestamp_ms(row.get("created_at"))
        updated_values = [timestamp_ms(row.get("updated_at")), created]
        updated_values.extend(timestamp_ms(pipeline.get(key)) for key in (
            "judge_completed_at", "search_completed_at", "write_completed_at", "format_completed_at",
            "publisher_decided_at", "publish_completed_at", "updated_at",
        ))
        snapshot.update({
            "item_id": f"task:{source}:{row.get('source_item_id') or row_id}",
            "business_key": SOURCE_TO_BUSINESS.get(source, "unmapped_source"),
            "source": source, "source_id": str(row.get("source_item_id") or row_id), "task_id": int(row_id) if row_id.isdigit() else None,
            "title": news_title(pipeline.get("final_title"), pipeline.get("draft_title"), row.get("title"), row.get("content")),
            "summary": str(error or reason or label)[:1200], "source_url": row.get("source_url"),
            "stage": _stage(status), "status": status, "result": result,
            "failure_category": "program_or_unknown" if status.endswith("failed") else None,
            "created_at_ms": created, "updated_at_ms": max((v for v in updated_values if v is not None), default=None),
            "legacy_history": False, "reason": str(reason or "")[:1200] or None, "error_code": "unknown" if error else None,
            "admission": admission, "news_visible": admission in {"accepted", "duplicate"} and source not in {"stock_market", "gate_market"},
            "duplicate_target": duplicate_target(pipeline),
            "source_name": _first(metadata, "effective_author_name", "author_username", "account_username", "site_display_name", "source_label") or source,
            "source_updated_at_ms": timestamp_ms(_first(metadata, "source_updated_at", "source_modified_at")),
        })
