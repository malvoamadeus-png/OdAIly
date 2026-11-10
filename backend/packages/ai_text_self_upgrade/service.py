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
    return hashlib.sha256(f"fact_auditor\n{source_item_id}\n{content_hash}".encode("utf-8")).hexdigest()


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fact_review_brief(value: Any) -> dict[str, Any]:
    result = _object(value)
    stages = _object(result.get("stages"))
    brief_stages: dict[str, Any] = {}
    for stage_name in ("screening", "comparison", "external"):
        stage = _object(stages.get(stage_name))
        stage_result = _object(stage.get("result"))
        if not stage_result:
            continue
        brief: dict[str, Any] = {
            key: stage_result[key]
            for key in ("status", "summary", "reason", "issues", "questions", "suggestions", "findings")
            if key in stage_result
        }
        if stage_name == "external":
            brief["sources"] = stage.get("sources") or []
        brief_stages[stage_name] = brief
    return {
        "status": result.get("status"),
        "summary": result.get("summary"),
        "reason": result.get("reason"),
        "questions": result.get("questions") or [],
        "stages": brief_stages,
    }


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
        unlinked = 0
        with connect_sqlite_readonly(self.primary_database_path, timeout_seconds=10) as conn:
            conn.row_factory = sqlite3.Row
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"fact_checks", "fact_check_runs", "newsflash_production_metadata", "x_task_pipeline", "tasks"}
            if not required <= tables:
                return [], 0
            rows = conn.execute(
                """SELECT f.id AS fact_check_id,f.source_item_id,f.content_hash,f.policy_version,
                    f.title AS audited_title,f.content AS audited_content,f.source_url AS audit_source_url,
                    f.published_at,f.status AS fact_status,f.model AS fact_model,f.reason AS fact_reason,
                    f.summary AS fact_summary,f.original_text AS fact_original_text,
                    f.original_url AS fact_original_url,f.result_json AS fact_result,
                    r.completed_at AS fact_completed_at,
                    m.delivery_key,m.id_match_status,p.task_id,p.writer_model,p.writer_output,p.prompt_template_key,
                    p.prompt_version_id,p.draft_title,p.draft_content,p.final_title,p.final_content,p.write_completed_at,
                    p.publish_completed_at,t.source AS task_source,t.source_url AS task_source_url,
                    t.title AS source_title,t.content AS source_content
                FROM fact_checks f
                JOIN fact_check_runs r ON r.id=(
                    SELECT latest.id FROM fact_check_runs latest
                    WHERE latest.check_id=f.id ORDER BY latest.id DESC LIMIT 1
                )
                LEFT JOIN newsflash_production_metadata m
                    ON m.odaily_id=f.source_item_id AND m.id_match_status='matched'
                LEFT JOIN x_task_pipeline p
                    ON m.delivery_key='odaily-task-' || p.task_id
                LEFT JOIN tasks t ON t.id=p.task_id
                WHERE f.status='flagged' AND r.status='flagged'
                    AND julianday(r.completed_at)>=julianday(?)
                ORDER BY julianday(r.completed_at) DESC,r.id DESC""",
                (self.repository.fact_enabled_at(),),
            ).fetchall()
        deduped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if (row["task_id"] is None or not row["write_completed_at"] or not row["publish_completed_at"]
                    or not row["writer_model"]):
                unlinked += 1
                continue
            title = str(row["final_title"] or "")
            content = str(row["final_content"] or "")
            audited_title = str(row["audited_title"] or "")
            audited_content = str(row["audited_content"] or "")
            if calculate_content_hash(title, content) != calculate_content_hash(audited_title, audited_content):
                unlinked += 1
                continue
            source_item_id = str(row["source_item_id"])
            digest = calculate_content_hash(audited_title, audited_content)
            key = case_key(source_item_id, digest)
            if key in deduped:
                continue
            fact_result = _fact_review_brief(_decode(row["fact_result"], {}))
            deduped[key] = {
                "source_type": "fact_auditor",
                "case_key": key,
                "auditor_check_id": int(row["fact_check_id"]),
                "fact_check_id": int(row["fact_check_id"]),
                "fact_policy_version": row["policy_version"],
                "fact_model": row["fact_model"],
                "fact_status": row["fact_status"],
                "fact_reason": row["fact_reason"] or "",
                "fact_summary": row["fact_summary"] or "",
                "fact_result": fact_result,
                "fact_original_text": row["fact_original_text"] or "",
                "fact_original_url": row["fact_original_url"] or "",
                "fact_completed_at": row["fact_completed_at"],
                "source_item_id": source_item_id,
                "content_hash": digest,
                "audit_prompt_version": None,
                "audit_model": row["fact_model"],
                "audited_title": audited_title,
                "audited_content": audited_content,
                "audit_result": {},
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
