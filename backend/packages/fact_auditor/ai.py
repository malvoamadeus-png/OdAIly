from __future__ import annotations

import time
from typing import Any

import requests

from packages.x_processing.ai_client import OpenAIResponsesClient, extract_response_text, parse_openai_response_payload

from .prompts import parse_object
from .settings import FactAuditorSettings


class FactReviewAI:
    def __init__(self, settings: FactAuditorSettings) -> None:
        self.settings = settings
        if not settings.api_key:
            raise ValueError("Missing fact auditor model credentials")
        self.text_client = OpenAIResponsesClient(
            api_key=settings.api_key, base_url=settings.base_url, api_style=settings.api_style,
            timeout_seconds=settings.request_timeout, max_attempts=1, backoff_seconds=0,
        )

    def review(self, prompt: str) -> dict[str, Any]:
        started = time.perf_counter()
        raw = self.text_client.generate_text(model=self.settings.model, prompt=prompt, reasoning_effort="medium")
        return {"result": parse_object(raw), "raw_output": raw, "model": self.settings.model,
                "duration_ms": round((time.perf_counter() - started) * 1000)}

    def search(self, prompt: str) -> dict[str, Any]:
        started = time.perf_counter()
        base = (self.settings.search_base_url or self.settings.base_url).rstrip("/")
        url = base if base.endswith("/responses") else base + "/responses"
        response = requests.post(url, headers={"Authorization": "Bearer " + (self.settings.search_api_key or self.settings.api_key)},
            json={"model": self.settings.model, "input": prompt, "tools": [{"type": "web_search"}],
                  "tool_choice": "required", "include": ["web_search_call.action.sources"], "reasoning": {"effort": "medium"}},
            timeout=self.settings.request_timeout)
        response.raise_for_status()
        payload = parse_openai_response_payload(response)
        calls = [x for x in payload.get("output", []) if x.get("type") == "web_search_call"]
        if not any(x.get("status") == "completed" for x in calls):
            raise ValueError("Search returned no completed web_search_call")
        sources: set[str] = set()
        for item in payload.get("output", []):
            for source in (item.get("action") or {}).get("sources", []):
                if isinstance(source, dict) and source.get("url"):
                    sources.add(source["url"])
            for block in item.get("content", []):
                for annotation in block.get("annotations", []):
                    if annotation.get("type") == "url_citation" and annotation.get("url"):
                        sources.add(annotation["url"])
        raw = extract_response_text(payload)
        return {"result": parse_object(raw), "raw_output": raw, "model": payload.get("model") or self.settings.model,
                "duration_ms": round((time.perf_counter() - started) * 1000), "usage": payload.get("usage", {}),
                "tool_calls": calls, "sources": sorted(sources)}
