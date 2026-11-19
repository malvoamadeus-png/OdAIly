#!/usr/bin/env python3
"""Plan or apply the reviewed 2026-11-19 frozen SQLite increment to PostgreSQL."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import ExitStack
from pathlib import Path

from psycopg import sql
from psycopg.types.json import Jsonb

from packages.common.native_sqlite_import import _convert


TABLES = {
    ("main", "newsflash_items"): ("main", ("source", "source_item_id"), ("raw_payload", "metadata")),
    ("main", "tasks"): ("main", ("source", "source_item_id"), ("raw_payload", "metadata")),
    ("main", "x_capture_attempts"): ("main", ("id",), ("metadata",)),
    ("hottopic", "x_agent_analysis_jobs"): ("hottopic", ("tweet_id", "module"), ()),
}
TASK_LINK_TABLES = ("x_task_pipeline", "x_processing_attempts")
TASK_SAFE_STATUSES = {"discarded", "duplicate", "ready_review"}
DEPENDENCY_TABLES = {"search_event_candidates", "search_event_sources", "x_processing_attempt_events"}


def _sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.expanduser().resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _insert(connection, schema: str, table: str, row: dict, target_columns: set[str], json_columns: tuple[str, ...]):
    columns = [name for name in row if name in target_columns]
    target_types = {item["column_name"]: item for item in connection.execute(
        "SELECT column_name,udt_name,udt_schema FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table)).fetchall()}
    mapping = type("ImportMapping", (), {"source_table": table, "json_columns": json_columns,
                                         "timestamp_columns": tuple(
                                             name for name, target in target_types.items()
                                             if target["udt_name"] in ("timestamp", "timestamptz"))})()
    values = [_convert(row[name], name, mapping, target_types[name]) for name in columns]
    connection.execute(sql.SQL("INSERT INTO {}.{} ({}) VALUES ({})").format(
        sql.Identifier(schema), sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)),
        sql.SQL(",").join(sql.Placeholder() for _ in columns)), values)


def _target_columns(connection, schema: str, table: str) -> set[str]:
    return {r["column_name"] for r in connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
        (schema, table)).fetchall()}


def _check_identity(connection, schema: str, table: str, row: dict, business_key: tuple[str, ...]):
    by_id = connection.execute(sql.SQL("SELECT id FROM {}.{} WHERE id=%s").format(
        sql.Identifier(schema), sql.Identifier(table)), (row["id"],)).fetchone()
    where = sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in business_key)
    by_key = connection.execute(sql.SQL("SELECT id FROM {}.{} WHERE ").format(
        sql.Identifier(schema), sql.Identifier(table)) + where,
        tuple(row[k] for k in business_key)).fetchone()
    if by_id and by_key and by_id["id"] == by_key["id"]:
        return "existing", by_id["id"]
    if by_id or by_key:
        return "conflict", {"id_match": by_id["id"] if by_id else None,
                            "key_match": by_key["id"] if by_key else None}
    return "missing", None


def reconcile(*, sources: dict[str, Path], bounds: dict[str, tuple[int, int]], apply: bool,
              source_writes_stopped: bool, connection=None, allow_pending_tasks: bool = False) -> dict:
    """Process only bounded events and their specifically modeled parent records."""
    if apply and not source_writes_stopped:
        raise RuntimeError("--apply requires --source-writes-stopped")
    if set(sources) != {"main", "hottopic"} or set(bounds) != set(sources):
        raise ValueError("both main and hottopic sources and sequence intervals are required")
    own_connection = connection is None
    stack = ExitStack()
    if own_connection:
        from packages.common.pg_native import connect_postgres
        from packages.common.storage import load_postgres_settings
        connection = stack.enter_context(connect_postgres(
            schema="main", application_name="odaily-frozen-increment-reconcile",
            settings=load_postgres_settings()))
    previous_autocommit = connection.autocommit
    connection.autocommit = False
    report = {"dry_run": not apply, "complete": False, "sources": {}, "counts": {
        "scanned": 0, "planned_inserts": 0, "inserted": 0, "existing": 0,
        "pending_tasks": 0, "conflicts": 0, "deleted_source": 0}}
    try:
        snapshots: dict[tuple[str, str], dict] = {}
        dbs = {name: stack.enter_context(_sqlite(path)) for name, path in sources.items()}
        for source, db in dbs.items():
            lower, upper = bounds[source]
            if lower < 0 or upper < lower:
                raise ValueError(f"invalid sequence interval for {source}")
            events = db.execute(
                "SELECT seq,table_name,source_row_id,operation FROM odaily_observability_outbox "
                "WHERE seq>? AND seq<=? ORDER BY seq", (lower, upper)).fetchall()
            current: dict[tuple[str, str], sqlite3.Row] = {}
            for event in events:
                if (source, event["table_name"]) not in TABLES and not (source == "main" and event["table_name"] in DEPENDENCY_TABLES):
                    raise RuntimeError(f"unapproved outbox table {source}.{event['table_name']} at seq {event['seq']}")
                if (source, event["table_name"]) in TABLES:
                    current[(event["table_name"], str(event["source_row_id"]))] = event
            report["sources"][source] = {"lower_exclusive": lower, "upper_inclusive": upper,
                "outbox_events": len(events), "distinct_touched_rows": len(current), "tables": {}}
            for (table, row_id), event in current.items():
                row = db.execute(f'SELECT * FROM "{table}" WHERE id=?', (row_id,)).fetchone()
                report["counts"]["scanned"] += 1
                if row is None:
                    report["counts"]["deleted_source"] += 1
                    continue
                snapshots[(source, table, row_id)] = {"row": dict(row), "last_seq": event["seq"],
                                                       "last_operation": event["operation"]}

        plans = []
        conflicts = []
        for (source, table, row_id), snapshot in snapshots.items():
            schema, business_key, json_columns = TABLES[(source, table)]
            row = snapshot["row"]
            state, match = _check_identity(connection, schema, table, row, business_key)
            entry = {"source": source, "table": table, "source_id": row_id,
                     "last_seq": snapshot["last_seq"], "business_key": {k: row[k] for k in business_key},
                     "state": state, "pg_match": match}
            if table == "tasks":
                entry["source_status"] = row["status"]
                if row["status"] == "pending":
                    entry["action"] = "import_source_state_requires_pending_approval"
                    report["counts"]["pending_tasks"] += 1
                else: entry["action"] = "import_source_state"
            elif table == "x_agent_analysis_jobs":
                entry["action"] = "import_with_inbox_parent"
            else:
                entry["action"] = "import_if_missing"
            if state == "conflict":
                conflicts.append(entry)
            elif state == "existing":
                report["counts"]["existing"] += 1
            elif table != "tasks" or row["status"] != "pending" or allow_pending_tasks:
                plans.append(((source, table, row_id), schema, business_key, json_columns, entry))
            report["sources"][source]["tables"].setdefault(table, {"scanned": 0, "missing": 0, "existing": 0})
            counts = report["sources"][source]["tables"][table]
            counts["scanned"] += 1
            counts["missing" if state == "missing" else "existing"] += int(state != "conflict")

        # Only linked rows present in the audited source increment are eligible, to avoid broad copy.
        task_ids = {int(row_id) for (source, table, row_id) in snapshots if source == "main" and table == "tasks"}
        main_db = dbs["main"]
        linked_rows = []
        for table in TASK_LINK_TABLES:
            if not task_ids:
                continue
            marks = ",".join("?" for _ in task_ids)
            for row in main_db.execute(f'SELECT * FROM "{table}" WHERE task_id IN ({marks})', tuple(sorted(task_ids))):
                linked_rows.append((table, dict(row)))
        report["linked_rows"] = {"x_task_pipeline": 0, "x_processing_attempts": 0,
                                 "x_processing_attempt_events": 0, "search_event_candidates": 0,
                                 "search_event_sources": 0, "hottopic_inbox": 0}
        for table, row in linked_rows:
            schema = "main"
            if table == "x_task_pipeline":
                by_id = connection.execute("SELECT task_id FROM main.x_task_pipeline WHERE task_id=%s", (row["task_id"],)).fetchone()
                if by_id:
                    # Do not replace pipeline state already in PG.
                    continue
                plans.append((None, schema, (), (), {"source": "main", "table": table, "source_id": row["task_id"], "state": "missing", "action": "import_linked_row", "row": row}))
                report["linked_rows"][table] += 1
            else:
                state, match = _check_identity(connection, schema, table, row, ("task_id", "stage", "attempt_no"))
                if state == "conflict":
                    conflicts.append({"source": "main", "table": table, "source_id": row["id"], "state": state, "pg_match": match})
                elif state == "missing":
                    plans.append((None, schema, (), (), {"source": "main", "table": table, "source_id": row["id"], "state": state, "action": "import_linked_row", "row": row}))
                    report["linked_rows"][table] += 1
                # Attempt-event rows are the immutable history for each attempt.
                if table == "x_processing_attempts":
                    for event in main_db.execute("SELECT * FROM x_processing_attempt_events WHERE attempt_id=? ORDER BY id", (row["id"],)):
                        event_row = dict(event)
                        exists = connection.execute("SELECT 1 FROM main.x_processing_attempt_events WHERE id=%s", (event_row["id"],)).fetchone()
                        if not exists:
                            plans.append((None, schema, (), (), {"source": "main", "table": "x_processing_attempt_events", "source_id": event_row["id"], "state": "missing", "action": "import_attempt_event", "row": event_row}))
                            report["linked_rows"]["x_processing_attempt_events"] += 1
        # Candidate and source rows are a small dependency closure of the only
        # linked pipeline row found in the audited task set.
        linked_task_ids = {int(row["task_id"]) for table, row in linked_rows if table == "x_task_pipeline"}
        for candidate in main_db.execute("SELECT * FROM search_event_candidates WHERE primary_task_id IN (%s)" % ",".join("?" for _ in linked_task_ids), tuple(sorted(linked_task_ids))) if linked_task_ids else ():
            candidate_row = dict(candidate)
            state, match = _check_identity(connection, "main", "search_event_candidates", candidate_row, ("id",))
            if state == "conflict":
                conflicts.append({"source": "main", "table": "search_event_candidates", "source_id": candidate_row["id"], "state": state, "pg_match": match})
            elif state == "missing":
                plans.append((None, "main", (), (), {"source": "main", "table": "search_event_candidates", "source_id": candidate_row["id"], "state": state, "action": "import_pipeline_candidate", "row": candidate_row}))
                report["linked_rows"]["search_event_candidates"] += 1
            for source_row in main_db.execute("SELECT * FROM search_event_sources WHERE candidate_id=? ORDER BY id", (candidate_row["id"],)):
                source_row = dict(source_row)
                existing = connection.execute("SELECT id FROM main.search_event_sources WHERE id=%s", (source_row["id"],)).fetchone()
                if not existing:
                    plans.append((None, "main", (), (), {"source": "main", "table": "search_event_sources", "source_id": source_row["id"], "state": "missing", "action": "import_candidate_source", "row": source_row}))
                    report["linked_rows"]["search_event_sources"] += 1
        # Parent inbox rows are included only for the six audited job rows.
        hotdb = dbs["hottopic"]
        job_keys = {(s["row"]["tweet_id"], s["row"]["module"]) for (src, table, _), s in snapshots.items() if src == "hottopic" and table == "x_agent_analysis_jobs"}
        for tweet_id, module in job_keys:
            inbox = hotdb.execute("SELECT * FROM hottopic_inbox WHERE tweet_id=?", (tweet_id,)).fetchone()
            if inbox is None:
                conflicts.append({"source": "hottopic", "table": "hottopic_inbox", "business_key": tweet_id, "state": "missing_source_parent"})
                continue
            exists = connection.execute("SELECT 1 FROM hottopic.hottopic_inbox WHERE tweet_id=%s", (tweet_id,)).fetchone()
            if not exists:
                plans.append((None, "hottopic", (), ("payload_json",), {"source": "hottopic", "table": "hottopic_inbox", "source_id": tweet_id, "state": "missing", "action": "import_job_parent", "row": dict(inbox)}))
                report["linked_rows"]["hottopic_inbox"] += 1

        report["planned"] = [entry for *_, entry in plans]
        report["counts"]["planned_inserts"] = len(plans)
        report["conflicts_detail"] = conflicts
        report["counts"]["conflicts"] = len(conflicts)
        if apply and report["counts"]["pending_tasks"] and not allow_pending_tasks:
            raise RuntimeError("pending tasks require explicit --allow-pending-tasks after replay review")
        if apply and conflicts:
            raise RuntimeError("conflicts detected; refusing entire apply transaction")
        if apply:
            priority = {"tasks": 0, "newsflash_items": 1, "x_capture_attempts": 1,
                        "search_event_candidates": 2, "hottopic_inbox": 2,
                        "search_event_sources": 3, "x_task_pipeline": 4,
                        "x_processing_attempts": 5, "x_processing_attempt_events": 6,
                        "x_agent_analysis_jobs": 3}
            plans.sort(key=lambda item: priority.get(item[4]["table"], 99))
            for identity, schema, key, json_columns, entry in plans:
                row = entry.get("row")
                if row is None:
                    source, table, row_id = identity
                    row = snapshots[identity]["row"]
                table = entry["table"]
                if table == "x_agent_analysis_jobs" or table in ("tasks", "newsflash_items", "x_capture_attempts", "search_event_candidates", "search_event_sources", "x_processing_attempts", "x_processing_attempt_events"):
                    target_columns = _target_columns(connection, schema, table)
                    _insert(connection, schema, table, row, target_columns, json_columns)
                elif table == "hottopic_inbox":
                    _insert(connection, schema, table, row, _target_columns(connection, schema, table), json_columns)
                else:
                    _insert(connection, schema, table, row, _target_columns(connection, schema, table),
                            ("judge_output", "search_result", "writer_output", "publisher_output", "push_result", "telegram_result", "input_json", "rule_json", "result_json"))
                report["counts"]["inserted"] += 1
            for seq_table in ("tasks", "newsflash_items", "x_capture_attempts", "search_event_candidates", "search_event_sources", "x_processing_attempts", "x_processing_attempt_events"):
                max_id = connection.execute(sql.SQL("SELECT COALESCE(MAX(id),0) AS n FROM main.{}").format(sql.Identifier(seq_table))).fetchone()["n"]
                connection.execute("SELECT setval(pg_get_serial_sequence(%s,'id'),%s,true)", (f"main.{seq_table}", max_id))
            max_hot = connection.execute("SELECT COALESCE(MAX(id),0) AS n FROM hottopic.x_agent_analysis_jobs").fetchone()["n"]
            connection.execute("SELECT setval(pg_get_serial_sequence('hottopic.x_agent_analysis_jobs','id'),%s,true)", (max_hot,))
            connection.commit()
        else:
            connection.rollback()
        report["complete"] = not conflicts
        return report
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.autocommit = previous_autocommit
        stack.close()


def _parse_source(values: list[str]) -> dict[str, Path]:
    out = {}
    for item in values:
        key, sep, path = item.partition("=")
        if not sep or key not in ("main", "hottopic") or key in out or not path:
            raise ValueError("--source must specify unique main=PATH and hottopic=PATH")
        out[key] = Path(path)
    return out


def _parse_bound(values: list[str]) -> dict[str, tuple[int, int]]:
    out = {}
    for item in values:
        key, sep, limits = item.partition("=")
        low, colon, high = limits.partition(":")
        if not sep or not colon or key not in ("main", "hottopic") or key in out:
            raise ValueError("--seq must specify unique DATABASE=LOWER:UPPER")
        out[key] = (int(low), int(high))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", default=[], metavar="DATABASE=PATH")
    parser.add_argument("--seq", action="append", default=[], metavar="DATABASE=LOWER:UPPER")
    parser.add_argument("--source-writes-stopped", action="store_true")
    parser.add_argument("--apply", action="store_true", help="commit after full conflict audit")
    parser.add_argument("--allow-pending-tasks", action="store_true",
                        help="preserve source pending states; acknowledges they become claimable in PostgreSQL")
    args = parser.parse_args(argv)
    try:
        report = reconcile(sources=_parse_source(args.source), bounds=_parse_bound(args.seq), apply=args.apply,
                            source_writes_stopped=args.source_writes_stopped,
                            allow_pending_tasks=args.allow_pending_tasks)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["complete"] else 2
    except Exception as exc:
        print(json.dumps({"complete": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
