from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .models import BinanceSquarePost


PROFILE_RESPONSE_MARKER = "queryUserProfilePageContentsWithFilter"


def normalize_profile_url(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("币安广场主页链接不能为空")
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme not in {"http", "https"} or (parsed.hostname or "").lower() not in {
        "binance.com",
        "www.binance.com",
    }:
        raise ValueError("只支持 www.binance.com 的币安广场主页链接")
    parts = [part for part in parsed.path.split("/") if part]
    try:
        square_index = parts.index("square")
    except ValueError as exc:
        raise ValueError("链接必须是币安广场账号主页") from exc
    if len(parts) != square_index + 3 or parts[square_index + 1] != "profile":
        raise ValueError("链接必须符合 /square/profile/{账号} 格式")
    slug = parts[square_index + 2].strip()
    if not slug or any(char in slug for char in "/?#"):
        raise ValueError("币安广场账号标识无效")
    return slug, f"https://www.binance.com/en/square/profile/{slug}"


def _find_content_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        rows = [item for item in value if isinstance(item, dict)]
        if rows and any("contentType" in row and "id" in row for row in rows):
            return rows
        for item in value:
            found = _find_content_rows(item)
            if found:
                return found
    elif isinstance(value, dict):
        for item in value.values():
            found = _find_content_rows(item)
            if found:
                return found
    return []


def _media_urls(row: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for image in row.get("imageList") or []:
        if isinstance(image, str):
            values.append(image)
        elif isinstance(image, dict):
            candidate = image.get("url") or image.get("originalUrl") or image.get("thumbnailUrl")
            if candidate:
                values.append(str(candidate))
    if row.get("videoLink"):
        values.append(str(row["videoLink"]))
    return list(dict.fromkeys(value for value in values if value))


def _published_at(value: Any) -> str | None:
    try:
        stamp = int(value)
    except (TypeError, ValueError):
        return None
    if stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp / 1000, tz=UTC).isoformat()


def parse_profile_response(payload: Any) -> list[BinanceSquarePost]:
    posts: list[BinanceSquarePost] = []
    for row in _find_content_rows(payload):
        if int(row.get("contentType") or 0) != 1:
            continue
        post_id = str(row.get("id") or "").strip()
        text = str(row.get("bodyTextOnly") or "").strip()
        if not post_id or not text:
            continue
        posts.append(
            BinanceSquarePost(
                post_id=post_id,
                username=str(row.get("username") or "").strip(),
                display_name=str(row.get("displayName") or row.get("username") or "").strip(),
                text=text,
                published_at=_published_at(row.get("firstReleaseTime")),
                url=str(row.get("webLink") or f"https://www.binance.com/en/square/post/{post_id}").strip(),
                square_uid=str(row.get("userId") or row.get("squareUid") or "").strip() or None,
                media_urls=_media_urls(row),
                raw_payload=row,
            )
        )
    posts.sort(key=lambda item: (item.published_at or "", item.post_id))
    return posts[-20:]


class BinanceSquareClient:
    def __init__(self, *, timeout_seconds: float = 45.0) -> None:
        self.timeout_seconds = max(5.0, float(timeout_seconds))
        self._playwright = None
        self._browser = None
        self._context = None

    def _ensure_browser(self):
        if self._context is not None:
            return self._context
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=False)
        self._context = self._browser.new_context(locale="zh-CN")
        return self._context

    def fetch_profile(self, profile_url: str) -> list[BinanceSquarePost]:
        context = self._ensure_browser()
        page = context.new_page()
        try:
            with page.expect_response(
                lambda response: PROFILE_RESPONSE_MARKER in response.url,
                timeout=self.timeout_seconds * 1000,
            ) as response_info:
                page.goto(profile_url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1000)
            response = response_info.value
            if not response.ok:
                raise RuntimeError(f"币安广场内容请求失败: HTTP {response.status}")
            posts = parse_profile_response(response.json())
            if not posts:
                raise RuntimeError("币安广场响应没有可用的普通帖子")
            return posts
        finally:
            page.close()

    def close(self) -> None:
        if self._context is not None:
            self._context.close()
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._context = self._browser = self._playwright = None
