from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from packages.common.config import DEFAULT_GPT_WRITER_MODEL
from packages.common.paths import get_paths

from . import narrative_v2


PATHS = get_paths()
DEFAULT_AUDIT_DIR = PATHS.exports_dir / "meme_scanner"
DEFAULT_TELEGRAM_CONFIG = PATHS.config_dir / "meme_telegram.txt"
DEFAULT_TELEGRAM_SESSION = PATHS.processed_dir / "meme_telegram_narrative"
DEFAULT_ALLOWED_CHATS = PATHS.config_dir / "meme_whitelist.txt"


def _run(factory: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(factory())
    raise RuntimeError("Meme narrative V2 cannot run inside an active event loop")


def _settings(audit_output: Path | None, timeout: int) -> Any:
    output = audit_output or DEFAULT_AUDIT_DIR / "narrative-v2.json"
    return type("NarrativeArgs", (), {
        "chain": "bsc",
        "contract": "",
        "output_dir": str(output.parent),
        "output": str(output),
        "gpt_model": os.getenv("MEME_WRITER_MODEL") or DEFAULT_GPT_WRITER_MODEL,
        "grok_model": os.getenv("MEME_GROK_MODEL") or os.getenv("GROK_MODEL") or narrative_v2.DEFAULT_GROK_MODEL,
        "gpt_timeout": timeout,
        "grok_timeout": timeout,
        "gmgn_timeout": int(os.getenv("MEME_GMGN_TIMEOUT") or min(timeout, 20)),
        "telegram_config": os.getenv("MEME_TELEGRAM_CONFIG") or str(DEFAULT_TELEGRAM_CONFIG),
        "telegram_session": os.getenv("MEME_TELEGRAM_NARRATIVE_SESSION")
        or os.getenv("MEME_TELEGRAM_WATCH_SESSION")
        or str(DEFAULT_TELEGRAM_SESSION),
        "allowed_chats": os.getenv("MEME_TELEGRAM_ALLOWED_CHATS") or str(DEFAULT_ALLOWED_CHATS),
        "dialogs_limit": int(os.getenv("MEME_TELEGRAM_DIALOGS_LIMIT") or 300),
        "proxy": os.getenv("MEME_TELEGRAM_PROXY") or "auto",
        "telegram_timeout": int(os.getenv("MEME_TELEGRAM_TIMEOUT") or 20),
        "connection_retries": int(os.getenv("MEME_TELEGRAM_CONNECTION_RETRIES") or 3),
    })()


def _grok_text(result: dict[str, Any]) -> str:
    return json.dumps(
        {
            "source_actions": result.get("grok_research", {}).get("source_actions", []),
            "narrative_materials": result.get("grok_research", {}).get("narrative_materials", []),
            "supplemental_information": result.get("grok_research", {}).get("supplemental_information", []),
        },
        ensure_ascii=False,
    )


def generate_reader_text(
    *,
    address: str,
    symbol: str,
    trigger_kind: str,
    database_path: Path,
    evidence: dict[str, Any] | None,
    timeout: int,
    audit_output: Path | None = None,
) -> dict[str, Any]:
    del symbol, database_path, evidence
    args = _settings(audit_output, timeout)
    args.contract = address
    args.trigger_kind = trigger_kind
    try:
        result = _run(lambda: narrative_v2.run_async(args))
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
            "gmgn_diagnostic": {"stage": "gmgn_narrative", "optional": True},
            "grok_research": {},
            "grok_diagnostics": [{"stage": "narrative_v2", "error": str(exc)}],
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
        "grok_text": _grok_text(result),
        "grok_error": next(
            (
                str(item.get("http_status"))
                for item in result.get("grok_diagnostics", [])
                if isinstance(item, dict) and int(item.get("http_status", 0) or 0) >= 400
            ),
            None,
        ),
    }
