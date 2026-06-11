#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def import_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except Exception as exc:  # pragma: no cover
        raise SystemExit("psycopg is required. Install backend requirements first.") from exc
    return psycopg, dict_row


@dataclass(frozen=True)
class Section:
    title: str
    rows: list[dict[str, Any]]


class Diagnostics:
    def __init__(self, dsn: str, *, statement_timeout_seconds: int, include_counts: bool) -> None:
        self.dsn = dsn
        self.statement_timeout_seconds = statement_timeout_seconds
        self.include_counts = include_counts
        self.psycopg, self.dict_row = import_psycopg()

    def run(self) -> list[Section]:
        with self.psycopg.connect(
            self.dsn,
            row_factory=self.dict_row,
            connect_timeout=10,
            application_name="odaily-supabase-readonly-diagnostics",
            autocommit=True,
        ) as conn:
            conn.execute(f"SET statement_timeout = '{self.statement_timeout_seconds}s'")
            conn.execute("SET idle_in_transaction_session_timeout = '20s'")
            conn.execute("SET default_transaction_read_only = on")

            sections = [
                Section("database_size", self.query_database_size(conn)),
                Section("connections_by_state", self.query_connections_by_state(conn)),
                Section("active_or_old_sessions", self.query_active_or_old_sessions(conn)),
                Section("largest_relations", self.query_largest_relations(conn)),
                Section("table_activity", self.query_table_activity(conn)),
                Section("largest_indexes", self.query_largest_indexes(conn)),
                Section("unused_large_indexes", self.query_unused_large_indexes(conn)),
                Section("candidate_large_columns", self.query_candidate_large_columns(conn)),
                Section("pg_stat_statements_by_total_time", self.query_pg_stat_statements(conn, "total_time")),
                Section("pg_stat_statements_by_calls", self.query_pg_stat_statements(conn, "calls")),
            ]
            if self.include_counts:
                sections.append(Section("retention_candidate_counts", self.query_retention_candidate_counts(conn)))
            return sections

    @staticmethod
    def fetchall(conn, sql: str, params: dict[str, Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        cur = conn.execute(sql, params or {})
        return [dict(row) for row in cur.fetchall()]

    def query_database_size(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                current_database() AS database,
                pg_size_pretty(pg_database_size(current_database())) AS database_size,
                pg_database_size(current_database()) AS database_bytes
            """,
        )

    def query_connections_by_state(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                COALESCE(application_name, '') AS application_name,
                state,
                wait_event_type,
                count(*) AS connections,
                max(now() - xact_start) AS max_xact_age,
                max(now() - query_start) AS max_query_age
            FROM pg_stat_activity
            WHERE datname = current_database()
            GROUP BY 1, 2, 3
            ORDER BY connections DESC, application_name
            LIMIT 30
            """,
        )

    def query_active_or_old_sessions(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                pid,
                usename,
                COALESCE(application_name, '') AS application_name,
                state,
                wait_event_type,
                wait_event,
                now() - xact_start AS xact_age,
                now() - query_start AS query_age,
                left(regexp_replace(query, '\\s+', ' ', 'g'), 300) AS query_sample
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND (state = 'active' OR xact_start < now() - interval '5 minutes')
            ORDER BY COALESCE(xact_start, query_start) ASC NULLS LAST
            LIMIT 25
            """,
        )

    def query_largest_relations(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                n.nspname AS schema,
                c.relname AS relation,
                c.relkind,
                pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
                pg_total_relation_size(c.oid) AS total_bytes,
                pg_size_pretty(pg_relation_size(c.oid)) AS heap_size,
                pg_size_pretty(pg_indexes_size(c.oid)) AS indexes_size,
                COALESCE(s.n_live_tup, 0) AS est_live_rows,
                COALESCE(s.n_dead_tup, 0) AS est_dead_rows,
                s.last_vacuum,
                s.last_autovacuum,
                s.last_analyze,
                s.last_autoanalyze
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
            WHERE n.nspname IN ('public', 'storage', 'auth')
              AND c.relkind IN ('r', 'm', 't')
            ORDER BY pg_total_relation_size(c.oid) DESC
            LIMIT 50
            """,
        )

    def query_table_activity(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                relname,
                n_live_tup,
                n_dead_tup,
                seq_scan,
                seq_tup_read,
                idx_scan,
                idx_tup_fetch,
                n_tup_ins,
                n_tup_upd,
                n_tup_del,
                n_tup_hot_upd,
                last_autovacuum,
                last_autoanalyze,
                vacuum_count,
                autovacuum_count
            FROM pg_stat_user_tables
            ORDER BY greatest(seq_tup_read, n_tup_ins + n_tup_upd + n_tup_del, n_dead_tup) DESC
            LIMIT 50
            """,
        )

    def query_largest_indexes(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                s.schemaname,
                s.relname AS tablename,
                s.indexrelname,
                pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
                pg_relation_size(indexrelid) AS index_bytes,
                idx_scan,
                idx_tup_read,
                idx_tup_fetch
            FROM pg_stat_user_indexes s
            ORDER BY pg_relation_size(indexrelid) DESC
            LIMIT 50
            """,
        )

    def query_unused_large_indexes(self, conn) -> list[dict[str, Any]]:
        return self.fetchall(
            conn,
            """
            SELECT
                s.schemaname,
                s.relname AS tablename,
                s.indexrelname,
                pg_size_pretty(pg_relation_size(indexrelid)) AS index_size,
                pg_relation_size(indexrelid) AS index_bytes,
                idx_scan
            FROM pg_stat_user_indexes s
            WHERE idx_scan = 0
              AND pg_relation_size(indexrelid) > 1024 * 1024
            ORDER BY pg_relation_size(indexrelid) DESC
            LIMIT 50
            """,
        )

    def query_candidate_large_columns(self, conn) -> list[dict[str, Any]]:
        column_names = (
            "raw_payload",
            "metadata",
            "raw_output",
            "audit_result",
            "ai_result",
            "publisher_output",
            "push_result",
            "telegram_result",
            "result_json",
            "extra_json",
        )
        return self.fetchall(
            conn,
            """
            SELECT
                table_schema,
                table_name,
                column_name,
                data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND column_name = ANY(%(column_names)s)
            ORDER BY table_name, column_name
            """,
            {"column_names": list(column_names)},
        )

    def query_pg_stat_statements(self, conn, order: str) -> list[dict[str, Any]]:
        cols = self.fetchall(
            conn,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'pg_stat_statements'
            """,
        )
        colset = {str(row["column_name"]) for row in cols}
        total_time = "total_exec_time" if "total_exec_time" in colset else "total_time"
        mean_time = "mean_exec_time" if "mean_exec_time" in colset else "mean_time"
        if total_time not in colset:
            return [{"available": False, "reason": "pg_stat_statements view not visible"}]

        extra = [col for col in ["rows", "shared_blks_hit", "shared_blks_read", "temp_blks_written", "wal_bytes"] if col in colset]
        extra_sql = ", " + ", ".join(extra) if extra else ""
        order_sql = "calls DESC" if order == "calls" else f"{total_time} DESC"
        return self.fetchall(
            conn,
            f"""
            SELECT
                calls,
                round({total_time}::numeric, 2) AS total_ms,
                round({mean_time}::numeric, 2) AS mean_ms
              {extra_sql},
                left(regexp_replace(query, '\\s+', ' ', 'g'), 700) AS query_sample
            FROM pg_stat_statements
            WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND query NOT ILIKE %(excluded_query)s
            ORDER BY {order_sql}
            LIMIT 20
            """,
            {"excluded_query": "%pg_stat_statements%"},
        )

    def query_retention_candidate_counts(self, conn) -> list[dict[str, Any]]:
        specs = [
            ("x_capture_attempts", "started_at", "7 days"),
            ("editor_plugin_generation_logs", "created_at", "7 days"),
            ("editor_plugin_receipts", "created_at", "7 days"),
            ("editor_plugin_feedbacks", "created_at", "90 days"),
            ("whale_watch_activities", "created_at", "7 days"),
            ("whale_watch_hyperliquid_activities", "created_at", "7 days"),
            ("pipeline_worker_heartbeats", "updated_at", "7 days"),
        ]
        rows: list[dict[str, Any]] = []
        for table, column, retention in specs:
            exists = self.fetchall(conn, "SELECT to_regclass(%s) AS table_oid", (f"public.{table}",))[0]["table_oid"]
            if exists is None:
                continue
            try:
                result = self.fetchall(
                    conn,
                    f"""
                    SELECT
                        %(table)s AS table_name,
                        %(column)s AS cutoff_column,
                        %(retention)s AS retention,
                        count(*) AS rows_older_than_retention,
                        min({column}) AS oldest_at,
                        max({column}) AS newest_at
                    FROM public.{table}
                    WHERE {column} < now() - (%(retention)s)::interval
                    """,
                    {"table": table, "column": column, "retention": retention},
                )
                rows.extend(result)
            except Exception as exc:
                rows.append({"table_name": table, "error": str(exc)})
        return rows


def to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def print_markdown(sections: list[Section]) -> None:
    for section in sections:
        print(f"\n## {section.title}")
        if not section.rows:
            print("(none)")
            continue
        for row in section.rows:
            safe_row = {key: to_jsonable(value) for key, value in row.items()}
            print(json.dumps(safe_row, ensure_ascii=False, sort_keys=True))


def print_json(sections: list[Section]) -> None:
    payload = {
        section.title: [{key: to_jsonable(value) for key, value in row.items()} for row in section.rows]
        for section in sections
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run read-only Supabase/Postgres diagnostics for OdAIly.")
    parser.add_argument("--database-url", help="Override SUPABASE_DB_URL/DATABASE_URL.")
    parser.add_argument("--env-file", default=".env", help="Env file to load before reading SUPABASE_DB_URL.")
    parser.add_argument("--statement-timeout-seconds", type=int, default=15)
    parser.add_argument(
        "--include-counts",
        action="store_true",
        help="Run exact retention candidate counts. These are read-only but may scan large tables.",
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_env_file(Path(args.env_file))
    dsn = args.database_url or os.getenv("SUPABASE_DB_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("Missing SUPABASE_DB_URL or DATABASE_URL.")

    diagnostics = Diagnostics(
        dsn,
        statement_timeout_seconds=max(1, int(args.statement_timeout_seconds)),
        include_counts=bool(args.include_counts),
    )
    sections = diagnostics.run()
    if args.format == "json":
        print_json(sections)
    else:
        print_markdown(sections)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
