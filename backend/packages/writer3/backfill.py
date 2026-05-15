from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from packages.common.time_utils import SHANGHAI_TZ

from .models import OdailyReference


HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; OdAIlyWriter3/1.0)",
    "Accept": "application/json,text/plain,*/*",
}


def backfill_odaily_references(
    *,
    repository,
    days: int,
    timeout_seconds: float,
    page_size: int = 100,
    sleep_seconds: float = 0.2,
) -> dict[str, int]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    fetched = 0
    upserted = 0
    page = 1
    while True:
        payload = fetch_odaily_page(page=page, size=page_size, timeout_seconds=timeout_seconds)
        items = [parse_odaily_item(item) for item in extract_list(payload)]
        items = [item for item in items if item is not None]
        if not items:
            break
        fetched += len(items)
        in_window = [item for item in items if item.published_at is None or item.published_at >= cutoff]
        if in_window:
            upserted += repository.upsert_odaily_references(in_window)
        oldest = min((item.published_at for item in items if item.published_at), default=None)
        if oldest and oldest < cutoff:
            break
        if not has_more(payload):
            break
        page += 1
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
    return {"fetched": fetched, "upserted": upserted, "pages": page}


def fetch_odaily_page(*, page: int, size: int, timeout_seconds: float) -> Any:
    last_error: Exception | None = None
    for base_url in ("https://api.odaily.news", "https://rss.odaily.news"):
        try:
            response = requests.get(
                f"{base_url}/api/v1/newsflash",
                params={"page": page, "size": size, "lang": "zh-cn"},
                headers=HEADERS,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "Odaily newsflash request failed")


def parse_odaily_item(news: dict[str, Any]) -> OdailyReference | None:
    title = clean_text(str(news.get("title") or ""))
    if not title:
        return None
    source_id = str(news.get("id") or news.get("newsflashId") or "").strip()
    if not source_id:
        source_id = str(abs(hash(title)))
    content = remove_odaily_prefix(normalize_content(title, str(news.get("content") or news.get("description") or news.get("summary") or title)))
    published_at = parse_datetime(news.get("publishTimestamp") or news.get("publishDate") or news.get("publishedAt") or news.get("createdAt") or news.get("createTime"))
    source_url = f"https://www.odaily.news/zh-CN/newsflash/{source_id}"
    return OdailyReference(
        source_item_id=source_id,
        source_url=source_url,
        title=title,
        content=content,
        published_at=published_at,
        raw_payload=news,
        metadata={},
    )


def extract_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "data", "items", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def has_more(payload: Any) -> bool:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and "hasMore" in data:
        return bool(data.get("hasMore"))
    return bool(extract_list(payload))


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)
    text = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            parsed = datetime.strptime(text, fmt)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=SHANGHAI_TZ).astimezone(UTC)
            return parsed.astimezone(UTC)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=SHANGHAI_TZ).astimezone(UTC)
    return parsed.astimezone(UTC)


def normalize_content(title: str, content: str) -> str:
    value = clean_text(strip_html(content))
    return value or clean_text(strip_html(title))


def remove_odaily_prefix(text: str) -> str:
    return re.sub(r"^Odaily\s*星球日报讯\s*", "", clean_text(strip_html(text)), flags=re.IGNORECASE).strip()


def strip_html(value: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
