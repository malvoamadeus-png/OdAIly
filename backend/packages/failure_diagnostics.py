from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from packages.common.failure_diagnostics import (
    FAILURE_STATUS_STAGE_LABELS,
    PROCESSING_STATUS_STAGE_LABELS,
    classify_failure,
    stage_label_for_status,
)


FAILURE_STATUSES = frozenset(FAILURE_STATUS_STAGE_LABELS)
PROCESSING_STATUSES = frozenset(PROCESSING_STATUS_STAGE_LABELS)
ALERT_SOURCES = frozenset({"external_media_alert", "ai_source_alert"})


def _decode_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _redact_error(value: Any, *, limit: int = 2000) -> str:
    text = str(value or "")[:limit]
    text = re.sub(r"(?i)(authorization\s*:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|password|secret)\s*[=:]\s*)[^\s]+", r"\1[REDACTED]", text)
    return text


def _extract_match(pattern: str, text: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    value = match.group(1).strip().rstrip(".,;)")
    return value or None


def _extract_evidence(error: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for key, pattern in {
        "model": r"\bmodel=([^\s]+)",
        "api_style": r"\bapi_style=([^\s]+)",
        "endpoint": r"\b(?:url|chat_url|responses_url)=([^\s]+)",
        "http_status": r"\b(?:status_code|chat_status_code|responses_status_code)=(\d{3})\b",
        "timeout_seconds": r"\btimeout_seconds=([0-9]+(?:\.[0-9]+)?)\b",
    }.items():
        value = _extract_match(pattern, error)
        if value is None:
            continue
        evidence[key] = value
    if "endpoint" in evidence:
        evidence["endpoint"] = re.sub(
            r"(?i)([?&](?:key|token|secret|api_key)=)[^&]+",
            r"\1[REDACTED]",
            str(evidence["endpoint"]),
        )
    return evidence


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{path.expanduser().resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=5000")
    try:
        yield connection
    finally:
        connection.close()


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return bool(
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    )


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _serialize_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _safe_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


class FailureDiagnosticsStore:
    """Read-only adapter joining business task, queue and worker evidence for one task."""

    def __init__(
        self,
        database_path: Path,
        queue_path: Path,
        *,
        storage_epoch: str,
        task_stuck_minutes: int | None = None,
        heartbeat_stale_minutes: int | None = None,
    ) -> None:
        self.database_path = database_path
        self.queue_path = queue_path
        self.storage_epoch = storage_epoch
        self.task_stuck_minutes = task_stuck_minutes or _env_int("PIPELINE_SUPERVISOR_TASK_STUCK_MINUTES", 10)
        self.heartbeat_stale_minutes = heartbeat_stale_minutes or _env_int(
            "PIPELINE_SUPERVISOR_HEARTBEAT_STALE_MINUTES", 10
        )

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        if not self.database_path.exists():
            return None
        with _readonly_connection(self.database_path) as connection:
            task_row = connection.execute(
                """
                SELECT id, source, source_item_id, title, status, created_at, updated_at,
                       attempt_count, locked_by, locked_until
                FROM tasks WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            if task_row is None:
                return None
            task = dict(task_row)
            pipeline = self._pipeline_row(connection, task_id=int(task["id"]), source=str(task["source"]))
            heartbeat = self._heartbeat_row(connection)

        queue = self._queue_row(task_id=int(task["id"]))
        status = str(task["status"] or "")
        pipeline_error = _safe_text((pipeline or {}).get("last_error"))
        queue_error = _safe_text((queue or {}).get("last_error"))
        error = pipeline_error or queue_error
        classification_status = status
        if status == "publisher_failed" and (pipeline or {}).get("publisher_reason_code") == "model_failed":
            classification_status = "write_failed"
        classification = classify_failure(error, status=classification_status)
        kind = "failure" if status in FAILURE_STATUSES else "processing" if status in PROCESSING_STATUSES else "none"
        worker = self._worker_payload(heartbeat)
        processing_state = self._processing_state(task, worker) if kind == "processing" else None
        evidence = _extract_evidence(error or "")
        model = self._pipeline_model(pipeline, status)
        if model and "model" not in evidence:
            evidence["model"] = model

        diagnosis = {
            "kind": kind,
            "stage": status,
            "stage_label": stage_label_for_status(status),
            "code": classification.code if kind == "failure" else None,
            "category": classification.category if kind == "failure" else None,
            "category_label": classification.category_label if kind == "failure" else None,
            "reason": classification.reason if kind == "failure" else processing_state,
            "action_hint": classification.action_hint if kind == "failure" else self._processing_action(processing_state),
            "raw_error": _redact_error(error),
            "evidence": evidence,
            "retryable": None if queue is None else str(queue.get("status")) != "exhausted",
        }
        response = {
            "available": True,
            "task": {
                "id": int(task["id"]),
                "source": str(task["source"]),
                "source_item_id": str(task["source_item_id"]),
                "title": _safe_text(task.get("title")),
                "status": status,
                "created_at": task.get("created_at"),
                "updated_at": task.get("updated_at"),
                "attempt_count": int(task.get("attempt_count") or 0),
                "locked_by": _safe_text(task.get("locked_by")),
                "locked_until": task.get("locked_until"),
            },
            "diagnosis": diagnosis,
            "queue": self._queue_payload(queue),
            "worker": worker,
            "handoff_summary": self._handoff_summary(task, diagnosis, queue),
        }
        return response

    @staticmethod
    def _pipeline_row(connection: sqlite3.Connection, *, task_id: int, source: str) -> dict[str, Any] | None:
        preferred = "external_media_alert_pipeline" if source in ALERT_SOURCES else "x_task_pipeline"
        candidates = [preferred, "x_task_pipeline", "external_media_alert_pipeline"]
        for table in candidates:
            if _table_exists(connection, table):
                row = connection.execute(f"SELECT * FROM {table} WHERE task_id=?", (task_id,)).fetchone()
                if row is not None:
                    return dict(row)
        return None

    def _queue_row(self, *, task_id: int) -> dict[str, Any] | None:
        if not self.queue_path.exists():
            return None
        try:
            with _readonly_connection(self.queue_path) as connection:
                if not _table_exists(connection, "local_pipeline_jobs"):
                    return None
                available_columns = _columns(connection, "local_pipeline_jobs")
                epoch_clause = " AND storage_epoch=?" if "storage_epoch" in available_columns else ""
                params: tuple[Any, ...] = (task_id, self.storage_epoch) if epoch_clause else (task_id,)
                row = connection.execute(
                    f"""
                    SELECT id, job_type, source, source_item_id, status, attempt_count,
                           last_error, next_attempt_at, created_at, updated_at
                    FROM local_pipeline_jobs
                    WHERE task_id=?{epoch_clause}
                    ORDER BY id DESC LIMIT 1
                    """,
                    params,
                ).fetchone()
                return _serialize_row(row)
        except (OSError, sqlite3.Error):
            return None

    @staticmethod
    def _heartbeat_row(connection: sqlite3.Connection) -> dict[str, Any] | None:
        if not _table_exists(connection, "pipeline_worker_heartbeats"):
            return None
        row = connection.execute(
            """
            SELECT component, worker_id, status, last_seen_at, last_success_at, last_error, metadata
            FROM pipeline_worker_heartbeats
            WHERE component='local_pipeline'
            ORDER BY last_seen_at DESC LIMIT 1
            """
        ).fetchone()
        return _serialize_row(row)

    def _worker_payload(self, row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        now = datetime.now(UTC)
        last_seen = _parse_datetime(row.get("last_seen_at"))
        stale = last_seen is None or now - last_seen > timedelta(minutes=self.heartbeat_stale_minutes)
        return {
            "component": row.get("component"),
            "worker_id": row.get("worker_id"),
            "status": row.get("status"),
            "last_seen_at": row.get("last_seen_at"),
            "last_success_at": row.get("last_success_at"),
            "last_error": _redact_error(row.get("last_error")),
            "stale": stale,
        }

    def _processing_state(self, task: dict[str, Any], worker: dict[str, Any] | None) -> str:
        now = datetime.now(UTC)
        locked_until = _parse_datetime(task.get("locked_until"))
        updated_at = _parse_datetime(task.get("updated_at"))
        if locked_until is not None and locked_until < now:
            return "任务锁已过期，可能是 worker 在处理中断。"
        if worker is None or worker.get("stale"):
            return "local_pipeline 最近没有有效心跳，任务可能没有继续执行。"
        if updated_at is not None and now - updated_at > timedelta(minutes=self.task_stuck_minutes):
            return f"任务已超过 {self.task_stuck_minutes} 分钟没有更新，可能卡在当前环节。"
        return "任务仍在处理中，当前没有已记录的失败错误。"

    @staticmethod
    def _processing_action(state: str | None) -> str:
        if not state:
            return "刷新任务详情并检查对应服务状态。"
        if "心跳" in state:
            return "检查 odaily-local-pipeline.service 和 local_pipeline worker 日志。"
        if "锁已过期" in state:
            return "检查 local_pipeline 是否重启或异常退出，再查看队列重试状态。"
        return "检查对应阶段日志和 local_pipeline worker 心跳。"

    @staticmethod
    def _pipeline_model(pipeline: dict[str, Any] | None, status: str) -> str | None:
        if not pipeline:
            return None
        keys = {
            "judge_failed": ("judge_model", "domain_model"),
            "domain_failed": ("domain_model", "judge_model"),
            "write_failed": ("writer_model",),
            "publisher_failed": ("publisher_model",),
            "publish_failed": ("publisher_model",),
        }.get(status, ())
        for key in keys:
            value = _safe_text(pipeline.get(key))
            if value:
                return value
        return None

    @staticmethod
    def _queue_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "job_type": row.get("job_type"),
            "source": row.get("source"),
            "source_item_id": row.get("source_item_id"),
            "status": row.get("status"),
            "attempt_count": int(row.get("attempt_count") or 0),
            "last_error": _redact_error(row.get("last_error")),
            "next_attempt_at": row.get("next_attempt_at"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }

    @staticmethod
    def _handoff_summary(task: dict[str, Any], diagnosis: dict[str, Any], queue: dict[str, Any] | None) -> str:
        lines = [
            "OdAIly 任务故障摘要",
            f"任务 ID：{task.get('id')}",
            f"来源：{task.get('source')}",
            f"环节：{diagnosis.get('stage_label')}（{diagnosis.get('stage')}）",
        ]
        if diagnosis.get("kind") == "failure":
            lines.extend(
                [
                    f"分类：{diagnosis.get('category_label')}（{diagnosis.get('code')}）",
                    f"原因：{diagnosis.get('reason')}",
                ]
            )
        else:
            lines.append(f"状态：{diagnosis.get('reason') or diagnosis.get('kind')}")
        evidence = diagnosis.get("evidence") or {}
        for key, label in (("model", "模型"), ("http_status", "HTTP"), ("timeout_seconds", "超时"), ("endpoint", "端点")):
            if evidence.get(key):
                lines.append(f"{label}：{evidence[key]}")
        if queue:
            lines.append(f"队列：{queue.get('status')}，尝试 {queue.get('attempt_count')} 次，job_id={queue.get('id')}")
        if diagnosis.get("raw_error"):
            lines.append(f"原始错误：{diagnosis['raw_error']}")
        return "\n".join(lines)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name) or default))
    except (TypeError, ValueError):
        return default
