from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from packages.common.paths import get_paths

from . import fast_narrative


PATHS = get_paths()
DEFAULT_AUDIT_DIR = PATHS.exports_dir / "meme_scanner"


def _run(factory: Any) -> Any:
    return factory()


def _settings(audit_output: Path | None, timeout: int, chain: str) -> Any:
    output = audit_output or DEFAULT_AUDIT_DIR / "narrative-v2.json"
    return type("NarrativeArgs", (), {
        "chain": chain,
        "contract": "",
        "output_dir": str(output.parent),
        "output": str(output),
        "gpt_model": os.getenv("MEME_FAST_WRITER_MODEL") or "gpt-5.6-luna",
        "gpt_timeout": timeout,
        "symbol": "",
    })()


def generate_reader_text(
    *,
    address: str,
    symbol: str,
    chain: str = "bsc",
    trigger_kind: str,
    database_path: Path,
    evidence: dict[str, Any] | None,
    timeout: int,
    audit_output: Path | None = None,
) -> dict[str, Any]:
    del database_path, evidence
    args = _settings(audit_output, timeout, chain)
    args.contract = address
    args.symbol = symbol
    args.trigger_kind = trigger_kind
    provider = fast_narrative.HTTPFastEvidenceAdapter(
        endpoint=os.getenv("MEME_FAST_EVIDENCE_URL") or "",
        internal_key=os.getenv("MEME_FAST_EVIDENCE_INTERNAL_KEY") or "",
        timeout_seconds=int(os.getenv("MEME_FAST_EVIDENCE_TIMEOUT") or min(timeout, 45)),
    )
    try:
        result = _run(lambda: fast_narrative.run(args, provider=provider))
    except Exception as exc:
        stage = str(getattr(exc, "stage", "narrative_pipeline") or "narrative_pipeline")
        message = str(exc) or exc.__class__.__name__
        return {
            "status": "error",
            "failure_stage": stage,
            "failure_code": "stage_failed",
            "failure_message": message[:1000],
            "material_counts": {},
            "decision_code": "final_validation_error" if stage == "final_validation" else "narrative_error",
            "decision_reason": f"叙事流程在 {stage} 阶段失败：{message[:500]}",
            "reader_text": "",
            "telegram_contexts": [],
            "telegram_messages": [],
            "x_posts": [],
            "gmgn_supplement": [],
            "fomo_materials": [],
            "fast_evidence": {},
            "gmgn_diagnostic": {"stage": "gmgn_narrative", "status": "disabled"},
            "grok_research": {},
            "grok_diagnostics": [],
            "grok_text": "",
            "grok_error": str(exc),
            "transient_error": f"narrative_{stage}_failed",
        }

    if result.get("status") == "error":
        stage = str(result.get("failure_stage") or "narrative_pipeline")
        result = {
            **result,
            "transient_error": f"narrative_{stage}_failed",
        }

    return {
        **result,
        "grok_text": "",
        "grok_error": None,
    }
