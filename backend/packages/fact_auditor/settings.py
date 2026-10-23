from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from dotenv import load_dotenv


@dataclass(frozen=True)
class FactAuditorSettings:
    model: str = "gpt-6-luna"
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    api_style: str = "responses"
    search_api_key: str = ""
    search_base_url: str = ""
    request_timeout: float = 90.0
    lookback_minutes: int = 1440
    max_items_per_run: int = 20
    interval_seconds: float = 30.0
    max_attempts: int = 3
    max_search_questions: int = 3
    start_after: str = ""


def load_settings() -> FactAuditorSettings:
    load_dotenv()
    key = os.getenv("FACT_AUDITOR_OPENAI_API_KEY") or os.getenv("ODAILY_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    base = os.getenv("FACT_AUDITOR_OPENAI_BASE_URL") or os.getenv("ODAILY_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
    model = os.getenv("FACT_AUDITOR_MODEL") or "gpt-6-luna"
    if model != "gpt-6-luna":
        raise ValueError("FACT_AUDITOR_MODEL must be gpt-6-luna")
    result = FactAuditorSettings(
        model=model, api_key=key, base_url=base,
        api_style=os.getenv("FACT_AUDITOR_OPENAI_API_STYLE") or "responses",
        search_api_key=os.getenv("FACT_AUDITOR_SEARCH_API_KEY") or key,
        search_base_url=os.getenv("FACT_AUDITOR_SEARCH_BASE_URL") or base,
        request_timeout=float(os.getenv("FACT_AUDITOR_REQUEST_TIMEOUT_SECONDS") or 90),
        lookback_minutes=int(os.getenv("FACT_AUDITOR_LOOKBACK_MINUTES") or 1440),
        max_items_per_run=int(os.getenv("FACT_AUDITOR_MAX_ITEMS_PER_RUN") or 20),
        interval_seconds=float(os.getenv("FACT_AUDITOR_INTERVAL_SECONDS") or 30),
        max_attempts=int(os.getenv("FACT_AUDITOR_MAX_ATTEMPTS") or 3),
        max_search_questions=int(os.getenv("FACT_AUDITOR_MAX_SEARCH_QUESTIONS") or 3),
        start_after=os.getenv("FACT_AUDITOR_START_AFTER") or "",
    )
    if not (1 <= result.lookback_minutes <= 10080 and 1 <= result.max_items_per_run <= 200
            and 1 <= result.max_attempts <= 5 and 1 <= result.max_search_questions <= 5
            and 1 <= result.request_timeout <= 180 and 5 <= result.interval_seconds <= 300):
        raise ValueError("Invalid fact auditor limits")
    if result.api_style not in {"responses", "chat_completions"}:
        raise ValueError("Invalid fact auditor API style")
    if result.start_after:
        parsed = datetime.fromisoformat(result.start_after.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("FACT_AUDITOR_START_AFTER requires a timezone")
    return result
