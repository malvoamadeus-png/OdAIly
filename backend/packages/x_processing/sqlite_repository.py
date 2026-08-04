from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite

from .models import (
    ACTIVE_CANDIDATE_TTL,
    AI_SOURCE,
    JIN10_SOURCE,
    NEWS_TYPES,
    PROCESSING_SOURCES,
    SEARCH_FIRST_SOURCES,
    STAGE_SPECS,
    WRITE_STAGE_SOURCES,
    NewsType,
    ProcessingStage,
    PromptTemplateVersion,
    TaskRecord,
)
from .repository import (
    CRYPTO_SEARCH_FIRST_SOURCES,
    DEFAULT_FEATURE_MODE_TEXT,
    LEGACY_SKIP_SOURCES,
    LEGACY_SKIP_UNFINISHED_STATUSES,
    PROMPT_FEATURE_MODE_DEFAULTS,
    PROMPT_SEEDS,
    PUBLISHER_CHANNEL_DEFAULTS,
    PUBLISHER_SETTINGS_DEFAULT,
    _row_to_pipeline,
    _row_to_prompt,
    _row_to_publisher_channel,
    _row_to_publisher_settings,
    _row_to_task,
)
from .searcher import SearchDocument, content_hash


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    decoded = json.loads(str(value))
    return decoded if isinstance(decoded, dict) else {}


def _dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


JSON_COLUMNS = {
    "raw_payload", "metadata", "judge_output", "search_result", "writer_output",
    "publisher_output", "push_result", "telegram_result", "config_json",
}
DATETIME_COLUMNS = {
    "published_at", "created_at", "updated_at", "locked_until", "judge_completed_at",
    "search_completed_at", "write_completed_at", "format_completed_at",
    "publisher_decided_at", "publish_completed_at", "expires_at", "deleted_at",
}


def _record(row: Any) -> dict[str, Any]:
    data = dict(row)
    for key in JSON_COLUMNS & data.keys():
        data[key] = _decode(data[key])
    for key in DATETIME_COLUMNS & data.keys():
        data[key] = _dt(data[key])
    return data


class SQLiteXProcessingRepository:
    """SQLite implementation of the processing state machine for one Linux host."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.init_schema()

    def _connect(self):
        return connect_sqlite(self.path)

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SQLITE_SCHEMA_SQL)
            self._ensure_publisher_defaults(conn)
            conn.commit()

    def _ensure_publisher_defaults(self, conn) -> None:
        settings = PUBLISHER_SETTINGS_DEFAULT
        conn.execute(
            "INSERT OR IGNORE INTO publisher_settings(singleton_key, enabled, timezone, window_start_local, window_end_local) VALUES (?, ?, ?, ?, ?)",
            (settings["singleton_key"], int(settings["enabled"]), settings["timezone"], settings["window_start_local"], settings["window_end_local"]),
        )
        for key, name, enabled in PUBLISHER_CHANNEL_DEFAULTS:
            conn.execute(
                "INSERT INTO publisher_channels(channel_key, display_name, enabled) VALUES (?, ?, ?) ON CONFLICT(channel_key) DO UPDATE SET display_name=excluded.display_name, updated_at=CURRENT_TIMESTAMP",
                (key, name, int(enabled)),
            )

    def clear_old_pending_x_tasks(self) -> int:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE source='x' AND status='pending'")
            conn.commit()
            return cur.rowcount

    def seed_prompt_templates(self, *, root_dir: Path) -> None:
        with self._connect() as conn:
            for key, (name, relative_path, note) in PROMPT_SEEDS.items():
                content = (root_dir / relative_path).read_text(encoding="utf-8")
                conn.execute(
                    "INSERT INTO prompt_templates(template_key, display_name, feature_mode_enabled, feature_mode_text) VALUES (?, ?, ?, ?) ON CONFLICT(template_key) DO UPDATE SET display_name=excluded.display_name, feature_mode_text=CASE WHEN prompt_templates.feature_mode_text='' THEN excluded.feature_mode_text ELSE prompt_templates.feature_mode_text END, updated_at=CURRENT_TIMESTAMP",
                    (key, name, int(PROMPT_FEATURE_MODE_DEFAULTS.get(key, False)), DEFAULT_FEATURE_MODE_TEXT),
                )
                active = conn.execute("SELECT active_version_id FROM prompt_templates WHERE template_key=?", (key,)).fetchone()
                if active and active["active_version_id"]:
                    continue
                conn.execute(
                    "INSERT INTO prompt_template_versions(template_key, version_number, content, note, published_at) VALUES (?, 1, ?, ?, ?) ON CONFLICT(template_key, version_number) DO UPDATE SET content=excluded.content, note=excluded.note, published_at=COALESCE(prompt_template_versions.published_at, excluded.published_at)",
                    (key, content, note, _iso()),
                )
                version = conn.execute("SELECT id FROM prompt_template_versions WHERE template_key=? AND version_number=1", (key,)).fetchone()
                conn.execute("UPDATE prompt_templates SET active_version_id=?, updated_at=CURRENT_TIMESTAMP WHERE template_key=?", (version["id"], key))
            conn.commit()

    def _eligible(self, stage: ProcessingStage, task: dict[str, Any], is_ai_source: bool) -> bool:
        source, status = str(task["source"]), str(task["status"])
        spec = STAGE_SPECS[stage]
        retryable_statuses = {spec.processing_status, spec.failure_status}
        if source not in (WRITE_STAGE_SOURCES if stage in {"write", "format_publish", "publish"} else PROCESSING_SOURCES):
            return False
        if stage == "judge_crypto":
            return (
                (source == "x" and not is_ai_source and (status == "pending" or status in retryable_statuses))
                or (source in CRYPTO_SEARCH_FIRST_SOURCES and (status == "searched" or status in retryable_statuses))
            )
        if stage == "judge_ai":
            return (
                (source == "x" and is_ai_source and (status == "pending" or status in retryable_statuses))
                or (source == AI_SOURCE and (status == "searched" or status in retryable_statuses))
            )
        if stage == "judge_jin10":
            return source == JIN10_SOURCE and status in {"pending", *retryable_statuses}
        if stage == "judge":
            return status in {spec.claim_status, *retryable_statuses} or (source in SEARCH_FIRST_SOURCES and status in {"searched", *retryable_statuses})
        if stage == "search":
            return status in {spec.claim_status, *retryable_statuses} or (source in SEARCH_FIRST_SOURCES and status in {"pending", *retryable_statuses})
        return status in {spec.claim_status, *retryable_statuses}

    def claim_task(self, stage: ProcessingStage, *, worker_id: str, lock_seconds: int = 300) -> TaskRecord | None:
        return self._claim_task(stage, worker_id=worker_id, lock_seconds=lock_seconds)

    def claim_task_by_id(
        self,
        stage: ProcessingStage,
        *,
        task_id: int,
        worker_id: str,
        lock_seconds: int = 300,
    ) -> TaskRecord | None:
        return self._claim_task(
            stage,
            worker_id=worker_id,
            lock_seconds=lock_seconds,
            task_id=task_id,
        )

    def _claim_task(
        self,
        stage: ProcessingStage,
        *,
        worker_id: str,
        lock_seconds: int = 300,
        task_id: int | None = None,
    ) -> TaskRecord | None:
        now, locked_until = _now(), _now() + timedelta(seconds=lock_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT t.*, COALESCE(a.is_ai_source, 0) AS account_is_ai_source FROM tasks t LEFT JOIN x_capture_accounts a ON t.source='x' AND a.username_lower=lower(COALESCE(json_extract(t.metadata, '$.account_username'), json_extract(t.metadata, '$.author_username'), '')) WHERE (? IS NULL OR t.id = ?) AND (t.locked_until IS NULL OR t.locked_until < ?) ORDER BY t.created_at, t.id",
                (task_id, task_id, _iso(now)),
            ).fetchall()
            selected = next((row for row in rows if self._eligible(stage, dict(row), bool(row["account_is_ai_source"]))), None)
            if selected is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE tasks SET status=?, locked_by=?, locked_until=?, attempt_count=attempt_count+1, updated_at=? WHERE id=?",
                (STAGE_SPECS[stage].processing_status, worker_id, _iso(locked_until), _iso(now), selected["id"]),
            )
            conn.execute("INSERT OR IGNORE INTO x_task_pipeline(task_id) VALUES (?)", (selected["id"],))
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (selected["id"],)).fetchone()
            conn.commit()
        return _row_to_task(_record(row))

    def get_pipeline(self, task_id: int):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM x_task_pipeline WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"pipeline row not found for task {task_id}")
        return _row_to_pipeline(_record(row))

    def get_task(self, task_id: int) -> TaskRecord:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"task not found: {task_id}")
        return _row_to_task(_record(row))

    def ensure_pipeline(self, task_id: int) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO x_task_pipeline(task_id) VALUES (?)", (task_id,))
            conn.commit()

    def count_legacy_unfinished_tasks(self) -> int:
        sql, params = _in_query("SELECT count(*) AS count FROM tasks WHERE source IN ({}) AND status IN ({})", LEGACY_SKIP_SOURCES, LEGACY_SKIP_UNFINISHED_STATUSES)
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["count"])

    def mark_legacy_unfinished_tasks_skipped(self) -> int:
        sql, params = _in_query("UPDATE tasks SET status='legacy_skipped', locked_by=NULL, locked_until=NULL, updated_at=CURRENT_TIMESTAMP WHERE source IN ({}) AND status IN ({})", LEGACY_SKIP_SOURCES, LEGACY_SKIP_UNFINISHED_STATUSES)
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.rowcount

    def get_active_prompt(self, template_key: str) -> PromptTemplateVersion:
        with self._connect() as conn:
            row = conn.execute("SELECT v.*, t.feature_mode_enabled, t.feature_mode_text FROM prompt_templates t JOIN prompt_template_versions v ON v.id=t.active_version_id WHERE t.template_key=? AND v.deleted_at IS NULL", (template_key,)).fetchone()
        if row is None:
            raise ValueError(f"active prompt not found: {template_key}")
        return _row_to_prompt(_record(row))

    def get_publisher_settings(self):
        with self._connect() as conn:
            self._ensure_publisher_defaults(conn)
            row = conn.execute("SELECT * FROM publisher_settings WHERE singleton_key='global'").fetchone()
            conn.commit()
        return _row_to_publisher_settings(_record(row))

    def list_publisher_channels(self):
        with self._connect() as conn:
            self._ensure_publisher_defaults(conn)
            rows = conn.execute("SELECT * FROM publisher_channels ORDER BY channel_key").fetchall()
            conn.commit()
        return [_row_to_publisher_channel(_record(row)) for row in rows]

    def get_publisher_rule_config_snapshot(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT config_json FROM publisher_rule_config WHERE singleton_key='global'").fetchone()
        return _decode(row["config_json"]) if row else None

    def upsert_publisher_rule_config_snapshot(self, *, config_json: dict[str, Any], prompt_text: str, updated_by: str | None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO publisher_rule_config(singleton_key, config_json, prompt_text, updated_by) VALUES ('global', ?, ?, ?) ON CONFLICT(singleton_key) DO UPDATE SET config_json=excluded.config_json, prompt_text=excluded.prompt_text, updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP", (_json(config_json), prompt_text, updated_by))
            conn.commit()

    def complete_judge(self, task_id: int, *, news_type: NewsType, model: str, raw_output: str, rule_set: str | None = None, rule_version: str | None = None) -> None:
        output = {"route": news_type, "discard_type": "none", "raw_output": raw_output}
        if rule_set: output["rule_set"] = rule_set
        if rule_version: output["rule_version"] = rule_version
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET news_type=?, judge_model=?, judge_output=?, judge_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (news_type, model, _json(output), _iso(), _iso(), task_id))
            source = conn.execute("SELECT source FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._set_task_status(conn, task_id, "deduped" if source and source["source"] in SEARCH_FIRST_SOURCES else "judged")
            conn.commit()

    def complete_judge_discard(self, task_id: int, *, discard_type: str, model: str, raw_output: str, rule_set: str | None = None, rule_version: str | None = None) -> None:
        output = {"route": "discard", "discard_type": discard_type, "raw_output": raw_output}
        if rule_set: output["rule_set"] = rule_set
        if rule_version: output["rule_version"] = rule_version
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET news_type=NULL, judge_model=?, judge_output=?, judge_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (model, _json(output), _iso(), _iso(), task_id))
            self._release_primary_candidate(conn, task_id, "discarded")
            self._set_task_status(conn, task_id, "discarded")
            conn.commit()

    def complete_search(self, task_id: int) -> None:
        self.complete_search_ready(task_id, candidate_id=0, result={"skipped": True, "reason": "searcher is no-op"})

    def complete_search_duplicate(self, task_id: int, *, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET candidate_id=COALESCE(?, candidate_id), search_result=?, search_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (result.get("candidate_id"), _json(result), _iso(), _iso(), task_id))
            self._set_task_status(conn, task_id, "duplicate")
            conn.commit()

    def complete_search_ready(self, task_id: int, *, candidate_id: int, result: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET candidate_id=?, search_result=?, search_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (candidate_id or None, _json(result), _iso(), _iso(), task_id))
            source = conn.execute("SELECT source FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._set_task_status(conn, task_id, "searched" if source and source["source"] in SEARCH_FIRST_SOURCES else "deduped")
            conn.commit()

    def list_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM odaily_reference_items WHERE published_at IS NULL OR published_at>=? ORDER BY published_at DESC, updated_at DESC", (_iso(since),)).fetchall()
        return [SearchDocument(doc_type="odaily_reference", doc_id=str(r["source_item_id"]), title=r["title"], content=str(r["content"]), source="odaily", source_url=r["source_url"], published_at=_dt(r["published_at"]), metadata=_decode(r["metadata"])) for r in rows]

    def list_active_candidate_documents(self) -> list[SearchDocument]:
        cutoff, now = _iso(_now() - ACTIVE_CANDIDATE_TTL), _iso()
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM search_event_candidates WHERE status='active' AND created_at>? AND expires_at>? ORDER BY updated_at DESC", (cutoff, now)).fetchall()
        return [SearchDocument(doc_type="candidate", doc_id=str(r["id"]), title=r["title"], content=str(r["content"]), source="candidate", task_id=r["primary_task_id"], candidate_id=int(r["id"]), status=r["status"], created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]), expires_at=_dt(r["expires_at"]), metadata=_decode(r["metadata"])) for r in rows]

    def create_candidate_for_task(self, task: TaskRecord, *, search_result: dict[str, Any]) -> tuple[int, bool]:
        digest = content_hash(f"{task.title or ''}\n{task.content}".strip())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT id, primary_task_id FROM search_event_candidates WHERE content_hash=? AND status='active' AND created_at>? AND expires_at>? ORDER BY created_at LIMIT 1", (digest, _iso(_now() - ACTIVE_CANDIDATE_TTL), _iso())).fetchone()
            if existing:
                candidate_id = int(existing["id"])
                primary = int(existing["primary_task_id"]) == task.id
                self._insert_event_source(conn, candidate_id, task, "primary" if primary else "supporting", search_result)
                conn.commit()
                return candidate_id, primary
            cur = conn.execute("INSERT INTO search_event_candidates(primary_task_id, status, title, content, content_hash, metadata, expires_at) VALUES (?, 'active', ?, ?, ?, ?, ?)", (task.id, task.title, task.content, digest, _json({"source": task.source, "source_item_id": task.source_item_id}), _iso(_now() + ACTIVE_CANDIDATE_TTL)))
            candidate_id = int(cur.lastrowid)
            self._insert_event_source(conn, candidate_id, task, "primary", search_result)
            conn.commit()
            return candidate_id, True

    def link_task_to_candidate(self, task: TaskRecord, *, candidate_id: int, search_result: dict[str, Any]) -> None:
        with self._connect() as conn:
            self._insert_event_source(conn, candidate_id, task, "supporting", search_result)
            conn.commit()

    def _insert_event_source(self, conn, candidate_id: int, task: TaskRecord, role: str, search_result: dict[str, Any]) -> None:
        conn.execute("INSERT INTO search_event_sources(candidate_id, task_id, source, source_item_id, source_url, title, content, role, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(candidate_id, task_id) DO UPDATE SET role=excluded.role, metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP", (candidate_id, task.id, task.source, task.source_item_id, task.source_url, task.title, task.content, role, _json({"search_result": search_result})))

    def complete_write(self, task_id: int, *, prompt: PromptTemplateVersion, model: str, draft_title: str, draft_content: str, raw_output: str, trace: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET prompt_template_key=?, prompt_version_id=?, writer_feature_mode_enabled=?, writer_model=?, writer_output=?, draft_title=?, draft_content=?, write_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (prompt.template_key, prompt.id, int(prompt.feature_mode_enabled), model, _json({"raw_output": raw_output, "trace": trace}), draft_title, draft_content, _iso(), _iso(), task_id))
            self._set_task_status(conn, task_id, "written")
            conn.commit()

    def complete_format_publish(self, task_id: int, *, final_title: str, final_content: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET final_title=?, final_content=?, format_completed_at=?, last_error=NULL, updated_at=? WHERE task_id=?", (final_title, final_content, _iso(), _iso(), task_id))
            self._set_task_status(conn, task_id, "publisher_pending")
            conn.commit()

    def complete_publish(self, task_id: int, *, publisher_channel: str | None, publisher_model: str | None, publisher_category: str | None, publisher_decision: str, publisher_reason_code: str, publisher_output: dict[str, Any], push_result: dict[str, Any], telegram_result: dict[str, Any], decided_at: datetime, status: str, last_error: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET publisher_channel=?, publisher_model=?, publisher_category=?, publisher_decision=?, publisher_reason_code=?, publisher_output=?, publisher_decided_at=?, publish_completed_at=?, push_result=?, telegram_result=?, last_error=?, updated_at=? WHERE task_id=?", (publisher_channel, publisher_model, publisher_category, publisher_decision, publisher_reason_code, _json(publisher_output), _iso(decided_at), _iso(), _json(push_result), _json(telegram_result), last_error[:2000] if last_error else None, _iso(), task_id))
            if status == "publisher_failed": self._release_primary_candidate(conn, task_id, status)
            self._set_task_status(conn, task_id, status)
            conn.commit()

    def fail_task(self, task_id: int, *, stage: ProcessingStage, error: str, status: str | None = None) -> None:
        status = status or STAGE_SPECS[stage].failure_status
        with self._connect() as conn:
            conn.execute("UPDATE x_task_pipeline SET last_error=?, updated_at=? WHERE task_id=?", (error[:2000], _iso(), task_id))
            self._release_primary_candidate(conn, task_id, status)
            self._set_task_status(conn, task_id, status)
            conn.commit()

    def record_worker_heartbeat(self, *, component: str, worker_id: str, status: str, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        now = _iso()
        with self._connect() as conn:
            conn.execute("INSERT INTO pipeline_worker_heartbeats(component, worker_id, status, last_seen_at, last_success_at, last_error, metadata) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(component, worker_id) DO UPDATE SET status=excluded.status, last_seen_at=excluded.last_seen_at, last_success_at=CASE WHEN excluded.last_success_at IS NOT NULL THEN excluded.last_success_at ELSE pipeline_worker_heartbeats.last_success_at END, last_error=excluded.last_error, metadata=excluded.metadata, updated_at=CURRENT_TIMESTAMP", (component, worker_id, status, now, now if success else None, error[:2000] if error else None, _json(metadata or {})))
            conn.commit()

    def _set_task_status(self, conn, task_id: int, status: str) -> None:
        conn.execute("UPDATE tasks SET status=?, locked_by=NULL, locked_until=NULL, updated_at=? WHERE id=?", (status, _iso(), task_id))

    def _release_primary_candidate(self, conn, task_id: int, release_reason: str) -> None:
        row = conn.execute("SELECT c.id, c.metadata FROM x_task_pipeline p JOIN search_event_candidates c ON c.id=p.candidate_id WHERE p.task_id=? AND c.primary_task_id=? AND c.status='active'", (task_id, task_id)).fetchone()
        if not row: return
        metadata = _decode(row["metadata"])
        metadata.update({"released_by_task_id": task_id, "released_by_task_status": release_reason})
        conn.execute("UPDATE search_event_candidates SET status='inactive', expires_at=?, metadata=?, updated_at=? WHERE id=?", (_iso(), _json(metadata), _iso(), row["id"]))


def _in_query(template: str, first: list[str], second: list[str]) -> tuple[str, tuple[str, ...]]:
    return template.format(",".join("?" for _ in first), ",".join("?" for _ in second)), tuple(first) + tuple(second)


SQLITE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tasks (id integer PRIMARY KEY AUTOINCREMENT, source text NOT NULL, source_item_id text NOT NULL, source_url text, title text, content text NOT NULL, published_at text, raw_payload text NOT NULL DEFAULT '{}', metadata text NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'pending', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, locked_by text, locked_until text, attempt_count integer NOT NULL DEFAULT 0, UNIQUE(source, source_item_id));
CREATE TABLE IF NOT EXISTS x_capture_accounts (id integer PRIMARY KEY AUTOINCREMENT, username text NOT NULL, username_lower text NOT NULL UNIQUE, display_name text, write_name text, profile_url text, enabled integer NOT NULL DEFAULT 1, is_ai_source integer NOT NULL DEFAULT 0, interval_seconds integer, seeded_at text, last_polled_at text, last_success_at text, last_error text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS pipeline_worker_heartbeats (component text NOT NULL, worker_id text NOT NULL, status text NOT NULL, last_seen_at text NOT NULL, last_success_at text, last_error text, metadata text NOT NULL DEFAULT '{}', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(component, worker_id));
CREATE TABLE IF NOT EXISTS odaily_reference_items (source_item_id text PRIMARY KEY, source_url text, title text, content text NOT NULL, raw_payload text NOT NULL DEFAULT '{}', metadata text NOT NULL DEFAULT '{}', published_at text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS prompt_templates (template_key text PRIMARY KEY, display_name text NOT NULL, active_version_id integer, feature_mode_enabled integer NOT NULL DEFAULT 0, feature_mode_text text NOT NULL DEFAULT '', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS prompt_template_versions (id integer PRIMARY KEY AUTOINCREMENT, template_key text NOT NULL REFERENCES prompt_templates(template_key) ON DELETE CASCADE, version_number integer NOT NULL, content text NOT NULL, note text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, published_at text, deleted_at text, UNIQUE(template_key, version_number));
CREATE TABLE IF NOT EXISTS publisher_settings (singleton_key text PRIMARY KEY, enabled integer NOT NULL DEFAULT 1, timezone text NOT NULL DEFAULT 'Asia/Shanghai', window_start_local text NOT NULL DEFAULT '00:01', window_end_local text NOT NULL DEFAULT '07:30', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS publisher_channels (channel_key text PRIMARY KEY, display_name text NOT NULL, enabled integer NOT NULL DEFAULT 0, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS publisher_rule_config (singleton_key text PRIMARY KEY, config_json text NOT NULL DEFAULT '{}', prompt_text text NOT NULL DEFAULT '', updated_by text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS x_task_pipeline (task_id integer PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE, news_type text, candidate_id integer, judge_model text, judge_output text NOT NULL DEFAULT '{}', judge_completed_at text, search_result text NOT NULL DEFAULT '{}', search_completed_at text, prompt_template_key text, prompt_version_id integer, writer_feature_mode_enabled integer, writer_model text, writer_output text NOT NULL DEFAULT '{}', draft_title text, draft_content text, write_completed_at text, final_title text, final_content text, format_completed_at text, publisher_channel text, publisher_model text, publisher_category text, publisher_decision text, publisher_reason_code text, publisher_output text NOT NULL DEFAULT '{}', publisher_decided_at text, publish_completed_at text, push_result text NOT NULL DEFAULT '{}', telegram_result text NOT NULL DEFAULT '{}', last_error text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS search_event_candidates (id integer PRIMARY KEY AUTOINCREMENT, primary_task_id integer REFERENCES tasks(id) ON DELETE SET NULL, status text NOT NULL DEFAULT 'active', title text, content text NOT NULL, content_hash text NOT NULL, metadata text NOT NULL DEFAULT '{}', expires_at text, created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS search_event_sources (id integer PRIMARY KEY AUTOINCREMENT, candidate_id integer NOT NULL REFERENCES search_event_candidates(id) ON DELETE CASCADE, task_id integer REFERENCES tasks(id) ON DELETE SET NULL, source text NOT NULL, source_item_id text NOT NULL, source_url text, title text, content text NOT NULL, role text NOT NULL CHECK(role IN ('primary','supporting')), metadata text NOT NULL DEFAULT '{}', created_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at text NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(candidate_id, task_id));
CREATE INDEX IF NOT EXISTS idx_tasks_status_lock ON tasks(status, locked_until, created_at);
CREATE INDEX IF NOT EXISTS idx_x_task_pipeline_candidate ON x_task_pipeline(candidate_id);
CREATE INDEX IF NOT EXISTS idx_odaily_reference_published ON odaily_reference_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_candidates_status_expires ON search_event_candidates(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_search_candidates_hash ON search_event_candidates(content_hash);
"""
