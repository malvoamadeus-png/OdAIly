from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urljoin, urlparse, urlunparse

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
FORBES_BASE_URL = "https://www.forbes.com"
FORBES_SECTION_URL = "https://www.forbes.com/sites/digital-assets/"
FORBES_FEED_URL = "https://www.forbes.com/sites/digital-assets/feed/"
HK01_BASE_URL = "https://www.hk01.com"
FORTUNE_BASE_URL = "https://fortune.com"
FT_BASE_URL = "https://www.ft.com"
WSJ_BASE_URL = "https://www.wsj.com"
BLOOMBERG_BASE_URL = "https://www.bloomberg.com"
GOOGLE_NEWS_BASE_URL = "https://news.google.com"
FT_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=site:ft.com+crypto&hl=en-US&gl=US&ceid=US:en"
FT_GOOGLE_NEWS_MAX_ITEMS = 25
HK01_ISSUE_URL = (
    "https://www.hk01.com/issue/10154/"
    "nft%E8%99%9B%E6%93%AC%E8%B2%A8%E5%B9%A3-"
    "%E5%B0%88%E5%8D%80-%E6%AF%94%E7%89%B9%E5%B9%A3-"
    "%E4%BB%A5%E5%A4%AA%E5%B9%A3-%E5%8D%80%E5%A1%8A"
    "%E9%8F%88%E4%B8%AD%E4%BD%A0%E9%9C%80%E9%97%9C%E6%B3%A8"
    "%E7%9A%84%E4%B8%80%E5%88%87"
)
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
        pipeline_mode="write_flow",
    ),
    "forbes_digital_assets": SiteDefinition(
        site_key="forbes_digital_assets",
        display_name="Forbes Digital Assets",
        homepage_url=FORBES_SECTION_URL,
        list_url=FORBES_FEED_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "hk01_virtual_assets": SiteDefinition(
        site_key="hk01_virtual_assets",
        display_name="HK01 NFT / Virtual Assets",
        homepage_url=HK01_ISSUE_URL,
        list_url=HK01_ISSUE_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "ft_crypto": SiteDefinition(
        site_key="ft_crypto",
        display_name="FT Crypto",
        homepage_url="https://www.ft.com/crypto",
        list_url="https://www.ft.com/crypto",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_business": SiteDefinition(
        site_key="wsj_business",
        display_name="WSJ Business",
        homepage_url="https://www.wsj.com/business?mod=nav_top_section",
        list_url="https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_economy": SiteDefinition(
        site_key="wsj_economy",
        display_name="WSJ Economy",
        homepage_url="https://www.wsj.com/economy?mod=nav_top_section",
        list_url="https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_finance": SiteDefinition(
        site_key="wsj_finance",
        display_name="WSJ Finance",
        homepage_url="https://www.wsj.com/finance?mod=nav_top_section",
        list_url="https://feeds.content.dowjones.io/public/rss/socialmarketsfeed",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "fortune_crypto": SiteDefinition(
        site_key="fortune_crypto",
        display_name="Fortune Crypto",
        homepage_url="https://fortune.com/section/crypto/",
        list_url="https://fortune.com/section/crypto/",
        capture_method="html_request",
        pipeline_mode="alert_only",
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
    if site.site_key == "forbes_digital_assets":
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_forbes_pages(xml, base_url=site.homepage_url)
    if site.site_key == "hk01_virtual_assets":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_hk01_pages(html, base_url=site.homepage_url)
    if site.site_key == "ft_crypto":
        xml = fetch_html(
            FT_GOOGLE_NEWS_RSS_URL,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        return discover_ft_pages(
            xml,
            base_url=site.homepage_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    if site.site_key in {"wsj_business", "wsj_economy", "wsj_finance"}:
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_wsj_pages(xml, base_url=site.homepage_url)
    if site.site_key == "fortune_crypto":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_fortune_pages(html, base_url=site.homepage_url)
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
    if site.site_key == "forbes_digital_assets":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return parse_forbes_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    if site.site_key == "hk01_virtual_assets":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return parse_hk01_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
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


def discover_forbes_pages(xml_text: str, *, base_url: str = FORBES_BASE_URL) -> list[DiscoveredPage]:
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid Forbes RSS payload") from exc
    for item in root.findall(".//item"):
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        excerpt = clean_inline_text(item.findtext("description", default=""))
        if not link:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if not is_forbes_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                excerpt=excerpt or None,
            )
        )
    return results


def discover_hk01_pages(html: str, *, base_url: str = HK01_BASE_URL) -> list[DiscoveredPage]:
    payload = extract_next_data_payload(html)
    props = payload.get("props", {})
    page_props = props.get("pageProps") or props.get("initialProps", {}).get("pageProps", {})
    issue = page_props.get("issue") or {}
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for block in issue.get("blocks", []):
        for article in block.get("articles", []):
            article_data = article.get("data") or {}
            detail_path = article_data.get("publishUrl") or article_data.get("canonicalUrl")
            if not detail_path:
                continue
            detail_url = normalize_url(urljoin(base_url, str(detail_path)))
            if detail_url in seen:
                continue
            seen.add(detail_url)
            title = clean_inline_text(str(article_data.get("title") or article_data.get("name") or ""))
            results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title or None))
    return results


def discover_fortune_pages(html: str, *, base_url: str = FORTUNE_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        if not is_fortune_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title))
    return results


def discover_ft_pages(
    xml_text: str,
    *,
    base_url: str = FT_BASE_URL,
    timeout_seconds: float = 20.0,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid FT discovery RSS payload") from exc
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for item in root.findall(".//item")[:FT_GOOGLE_NEWS_MAX_ITEMS]:
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        source_name = clean_inline_text(item.findtext("source", default=""))
        description_html = item.findtext("description", default="")
        if not link or not title:
            continue
        if source_name and source_name != "Financial Times":
            continue
        decoded_url = decode_google_news_url(
            link,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        if not decoded_url:
            continue
        detail_url = normalize_url(urljoin(base_url, decoded_url))
        if not is_ft_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        excerpt = extract_google_news_excerpt(description_html, title=title, source_name=source_name or "Financial Times")
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=strip_google_news_source_suffix(title, "Financial Times"),
                excerpt=excerpt or None,
            )
        )
    return results


def discover_wsj_pages(xml_text: str, *, base_url: str = WSJ_BASE_URL) -> list[DiscoveredPage]:
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid WSJ RSS payload") from exc
    for item in root.findall(".//item"):
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        excerpt = clean_inline_text(item.findtext("description", default=""))
        if not link or not title:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if not is_wsj_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title,
                excerpt=excerpt or None,
            )
        )
    return results


def discover_bloomberg_pages(html: str, *, base_url: str = BLOOMBERG_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        if not is_bloomberg_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title))
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


def parse_forbes_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    payload = extract_next_data_payload(html)
    article = (((payload.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("article") or {}
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(article.get("canonicalUrl") or "")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            article.get("title")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_published_at(article.get("date"))
    author_names = normalize_string_list(article.get("authorsList"), field_name="name") or normalize_string_list(
        article.get("author"),
        field_name="name",
    )
    categories = normalize_string_list(
        [
            article.get("displayChannel"),
            article.get("displaySection"),
            article.get("channelSection"),
        ]
    )
    body = clean_body_text(extract_forbes_body(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            article.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "article_id": article.get("articleId"),
        "blog_name": article.get("blogName"),
        "date_raw": article.get("date"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=published_at,
        author_names=author_names,
        tags=[],
        categories=categories,
        excerpt=excerpt,
        content_format="forbes_digital_assets",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "article_id": article.get("articleId"),
            "blog_name": article.get("blogName"),
        },
        metadata=metadata,
    )


def parse_hk01_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    payload = extract_next_data_payload(html)
    props = payload.get("props", {})
    page_props = props.get("pageProps") or props.get("initialProps", {}).get("pageProps", {})
    article = page_props.get("article") or {}
    canonical = normalize_url(
        urljoin(
            HK01_BASE_URL,
            str(article.get("canonicalUrl") or article.get("publishUrl") or select_attr(soup, "link[rel='canonical']", "href") or page_url),
        )
    )
    title = clean_inline_text(
        str(
            article.get("title")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_unix_timestamp(article.get("publishTime")) or parse_published_at(article.get("publishTime"))
    author_names = normalize_string_list(article.get("authors"), field_name="name")
    tags = normalize_string_list(article.get("tags"), field_name="name")
    categories = normalize_string_list(
        [
            article.get("mainCategory"),
            article.get("categories"),
        ],
        field_name="name",
    )
    body = clean_body_text(extract_hk01_body(article.get("blocks", [])) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            article.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "article_id": article.get("articleId"),
        "publish_time_raw": article.get("publishTime"),
        "content_type": article.get("contentType"),
        "main_category_id": article.get("mainCategoryId"),
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
        content_format="hk01_issue_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "article_id": article.get("articleId"),
            "content_type": article.get("contentType"),
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


def extract_next_data_payload(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if match is None:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def decode_google_news_url(
    source_url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str | None:
    parsed = urlparse(source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() != "news.google.com" or len(path_parts) < 2 or path_parts[-2] not in {"articles", "read"}:
        return source_url
    base64_str = path_parts[-1]
    params = get_google_news_decoding_params(
        base64_str,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )
    if not params:
        return None
    signature, timestamp = params
    return execute_google_news_decode(
        base64_str,
        signature=signature,
        timestamp=timestamp,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )


def get_google_news_decoding_params(
    base64_str: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[str, str] | None:
    for candidate_url in (
        f"{GOOGLE_NEWS_BASE_URL}/articles/{base64_str}",
        f"{GOOGLE_NEWS_BASE_URL}/rss/articles/{base64_str}",
    ):
        try:
            html = fetch_html(
                candidate_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
        except RuntimeError:
            continue
        signature_match = re.search(r'data-n-a-sg="([^"]+)"', html)
        timestamp_match = re.search(r'data-n-a-ts="([^"]+)"', html)
        if signature_match and timestamp_match:
            return signature_match.group(1), timestamp_match.group(1)
    return None


def execute_google_news_decode(
    base64_str: str,
    *,
    signature: str,
    timestamp: str,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str | None:
    payload = [
        "Fbv4je",
        (
            f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{base64_str}",{timestamp},"{signature}"]'
        ),
    ]
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.post(
                f"{GOOGLE_NEWS_BASE_URL}/_/DotsSplashUi/data/batchexecute",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "User-Agent": REQUEST_HEADERS["User-Agent"],
                },
                data=f"f.req={quote(json.dumps([[payload]]))}",
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            parts = response.text.split("\n\n")
            if len(parts) < 2:
                return None
            parsed = json.loads(parts[1])[:-2]
            decoded = json.loads(parsed[0][2])[1]
            return normalize_url(decoded)
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(max(0.0, backoff_seconds) * attempt)
    if last_error:
        return None
    return None


def strip_google_news_source_suffix(title: str, source_name: str) -> str:
    suffix = f" - {source_name}".strip()
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title.strip()


def extract_google_news_excerpt(description_html: str, *, title: str, source_name: str) -> str:
    if not description_html:
        return ""
    text = clean_inline_text(BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True))
    clean_title = strip_google_news_source_suffix(title, source_name)
    if text.startswith(clean_title):
        text = text[len(clean_title) :].strip()
    if text.endswith(source_name):
        text = text[: -len(source_name)].strip()
    return text.strip(" -\u00a0")


def is_forbes_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("forbes.com") and parsed.path.startswith("/sites/digital-assets/") and not parsed.path.endswith("/feed/")


def is_ft_article_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return parsed.netloc.lower().endswith("ft.com") and len(parts) == 2 and parts[0] == "content"


def is_fortune_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("fortune.com"):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return False
    if parts[0] == "section":
        return False
    return all(part.isdigit() for part in parts[:3])


def is_wsj_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("wsj.com"):
        return False
    path = parsed.path.rstrip("/")
    if path.startswith("/articles/"):
        return True
    parts = [part for part in path.split("/") if part]
    return len(parts) >= 2 and parts[0] in {"business", "economy", "finance"}


def is_bloomberg_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("bloomberg.com"):
        return False
    return parsed.path.startswith(("/news/articles/", "/opinion/articles/", "/graphics/"))


def extract_forbes_body(soup: BeautifulSoup) -> str:
    best_text = ""
    for selector in (".article-body", ".fs-article", "article"):
        node = soup.select_one(selector)
        if node is None:
            continue
        fragment = BeautifulSoup(str(node), "html.parser")
        drop_noise(fragment)
        parts: list[str] = []
        for part in collect_text_parts(fragment):
            lower_part = part.lower()
            if lower_part.startswith("sign up now for cryptocodex"):
                continue
            if lower_part.startswith("sign up now for the free cryptocodex"):
                continue
            if lower_part.startswith("this voice experience is generated by ai"):
                continue
            parts.append(part)
        text = "\n\n".join(parts).strip()
        if len(text) > len(best_text):
            best_text = text
    return best_text


def extract_hk01_body(blocks: list[dict[str, Any]]) -> str:
    html_token_parts: list[str] = []
    summary_parts: list[str] = []
    for block in blocks:
        token_parts = extract_hk01_text_nodes(block.get("htmlTokens"))
        if token_parts:
            html_token_parts.extend(token_parts)
            continue
        summary_parts.extend(extract_hk01_text_nodes(block.get("summary")))
    parts = html_token_parts or summary_parts
    return "\n\n".join(unique_preserve_order(parts)).strip()


def extract_hk01_text_nodes(value: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(value, str):
        text = clean_inline_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))
        if text:
            parts.append(text)
        return parts
    if isinstance(value, list):
        for item in value:
            parts.extend(extract_hk01_text_nodes(item))
        return parts
    if isinstance(value, dict):
        for key in ("content", "text", "summary", "title"):
            if key in value:
                parts.extend(extract_hk01_text_nodes(value.get(key)))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                parts.extend(extract_hk01_text_nodes(nested))
        return parts
    return parts


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
        if not isinstance(node, Tag) or getattr(node, "attrs", None) is None:
            continue
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
        lower_normalized = normalized.lower()
        if lower_normalized.startswith("sign up now for cryptocodex"):
            continue
        if lower_normalized.startswith("sign up now for the free cryptocodex"):
            continue
        if lower_normalized.startswith("this voice experience is generated by ai"):
            continue
        if normalized.lower().startswith(("related posts", "read more", "recommended")):
            continue
        paragraphs.append(normalized)
    return "\n\n".join(paragraphs).strip()


def infer_content_format(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith("forbes.com") and parsed.path.startswith("/sites/digital-assets/"):
        return "forbes_digital_assets"
    if parsed.netloc.lower().endswith("hk01.com"):
        return "hk01_issue_article"
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


def parse_unix_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number = number / 1000
    return datetime.fromtimestamp(number, tz=UTC)


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
