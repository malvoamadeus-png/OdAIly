"""Editorial replay without modifying tasks, queues, or external drafts."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.x_processing.title_trace import WRITER_JSON_SCHEMA, parse_structured_writer_output
from packages.x_processing.worker import build_structured_writer_prompt
from .copy import format_news


def generate_preview(*, task: Any, prompt: Any, settings: Any, ai_client: Any) -> dict[str, Any]:
    raw = ai_client.generate_text(model=settings.writer_model,
        prompt=build_structured_writer_prompt(task=task, prompt=prompt, known_subjects=[]),
        text_format=WRITER_JSON_SCHEMA, reasoning_effort=settings.writer_reasoning_effort)
    structured = parse_structured_writer_output(raw, known_subjects=[], feature_mode_enabled=prompt.feature_mode_enabled)
    final = format_news(structured.draft, task.raw_payload)
    return {'title': final.title, 'content': final.content, 'writer_model': settings.writer_model,
            'prompt_version_id': prompt.id, 'prompt_version_number': prompt.version_number,
            'generated_at': datetime.now(UTC).isoformat(), 'observation_ms': task.raw_payload['observed_ms'],
            'mode': 'preview_only', 'writer_output': raw}
