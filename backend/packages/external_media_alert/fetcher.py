from __future__ import annotations

import os
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from packages.common.heartbeat import HeartbeatThrottle

from .models import ExternalMediaSourceDefinition, MediaNewsflashItem, MediaSourceRunResult
from .repository import ExternalMediaAlertRepository


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

THE_BLOCK_RSS_URL = "https://www.theblock.co/rss.xml"
MAX_ITEMS_PER_SOURCE = 25


SITE_REGISTRY: dict[str, ExternalMediaSourceDefinition] = {
    "coindesk": ExternalMediaSourceDefinition(
        site_key="coindesk",
        display_name="CoinDesk",
        homepage_url="https://www.coindesk.com/",
        feed_url="https://www.coindesk.com/arc/outboundfeeds/rss/",
        list_url="https://www.coindesk.com/",
        capture_method="rss",
    ),
    "cointelegraph": ExternalMediaSourceDefinition(
        site_key="cointelegraph",
        display_name="Cointelegraph",
        homepage_url="https://cointelegraph.com/",
        feed_url="https://cointelegraph.com/rss",
        list_url="https://cointelegraph.com/category/latest-news",
        capture_method="rss",
    ),
    "the_block": ExternalMediaSourceDefinition(
        site_key="the_block",
        display_name="The Block",
        homepage_url="https://www.theblock.co/",
        feed_url=THE_BLOCK_RSS_URL,
        list_url="https://www.theblock.co/latest-crypto-news",
        capture_method="rss",
    ),
    "decrypt": ExternalMediaSourceDefinition(
        site_key="decrypt",
        display_name="Decrypt",
        homepage_url="https://decrypt.co/",
        feed_url="https://decrypt.co/feed",
        list_url="https://decrypt.co/news",
        capture_method="rss",
    ),
}


def get_site_registry() -> dict[str, ExternalMediaSourceDefinition]:
    return dict(SITE_REGISTRY)


def fetch_site_newsflashes(
    site: ExternalMediaSourceDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[MediaNewsflashItem]:
    errors: list[str] = []
    if site.feed_url:
        try:
            xml_text = fetch_text(
                site.feed_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            items = parse_feed_items(
                site,
                xml_text,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            if items:
                return items
            errors.append(f"empty feed: {site.feed_url}")
        except Exception as exc:
            errors.append(f"feed failed: {exc}")
    if site.list_url:
        try:
            html = fetch_text(
                site.list_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            items = parse_html_items(site, html)
            if items:
                return items
            errors.append(f"empty html: {site.list_url}")
        except Exception as exc:
            errors.append(f"html failed: {exc}")
    raise RuntimeError("; ".join(errors) if errors else f"no fetch strategy for {site.site_key}")


def fetch_text(
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


def parse_feed_items(
    site: ExternalMediaSourceDefinition,
    xml_text: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[MediaNewsflashItem]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid RSS payload for {site.site_key}") from exc
    seen: set[str] = set()
    items: list[MediaNewsflashItem] = []
    for item in root.findall(".//item")[:MAX_ITEMS_PER_SOURCE]:
        title = clean_inline_text(item.findtext("title", default=""))
        if not title:
            continue
        link = clean_inline_text(item.findtext("link", default=""))
        if not link:
            continue
        source_url = normalize_url(urljoin(site.homepage_url, link))
        if source_url in seen:
            continue
        seen.add(source_url)
        description = first_non_empty(
            item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded", default=""),
            item.findtext("description", default=""),
            item.findtext("{https://www.w3.org/2005/Atom}summary", default=""),
        )
        content = choose_content(title=title, description=description)
        items.append(
            MediaNewsflashItem(
                source=site.site_key,
                title=title,
                content=content,
                source_url=source_url,
                published_at=parse_feed_datetime(item.findtext("pubDate", default="") or item.findtext("published", default="")),
                raw_payload={"title": title, "link": link, "description": description},
                metadata={
                    "site_key": site.site_key,
                    "site_display_name": site.display_name,
                    "capture_method": "rss",
                },
            )
        )
    return items


def parse_html_items(site: ExternalMediaSourceDefinition, html: str) -> list[MediaNewsflashItem]:
    if site.site_key == "coindesk":
        return discover_card_items(
            site,
            html,
            href_patterns=(r"^https://www\.coindesk\.com/.+/$", r"^https://www\.coindesk\.com/.+/.+/.+/$"),
            excluded_patterns=(r"/tv/", r"/video/", r"/podcasts?/"),
        )
    if site.site_key == "cointelegraph":
        return discover_card_items(
            site,
            html,
            href_patterns=(r"^https://cointelegraph\.com/news/.+$",),
        )
    if site.site_key == "the_block":
        return discover_card_items(
            site,
            html,
            href_patterns=(r"^https://www\.theblock\.co/post/\d+/.+$",),
        )
    if site.site_key == "decrypt":
        return discover_card_items(
            site,
            html,
            href_patterns=(r"^https://decrypt\.co/\d+/.+$",),
        )
    return []


def discover_card_items(
    site: ExternalMediaSourceDefinition,
    html: str,
    *,
    href_patterns: tuple[str, ...],
    excluded_patterns: tuple[str, ...] = (),
) -> list[MediaNewsflashItem]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    items: list[MediaNewsflashItem] = []
    for article in soup.select("article"):
        parsed = extract_card_item(
            site,
            article,
            href_patterns=href_patterns,
            excluded_patterns=excluded_patterns,
        )
        if parsed is None or not parsed.source_url or parsed.source_url in seen:
            continue
        seen.add(parsed.source_url)
        items.append(parsed)
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            return items
    for anchor in soup.select("a[href]"):
        parsed = extract_anchor_item(
            site,
            anchor,
            href_patterns=href_patterns,
            excluded_patterns=excluded_patterns,
        )
        if parsed is None or not parsed.source_url or parsed.source_url in seen:
            continue
        seen.add(parsed.source_url)
        items.append(parsed)
        if len(items) >= MAX_ITEMS_PER_SOURCE:
            break
    return items


def extract_card_item(
    site: ExternalMediaSourceDefinition,
    article: Tag,
    *,
    href_patterns: tuple[str, ...],
    excluded_patterns: tuple[str, ...],
) -> MediaNewsflashItem | None:
    anchor = find_matching_anchor(
        article.select("a[href]"),
        base_url=site.homepage_url,
        href_patterns=href_patterns,
        excluded_patterns=excluded_patterns,
    )
    if anchor is None:
        return None
    source_url = normalize_url(urljoin(site.homepage_url, str(anchor.get("href") or "")))
    title = card_title(article, anchor)
    if not title:
        return None
    excerpt = ""
    for selector in ("p", "[class*='excerpt']", "[class*='dek']", "[class*='summary']", "[class*='description']"):
        node = article.select_one(selector)
        if node is None:
            continue
        excerpt = clean_inline_text(node.get_text(" ", strip=True))
        if excerpt and excerpt != title:
            break
    return MediaNewsflashItem(
        source=site.site_key,
        title=title,
        content=choose_content(title=title, description=excerpt),
        source_url=source_url,
        raw_payload={"source_url": source_url, "title": title, "excerpt": excerpt},
        metadata={
            "site_key": site.site_key,
            "site_display_name": site.display_name,
            "capture_method": "html_request",
        },
    )


def extract_anchor_item(
    site: ExternalMediaSourceDefinition,
    anchor: Tag,
    *,
    href_patterns: tuple[str, ...],
    excluded_patterns: tuple[str, ...],
) -> MediaNewsflashItem | None:
    matched = find_matching_anchor(
        [anchor],
        base_url=site.homepage_url,
        href_patterns=href_patterns,
        excluded_patterns=excluded_patterns,
    )
    if matched is None:
        return None
    title = clean_inline_text(matched.get_text(" ", strip=True))
    if len(title) < 12:
        return None
    source_url = normalize_url(urljoin(site.homepage_url, str(matched.get("href") or "")))
    return MediaNewsflashItem(
        source=site.site_key,
        title=title,
        content=title,
        source_url=source_url,
        raw_payload={"source_url": source_url, "title": title},
        metadata={
            "site_key": site.site_key,
            "site_display_name": site.display_name,
            "capture_method": "html_request",
        },
    )


def find_matching_anchor(
    anchors: list[Tag] | Any,
    *,
    base_url: str | None = None,
    href_patterns: tuple[str, ...],
    excluded_patterns: tuple[str, ...],
) -> Tag | None:
    for anchor in anchors:
        href = str(anchor.get("href") or "").strip()
        if not href:
            continue
        absolute = normalize_url(urljoin(base_url or "", href))
        if excluded_patterns and any(re.search(pattern, absolute, flags=re.IGNORECASE) for pattern in excluded_patterns):
            continue
        if any(re.search(pattern, absolute, flags=re.IGNORECASE) for pattern in href_patterns):
            return anchor
    return None


def card_title(article: Tag, anchor: Tag) -> str:
    for selector in ("h1", "h2", "h3", "h4", "[class*='title']", "[data-testid*='title']"):
        node = article.select_one(selector)
        if node is None:
            continue
        title = clean_inline_text(node.get_text(" ", strip=True))
        if len(title) >= 12:
            return title
    return clean_inline_text(anchor.get_text(" ", strip=True))


def choose_content(*, title: str, description: str | None) -> str:
    description_text = clean_html_text(description or "")
    if not description_text:
        return title
    if description_text.startswith(title):
        trimmed = description_text[len(title) :].strip(" -:\u00a0")
        if trimmed:
            return trimmed
    return description_text


def clean_html_text(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True)
    return clean_inline_text(text)


def clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def first_non_empty(*values: str) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def normalize_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if not parsed.scheme:
        parsed = urlparse(urljoin("https://", str(url or "").strip()))
    path = parsed.path or "/"
    cleaned_query = ""
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path.rstrip("/") or "/", "", cleaned_query, ""))


def parse_feed_datetime(value: str | None):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None


class ExternalMediaFetcher:
    def __init__(
        self,
        *,
        repository: ExternalMediaAlertRepository,
        site_registry: dict[str, ExternalMediaSourceDefinition] | None = None,
        poll_interval_seconds: float = 40.0,
        request_timeout_seconds: float = 20.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        self.repository = repository
        self.site_registry = site_registry or get_site_registry()
        self.poll_interval_seconds = max(5.0, float(poll_interval_seconds))
        self.request_timeout_seconds = request_timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.worker_id = f"external-media-fetcher-{os.getpid()}"
        self._stop_event = threading.Event()
        self._heartbeat = HeartbeatThrottle(
            component="external_media_alert_fetcher",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> list[MediaSourceRunResult]:
        stats = [self.process_source(site) for site in self.site_registry.values()]
        self._record_heartbeat(stats)
        return stats

    def run_forever(self) -> None:
        print(
            "[odaily] external media fetcher started. "
            f"sources={len(self.site_registry)} interval={int(self.poll_interval_seconds)}s"
        )
        while not self._stop_event.is_set():
            try:
                stats = self.run_once()
                for item in stats:
                    print(
                        "[odaily] external media fetcher "
                        f"site={item.source.site_key} status={item.status} "
                        f"candidates={item.candidate_count} saved={item.saved_count} "
                        f"duplicates={item.duplicate_count} error={item.error or '-'}"
                    )
            except Exception as exc:
                print(f"[odaily] external media fetcher round failed: {exc}")
            wait_seconds = self.poll_interval_seconds + random.uniform(0, 0.5)
            self._stop_event.wait(wait_seconds)

    def process_source(self, site: ExternalMediaSourceDefinition) -> MediaSourceRunResult:
        try:
            items = fetch_site_newsflashes(
                site,
                timeout_seconds=self.request_timeout_seconds,
                max_attempts=self.max_attempts,
                backoff_seconds=self.backoff_seconds,
            )
        except Exception as exc:
            return MediaSourceRunResult(source=site, status="fetch_failed", error=str(exc))
        if not items:
            return MediaSourceRunResult(source=site, status="parse_empty")
        saved_count, duplicate_count = self.repository.save_media_newsflash_items(items)
        return MediaSourceRunResult(
            source=site,
            status="success",
            candidate_count=len(items),
            saved_count=saved_count,
            duplicate_count=duplicate_count,
        )

    def _record_heartbeat(self, stats: list[MediaSourceRunResult]) -> None:
        failed = [item for item in stats if item.status != "success"]
        self._heartbeat.send(
            status="ok" if not failed else "failed",
            success=not failed,
            error="; ".join(item.error or item.status for item in failed) if failed else None,
            metadata={
                "sources": len(stats),
                "failed_sources": len(failed),
                "saved_count": sum(item.saved_count for item in stats),
                "duplicate_count": sum(item.duplicate_count for item in stats),
                "sites": [
                    {
                        "site_key": item.source.site_key,
                        "status": item.status,
                        "saved_count": item.saved_count,
                        "duplicate_count": item.duplicate_count,
                    }
                    for item in stats
                ],
            },
        )
