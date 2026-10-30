from __future__ import annotations

from typing import Any


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
        if (
            isinstance(finding, dict)
            and finding.get("verdict") == "contradicted"
            and isinstance(finding.get("reason"), str)
            and finding["reason"].strip()
        ):
            reasons.append(finding["reason"].strip())
    unique_reasons = list(dict.fromkeys(reasons))
    detail = "；".join(unique_reasons[:2]) or str(result.get("summary") or "发现事实疑点").strip()
    text = f"事实疑点：{detail}"
    return text if len(text) <= max_length else text[: max_length - 3].rstrip() + "..."
