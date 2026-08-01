from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite, load_storage_settings

from .models import AuditorTask
from .repository import calculate_content_hash


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class SQLiteAuditorRepository:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or load_storage_settings().sqlite_path

    def init_schema(self) -> None:
        with connect_sqlite(self.path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS auditor_checks (
                    id integer PRIMARY KEY AUTOINCREMENT,
                    source_item_id text NOT NULL, source_url text, title text, content text NOT NULL,
                    content_hash text NOT NULL, published_at text, prompt_version text NOT NULL,
                    status text NOT NULL DEFAULT 'pending', model text, raw_output text,
                    audit_result text NOT NULL DEFAULT '{}', telegram_text text,
                    telegram_result text NOT NULL DEFAULT '{}', alerted_at text,
                    locked_by text, locked_until text, attempt_count integer NOT NULL DEFAULT 0,
                    last_error text, metadata text NOT NULL DEFAULT '{}',
                    created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_item_id, content_hash, prompt_version)
                );
                CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (
                    component text NOT NULL, worker_id text NOT NULL, status text NOT NULL,
                    last_seen_at text NOT NULL, last_success_at text, last_error text,
                    metadata text NOT NULL DEFAULT '{}', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(component, worker_id)
                );
                """
            )
            conn.commit()

    def claim_task(self, *, worker_id: str, prompt_version: str, lookback_minutes: int, lock_seconds: int = 300) -> AuditorTask | None:
        self.init_schema()
        now = datetime.now(UTC)
        cutoff = _iso(now - timedelta(minutes=lookback_minutes))
        lock_until = _iso(now + timedelta(seconds=lock_seconds))
        with connect_sqlite(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidates = conn.execute(
                "SELECT source_item_id, source_url, title, content, published_at, metadata "
                "FROM odaily_reference_items WHERE content <> '' AND published_at IS NOT NULL "
                "AND published_at >= ? AND published_at <= ? ORDER BY published_at, source_item_id",
                (cutoff, _iso(now)),
            ).fetchall()
            selected = None
            content_hash = ""
            for row in candidates:
                content_hash = calculate_content_hash(row["title"], row["content"])
                existing = conn.execute(
                    "SELECT id, status, locked_until FROM auditor_checks WHERE source_item_id=? AND content_hash=? AND prompt_version=?",
                    (row["source_item_id"], content_hash, prompt_version),
                ).fetchone()
                if existing is None or (
                    existing["status"] in {"pending", "processing", "failed"}
                    and (not existing["locked_until"] or existing["locked_until"] < _iso(now))
                ):
                    selected = row
                    break
            if selected is None:
                conn.commit()
                return None
            metadata = selected["metadata"] or "{}"
            conn.execute(
                """INSERT INTO auditor_checks
                (source_item_id,source_url,title,content,content_hash,published_at,prompt_version,status,locked_by,locked_until,attempt_count,metadata)
                VALUES (?,?,?,?,?,?,?,'processing',?,?,1,?)
                ON CONFLICT(source_item_id,content_hash,prompt_version) DO UPDATE SET
                source_url=excluded.source_url,title=excluded.title,content=excluded.content,published_at=excluded.published_at,
                status='processing',locked_by=excluded.locked_by,locked_until=excluded.locked_until,
                attempt_count=auditor_checks.attempt_count+1,last_error=NULL,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP""",
                (selected["source_item_id"], selected["source_url"], selected["title"], selected["content"], content_hash,
                 selected["published_at"], prompt_version, worker_id, lock_until, metadata),
            )
            check = conn.execute(
                "SELECT id FROM auditor_checks WHERE source_item_id=? AND content_hash=? AND prompt_version=?",
                (selected["source_item_id"], content_hash, prompt_version),
            ).fetchone()
            conn.commit()
        return AuditorTask(id=int(check["id"]), source_item_id=str(selected["source_item_id"]), source_url=selected["source_url"],
                           title=selected["title"], content=str(selected["content"]), content_hash=content_hash,
                           published_at=_dt(selected["published_at"]), metadata=json.loads(metadata))

    def complete_passed(self, task: AuditorTask, *, model: str, prompt_version: str, raw_output: str, result: dict[str, Any]) -> None:
        self._complete(task, "passed", model, prompt_version, raw_output, result, None, {})

    def complete_flagged(self, task: AuditorTask, *, model: str, prompt_version: str, raw_output: str, result: dict[str, Any], telegram_text: str, telegram_result: dict[str, Any]) -> None:
        self._complete(task, "flagged", model, prompt_version, raw_output, result, telegram_text, telegram_result)

    def _complete(self, task: AuditorTask, status: str, model: str, prompt_version: str, raw_output: str,
                  result: dict[str, Any], telegram_text: str | None, telegram_result: dict[str, Any]) -> None:
        error = None if telegram_result.get("ok", True) else telegram_result.get("error")
        with connect_sqlite(self.path) as conn:
            conn.execute("""UPDATE auditor_checks SET status=?,model=?,prompt_version=?,raw_output=?,audit_result=?,telegram_text=?,
                         telegram_result=?,alerted_at=CASE WHEN ?='flagged' THEN CURRENT_TIMESTAMP ELSE alerted_at END,
                         locked_by=NULL,locked_until=NULL,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                         (status, model, prompt_version, raw_output, json.dumps(result, ensure_ascii=False), telegram_text,
                          json.dumps(telegram_result, ensure_ascii=False), status, error, task.id))
            conn.commit()

    def complete_failed(self, task: AuditorTask, *, error: str) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE auditor_checks SET status='failed',last_error=?,locked_by=NULL,locked_until=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=?", (error[:2000], task.id))
            conn.commit()

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool,
                                error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        now = _iso(datetime.now(UTC))
        with connect_sqlite(self.path) as conn:
            conn.execute("""INSERT INTO pipeline_worker_heartbeats(component,worker_id,status,last_seen_at,last_success_at,last_error,metadata)
                         VALUES(?,?,?,?,?,?,?) ON CONFLICT(component,worker_id) DO UPDATE SET status=excluded.status,
                         last_seen_at=excluded.last_seen_at,last_success_at=CASE WHEN ? THEN excluded.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END,
                         last_error=excluded.last_error,metadata=excluded.metadata,updated_at=CURRENT_TIMESTAMP""",
                         (component, worker_id, status, now, now if success else None, error, json.dumps(metadata or {}, ensure_ascii=False), success))
            conn.commit()


def create_auditor_repository(database_url: str | None = None) -> SQLiteAuditorRepository:
    del database_url
    return SQLiteAuditorRepository(load_storage_settings().sqlite_path)
