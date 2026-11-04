"""News admission and identities, independent of technical observation records."""
from __future__ import annotations

import json
import re
from typing import Any


def json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(value or '{}')
        return result if isinstance(result, dict) else {}
    except (TypeError, ValueError):
        return {}


def news_title(*values: Any) -> str:
    for value in values:
        text = re.sub(r'\s+', ' ', str(value or '')).strip()
        if text and not text.isdecimal():
            return text[:160]
    return '新闻标题待补充'


def task_admission(status: str, pipeline: dict[str, Any], source: str) -> str:
    judge = json_object(pipeline.get('judge_output'))
    search = json_object(pipeline.get('search_result'))
    if status == 'duplicate' or search.get('is_duplicate') is True or search.get('duplicate') is True:
        return 'duplicate'
    if status in {'discarded', 'ignored', 'legacy_skipped', 'off_topic', 'expired'} or judge.get('route') == 'discard':
        return 'rejected'
    # deduped means the source-specific prerequisites passed; it does NOT mean duplicate.
    if pipeline.get('news_type') or judge.get('route') in {'regular', 'onchain', 'funding', 'publish'}:
        return 'accepted'
    if status in {'judged', 'deduped', 'writing', 'written', 'formatting', 'formatted', 'publisher_pending', 'publishing',
                  'ready_review', 'auto_published', 'published', 'write_failed', 'format_failed',
                  'format_publish_failed', 'publisher_failed', 'publish_failed'}:
        return 'accepted'
    if status in {'deduping', 'searched', 'search_failed'} and source in {'x', 'binance_square', 'jin10'}:
        return 'accepted'
    if source in {'hyperliquid_news', 'event_tracking'} and status not in {'judge_failed', 'pending'}:
        return 'accepted'
    return 'unassessed'


def duplicate_target(pipeline: dict[str, Any]) -> dict[str, Any] | None:
    search = json_object(pipeline.get('search_result'))
    target = search.get('duplicate_target')
    return target if isinstance(target, dict) else None
