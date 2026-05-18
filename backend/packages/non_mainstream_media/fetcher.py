from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from .models import DiscoveredPage, ParsedArticle, SiteDefinition


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

A16Z_CONTENT_TYPES = {"article", "podcast", "videos", "listicles", "papers"}
A16Z_BASE_URL = "https://a16zcrypto.com"
DISCLAIMER_MARKERS = [
    "the views expressed here are those of the individual ah capital management",
    "the views expressed here are those of the individual a16z personnel",
    "this material is for informational purposes only",
    "nothing in this post constitutes investment, legal, or tax advice",
    "certain information contained herein has been obtained from third-party sources",
    "see a16z.com/disclosures",
]


SITE_REGISTRY: dict[str, SiteDefinition] = {
    "a16z_crypto_posts": SiteDefinition(
        site_key="a16z_crypto_posts",
        display_name="a16z crypto Posts",
        homepage_url="https://a16zcrypto.com/posts/",
        list_url="https://a16zcrypto.com/posts/",
        capture_method="html_request",
    ),
}


def get_site_registry() -> dict[str, SiteDefinition]:
    return dict(SITE_REGISTRY)


def fetch_discovered_pages(
    site: SiteDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    if site.site_key == "a16z_crypto_posts":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_a16z_pages(html, base_url=site.homepage_url)
    raise ValueError(f"unsupported site registry entry: {site.site_key}")


def fetch_article(
    site: SiteDefinition,
    page: DiscoveredPage,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> ParsedArticle:
    if site.site_key == "a16z_crypto_posts":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return parse_a16z_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    raise ValueError(f"unsupported site registry entry: {site.site_key}")


def fetch_html(
    url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(max(0.0, backoff_seconds) * attempt)
    raise RuntimeError(f"request failed url={url}: {last_error}") from last_error


def discover_a16z_pages(html: str, *, base_url: str = A16Z_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        parsed = urlparse(detail_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "posts":
            continue
        if parts[1] not in A16Z_CONTENT_TYPES:
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url))
    return results


def parse_a16z_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    structured = find_structured_content(soup)
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(structured.get("url") or "")
        or page_url
    )
    title = clean_inline_text(
        str(
            structured.get("headline")
            or structured.get("name")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_published_at(structured.get("datePublished") or structured.get("dateCreated"))
    author_names = normalize_string_list(structured.get("author"), field_name="name")
    categories = normalize_string_list(structured.get("articleSection"))
    tags = normalize_keywords(structured.get("keywords"))
    body = clean_body_text(str(structured.get("articleBody") or "") or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            structured.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    content_format = infer_content_format(canonical)
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": structured.get("datePublished") or structured.get("dateCreated"),
        "structured_type": structured.get("@type"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=published_at,
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format=content_format,
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "structured_type": structured.get("@type"),
            "structured_headline": structured.get("headline") or structured.get("name"),
        },
        metadata=metadata,
    )


def find_structured_content(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        match = pick_structured_article(payload)
        if match:
            return match
    return {}


def pick_structured_article(payload: Any) -> dict[str, Any] | None:
    article_types = {
        "Article",
        "NewsArticle",
        "BlogPosting",
        "VideoObject",
        "PodcastEpisode",
        "PodcastSeries",
        "CreativeWork",
    }
    if isinstance(payload, dict):
        payload_type = payload.get("@type")
        type_values = payload_type if isinstance(payload_type, list) else [payload_type]
        if any(value in article_types for value in type_values):
            if payload.get("headline") or payload.get("name") or payload.get("articleBody"):
                return payload
        for value in payload.values():
            match = pick_structured_article(value)
            if match:
                return match
    elif isinstance(payload, list):
        for item in payload:
            match = pick_structured_article(item)
            if match:
                return match
    return None


def extract_body_text(soup: BeautifulSoup) -> str:
    candidates: list[Tag] = []
    for selector in (
        "article",
        "main article",
        "main",
        "[itemprop='articleBody']",
        ".entry-content",
        ".post-content",
        ".content",
        ".prose",
        ".wysiwyg",
    ):
        candidates.extend(soup.select(selector))
    if not candidates:
        candidates = [tag for tag in soup.select("div, section") if len(tag.select("p")) >= 4]
    best_text = ""
    for candidate in candidates:
        fragment = BeautifulSoup(str(candidate), "html.parser")
        drop_noise(fragment)
        parts = collect_text_parts(fragment)
        text = "\n\n".join(part for part in parts if part)
        if len(text) > len(best_text):
            best_text = text
    return best_text


def drop_noise(fragment: BeautifulSoup) -> None:
    for selector in (
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        "button",
        "svg",
        "aside",
        "picture",
        "img",
        "figure",
        "figcaption",
    ):
        for node in fragment.select(selector):
            node.decompose()
    pattern = re.compile(
        r"(share|social|related|recommend|newsletter|subscribe|breadcrumb|footer|header|nav|search|topic)",
        re.IGNORECASE,
    )
    for node in fragment.find_all(True):
        attrs = " ".join(
            str(value)
            for key in ("id", "class", "aria-label", "data-testid")
            for value in ([node.get(key)] if node.get(key) is not None else [])
        )
        if pattern.search(attrs):
            node.decompose()


def collect_text_parts(fragment: BeautifulSoup) -> list[str]:
    parts: list[str] = []
    for node in fragment.select("h1, h2, h3, h4, p, li, blockquote"):
        text = clean_inline_text(node.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) <= 2:
            continue
        parts.append(text)
    return parts


def clean_body_text(value: str) -> str:
    text = value.replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    lower_text = text.lower()
    cut_points = [lower_text.find(marker) for marker in DISCLAIMER_MARKERS if marker in lower_text]
    cut_points = [index for index in cut_points if index >= 0]
    if cut_points:
        text = text[: min(cut_points)].strip()
    paragraphs: list[str] = []
    for chunk in re.split(r"\n{2,}", text):
        normalized = clean_inline_text(chunk)
        if not normalized:
            continue
        if normalized.lower().startswith(("related posts", "read more", "recommended")):
            continue
        paragraphs.append(normalized)
    return "\n\n".join(paragraphs).strip()


def infer_content_format(url: str) -> str | None:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts":
        return parts[1]
    return None


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") + "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))


def parse_published_at(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def pick_heading_text(soup: BeautifulSoup) -> str:
    for selector in ("main h1", "article h1", "h1"):
        tag = soup.select_one(selector)
        if tag:
            return tag.get_text(" ", strip=True)
    return ""


def select_attr(soup: BeautifulSoup, selector: str, attr: str) -> str | None:
    node = soup.select_one(selector)
    if node is None:
        return None
    value = node.get(attr)
    return str(value).strip() if value else None


def select_meta_content(soup: BeautifulSoup, attr_name: str, attr_value: str) -> str | None:
    node = soup.find("meta", attrs={attr_name: attr_value})
    if node is None:
        return None
    content = node.get("content")
    return str(content).strip() if content else None


def normalize_string_list(value: Any, *, field_name: str | None = None) -> list[str]:
    items: list[str] = []
    if isinstance(value, list):
        for item in value:
            items.extend(normalize_string_list(item, field_name=field_name))
    elif isinstance(value, dict):
        if field_name and value.get(field_name):
            items.extend(normalize_string_list(value.get(field_name), field_name=field_name))
        elif value.get("name"):
            items.extend(normalize_string_list(value.get("name"), field_name=field_name))
    elif isinstance(value, str):
        cleaned = clean_inline_text(value)
        if cleaned:
            items.append(cleaned)
    return unique_preserve_order(items)


def normalize_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,|/]", value)
        return unique_preserve_order(clean_inline_text(part) for part in parts if clean_inline_text(part))
    return normalize_string_list(value)


def clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def unique_preserve_order(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
