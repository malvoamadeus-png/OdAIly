from __future__ import annotations

import os
import time
from typing import Any

from packages.common.heartbeat import HeartbeatThrottle
from packages.common.source_exclusions import SQLiteSourceExclusionRepository, is_source_excluded
from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter

from .ai import FactReviewAI
from .engine import FactReviewEngine
from .original import deterministic_reason, linked_material, resolve_original
from .repository import FactReviewRepository
from .settings import FactAuditorSettings


class FactAuditorWorker:
    def __init__(self, repository: FactReviewRepository, settings: FactAuditorSettings, *, ai=None) -> None:
        self.repository = repository
        self.settings = settings
        self.feed_writer = LocalEditorPluginFeedWriter()
        self.worker_id = "fact-auditor-" + str(os.getpid())
        self.engine = FactReviewEngine(ai or FactReviewAI(settings), max_questions=settings.max_search_questions)
        self.exclusions = SQLiteSourceExclusionRepository(repository.path)
        from packages.auditor.sqlite_repository import SQLiteAuditorRepository
        heartbeat_repo = SQLiteAuditorRepository(repository.path)
        heartbeat_repo.init_schema()
        self.heartbeat = HeartbeatThrottle(component="fact_auditor", worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: heartbeat_repo.record_worker_heartbeat(
                component=component, worker_id=worker_id, status=status, success=success, error=error, metadata=metadata))

    def run_once(self) -> dict[str, Any] | None:
        task = self.repository.claim(worker_id=self.worker_id, lookback_minutes=self.settings.lookback_minutes,
            max_attempts=self.settings.max_attempts, start_after=self.settings.start_after)
        if task is None:
            self.heartbeat.send(status="ok", success=True, metadata={"processed": 0})
            return None
        try:
            linked = linked_material(self.repository.path, task)
            reason = deterministic_reason(task, linked)
            matched = []
            if not reason:
                for group in self.exclusions.list_enabled_rule_groups():
                    if is_source_excluded([group], scopes=["fact_auditor"], title_texts=[task["title"]], body_texts=[task["content"]]):
                        matched.append({"rule_key": group.rule_key, "name": group.name, "terms": list(group.terms), "match_target": group.match_target})
                if matched:
                    reason = "命中事实审核者排除规则：" + "、".join(x["name"] for x in matched)
            if reason:
                result = {"status": "exempt", "reason": reason, "summary": reason, "exclusion_rules": matched, "stages": {}}
            else:
                result = self.engine.review(title=task["title"] or "", content=task["content"],
                    original_loader=lambda: resolve_original(self.repository.path, task, linked))
        except Exception as exc:
            result = {"status": "failed", "reason": "", "summary": "事实审查执行失败", "error": str(exc)[:1000], "stages": {}}
        self.repository.save(task, result, worker_id=self.worker_id)
        if result["status"] == "flagged":
            try:
                self.feed_writer.upsert_fact_auditor_alert(
                    check_id=task["id"],
                    source_item_id=task["source_item_id"],
                    source_url=task.get("source_url"),
                    title=task.get("title"),
                    summary=plugin_alert_summary(result),
                    audit_result=result,
                )
            except Exception as exc:
                print(f"[odaily] local feed write skipped fact_check_id={task['id']} error={exc}")
        self.heartbeat.send(status="failed" if result["status"] == "failed" else "ok", success=result["status"] != "failed",
            error=result.get("error"), metadata={"processed": 1, "source_item_id": task["source_item_id"], "result": result["status"]})
        return {"source_item_id": task["source_item_id"], "check_id": task["id"], **result}

    def run_forever(self) -> None:
        while True:
            try:
                for _ in range(self.settings.max_items_per_run):
                    result = self.run_once()
                    if result is None:
                        break
                    print(f"[odaily] fact auditor id={result['source_item_id']} result={result['status']}", flush=True)
                    if result["status"] == "failed":
                        break
            except Exception as exc:
                print(f"[odaily] fact auditor round failed: {exc}", flush=True)
            time.sleep(self.settings.interval_seconds)


def plugin_alert_summary(result: dict[str, Any], *, max_length: int = 180) -> str:
    stages = result.get("stages") or {}
    comparison = (stages.get("comparison") or {}).get("result") or {}
    external = (stages.get("external") or {}).get("result") or {}
    reasons: list[str] = []
    if comparison.get("status") == "source_mismatch":
        reasons.append("原文与稿件不对应")
    for issue in comparison.get("issues") or []:
        if isinstance(issue, dict) and isinstance(issue.get("reason"), str) and issue["reason"].strip():
            reasons.append(issue["reason"].strip())
    for finding in external.get("findings") or []:
        if (isinstance(finding, dict) and finding.get("verdict") == "contradicted"
                and isinstance(finding.get("reason"), str) and finding["reason"].strip()):
            reasons.append(finding["reason"].strip())
    unique_reasons = list(dict.fromkeys(reasons))
    detail = "；".join(unique_reasons[:2]) or str(result.get("summary") or "发现事实疑点").strip()
    text = f"事实疑点：{detail}"
    return text if len(text) <= max_length else text[: max_length - 3].rstrip() + "..."
