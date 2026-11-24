from __future__ import annotations

import json
import re
import sqlite3
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite
from packages.common.storage import load_storage_settings


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_TABLES = {
    "binance_square_accounts", "binance_square_attempts", "binance_square_settings",
    "console_admins", "jin10_settings",
    "newsflash_items", "non_mainstream_media_settings", "non_mainstream_media_sources",
    "prompt_template_versions", "prompt_templates", "publisher_channels",
    "publisher_rule_config", "publisher_settings", "source_exclusion_rule_groups", "tasks",
    "whale_watch_activities", "whale_watch_addresses", "whale_watch_chain_states",
    "x_capture_accounts", "x_capture_attempts", "x_capture_settings",
    "telegram_channel_names",
}
FILTER_OPERATORS = {"eq": "=", "neq": "!=", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}
READ_ONLY_TABLES = {"console_admins", "binance_square_attempts"}
CONSOLE_ADMIN_COLUMNS = {"email", "created_at", "updated_at"}


def _identifier(value: Any) -> str:
    text = str(value or "")
    if not IDENTIFIER.fullmatch(text):
        raise ValueError("invalid SQL identifier")
    return text


def _decode_row(row: Any) -> dict[str, Any]:
    result = dict(row)
    for key, value in result.items():
        if not isinstance(value, str) or not value or value[0] not in "[{":
            continue
        try:
            result[key] = json.loads(value)
        except json.JSONDecodeError:
            pass
    return result


def _encode(value: Any, *, postgres: bool = False) -> Any:
    if isinstance(value, (dict, list)):
        if postgres:
            from psycopg.types.json import Jsonb

            return Jsonb(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool) and not postgres:
        return int(value)
    return value


class ConsoleDataApi:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.backend = load_storage_settings().backend
        self._postgres = None
        if self.backend == "postgres":
            from packages.common.pg_native import connect_postgres

            self._postgres = connect_postgres

    def _connect(self):
        if self.backend == "postgres":
            return self._postgres(schema="main", application_name="odaily-console-data")
        return connect_sqlite(self.path or load_storage_settings().sqlite_path)

    def execute(self, payload: dict[str, Any]) -> Any:
        table = _identifier(payload.get("table"))
        if table not in ALLOWED_TABLES:
            raise ValueError("table is not exposed to the console")
        operation = str(payload.get("operation") or "select")
        if operation == "select":
            return self._select(table, payload)
        if operation in {"insert", "upsert", "update", "delete"}:
            if table in READ_ONLY_TABLES:
                raise ValueError("table is read-only in the console")
            return self._mutate(table, operation, payload)
        raise ValueError("unsupported console data operation")

    def _where(self, payload: dict[str, Any], *, allowed_columns: set[str] | None = None) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for item in payload.get("filters") or []:
            column = _identifier(item.get("column"))
            if allowed_columns is not None and column not in allowed_columns:
                raise ValueError("column is not exposed to the console")
            op = str(item.get("op"))
            value = item.get("value")
            if op in FILTER_OPERATORS:
                placeholder = "%s" if self.backend == "postgres" else "?"
                clauses.append(f"{column} {FILTER_OPERATORS[op]} {placeholder}")
                params.append(_encode(value, postgres=self.backend == "postgres"))
            elif op == "in":
                values = list(value or [])
                if not values:
                    clauses.append("FALSE" if self.backend == "postgres" else "0")
                else:
                    placeholder = "%s" if self.backend == "postgres" else "?"
                    clauses.append(f"{column} IN ({','.join(placeholder for _ in values)})")
                    params.extend(_encode(entry, postgres=self.backend == "postgres") for entry in values)
            elif op == "is":
                if value is None:
                    clauses.append(f"{column} IS NULL")
                else:
                    clauses.append(f"{column} IS NOT DISTINCT FROM %s" if self.backend == "postgres" else f"{column} IS ?")
                    params.append(_encode(value, postgres=self.backend == "postgres"))
            else:
                raise ValueError("unsupported console filter")
        return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)

    def _select(self, table: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_select = str(payload.get("select") or "*")
        include_pipeline = table == "tasks" and "x_task_pipeline(" in raw_select
        base_select = raw_select.split(",x_task_pipeline(", 1)[0] if include_pipeline else raw_select
        admin_projection = table == "console_admins" and self.backend == "postgres"
        if admin_projection:
            if base_select == "*":
                columns = ",".join(sorted(CONSOLE_ADMIN_COLUMNS))
            else:
                selected_columns = [_identifier(value.strip()) for value in base_select.split(",") if value.strip()]
                if any(column not in CONSOLE_ADMIN_COLUMNS for column in selected_columns):
                    raise ValueError("column is not exposed to the console")
                columns = ",".join(selected_columns)
        else:
            columns = "*" if base_select == "*" else ",".join(_identifier(value.strip()) for value in base_select.split(",") if value.strip())
        where, params = self._where(payload, allowed_columns=CONSOLE_ADMIN_COLUMNS if admin_projection else None)
        source_table = "local_auth_users" if admin_projection else table
        if admin_projection:
            where = " WHERE enabled = true" + (f" AND {where[len(' WHERE '):]}" if where else "")
        order_parts = []
        for item in payload.get("orders") or []:
            column = _identifier(item.get("column"))
            if admin_projection and column not in CONSOLE_ADMIN_COLUMNS:
                raise ValueError("column is not exposed to the console")
            # Tasks contain both SQLite CURRENT_TIMESTAMP values and ISO-8601
            # values with a `T` separator. Normalize them before pagination.
            expression = "datetime(created_at)" if self.backend == "sqlite" and table == "tasks" and column == "created_at" else column
            order_parts.append(f"{expression} {'ASC' if item.get('ascending', True) else 'DESC'}")
        sql = f"SELECT {columns} FROM {source_table}{where}"
        if order_parts:
            sql += " ORDER BY " + ",".join(order_parts)
        offset = max(0, int(payload.get("offset") or 0))
        limit = payload.get("limit")
        if limit is not None:
            sql += " LIMIT %s OFFSET %s" if self.backend == "postgres" else " LIMIT ? OFFSET ?"
            params.extend([max(0, int(limit)), offset])
        with self._connect() as conn:
            rows = [_decode_row(row) for row in conn.execute(sql, params).fetchall()]
            if include_pipeline:
                for row in rows:
                    marker = "%s" if self.backend == "postgres" else "?"
                    pipeline = conn.execute(f"SELECT * FROM x_task_pipeline WHERE task_id={marker}", (row["id"],)).fetchone()
                    decoded_pipeline = _decode_row(pipeline) if pipeline else None
                    if decoded_pipeline is not None:
                        self._hydrate_duplicate_target(conn, decoded_pipeline)
                    row["x_task_pipeline"] = decoded_pipeline
        return rows

    def _hydrate_duplicate_target(self, conn: Any, pipeline: dict[str, Any]) -> None:
        search_result = pipeline.get("search_result")
        if not isinstance(search_result, dict) or not search_result.get("is_duplicate"):
            return
        existing = search_result.get("duplicate_target")
        target = dict(existing) if isinstance(existing, dict) else {}
        target_type = str(target.get("target_type") or search_result.get("duplicate_target_type") or "")
        target_id = str(target.get("target_id") or search_result.get("duplicate_target_id") or "")
        if not target_type or not target_id:
            return
        target.setdefault("target_type", target_type)
        target.setdefault("target_id", target_id)
        observed_matches = search_result.get("observed_matches")
        if isinstance(observed_matches, list):
            observed = next(
                (
                    item
                    for item in observed_matches
                    if isinstance(item, dict)
                    and str(item.get("target_type") or "") == target_type
                    and str(item.get("target_id") or "") == target_id
                ),
                None,
            )
            if observed is not None:
                for key in ("candidate_id", "title", "source_url", "similarity"):
                    if observed.get(key) is not None:
                        target[key] = observed[key]
        if target.get("title"):
            search_result["duplicate_target"] = target
            return

        resolved: Any | None = None
        marker = "%s" if self.backend == "postgres" else "?"
        try:
            if target_type == "odaily_published":
                resolved = conn.execute(
                    f"SELECT source_item_id AS target_id, title, source_url FROM odaily_reference_items WHERE source_item_id={marker}",
                    (target_id,),
                ).fetchone()
            elif target_type in {"inflight_candidate", "recent_processed"}:
                resolved = conn.execute(
                    f"""
                    SELECT c.id AS target_id, c.title,
                           COALESCE(es.source_url, t.source_url) AS source_url
                    FROM search_event_candidates c
                    LEFT JOIN search_event_sources es
                      ON es.candidate_id = c.id AND es.role = 'primary'
                    LEFT JOIN tasks t ON t.id = c.primary_task_id
                    WHERE c.id={marker}
                    ORDER BY es.id DESC
                    LIMIT 1
                    """,
                    (int(target_id),),
                ).fetchone()
        except (TypeError, ValueError, sqlite3.Error, RuntimeError):
            resolved = None
        if resolved is not None:
            for key in ("target_id", "title", "source_url"):
                if resolved[key] is not None:
                    target[key] = str(resolved[key])
            if target_type in {"inflight_candidate", "recent_processed"}:
                target.setdefault("candidate_id", int(target_id))
        search_result["duplicate_target"] = target
        pipeline["search_result"] = search_result

    def _mutate(self, table: str, operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        if table == "telegram_channel_names":
            data = payload.get("data")
            filters = payload.get("filters") or []
            if operation != "update" or not isinstance(data, dict) or set(data) != {"write_name"}:
                raise ValueError("only Telegram writing names are editable")
            if not isinstance(data["write_name"], str) or not data["write_name"].strip() or len(data["write_name"]) > 100:
                raise ValueError("invalid Telegram writing name")
            if len(filters) != 1 or filters[0].get("column") != "channel_key" or filters[0].get("op") != "eq" or filters[0].get("value") not in {"vida", "durov"}:
                raise ValueError("invalid Telegram channel")
        where, where_params = self._where(payload)
        data = payload.get("data")
        if table == "binance_square_settings" and operation == "delete":
            raise ValueError("binance square settings cannot be deleted")
        with self._connect() as conn:
            transaction = conn.transaction() if self.backend == "postgres" else nullcontext()
            with transaction:
                return self._mutate_in_connection(conn, table, operation, payload, where, where_params, data)

    def _mutate_in_connection(self, conn, table, operation, payload, where, where_params, data):
        marker = "%s" if self.backend == "postgres" else "?"

        def placeholders(count: int) -> str:
            return ",".join(marker for _ in range(count))

        if operation == "delete":
            existing = [
                _decode_row(row)
                for row in conn.execute(f"SELECT * FROM {table}{where}", where_params).fetchall()
            ]
            conn.execute(f"DELETE FROM {table}{where}", where_params)
            return existing

        records = data if isinstance(data, list) else [data]
        if not records or not all(isinstance(record, dict) for record in records):
            raise ValueError("mutation data must be an object or array")
        self._validate_binance_square_mutation(table, operation, records)

        if operation == "update":
            if len(records) != 1:
                raise ValueError("update accepts one object")
            record = records[0]
            columns = [_identifier(key) for key in record]
            conn.execute(
                f"UPDATE {table} SET {','.join(f'{key}={marker}' for key in columns)}{where}",
                [_encode(record[key], postgres=self.backend == "postgres") for key in columns] + where_params,
            )
            return [
                _decode_row(row)
                for row in conn.execute(f"SELECT * FROM {table}{where}", where_params).fetchall()
            ]

        conflict = str(payload.get("on_conflict") or "")
        rows: list[dict[str, Any]] = []
        for record in records:
            columns = [_identifier(key) for key in record]
            sql = f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders(len(columns))})"
            conflict_columns: list[str] = []
            if operation == "upsert":
                conflict_columns = [_identifier(value.strip()) for value in conflict.split(",") if value.strip()]
                if not conflict_columns:
                    raise ValueError("upsert requires on_conflict")
                updates = [key for key in columns if key not in conflict_columns]
                if not updates:
                    sql += f" ON CONFLICT({','.join(conflict_columns)}) DO NOTHING"
                else:
                    assignments = ",".join(f"{key}=excluded.{key}" for key in updates)
                    sql += f" ON CONFLICT({','.join(conflict_columns)}) DO UPDATE SET {assignments}"
            if self.backend == "postgres":
                sql += " RETURNING *"
            cursor = conn.execute(
                sql,
                [_encode(record[key], postgres=self.backend == "postgres") for key in columns],
            )
            if self.backend == "postgres":
                returned = cursor.fetchone()
            elif operation == "insert":
                returned = conn.execute(f"SELECT * FROM {table} WHERE rowid=?", (cursor.lastrowid,)).fetchone()
            else:
                conflict_where = " AND ".join(f"{key}=?" for key in conflict_columns)
                returned = conn.execute(
                    f"SELECT * FROM {table} WHERE {conflict_where}",
                    [_encode(record[key]) for key in conflict_columns],
                ).fetchone()
            if returned is not None:
                rows.append(_decode_row(returned))
        return rows

    @staticmethod
    def _validate_binance_square_mutation(table: str, operation: str, records: list[dict[str, Any]]) -> None:
        if table == "binance_square_settings":
            allowed = {"singleton_key", "enabled", "updated_at"}
            if any(set(record) - allowed for record in records):
                raise ValueError("only the Binance Square enabled switch is editable")
            return
        if table != "binance_square_accounts":
            return
        allowed = (
            {"slug", "slug_lower", "profile_url", "enabled", "updated_at"}
            if operation in {"insert", "upsert"}
            else {"display_name", "write_name", "enabled", "updated_at"}
        )
        if any(set(record) - allowed for record in records):
            raise ValueError("unsupported Binance Square account field")
        if operation not in {"insert", "upsert"}:
            return
        from packages.binance_square.client import normalize_profile_url

        for record in records:
            slug, profile_url = normalize_profile_url(str(record.get("profile_url") or ""))
            record["slug"] = slug
            record["slug_lower"] = slug.lower()
            record["profile_url"] = profile_url
