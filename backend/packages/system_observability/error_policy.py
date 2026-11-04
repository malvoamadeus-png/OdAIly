"""Classification and calendar-day retention of dependency errors.

This module handles managed records, never application business states or retry inputs.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

ERROR_KEYS = frozenset({'error', 'last_error', 'error_message', 'error_code', 'failure_code', 'failure_category', 'failure_reason',
                        'error_summary', 'error_signature', 'response_archive_error', 'analysis_error'})
EXTERNAL_CATEGORIES = frozenset({'upstream_timeout', 'upstream_http', 'upstream_auth', 'upstream_network'})


def day_start_ms(now_ms: int | None = None) -> int:
    now = datetime.fromtimestamp(now_ms / 1000, UTC) if now_ms is not None else datetime.now(UTC)
    local = now + timedelta(hours=8)
    return int((local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)).timestamp() * 1000)


def failure_category(message: Any) -> str | None:
    text = str(message or '').casefold()
    if not text.strip():
        return None
    if text in EXTERNAL_CATEGORIES:
        return text
    if any(term in text for term in ('database is locked', 'database table is locked', 'disk is full', 'no space left', 'disk i/o', 'unable to open database')):
        return 'local_storage'
    if any(term in text for term in ('freshness', 'no target-author posts', 'no_target_author', 'no new posts', 'empty_result')):
        return 'expected_result'
    if re.search(r'\b(401|402|403|429)\b|access denied|rate.?limit|insufficient.?quota|authenticationerror|permissiondenied', text):
        return 'upstream_auth'
    if re.search(r'time.?out|timed out|readtimeout|connecttimeout|apitimeouterror', text):
        return 'upstream_timeout'
    if re.search(r'\b(404|408|500|502|503|504)\b|http.?error|badgateway|internalservererror|service.?unavailable', text):
        return 'upstream_http'
    if re.search(r'connection.?error|connection reset|connection refused|name resolution|sslerror|networkerror', text):
        return 'upstream_network'
    return 'program_or_unknown'


def error_messages(value: Any) -> list[str]:
    messages: list[str] = []
    if isinstance(value, dict):
        failed = any(term in str(value.get('status') or value.get('result') or '').casefold() for term in ('fail', 'error'))
        for key, part in value.items():
            if key.removeprefix('pipeline_') in ERROR_KEYS or (failed and key in {'reason', 'summary', 'raw_output', 'actual', 'analysis_error'}):
                if part:
                    messages.append(str(part))
            if isinstance(part, (dict, list)):
                messages.extend(error_messages(part))
            elif isinstance(part, str) and part.lstrip().startswith(('{', '[')):
                try:
                    messages.extend(error_messages(json.loads(part)))
                except ValueError:
                    pass
    elif isinstance(value, list):
        for part in value:
            messages.extend(error_messages(part))
    return messages


def external_error(value: Any) -> bool:
    return any(failure_category(message) in EXTERNAL_CATEGORIES for message in error_messages(value))


def scrub_external_errors(value: Any, _in_failure: bool = False) -> Any:
    """Remove external error material without keeping a per-record category or summary."""
    if isinstance(value, dict):
        external = _in_failure or external_error(value)
        if not external:
            return value
        result = {}
        for key, part in value.items():
            if external and (key.removeprefix('pipeline_') in ERROR_KEYS or key.removeprefix('pipeline_') in {'failure_category', 'response_gzip', 'response_ref',
                    'response_excerpt', 'raw_output', 'raw_model_response', 'raw_model_response_excerpt',
                    'reason', 'summary', 'actual', 'suggestion', 'validation', 'request_id'}):
                result[key] = None if key not in {'reason', 'summary', 'actual', 'suggestion', 'validation'} else ''
            elif isinstance(part, str) and part.lstrip().startswith(('{', '[')):
                try:
                    result[key] = json.dumps(scrub_external_errors(json.loads(part), external), ensure_ascii=False, separators=(',', ':'))
                except ValueError:
                    result[key] = part
            else:
                result[key] = scrub_external_errors(part, external)
        return result
    if isinstance(value, list):
        return [scrub_external_errors(part, _in_failure) for part in value]
    return value


def expire_external_errors(value: Any, happened_at_ms: int | None, *, now_ms: int | None = None) -> Any:
    if happened_at_ms is not None and happened_at_ms < day_start_ms(now_ms):
        return scrub_external_errors(value)
    return value
