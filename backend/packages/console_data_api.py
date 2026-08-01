from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ALLOWED_TABLES = {
    "jin10_settings", "newsflash_event_favorites", "newsflash_event_notes",
    "newsflash_event_sources", "newsflash_event_summary", "newsflash_item_notes",
    "newsflash_items", "non_mainstream_media_settings", "non_mainstream_media_sources",
    "prompt_template_versions", "prompt_templates", "publisher_channels",
    "publisher_rule_config", "publisher_settings", "source_exclusion_rule_groups", "tasks",
    "whale_watch_activities", "whale_watch_addresses", "whale_watch_chain_states",
    "whale_watch_hyperliquid_activities", "whale_watch_hyperliquid_addresses",
    "whale_watch_hyperliquid_settings", "whale_watch_hyperliquid_states",
    "x_capture_accounts", "x_capture_attempts", "x_capture_settings",
}
FILTER_OPERATORS = {"eq": "=", "neq": "!=", "gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}


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


def _encode(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, bool):
        return int(value)
    return value


class ConsoleDataApi:
    def __init__(self, path: Path) -> None:
        self.path = path

    def execute(self, payload: dict[str, Any]) -> Any:
        table = _identifier(payload.get("table"))
        if table not in ALLOWED_TABLES:
            raise ValueError("table is not exposed to the console")
        operation = str(payload.get("operation") or "select")
        if operation == "select":
            return self._select(table, payload)
        if operation in {"insert", "upsert", "update", "delete"}:
            return self._mutate(table, operation, payload)
        raise ValueError("unsupported console data operation")

    def _where(self, payload: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for item in payload.get("filters") or []:
            column = _identifier(item.get("column"))
            op = str(item.get("op"))
            value = item.get("value")
            if op in FILTER_OPERATORS:
                clauses.append(f"{column} {FILTER_OPERATORS[op]} ?")
                params.append(_encode(value))
            elif op == "in":
                values = list(value or [])
                if not values:
                    clauses.append("0")
                else:
                    clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                    params.extend(_encode(entry) for entry in values)
            elif op == "is":
                if value is None:
                    clauses.append(f"{column} IS NULL")
                else:
                    clauses.append(f"{column} IS ?")
                    params.append(_encode(value))
            else:
                raise ValueError("unsupported console filter")
        return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", params)

    def _select(self, table: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        raw_select = str(payload.get("select") or "*")
        include_pipeline = table == "tasks" and "x_task_pipeline(" in raw_select
        base_select = raw_select.split(",x_task_pipeline(", 1)[0] if include_pipeline else raw_select
        columns = "*" if base_select == "*" else ",".join(_identifier(value.strip()) for value in base_select.split(",") if value.strip())
        where, params = self._where(payload)
        order_parts = []
        for item in payload.get("orders") or []:
            order_parts.append(f"{_identifier(item.get('column'))} {'ASC' if item.get('ascending', True) else 'DESC'}")
        sql = f"SELECT {columns} FROM {table}{where}"
        if order_parts:
            sql += " ORDER BY " + ",".join(order_parts)
        offset = max(0, int(payload.get("offset") or 0))
        limit = payload.get("limit")
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend([max(0, int(limit)), offset])
        with connect_sqlite(self.path) as conn:
            rows = [_decode_row(row) for row in conn.execute(sql, params).fetchall()]
            if include_pipeline:
                for row in rows:
                    pipeline = conn.execute("SELECT * FROM x_task_pipeline WHERE task_id=?", (row["id"],)).fetchone()
                    row["x_task_pipeline"] = _decode_row(pipeline) if pipeline else None
        return rows

    def _mutate(self, table: str, operation: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
        where, where_params = self._where(payload)
        data = payload.get("data")
        with connect_sqlite(self.path) as conn:
            if operation == "delete":
                existing = [_decode_row(row) for row in conn.execute(f"SELECT * FROM {table}{where}", where_params).fetchall()]
                conn.execute(f"DELETE FROM {table}{where}", where_params)
                conn.commit()
                return existing
            records = data if isinstance(data, list) else [data]
            if not records or not all(isinstance(record, dict) for record in records):
                raise ValueError("mutation data must be an object or array")
            if operation == "update":
                if len(records) != 1:
                    raise ValueError("update accepts one object")
                record = records[0]
                columns = [_identifier(key) for key in record]
                conn.execute(
                    f"UPDATE {table} SET {','.join(f'{key}=?' for key in columns)}{where}",
                    [_encode(record[key]) for key in columns] + where_params,
                )
                rows = [_decode_row(row) for row in conn.execute(f"SELECT * FROM {table}{where}", where_params).fetchall()]
            else:
                conflict = str(payload.get("on_conflict") or "")
                rows: list[dict[str, Any]] = []
                for record in records:
                    columns = [_identifier(key) for key in record]
                    placeholders = ",".join("?" for _ in columns)
                    sql = f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})"
                    if operation == "upsert":
                        conflict_columns = [_identifier(value.strip()) for value in conflict.split(",") if value.strip()]
                        if not conflict_columns:
                            raise ValueError("upsert requires on_conflict")
                        updates = [key for key in columns if key not in conflict_columns]
                        sql += f" ON CONFLICT({','.join(conflict_columns)}) DO UPDATE SET " + ",".join(f"{key}=excluded.{key}" for key in updates)
                    cursor = conn.execute(sql, [_encode(record[key]) for key in columns])
                    if operation == "insert":
                        returned = conn.execute(f"SELECT * FROM {table} WHERE rowid=?", (cursor.lastrowid,)).fetchone()
                    else:
                        conflict_columns = [_identifier(value.strip()) for value in conflict.split(",") if value.strip()]
                        conflict_where = " AND ".join(f"{key}=?" for key in conflict_columns)
                        returned = conn.execute(
                            f"SELECT * FROM {table} WHERE {conflict_where}",
                            [_encode(record[key]) for key in conflict_columns],
                        ).fetchone()
                    if returned is not None:
                        rows.append(_decode_row(returned))
            conn.commit()
            return rows
