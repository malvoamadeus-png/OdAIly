from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from packages.auditor.repository import calculate_content_hash
from packages.common.storage import connect_sqlite_readonly
from packages.x_processing.ai_client import TextGenerationClient

from .prompts import MODEL, OUTPUT_SCHEMA, build_prompt, parse_output
from .repository import AITextSelfUpgradeRepository


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, json.JSONDecodeError):
        return default


def case_key(source_item_id: str, content_hash: str) -> str:
    return hashlib.sha256(f"{source_item_id}\n{content_hash}".encode("utf-8")).hexdigest()


class AITextSelfUpgradeService:
    def __init__(
        self,
        primary_database_path: Path,
        repository: AITextSelfUpgradeRepository,
        ai_client: TextGenerationClient,
        *,
        model: str = MODEL,
    ) -> None:
        self.primary_database_path = primary_database_path
        self.repository = repository
        self.ai_client = ai_client
        self.model = model

    def run_batch(self, *, run_key: str) -> dict[str, int]:
        discovered, unlinked = self._discover_cases()
        inserted = sum(self.repository.insert_case(item) for item in discovered)
        completed = failed = 0
        attempted: set[str] = set()
        while True:
            case = self.repository.claim_next(excluded_case_keys=attempted)
            if case is None:
                break
            attempted.add(str(case["case_key"]))
            try:
                result = self._analyze(case)
                self.repository.complete_analysis(case["case_key"], model=self.model, result=result)
                completed += 1
            except Exception as exc:
                self.repository.fail_analysis(case["case_key"], error=f"{type(exc).__name__}: {exc}")
                failed += 1
        self.repository.finish_run(
            run_key,
            status="failed" if failed and completed == 0 else "completed",
            cases_found=inserted,
            cases_completed=completed,
            cases_failed=failed,
            cases_unlinked=unlinked,
        )
        return {"cases_found": inserted, "cases_completed": completed, "cases_failed": failed, "cases_unlinked": unlinked}

    def fail_run(self, run_key: str, error: str) -> None:
        self.repository.finish_run(run_key, status="failed", error=error)

    def _discover_cases(self) -> tuple[list[dict[str, Any]], int]:
        if not self.primary_database_path.exists():
            return [], 0
        enabled_at = self.repository.enabled_at()
        candidates: list[dict[str, Any]] = []
        unlinked = 0
        with connect_sqlite_readonly(self.primary_database_path, timeout_seconds=10) as conn:
            conn.row_factory = sqlite3.Row
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"auditor_checks", "newsflash_production_metadata", "x_task_pipeline", "tasks"}
            if not required <= tables:
                return [], 0
            rows = conn.execute(
                """SELECT a.id AS auditor_check_id,a.source_item_id,a.source_url AS audit_source_url,
                    a.title AS audited_title,a.content AS audited_content,a.content_hash,a.published_at,
                    a.prompt_version AS audit_prompt_version,a.model AS audit_model,a.audit_result,a.alerted_at,
                    m.delivery_key,m.id_match_status,p.task_id,p.writer_model,p.writer_output,p.prompt_template_key,
                    p.prompt_version_id,p.draft_title,p.draft_content,p.final_title,p.final_content,p.write_completed_at,
                    p.publish_completed_at,t.source AS task_source,t.source_url AS task_source_url,
                    t.title AS source_title,t.content AS source_content
                FROM auditor_checks a
                LEFT JOIN newsflash_production_metadata m
                    ON m.odaily_id=a.source_item_id AND m.id_match_status='matched'
                LEFT JOIN x_task_pipeline p
                    ON m.delivery_key='odaily-task-' || p.task_id
                LEFT JOIN tasks t ON t.id=p.task_id
                WHERE a.status='flagged' AND a.alerted_at IS NOT NULL
                    AND julianday(a.alerted_at)>=julianday(?)
                ORDER BY julianday(a.alerted_at) DESC,a.id DESC""",
                (enabled_at,),
            ).fetchall()
        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["task_id"] is None or not row["write_completed_at"] or not row["publish_completed_at"] or not row["writer_model"]:
                unlinked += 1
                continue
            title = str(row["final_title"] or "")
            content = str(row["final_content"] or "")
            if calculate_content_hash(title, content) != str(row["content_hash"] or ""):
                unlinked += 1
                continue
            source_item_id = str(row["source_item_id"])
            digest = str(row["content_hash"])
            key = case_key(source_item_id, digest)
            if key in deduped:
                continue
            audit_result = _decode(row["audit_result"], {})
            deduped[key] = {
                "case_key": key,
                "auditor_check_id": int(row["auditor_check_id"]),
                "source_item_id": source_item_id,
                "content_hash": digest,
                "audit_prompt_version": row["audit_prompt_version"],
                "audit_model": row["audit_model"],
                "audited_title": row["audited_title"],
                "audited_content": str(row["audited_content"] or ""),
                "audit_result": audit_result if isinstance(audit_result, dict) else {},
                "published_at": row["published_at"],
                "source_url": row["task_source_url"] or row["audit_source_url"],
                "source_title": row["source_title"],
                "source_content": str(row["source_content"] or ""),
                "task_id": int(row["task_id"]),
                "writer_model": row["writer_model"],
                "prompt_template_key": row["prompt_template_key"],
                "prompt_version_id": row["prompt_version_id"],
                "draft_title": row["draft_title"],
                "draft_content": row["draft_content"],
                "final_title": title,
                "final_content": content,
            }
        return list(deduped.values()), unlinked

    def _analyze(self, case: dict[str, Any]) -> dict[str, Any]:
        raw = self.ai_client.generate_text(
            model=self.model,
            prompt=build_prompt(case),
            text_format=OUTPUT_SCHEMA,
            reasoning_effort="medium",
        )
        return parse_output(raw, case)
