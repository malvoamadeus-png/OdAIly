from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from packages.common.storage import connect_sqlite


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL DEFAULT 'text_auditor',
    auditor_check_id INTEGER NOT NULL,
    fact_check_id INTEGER,
    fact_policy_version TEXT,
    fact_model TEXT,
    fact_status TEXT,
    fact_reason TEXT NOT NULL DEFAULT '',
    fact_summary TEXT NOT NULL DEFAULT '',
    fact_result_json TEXT NOT NULL DEFAULT '{}',
    fact_original_text TEXT NOT NULL DEFAULT '',
    fact_original_url TEXT NOT NULL DEFAULT '',
    fact_completed_at TEXT,
    source_item_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    audit_prompt_version TEXT,
    audit_model TEXT,
    audited_title TEXT,
    audited_content TEXT NOT NULL,
    audit_result_json TEXT NOT NULL DEFAULT '{}',
    published_at TEXT,
    source_url TEXT,
    source_title TEXT,
    source_content TEXT NOT NULL,
    task_id INTEGER,
    writer_model TEXT,
    prompt_template_key TEXT,
    prompt_version_id INTEGER,
    draft_title TEXT,
    draft_content TEXT,
    final_title TEXT,
    final_content TEXT,
    analysis_status TEXT NOT NULL DEFAULT 'pending' CHECK(analysis_status IN ('pending','processing','completed','failed')),
    analysis_model TEXT,
    analysis_prompt_version TEXT,
    verdict TEXT CHECK(verdict IS NULL OR verdict IN ('writer1_error','auditor_false_positive','reviewer_correct_unattributed')),
    confidence TEXT CHECK(confidence IS NULL OR confidence IN ('low','medium','high')),
    rationale TEXT,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    analysis_error TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    human_decision TEXT NOT NULL DEFAULT 'pending' CHECK(human_decision IN ('pending','accepted','rejected','ignored')),
    decision_by TEXT,
    decision_note TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_queue
ON ai_text_self_upgrade_items(analysis_status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_decisions
ON ai_text_self_upgrade_items(human_decision,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL REFERENCES ai_text_self_upgrade_items(case_key),
    decision TEXT NOT NULL CHECK(decision IN ('accepted','rejected','ignored')),
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_decision_history
ON ai_text_self_upgrade_decisions(case_key,id DESC);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_key TEXT NOT NULL REFERENCES ai_text_self_upgrade_items(case_key),
    decision TEXT NOT NULL CHECK(decision IN ('reviewer_correct','writer_correct','ignored')),
    actor TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_state (
    state_key TEXT PRIMARY KEY,
    state_value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_runs (
    run_key TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    cases_found INTEGER NOT NULL DEFAULT 0,
    cases_completed INTEGER NOT NULL DEFAULT 0,
    cases_failed INTEGER NOT NULL DEFAULT 0,
    cases_unlinked INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_deleted_cases (
    case_key TEXT PRIMARY KEY,
    deleted_by TEXT NOT NULL,
    deleted_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_text_self_upgrade_manual_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    auditor_check_id INTEGER NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','processing','completed','deleted')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    case_key TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_text_upgrade_manual_rejections_due
ON ai_text_self_upgrade_manual_rejections(status,next_attempt_at,id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, json.JSONDecodeError):
        return default


def _item(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for source, target, default in (
        ("audit_result_json", "audit_result", {}),
        ("fact_result_json", "fact_result", {}),
        ("evidence_json", "evidence", []),
    ):
        result[target] = _decode(result.pop(source), default)
    if result.get("source_type") == "fact_auditor" and result.get("verdict") == "auditor_false_positive":
        result["verdict"] = "fact_auditor_error"
    if result.get("verdict") is None and result.get("analysis_status") == "completed":
        result["verdict"] = "inconclusive"
    return result


class AITextSelfUpgradeRepository:
    def __init__(self, path: Path, *, source_database_path: Path | None = None, recover_inflight: bool = True) -> None:
        self.path = path
        self.source_database_path = source_database_path
        self.initialize(recover_inflight=recover_inflight)

    def connect(self) -> sqlite3.Connection:
        return connect_sqlite(self.path)

    def initialize(self, *, recover_inflight: bool = True) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA_SQL)
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(ai_text_self_upgrade_items)")}
            migrations = {
                "human_resolution": "TEXT",
                "source_type": "TEXT NOT NULL DEFAULT 'text_auditor'",
                "fact_check_id": "INTEGER",
                "fact_policy_version": "TEXT",
                "fact_model": "TEXT",
                "fact_status": "TEXT",
                "fact_reason": "TEXT NOT NULL DEFAULT ''",
                "fact_summary": "TEXT NOT NULL DEFAULT ''",
                "fact_result_json": "TEXT NOT NULL DEFAULT '{}'",
                "fact_original_text": "TEXT NOT NULL DEFAULT ''",
                "fact_original_url": "TEXT NOT NULL DEFAULT ''",
                "fact_completed_at": "TEXT",
                "analysis_prompt_version": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE ai_text_self_upgrade_items ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ai_text_self_upgrade_source_queue "
                "ON ai_text_self_upgrade_items(source_type,analysis_status,created_at,id)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO ai_text_self_upgrade_state(state_key,state_value,updated_at) "
                "VALUES('enabled_at',COALESCE((SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='fact_enabled_at'),?),?)",
                (_now(), _now()),
            )
            task_id_column = next(
                (row for row in conn.execute("PRAGMA table_info(ai_text_self_upgrade_items)") if row["name"] == "task_id"),
                None,
            )
            table_sql = str(conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_text_self_upgrade_items'"
            ).fetchone()["sql"])
            if (task_id_column is not None and task_id_column["notnull"]) or "reviewer_correct_unattributed" not in table_sql:
                self._migrate_nullable_task_id(conn)
            if recover_inflight:
                conn.execute(
                    "UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=COALESCE(analysis_error,'服务重启时原分析未完成'),updated_at=? WHERE analysis_status='processing'",
                    (_now(),),
                )
                conn.execute(
                    "UPDATE ai_text_self_upgrade_runs SET status='failed',completed_at=?,error=COALESCE(error,'服务重启时原批次未完成') WHERE status='running'",
                    (_now(),),
                )

    @staticmethod
    def _migrate_nullable_task_id(conn: sqlite3.Connection) -> None:
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            table_sql = str(conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='ai_text_self_upgrade_items'"
            ).fetchone()["sql"]).replace(
                "CREATE TABLE ai_text_self_upgrade_items", "CREATE TABLE ai_text_self_upgrade_items_nullable", 1
            ).replace("task_id INTEGER NOT NULL", "task_id INTEGER", 1).replace(
                "'writer1_error','auditor_false_positive'",
                "'writer1_error','auditor_false_positive','reviewer_correct_unattributed'",
                1,
            )
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(table_sql)
            columns = [str(row["name"]) for row in conn.execute("PRAGMA table_info(ai_text_self_upgrade_items)")]
            column_list = ",".join(columns)
            conn.execute(
                f"INSERT INTO ai_text_self_upgrade_items_nullable({column_list}) SELECT {column_list} FROM ai_text_self_upgrade_items"
            )
            conn.execute("DROP TABLE ai_text_self_upgrade_items")
            conn.execute("ALTER TABLE ai_text_self_upgrade_items_nullable RENAME TO ai_text_self_upgrade_items")
            conn.execute("CREATE INDEX idx_ai_text_self_upgrade_queue ON ai_text_self_upgrade_items(analysis_status,created_at,id)")
            conn.execute("CREATE INDEX idx_ai_text_self_upgrade_decisions ON ai_text_self_upgrade_items(human_decision,created_at DESC,id DESC)")
            conn.execute("CREATE INDEX idx_ai_text_self_upgrade_source_queue ON ai_text_self_upgrade_items(source_type,analysis_status,created_at,id)")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    def recover_manual_rejections(self) -> int:
        now = _now()
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE ai_text_self_upgrade_manual_rejections SET status='pending',next_attempt_at=?,updated_at=? WHERE status='processing'",
                (now, now),
            )
            return int(cursor.rowcount)

    def enabled_at(self) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state_value FROM ai_text_self_upgrade_state WHERE state_key='enabled_at'"
            ).fetchone()
        if row is None:
            raise RuntimeError("AI文本自我审核启用时间未初始化")
        return str(row["state_value"])

    def fact_enabled_at(self) -> str:
        return self.enabled_at()

    def case_exists(self, case_key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=?", (case_key,)
            ).fetchone() is not None

    def insert_case(self, case: dict[str, Any]) -> bool:
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=?", (case["case_key"],)).fetchone():
                conn.rollback()
                return False
            cursor = conn.execute(
                """INSERT OR IGNORE INTO ai_text_self_upgrade_items(
                    case_key,source_type,auditor_check_id,fact_check_id,fact_policy_version,fact_model,fact_status,
                    fact_reason,fact_summary,fact_result_json,fact_original_text,fact_original_url,fact_completed_at,
                    source_item_id,content_hash,audit_prompt_version,audit_model,
                    audited_title,audited_content,audit_result_json,published_at,source_url,source_title,
                    source_content,task_id,writer_model,prompt_template_key,prompt_version_id,draft_title,
                    draft_content,final_title,final_content,created_at,updated_at
                ) VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                (
                    case["case_key"], case.get("source_type", "fact_auditor"), case["auditor_check_id"],
                    case.get("fact_check_id"), case.get("fact_policy_version"), case.get("fact_model"),
                    case.get("fact_status"), case.get("fact_reason") or "", case.get("fact_summary") or "",
                    _json(case.get("fact_result") or {}), case.get("fact_original_text") or "",
                    case.get("fact_original_url") or "", case.get("fact_completed_at"),
                    case["source_item_id"], case["content_hash"],
                    case.get("audit_prompt_version"), case.get("audit_model"), case.get("audited_title"),
                    case["audited_content"], _json(case.get("audit_result") or {}), case.get("published_at"),
                    case.get("source_url"), case.get("source_title"), case["source_content"], case.get("task_id"),
                    case.get("writer_model"), case.get("prompt_template_key"), case.get("prompt_version_id"),
                    case.get("draft_title"), case.get("draft_content"), case.get("final_title"),
                    case.get("final_content"), now, now,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1

    def queue_manual_rejection(self, auditor_check_id: int, *, actor: str) -> str | None:
        """Persist a manual rejection for background exact-link resolution."""
        now = _now()
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO ai_text_self_upgrade_manual_rejections(
                    auditor_check_id,actor,status,next_attempt_at,created_at,updated_at
                ) VALUES(?,?,'pending',?,?,?)
                ON CONFLICT(auditor_check_id) DO UPDATE SET
                    actor=excluded.actor,
                    status=CASE WHEN ai_text_self_upgrade_manual_rejections.status IN ('completed','deleted')
                                THEN ai_text_self_upgrade_manual_rejections.status ELSE 'pending' END,
                    next_attempt_at=CASE WHEN ai_text_self_upgrade_manual_rejections.status IN ('completed','deleted')
                                         THEN ai_text_self_upgrade_manual_rejections.next_attempt_at ELSE excluded.next_attempt_at END,
                    updated_at=excluded.updated_at""",
                (auditor_check_id, actor[:200], now, now, now),
            )
        return f"manual-rejection:{auditor_check_id}"

    def manual_rejection_status(self, auditor_check_id: int) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT status FROM ai_text_self_upgrade_manual_rejections WHERE auditor_check_id=?",
                (auditor_check_id,),
            ).fetchone()
        return str(row["status"]) if row else None

    def process_manual_rejection_queue(self, *, limit: int = 10) -> dict[str, int]:
        now = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_manual_rejections WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
                (now, max(1, min(int(limit), 100))),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE ai_text_self_upgrade_manual_rejections SET status='processing',attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
            conn.commit()
        result = {"processed": 0, "completed": 0, "retrying": 0, "deleted": 0}
        for row in rows:
            result["processed"] += 1
            try:
                case_key = self._create_manual_rejection_case(int(row["auditor_check_id"]), actor=str(row["actor"]))
                if case_key is None:
                    status = "pending"
                    result["retrying"] += 1
                    error = "精确发布关联尚未就绪"
                elif case_key == "__deleted__":
                    status = "deleted"
                    result["deleted"] += 1
                    error = None
                else:
                    status = "completed"
                    result["completed"] += 1
                    error = None
                attempts = int(row["attempt_count"]) + 1
                delay = min(6 * 60 * 60, 30 * (2 ** min(attempts - 1, 10)))
                next_attempt = (datetime.now(UTC).timestamp() + delay)
                retry_at = datetime.fromtimestamp(next_attempt, UTC).isoformat()
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE ai_text_self_upgrade_manual_rejections SET status=?,case_key=?,last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                        (status, None if case_key in (None, "__deleted__") else case_key, error, retry_at, _now(), row["id"]),
                    )
            except Exception as exc:
                result["retrying"] += 1
                attempts = int(row["attempt_count"]) + 1
                delay = min(6 * 60 * 60, 30 * (2 ** min(attempts - 1, 10)))
                retry_at = datetime.fromtimestamp(datetime.now(UTC).timestamp() + delay, UTC).isoformat()
                with self.connect() as conn:
                    conn.execute(
                        "UPDATE ai_text_self_upgrade_manual_rejections SET status='pending',last_error=?,next_attempt_at=?,updated_at=? WHERE id=?",
                        (f"{type(exc).__name__}: {exc}"[:2000], retry_at, _now(), row["id"]),
                    )
        return result

    def _create_manual_rejection_case(self, auditor_check_id: int, *, actor: str) -> str | None:
        """Snapshot a rejected text audit with one exact Writer1 publication."""
        from packages.auditor.repository import calculate_content_hash
        from packages.common.storage import connect_sqlite_readonly, load_storage_settings
        from .service import case_key

        source_path = self.source_database_path or load_storage_settings().sqlite_path
        with connect_sqlite_readonly(source_path, timeout_seconds=10) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM auditor_checks WHERE id=? AND status='flagged'", (auditor_check_id,)).fetchone()
            if row is None:
                return "__deleted__"
            expected_hash = calculate_content_hash(row["title"], str(row["content"] or ""))
            hash_matches = str(row["content_hash"] or "") == expected_hash
            source_item_id = str(row["source_item_id"] or f"auditor-check:{auditor_check_id}")
            rejected_case_key = case_key("text_auditor", source_item_id, expected_hash)
            with self.connect() as review_conn:
                if review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=?", (rejected_case_key,)).fetchone():
                    return "__deleted__"
            source_tables = {str(item[0]) for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(newsflash_production_metadata)")}
            publication_rows = []
            if "odaily_id" in metadata_columns:
                publication_rows = conn.execute(
                    "SELECT * FROM newsflash_production_metadata WHERE odaily_id=?",
                    (str(row["source_item_id"]),),
                ).fetchall()
            explicit_categories = {
                str(item["outlet_category"] or "") for item in publication_rows
                if "outlet_category" in item.keys() and item["outlet_category"]
            }
            explicit_non_manual = bool(explicit_categories - {"manual"})
            ambiguous_writer1_metadata = any(
                ("id_match_status" in item.keys() and item["id_match_status"] == "ambiguous")
                or ("delivery_key" in item.keys() and str(item["delivery_key"] or "").startswith("odaily-task-"))
                for item in publication_rows
            )
            manual_rows = [item for item in publication_rows
                           if item["outlet_category"] == "manual"] if "outlet_category" in metadata_columns else []
            manual_origin = any(
                "origin_key" in item.keys() and item["origin_key"] == "manual"
                for item in publication_rows
            )
            publisher_kind = None
            if "newsflash_operation_facts" in source_tables:
                fact_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(newsflash_operation_facts)")}
                if "publisher_kind" in fact_columns:
                    fact = conn.execute(
                        "SELECT publisher_kind FROM newsflash_operation_facts WHERE source_item_id=?",
                        (str(row["source_item_id"]),),
                    ).fetchone()
                    publisher_kind = str(fact["publisher_kind"] or "") if fact else None
            manual_publication = not explicit_non_manual and (
                bool(manual_rows) or manual_origin or publisher_kind in {"human", "human_unmapped"}
            )
            unknown_authoring_basis = (
                "explicit_non_manual_metadata" if explicit_non_manual
                else "ambiguous_writer1_mapping" if ambiguous_writer1_metadata and not manual_publication
                else None
            )
            task = None
            if manual_publication:
                metadata = manual_rows[0] if manual_rows else next(
                    (item for item in publication_rows if "title" in item.keys()), None
                )
                task = {
                    "task_id": None, "writer_model": None, "prompt_template_key": None, "prompt_version_id": None,
                    "draft_title": None, "draft_content": None, "final_title": str(row["title"] or ""),
                    "final_content": str(row["content"] or ""), "source_url": row["source_url"],
                    "source_title": str(metadata["title"] or "") if metadata is not None and "title" in metadata.keys() else "", "source_content": "",
                    "authoring_path": "manual_publication",
                }
            pipeline_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(x_task_pipeline)")}
            task_columns = {str(item[1]) for item in conn.execute("PRAGMA table_info(tasks)")}
            can_read_writer_snapshot = hash_matches and (
                {"newsflash_production_metadata", "x_task_pipeline", "tasks"} <= source_tables
                and {"delivery_key", "odaily_id", "id_match_status"} <= metadata_columns
                and {"task_id", "writer_model", "prompt_template_key", "prompt_version_id", "draft_title", "draft_content", "final_title", "final_content", "write_completed_at", "publish_completed_at"} <= pipeline_columns
                and {"id", "source_url", "title", "content"} <= task_columns
            )
            if task is None and can_read_writer_snapshot:
                tasks = conn.execute(
                """SELECT p.task_id,p.writer_model,p.prompt_template_key,p.prompt_version_id,
                          p.draft_title,p.draft_content,p.final_title,p.final_content,
                          t.source_url,t.title AS source_title,t.content AS source_content
                   FROM newsflash_production_metadata m
                   JOIN x_task_pipeline p ON m.delivery_key='odaily-task-' || CAST(p.task_id AS TEXT)
                   JOIN tasks t ON t.id=p.task_id
                   WHERE m.odaily_id=? AND m.id_match_status='matched'
                     AND p.writer_model IS NOT NULL AND p.write_completed_at IS NOT NULL
                     AND p.publish_completed_at IS NOT NULL""",
                    (str(row["source_item_id"]),),
                ).fetchall()
                if len(tasks) == 1:
                    candidate = tasks[0]
                    if ((candidate["draft_title"] or candidate["draft_content"])
                            and calculate_content_hash(candidate["final_title"], str(candidate["final_content"] or "")) == expected_hash):
                        task = candidate
            if task is None:
                task = {
                    "task_id": None, "writer_model": None, "prompt_template_key": None, "prompt_version_id": None,
                    "draft_title": None, "draft_content": None, "final_title": str(row["title"] or ""),
                    "final_content": str(row["content"] or ""), "source_url": row["source_url"],
                    "source_title": "", "source_content": "",
                    "authoring_path": "unknown_publication" if unknown_authoring_basis else "manual_publication",
                }
            audit_result = _decode(row["audit_result"], {})
            if not isinstance(audit_result, dict):
                audit_result = {}
            authoring_path = task["authoring_path"] if "authoring_path" in task.keys() else None
            audit_result["manual_rejection"] = {
                "actor": actor[:200], "rejected_at": _now(),
                **({"authoring_path": authoring_path, "writer1_snapshot": "unavailable"}
                   if authoring_path in {"manual_publication", "unknown_publication"} else {}),
                **({"authoring_basis": unknown_authoring_basis} if unknown_authoring_basis else {}),
                **({"audit_snapshot_integrity": "content_hash_mismatch"} if not hash_matches else {}),
            }
            case = {
                "case_key": rejected_case_key,
                "source_type": "text_auditor", "auditor_check_id": auditor_check_id,
                "source_item_id": source_item_id, "content_hash": expected_hash,
                "audit_prompt_version": row["prompt_version"], "audit_model": row["model"],
                "audited_title": row["title"], "audited_content": str(row["content"] or ""),
                "audit_result": audit_result, "published_at": row["published_at"],
                "source_url": task["source_url"] or row["source_url"], "source_title": task["source_title"],
                "source_content": str(task["source_content"] or ""), "task_id": int(task["task_id"]) if task["task_id"] is not None else None,
                "writer_model": task["writer_model"], "prompt_template_key": task["prompt_template_key"],
                "prompt_version_id": task["prompt_version_id"], "draft_title": task["draft_title"],
                "draft_content": task["draft_content"], "final_title": task["final_title"],
                "final_content": task["final_content"],
            }
        inserted = self.insert_case(case)
        if not inserted:
            with self.connect() as review_conn:
                tombstone = review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_deleted_cases WHERE case_key=?", (case["case_key"],)).fetchone()
                if tombstone:
                    return "__deleted__"
                review_conn.execute(
                    "UPDATE ai_text_self_upgrade_items SET audit_result_json=json_set(audit_result_json,'$.manual_rejection',json(?)),updated_at=? WHERE case_key=?",
                    (_json(audit_result["manual_rejection"]), _now(), case["case_key"]),
                )
                exists = review_conn.execute("SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=?", (case["case_key"],)).fetchone()
            if not exists:
                return None
        return case["case_key"]

    def has_run(self, run_key: str) -> bool:
        with self.connect() as conn:
            return conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_runs WHERE run_key=?", (run_key,)
            ).fetchone() is not None

    def run_status(self, run_key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT status FROM ai_text_self_upgrade_runs WHERE run_key=?", (run_key,)).fetchone()
            return str(row["status"]) if row else None

    def oldest_unfinished_storage_retention_run(self) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT run_key FROM ai_text_self_upgrade_runs WHERE run_key LIKE 'storage_retention:%' AND status IN ('running','failed') ORDER BY started_at,run_key LIMIT 1"
            ).fetchone()
            return str(row["run_key"]) if row else None

    def start_run(self, run_key: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO ai_text_self_upgrade_runs(run_key,status,started_at) VALUES(?,'running',?)",
                (run_key, _now()),
            )
            return cursor.rowcount == 1

    def retry_failed_run(self, run_key: str, *, retry_after_seconds: int = 300) -> bool:
        now = datetime.now(UTC)
        cutoff = (now - timedelta(seconds=max(0, retry_after_seconds))).isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE ai_text_self_upgrade_runs SET status='running',started_at=?,completed_at=NULL,error=NULL
                   WHERE run_key=? AND status='failed' AND completed_at IS NOT NULL AND completed_at<=?""",
                (now.isoformat(), run_key, cutoff),
            )
            return cursor.rowcount == 1

    def recover_storage_retention_runs(self) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE ai_text_self_upgrade_runs SET status='failed',completed_at=?,
                   error=COALESCE(error,'worker restarted during storage retention')
                   WHERE status='running' AND run_key LIKE 'storage_retention:%'""",
                (_now(),),
            )
            return int(cursor.rowcount)

    def finish_run(
        self,
        run_key: str,
        *,
        status: str,
        cases_found: int = 0,
        cases_completed: int = 0,
        cases_failed: int = 0,
        cases_unlinked: int = 0,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE ai_text_self_upgrade_runs SET status=?,completed_at=CASE WHEN ?='running' THEN NULL ELSE ? END,cases_found=?,cases_completed=?,
                    cases_failed=?,cases_unlinked=?,error=? WHERE run_key=?""",
                (status, status, _now(), cases_found, cases_completed, cases_failed, cases_unlinked,
                 error[:2000] if error else None, run_key),
            )

    def recent_runs(self, limit: int = 12, *, since: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM ai_text_self_upgrade_runs WHERE (? IS NULL OR julianday(started_at)>=julianday(?)) "
                "ORDER BY started_at DESC LIMIT ?",
                (since, since, max(1, min(50, limit))),
            ).fetchall()]

    def claim_next(
        self,
        *,
        excluded_case_keys: set[str] | None = None,
        included_case_keys: set[str] | None = None,
    ) -> dict[str, Any] | None:
        excluded = sorted(excluded_case_keys or ())
        included = sorted(included_case_keys or ())
        if included_case_keys is not None and not included:
            return None
        exclusion_sql = f" AND case_key NOT IN ({','.join('?' for _ in excluded)})" if excluded else ""
        inclusion_sql = f" AND case_key IN ({','.join('?' for _ in included)})" if included_case_keys is not None else ""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE analysis_status IN ('pending','failed') "
                + inclusion_sql + exclusion_sql + " ORDER BY created_at,id LIMIT 1",
                [*included, *excluded],
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET analysis_status='processing',attempt_count=attempt_count+1,analysis_error=NULL,updated_at=? WHERE case_key=?",
                (_now(), row["case_key"]),
            )
            claimed = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE case_key=?", (row["case_key"],)
            ).fetchone()
            conn.commit()
        return _item(claimed)

    def complete_analysis(
        self,
        case_key: str,
        *,
        model: str,
        prompt_version: str,
        result: dict[str, Any],
    ) -> None:
        stored_verdict = None if result["verdict"] == "inconclusive" else result["verdict"]
        with self.connect() as conn:
            conn.execute(
                """UPDATE ai_text_self_upgrade_items SET analysis_status='completed',analysis_model=?,analysis_prompt_version=?,verdict=?,
                    confidence=?,rationale=?,evidence_json=?,analysis_error=NULL,updated_at=? WHERE case_key=?""",
                (model, prompt_version, stored_verdict, result["confidence"], result["rationale"], _json(result["evidence"]),
                 _now(), case_key),
            )

    def fail_analysis(self, case_key: str, *, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET analysis_status='failed',analysis_error=?,updated_at=? WHERE case_key=?",
                (error[:2000], _now(), case_key),
            )

    def list_items(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = str(payload.get("status") or "pending")
        scope = str(payload.get("scope") or "all")
        if scope not in {"all", "manual_recent"}:
            raise ValueError("不支持的案例范围")
        scope_clauses: list[str] = []
        scope_params: list[Any] = []
        if scope == "manual_recent":
            scope_clauses = [
                "source_type='text_auditor'",
                "json_valid(audit_result_json)",
                "(json_type(audit_result_json,'$.manual_rejection') IN ('object','array','true') OR "
                "(json_type(audit_result_json,'$.manual_rejection')='text' AND json_extract(audit_result_json,'$.manual_rejection')<>'') OR "
                "(json_type(audit_result_json,'$.manual_rejection') IN ('integer','real') AND json_extract(audit_result_json,'$.manual_rejection')<>0))",
                "COALESCE(json_extract(audit_result_json,'$.manual_rejection.authoring_basis'),'') NOT IN ('explicit_non_manual_metadata','ambiguous_writer1_mapping')",
                "julianday(COALESCE(NULLIF(published_at,''),created_at))>=julianday(?)",
            ]
            scope_params = [(datetime.now(UTC) - timedelta(hours=72)).isoformat()]
        clauses = list(scope_clauses)
        params: list[Any] = list(scope_params)
        if status != "all":
            if status not in {"pending", "reviewer_correct", "writer_correct", "ignored", "legacy", "confirmed"}:
                raise ValueError("不支持的案例状态")
            if status == "confirmed":
                clauses.append("human_resolution IN ('reviewer_correct','writer_correct')")
            elif status == "legacy":
                clauses.append("human_resolution IS NULL AND human_decision IN ('accepted','rejected','ignored')")
            elif status == "pending":
                clauses.append("human_resolution IS NULL AND human_decision='pending'")
            else:
                clauses.append("human_resolution=?")
                params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        limit = max(1, min(100, int(payload.get("limit") or 30)))
        offset = max(0, int(payload.get("offset") or 0))
        columns = "*"
        if scope == "manual_recent":
            columns = ("id,case_key,source_type,audited_title,published_at,created_at,analysis_status,verdict,"
                       "human_decision,human_resolution,"
                       "json_object('summary',json_extract(audit_result_json,'$.summary'),"
                       "'manual_rejection',json_object("
                       "'authoring_path',json_extract(audit_result_json,'$.manual_rejection.authoring_path'),"
                       "'authoring_basis',json_extract(audit_result_json,'$.manual_rejection.authoring_basis'),"
                       "'writer1_snapshot',json_extract(audit_result_json,'$.manual_rejection.writer1_snapshot'))) AS audit_result_json,"
                       "'{}' AS fact_result_json,'[]' AS evidence_json")
        with self.connect() as conn:
            total = int(conn.execute(
                f"SELECT COUNT(*) FROM ai_text_self_upgrade_items{where}", params
            ).fetchone()[0])
            rows = conn.execute(
                f"SELECT {columns} FROM ai_text_self_upgrade_items{where} "
                "ORDER BY CASE WHEN human_resolution IS NULL AND human_decision='pending' THEN 0 ELSE 1 END,created_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            confirmed_where = " AND ".join([*scope_clauses, "human_resolution IN ('reviewer_correct','writer_correct')"])
            confirmed_count = int(conn.execute(
                "SELECT COUNT(*) FROM ai_text_self_upgrade_items WHERE " + confirmed_where, scope_params
            ).fetchone()[0])
        runs = self.recent_runs(limit=1, since=self.enabled_at())
        return {
            "items": [_item(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
            "confirmed_count": confirmed_count,
            "latest_run": runs[0] if runs else None,
        }

    def detail(self, case_key: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM ai_text_self_upgrade_items WHERE case_key=?", (case_key,)
            ).fetchone()
            if row is None:
                raise ValueError("自我审核案例不存在")
            result = _item(row)
            result["decisions"] = [dict(decision) for decision in conn.execute(
                "SELECT decision,actor,note,decided_at,1 AS legacy FROM ai_text_self_upgrade_decisions WHERE case_key=? "
                "UNION ALL SELECT decision,actor,note,decided_at,0 AS legacy FROM ai_text_self_upgrade_resolutions WHERE case_key=? ORDER BY decided_at",
                (case_key, case_key),
            ).fetchall()]
            return result

    def decide(self, case_key: str, *, decision: str, actor: str, note: str = "") -> dict[str, Any]:
        if decision not in {"reviewer_correct", "writer_correct", "ignored"}:
            raise ValueError("不支持的人工决定")
        stamp = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM ai_text_self_upgrade_items WHERE case_key=?", (case_key,)
            ).fetchone():
                raise ValueError("自我审核案例不存在")
            conn.execute(
                "UPDATE ai_text_self_upgrade_items SET human_resolution=?,decision_by=?,decision_note=?,decided_at=?,updated_at=? WHERE case_key=?",
                (decision, actor[:200], note[:2000], stamp, stamp, case_key),
            )
            conn.execute(
                "INSERT INTO ai_text_self_upgrade_resolutions(case_key,decision,actor,note,decided_at) VALUES(?,?,?,?,?)",
                (case_key, decision, actor[:200], note[:2000], stamp),
            )
        return self.detail(case_key)

    def delete_case(self, case_key: str, *, actor: str) -> bool:
        stamp = _now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR IGNORE INTO ai_text_self_upgrade_deleted_cases(case_key,deleted_by,deleted_at) VALUES(?,?,?)", (case_key, actor[:200], stamp))
            conn.execute("DELETE FROM ai_text_self_upgrade_decisions WHERE case_key=?", (case_key,))
            conn.execute("DELETE FROM ai_text_self_upgrade_resolutions WHERE case_key=?", (case_key,))
            deleted = conn.execute("DELETE FROM ai_text_self_upgrade_items WHERE case_key=?", (case_key,)).rowcount
            conn.commit()
            return bool(deleted)
