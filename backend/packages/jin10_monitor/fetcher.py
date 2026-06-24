from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

import requests

from .models import Jin10Item


def fetch_jin10_items(
    *,
    endpoint_url: str,
    headers: dict[str, str],
    channel: str | None,
    timeout_seconds: float,
) -> list[Jin10Item]:
    params: dict[str, str] = {}
    if channel:
        params["channel"] = channel
    response = requests.get(endpoint_url, params=params, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    return parse_jin10_payload(payload)


def parse_jin10_payload(payload: Any) -> list[Jin10Item]:
    items: list[Jin10Item] = []
    for row in _extract_items(payload):
        item = _parse_item(row)
        if item is not None:
            items.append(item)
    return _dedupe_items(items)


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("list", "items", "rows", "flash", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_items(value)
            if nested:
                return nested
    return []


def _parse_item(row: dict[str, Any]) -> Jin10Item | None:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    title = _clean_text(_first_text(row, data, keys=("title", "name", "headline")))
    content = _clean_text(
        _first_text(
            row,
            data,
            keys=("content", "remark", "summary", "text", "detail", "event_content"),
        )
    )
    if not content and title:
        content = title
    if not title:
        title = _title_from_content(content)
    if not content:
        return None
    source_item_id = _first_text(row, data, keys=("id", "news_id", "newsID", "flash_id", "data_id"))
    if not source_item_id:
        source_item_id = _stable_id(title, content, _first_text(row, data, keys=("time", "date", "created_at")))
    source_url = _first_text(row, data, keys=("url", "link", "source_url", "jump_url"))
    if not source_url:
        source_url = f"https://flash.jin10.com/detail/{source_item_id}"
    published_at = parse_jin10_datetime(
        _first_text(row, data, keys=("time", "date", "datetime", "created_at", "updated_at", "publish_time"))
    )
    return Jin10Item(
        source_item_id=str(source_item_id),
        title=title,
        content=content,
        source_url=source_url,
        published_at=published_at,
        raw_payload=row,
        metadata={
            "source_kind": "jin10",
            "jin10_type": row.get("type"),
            "jin10_channel": row.get("channel") or data.get("channel"),
        },
    )


def _first_text(*dicts: dict[str, Any], keys: tuple[str, ...]) -> str:
    for source in dicts:
        for key in keys:
            value = source.get(key)
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text:
                return text
    return ""


def _clean_text(value: str) -> str:
    text = re.sub(r"<[^>]+>", "", value or "")
    return re.sub(r"\s+", " ", text).strip()


def _title_from_content(content: str) -> str:
    match = re.match(r"^【([^】]+)】", content)
    if match:
        return match.group(1).strip()
    sentence = re.split(r"[。！？!?]", content, maxsplit=1)[0].strip()
    return sentence[:80] if sentence else content[:80]


def _stable_id(*parts: str) -> str:
    text = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def _dedupe_items(items: list[Jin10Item]) -> list[Jin10Item]:
    seen: set[str] = set()
    deduped: list[Jin10Item] = []
    for item in items:
        if item.source_item_id in seen:
            continue
        seen.add(item.source_item_id)
        deduped.append(item)
    return deduped


def parse_jin10_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{10,13}", text):
            number = int(text)
            if len(text) == 13:
                number = number / 1000
            return datetime.fromtimestamp(number, tz=UTC)
        normalized = text.replace("/", "-").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except Exception:
        return None
