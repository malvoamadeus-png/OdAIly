from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import urldefrag, urlparse

from .prompts import comparison_prompt, screening_prompt, search_prompt


def _contains(text: str, snippet: str) -> bool:
    return bool(snippet.strip()) and re.sub(r"\s+", "", snippet) in re.sub(r"\s+", "", text)


def _questions(items: Any, published: str, original: str) -> list[dict[str, str]]:
    if not isinstance(items, list):
        raise ValueError("questions must be an array")
    result = []
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("claim"), str)
                or not _contains(published, item["claim"]) or not isinstance(item.get("question"), str)
                or not item["question"].strip() or item.get("kind") not in {"name_mapping", "added_fact", "missing_scope"}
                or not isinstance(item.get("reason"), str) or not item["reason"].strip()
                or not isinstance(item.get("source_evidence"), str)
                or (item["source_evidence"] and not _contains(original, item["source_evidence"]))
                or (item["kind"] in {"name_mapping", "missing_scope"} and not item["source_evidence"].strip())):
            raise ValueError("Invalid or ungrounded fact review question")
        result.append({key: item[key] for key in ("claim", "question", "kind", "reason", "source_evidence")})
    return result


def _url_key(value: str) -> str:
    return urldefrag(value)[0].rstrip("/")


class FactReviewEngine:
    """One review interface for the worker and offline cases; no publication side effects."""

    def __init__(self, ai, *, max_questions: int = 3) -> None:
        self.ai = ai
        self.max_questions = max_questions

    def review(self, *, title: str, content: str, original_loader: Callable[[], dict[str, str]]) -> dict[str, Any]:
        result: dict[str, Any] = {"model": "gpt-6-luna", "stages": {}}
        published = title + "\n" + content
        try:
            screening = self.ai.review(screening_prompt(title, content))
            result["stages"]["screening"] = screening
            gate = screening["result"]
            if type(gate.get("needs_review")) is not bool or not isinstance(gate.get("reason"), str) or not gate["reason"].strip():
                raise ValueError("Invalid fact review screening response")
            result["stages"]["screening"] = screening
            result["reason"] = gate["reason"]
            if not gate["needs_review"]:
                return {**result, "status": "skipped", "summary": gate["reason"]}
            # Screening decides eligibility only. It has not seen the source and
            # cannot establish an editorial discrepancy or a search question.
            questions: list[dict[str, str]] = []
            original = original_loader()
            result["original_text"] = original.get("text", "")
            result["original_url"] = original.get("url", "")
            result["stages"]["original_material"] = {key: value for key, value in original.items() if key != "text"}
            source_flagged = False
            if original.get("status") == "available" and original.get("text"):
                comparison = self.ai.review(comparison_prompt(title, content, original["text"], original.get("url", "")))
                result["stages"]["comparison"] = comparison
                compared = comparison["result"]
                if compared.get("status") not in {"passed", "flagged", "source_mismatch"} or not isinstance(compared.get("summary"), str):
                    raise ValueError("Invalid original comparison response")
                issues = compared.get("issues")
                if not isinstance(issues, list):
                    raise ValueError("Original review issues must be an array")
                for issue in issues:
                    if (not isinstance(issue, dict) or not isinstance(issue.get("original"), str)
                            or not _contains(published, issue["original"]) or not isinstance(issue.get("reason"), str)
                            or not issue["reason"].strip() or not isinstance(issue.get("evidence"), str)
                            or (issue["evidence"] and not _contains(original["text"], issue["evidence"]))):
                        raise ValueError("Original review issue lacks a real text anchor")
                suggestions = compared.get("suggestions", [])
                if not isinstance(suggestions, list):
                    raise ValueError("Original review suggestions must be an array")
                anchored_suggestions = []
                for suggestion in suggestions:
                    if (not isinstance(suggestion, dict)
                            or not isinstance(suggestion.get("evidence"), str)
                            or not _contains(original["text"], suggestion["evidence"])
                            or not isinstance(suggestion.get("reason"), str)
                            or not suggestion["reason"].strip()):
                        # Optional editorial advice must not invalidate a completed
                        # factual comparison. Keep only verifiable source quotes.
                        continue
                    anchored_suggestions.append(suggestion)
                compared["suggestions"] = anchored_suggestions
                if compared["status"] == "passed" and issues:
                    raise ValueError("Passed original review cannot contain issues")
                if compared["status"] != "passed" and not issues:
                    raise ValueError("Flagged original review requires an anchored issue")
                result["stages"]["comparison"] = comparison
                source_flagged = compared["status"] != "passed"
                if compared["status"] != "source_mismatch":
                    questions = _questions(compared.get("questions"), published, original["text"])
            else:
                result["stages"]["comparison"] = {"result": {"status": "unavailable", "summary": original.get("reason") or "原文不可用", "issues": []}}

            unique: list[dict[str, str]] = []
            for question in questions:
                if not any(q["claim"] == question["claim"] and q["kind"] == question["kind"] for q in unique):
                    unique.append(question)
            result["questions"] = unique
            external_unverified = False
            contradicted = False
            if unique:
                try:
                    external = self.ai.search(search_prompt(title, content, unique[:self.max_questions], original.get("text", "")))
                    findings = external["result"].get("findings")
                    if not isinstance(findings, list):
                        raise ValueError("External findings must be an array")
                    source_urls = {_url_key(url) for url in external.get("sources", [])}
                    checked = []
                    for question in unique:
                        finding = next((item for item in findings if isinstance(item, dict) and item.get("claim") == question["claim"]
                                        and item.get("question") == question["question"]), None)
                        if finding is None or finding.get("verdict") not in {"supported", "contradicted", "unverified"}:
                            finding = {**question, "verdict": "unverified", "reason": "核查未返回此问题的有效结论，或超出本次搜索预算", "evidence": []}
                        evidence = finding.get("evidence") or []
                        verified_evidence = [item for item in evidence if isinstance(item, dict)
                            and isinstance(item.get("url"), str) and urlparse(item["url"]).scheme in {"http", "https"}
                            and _url_key(item["url"]) in source_urls and isinstance(item.get("quote"), str) and item["quote"].strip()]
                        finding["evidence"] = verified_evidence
                        if finding["verdict"] != "unverified" and not verified_evidence:
                            finding = {**finding, "verdict": "unverified", "reason": "结论缺少搜索工具可追溯的引用证据"}
                        checked.append(finding)
                    external["result"]["findings"] = checked
                    result["stages"]["external"] = external
                    contradicted = any(x["verdict"] == "contradicted" for x in checked)
                    external_unverified = any(x["verdict"] == "unverified" for x in checked)
                except Exception as exc:
                    result["stages"]["external"] = {"result": {"status": "failed", "summary": str(exc)[:500], "findings": []}}
                    # Preserve an already-proven source problem alongside the external failure.
                    if not source_flagged:
                        raise
                    external_unverified = True
            else:
                result["stages"]["external"] = {"result": {"status": "not_needed", "summary": "没有需要外部搜索的具体疑点", "findings": []}}
            if source_flagged or contradicted:
                status = "flagged"
            elif external_unverified or original.get("status") != "available":
                status = "unverifiable"
            else:
                status = "passed"
            summaries = [result["stages"]["comparison"]["result"].get("summary", "")]
            if unique:
                summaries.append(result["stages"]["external"]["result"].get("summary", ""))
            return {**result, "status": status, "summary": "；".join(x for x in summaries if x), "incomplete": external_unverified or original.get("status") != "available"}
        except Exception as exc:
            return {**result, "status": "failed", "summary": "事实审查执行失败，未作通过判断", "error": str(exc)[:1000]}
