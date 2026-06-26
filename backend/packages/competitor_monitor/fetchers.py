from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from typing import Any

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

BRAND_PATTERNS = [
    r"BlockBeats\s*消息[，,]\s*\d{1,2}\s*月\s*\d{1,2}\s*日[，,]\s*",
    r"PANews\s*\d{1,2}\s*月\s*\d{1,2}\s*日消息[，,]\s*",
    r"金色财经报道[，,]?\s*(\d{1,2}\s*月\s*\d{1,2}\s*日[，,])?\s*",
    r"Odaily\s*星球日报讯\s*",
]
COMPETITOR_BRAND_WORDS = ["律动", "BlockBeats", "PANews", "金色财经"]
ODAILY_BRAND_WORDS = ["Odaily 星球日报讯", "Odaily星球日报讯"]


@dataclass(frozen=True, slots=True)
class NewsflashItem:
    source: str
    source_item_id: str
    title: str
    content: str
    source_url: str | None = None
    published_at: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def strip_html(text: str, *, preserve_paragraph_breaks: bool = False) -> str:
    value = html.unescape(text or "")
    if preserve_paragraph_breaks:
        value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
        value = re.sub(r"<p\b[^>]*>", "", value, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", value).strip()


def clean_text(text: str, *, preserve_paragraph_breaks: bool = False) -> str:
    junk = ["原文链接", "微信扫码", "分享划过弹出", "复制链接", "转发到微博", "重要快讯", "点赞", "收藏"]
    for item in junk:
        text = text.replace(item, "")
    if preserve_paragraph_breaks:
        lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(line for line in lines if line).strip()
    return re.sub(r"\s+", " ", text).strip()


def remove_media_prefix(text: str, *, preserve_paragraph_breaks: bool = False) -> str:
    value = clean_text(strip_html(text, preserve_paragraph_breaks=preserve_paragraph_breaks), preserve_paragraph_breaks=preserve_paragraph_breaks)
    for pattern in BRAND_PATTERNS:
        value = re.sub(rf"^{pattern}", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^据[^，,]{2,20}(报道|监测|数据|消息)[，,]\s*", "", value).strip()
    return value


def scrub_competitor_brands(text: str, *, preserve_paragraph_breaks: bool = False) -> str:
    value = remove_media_prefix(text, preserve_paragraph_breaks=preserve_paragraph_breaks)
    for word in COMPETITOR_BRAND_WORDS:
        value = value.replace(word, "")
    return clean_text(value, preserve_paragraph_breaks=preserve_paragraph_breaks)


def normalize_item_content(title: str, content: str, *, preserve_paragraph_breaks: bool = False) -> str:
    value = clean_text(strip_html(content, preserve_paragraph_breaks=preserve_paragraph_breaks), preserve_paragraph_breaks=preserve_paragraph_breaks)
    if title and value.startswith(title):
        value = value[len(title):].strip()
    if title and value.startswith(f"【{title}】"):
        value = value[len(title) + 2:].strip()
    value = scrub_competitor_brands(value, preserve_paragraph_breaks=preserve_paragraph_breaks)
    return value or scrub_competitor_brands(title, preserve_paragraph_breaks=preserve_paragraph_breaks)


def stable_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()[:16]


def fetch_blockbeats(*, api_key: str | None, timeout_seconds: float) -> list[NewsflashItem]:
    if not api_key:
        raise RuntimeError("Missing BLOCKBEATS_API_KEY")
    headers = dict(HEADERS)
    headers["api-key"] = api_key
    response = requests.get(
        "https://api-pro.theblockbeats.info/v1/newsflash",
        params={"page": 1, "size": 50, "lang": "cn"},
        headers=headers,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    news_list = _extract_list(payload)
    items: list[NewsflashItem] = []
    for news in news_list:
        title = str(news.get("title") or news.get("name") or "").strip()
        if not title:
            continue
        source_id = str(news.get("id") or news.get("flash_id") or news.get("newsflash_id") or stable_id("blockbeats", title))
        content = normalize_item_content(title, str(news.get("content") or news.get("description") or news.get("summary") or title))
        published_at = str(news.get("create_time") or news.get("created_at") or news.get("publish_time") or news.get("published_at") or "")
        source_url = str(news.get("url") or news.get("link") or f"https://www.theblockbeats.info/flash/{source_id}")
        items.append(NewsflashItem("blockbeats", source_id, scrub_competitor_brands(title), content, source_url, published_at, news))
    return items


def fetch_panews(*, timeout_seconds: float) -> list[NewsflashItem]:
    response = requests.get(
        "https://universal-api.panewslab.com/articles",
        params={"type": "NEWS", "isShowInList": "true", "take": 50, "skip": 0},
        headers=HEADERS,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    news_list = payload if isinstance(payload, list) else payload.get("data", [])
    items: list[NewsflashItem] = []
    for news in news_list:
        if not isinstance(news, dict):
            continue
        title = str(news.get("title") or "").strip()
        if not title:
            continue
        source_id = str(news.get("articleId") or news.get("id") or stable_id("panews", title))
        content = normalize_item_content(title, str(news.get("content") or news.get("desc") or news.get("summary") or title))
        source_url = f"https://www.panewslab.com/zh/articledetails/{source_id}.html"
        items.append(NewsflashItem("panews", source_id, scrub_competitor_brands(title), content, source_url, str(news.get("publishedAt") or ""), news))
    return items


def fetch_jinse(*, timeout_seconds: float) -> list[NewsflashItem]:
    response = requests.get(
        "https://api.coinmeta.info/live/list",
        params={"limit": 50},
        headers=HEADERS,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    items: list[NewsflashItem] = []
    for news in _extract_jinse_lives(payload):
        if not isinstance(news, dict):
            continue
        title = str(news.get("title") or extract_jinse_title(news.get("content")) or "").strip()
        if not title:
            continue
        published_at = news.get("created_at") or news.get("published_at") or ""
        live_id = news.get("id")
        source_url = str(news.get("jump_url") or (f"https://www.jinse2.com/lives/{live_id}.html" if live_id else "https://www.jinse2.com/lives"))
        source_id = str(news.get("id") or extract_jinse_live_id(source_url) or stable_id("jinse", title, str(published_at)))
        content = normalize_item_content(title, str(news.get("content") or news.get("summary") or title))
        items.append(NewsflashItem("jinse", source_id, scrub_competitor_brands(title), content, source_url, str(published_at), news))
    return items


def _extract_jinse_lives(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    lives: list[dict[str, Any]] = []
    for group in payload.get("list") or []:
        if isinstance(group, dict):
            lives.extend(item for item in group.get("lives") or [] if isinstance(item, dict))
    if lives:
        return lives
    data = payload.get("data")
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def extract_jinse_title(content: Any) -> str:
    text = clean_text(strip_html(str(content or "")))
    match = re.match(r"^【([^】]+)】", text)
    return match.group(1).strip() if match else ""


def extract_jinse_live_id(source_url: str | None) -> str | None:
    if not source_url:
        return None
    match = re.search(r"/lives/(\d+)(?:\.html)?(?:[?#].*)?$", str(source_url))
    return match.group(1) if match else None


def fetch_odaily(*, timeout_seconds: float) -> list[NewsflashItem]:
    payload = _fetch_odaily_payload(timeout_seconds=timeout_seconds)
    items: list[NewsflashItem] = []
    for news in _extract_list(payload):
        title = str(news.get("title") or "").strip()
        if not title:
            continue
        source_id = str(news.get("id") or news.get("newsflashId") or stable_id("odaily", title))
        content = remove_odaily_prefix(
            normalize_item_content(
                title,
                str(news.get("content") or news.get("description") or news.get("summary") or title),
                preserve_paragraph_breaks=True,
            ),
            preserve_paragraph_breaks=True,
        )
        published_at = str(news.get("publishDate") or news.get("publishedAt") or news.get("createdAt") or news.get("createTime") or "")
        source_url = str(news.get("sourceUrl") or news.get("link") or news.get("url") or f"https://www.odaily.news/zh-CN/newsflash/{source_id}")
        items.append(NewsflashItem("odaily", source_id, title, content, source_url, published_at, news))
    return items


def _fetch_odaily_payload(*, timeout_seconds: float) -> Any:
    last_error: Exception | None = None
    for base_url in ("https://api.odaily.news", "https://rss.odaily.news"):
        try:
            response = requests.get(
                f"{base_url}/api/v1/newsflash",
                params={"page": 1, "size": 50, "lang": "zh-cn"},
                headers=HEADERS,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
    raise RuntimeError(str(last_error) if last_error else "Odaily newsflash request failed")


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("list", "data", "items", "records", "rows"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def remove_odaily_prefix(text: str, *, preserve_paragraph_breaks: bool = False) -> str:
    return re.sub(
        r"^Odaily\s*星球日报讯\s*",
        "",
        clean_text(strip_html(text, preserve_paragraph_breaks=preserve_paragraph_breaks), preserve_paragraph_breaks=preserve_paragraph_breaks),
        flags=re.IGNORECASE,
    ).strip()
