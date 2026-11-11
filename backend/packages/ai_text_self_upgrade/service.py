from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from packages.auditor.repository import calculate_content_hash
from packages.common.storage import connect_sqlite_readonly
from packages.common.text import normalize_multiline_text
from packages.x_processing.ai_client import TextGenerationClient
from packages.x_processing.formatter import ODAILY_PREFIX, _normalize_content_spaces

from .prompts import MODEL, OUTPUT_SCHEMA, PROMPT_VERSION, build_prompt, parse_output
from .repository import AITextSelfUpgradeRepository


def _decode(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value)) if value not in (None, "") else default
    except (TypeError, json.JSONDecodeError):
        return default


def case_key(source_type: str, source_item_id: str, content_hash: str) -> str:
    prefix = "" if source_type == "text_auditor" else f"{source_type}\n"
    return hashlib.sha256(f"{prefix}{source_item_id}\n{content_hash}".encode("utf-8")).hexdigest()


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


def _published_body_signature(value: str) -> str:
    body = normalize_multiline_text(value).strip()
    if body.startswith(ODAILY_PREFIX):
        body = body.removeprefix(ODAILY_PREFIX)
    elif body.startswith("Odaily星球日报讯"):
        body = body.removeprefix("Odaily星球日报讯").lstrip()
    return _normalize_content_spaces(body)


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

    def run_batch(self, *, run_key: str, since: str | None = None, until: str | None = None) -> dict[str, int]:
        discovered, unlinked = self._discover_cases(since=since, until=until)
        discovered_keys = {str(item["case_key"]) for item in discovered}
        inserted = sum(self.repository.insert_case(item) for item in discovered)
        completed = failed = 0
        attempted: set[str] = set()
        while True:
            case = self.repository.claim_next(
                excluded_case_keys=attempted,
                included_case_keys=discovered_keys if since is not None or until is not None else None,
            )
            if case is None:
                break
            attempted.add(str(case["case_key"]))
            try:
                result = self._analyze(case)
                self.repository.complete_analysis(
                    case["case_key"],
                    model=self.model,
                    prompt_version=PROMPT_VERSION,
                    result=result,
                )
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

    def _discover_cases(self, *, since: str | None = None, until: str | None = None) -> tuple[list[dict[str, Any]], int]:
        if not self.primary_database_path.exists():
            return [], 0
        unlinked = 0
        with connect_sqlite_readonly(self.primary_database_path, timeout_seconds=10) as conn:
            conn.row_factory = sqlite3.Row
            tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"newsflash_production_metadata", "x_task_pipeline", "tasks"}
            if not required <= tables:
                return [], 0
            since = since or self.repository.enabled_at()
            trigger_rows: list[sqlite3.Row] = []
            if "auditor_checks" in tables:
                trigger_rows.extend(conn.execute(
                    """SELECT 'text_auditor' AS source_type,a.id AS auditor_check_id,NULL AS fact_check_id,
                        a.source_item_id,a.content_hash,a.title AS audited_title,a.content AS audited_content,
                        a.source_url AS audit_source_url,a.published_at,a.prompt_version AS audit_prompt_version,
                        a.model AS audit_model,a.audit_result,NULL AS policy_version,NULL AS fact_status,
                        NULL AS fact_reason,NULL AS fact_summary,NULL AS fact_result,
                        NULL AS fact_original_text,NULL AS fact_original_url,NULL AS fact_completed_at,
                        COALESCE(a.alerted_at,a.created_at) AS reviewed_at
                    FROM auditor_checks a
                    WHERE a.status='flagged' AND julianday(COALESCE(a.alerted_at,a.created_at))>=julianday(?)
                        AND (? IS NULL OR julianday(COALESCE(a.alerted_at,a.created_at))<julianday(?))
                    ORDER BY julianday(COALESCE(a.alerted_at,a.created_at)) DESC,a.id DESC""",
                    (since, until, until),
                ).fetchall())
            if {"fact_checks", "fact_check_runs"} <= tables:
                trigger_rows.extend(conn.execute(
                    """SELECT 'fact_auditor' AS source_type,0 AS auditor_check_id,f.id AS fact_check_id,
                        f.source_item_id,f.content_hash,f.title AS audited_title,f.content AS audited_content,
                        f.source_url AS audit_source_url,f.published_at,NULL AS audit_prompt_version,
                        f.model AS audit_model,NULL AS audit_result,f.policy_version,f.status AS fact_status,
                        f.reason AS fact_reason,f.summary AS fact_summary,f.result_json AS fact_result,
                        f.original_text AS fact_original_text,f.original_url AS fact_original_url,
                        r.completed_at AS fact_completed_at,r.completed_at AS reviewed_at
                    FROM fact_checks f
                    JOIN fact_check_runs r ON r.id=(
                        SELECT latest.id FROM fact_check_runs latest
                        WHERE latest.check_id=f.id ORDER BY latest.id DESC LIMIT 1
                    )
                    WHERE f.status='flagged' AND r.status='flagged' AND julianday(r.completed_at)>=julianday(?)
                        AND (? IS NULL OR julianday(r.completed_at)<julianday(?))
                    ORDER BY julianday(r.completed_at) DESC,r.id DESC""",
                    (since, until, until),
                ).fetchall())
            if not trigger_rows:
                return [], 0

            publication_times = [str(row["published_at"]) for row in trigger_rows if row["published_at"]]
            task_rows: list[sqlite3.Row] = []
            if publication_times:
                task_rows = conn.execute(
                    """SELECT p.task_id,p.writer_model,p.prompt_template_key,p.prompt_version_id,
                        p.draft_title,p.draft_content,p.final_title,p.final_content,p.write_completed_at,
                        p.publish_completed_at,t.source_url AS task_source_url,t.title AS source_title,
                        t.content AS source_content,m.title AS metadata_title,m.odaily_id,m.id_match_status
                    FROM x_task_pipeline p JOIN tasks t ON t.id=p.task_id
                    LEFT JOIN newsflash_production_metadata m ON m.delivery_key='odaily-task-' || p.task_id
                    WHERE p.writer_model IS NOT NULL AND p.write_completed_at IS NOT NULL
                        AND p.publish_completed_at IS NOT NULL
                        AND julianday(p.publish_completed_at)>=julianday(?) - 2
                        AND julianday(p.publish_completed_at)<=julianday(?) + 2""",
                    (min(publication_times), max(publication_times)),
                ).fetchall()

        tasks_by_hash: dict[str, list[sqlite3.Row]] = {}
        for task in task_rows:
            digest = calculate_content_hash(task["final_title"], str(task["final_content"] or ""))
            tasks_by_hash.setdefault(digest, []).append(task)

        cases: dict[str, dict[str, Any]] = {}
        for row in trigger_rows:
            audited_title = str(row["audited_title"] or "")
            audited_content = str(row["audited_content"] or "")
            digest = calculate_content_hash(audited_title, audited_content)
            if row["content_hash"] and str(row["content_hash"]) != digest:
                unlinked += 1
                continue
            matches = tasks_by_hash.get(digest, [])
            id_matches = [
                task for task in task_rows
                if task["id_match_status"] == "matched" and str(task["odaily_id"] or "") == str(row["source_item_id"])
            ]
            if len(id_matches) == 1:
                matches = id_matches
            else:
                metadata_matches = [
                    task for task in task_rows
                    if str(task["metadata_title"] or "") == audited_title
                    and str(task["final_title"] or "") == audited_title
                    and _published_body_signature(str(task["final_content"] or ""))
                    == _published_body_signature(audited_content)
                ]
                if len(metadata_matches) == 1:
                    matches = metadata_matches
            if len(matches) != 1:
                unlinked += 1
                continue
            task = matches[0]
            if not task["draft_title"] and not task["draft_content"]:
                unlinked += 1
                continue
            source_type = str(row["source_type"])
            source_item_id = str(row["source_item_id"])
            key = case_key(source_type, source_item_id, digest)
            case = {
                "source_type": source_type,
                "case_key": key,
                "auditor_check_id": int(row["auditor_check_id"] or 0),
                "fact_check_id": int(row["fact_check_id"]) if row["fact_check_id"] is not None else None,
                "fact_policy_version": row["policy_version"],
                "fact_model": row["audit_model"] if source_type == "fact_auditor" else None,
                "fact_status": row["fact_status"],
                "fact_reason": row["fact_reason"] or "",
                "fact_summary": row["fact_summary"] or "",
                "fact_result": _fact_review_brief(_decode(row["fact_result"], {})),
                "fact_original_text": row["fact_original_text"] or "",
                "fact_original_url": row["fact_original_url"] or "",
                "fact_completed_at": row["fact_completed_at"],
                "source_item_id": source_item_id,
                "content_hash": digest,
                "audit_prompt_version": row["audit_prompt_version"],
                "audit_model": row["audit_model"],
                "audited_title": audited_title,
                "audited_content": audited_content,
                "audit_result": _decode(row["audit_result"], {}),
                "published_at": row["published_at"],
                "source_url": task["task_source_url"] or row["audit_source_url"],
                "source_title": task["source_title"],
                "source_content": str(task["source_content"] or ""),
                "task_id": int(task["task_id"]),
                "writer_model": task["writer_model"],
                "prompt_template_key": task["prompt_template_key"],
                "prompt_version_id": task["prompt_version_id"],
                "draft_title": task["draft_title"],
                "draft_content": task["draft_content"],
                "final_title": task["final_title"],
                "final_content": task["final_content"],
            }
            cases.setdefault(key, case)
        return list(cases.values()), unlinked

    def _analyze(self, case: dict[str, Any]) -> dict[str, Any]:
        raw = self.ai_client.generate_text(
            model=self.model,
            prompt=build_prompt(case),
            text_format=OUTPUT_SCHEMA,
            reasoning_effort="medium",
        )
        return parse_output(raw, case)
