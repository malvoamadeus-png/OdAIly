"""Collect public X account timelines and linked token pages from GMGN metadata."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests


USER_AGENT = "OdAIly-meme/1.0"
MAX_PAGE_BYTES = 1_000_000
MAX_PAGE_TEXT = 12_000
IGNORED_TAGS = {"aside", "button", "footer", "form", "iframe", "nav", "noscript", "script", "style", "svg"}


def _normalize_handle(value: Any) -> str:
    handle = str(value or "").strip()
    if handle.startswith(("https://", "http://")):
        parsed = urlsplit(handle)
        if parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            return ""
        handle = parsed.path.strip("/").split("/", 1)[0]
    handle = handle.lstrip("@")
    return handle if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) else ""


def _tweet_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or value.get("raw_text") or "").strip()
    return ""


def _collect_account(handle_value: Any, *, timeout: float, limit: int) -> dict[str, Any]:
    handle = _normalize_handle(handle_value)
    if not handle:
        return {"evidence": [], "diagnostic": {"status": "skipped", "code": "invalid_x_handle", "itemCount": 0}}
    url = f"https://api.fxtwitter.com/2/profile/{handle}/statuses"
    if timeout <= 0:
        return {"evidence": [], "diagnostic": {"status": "error", "code": "collection_timeout", "itemCount": 0}}
    try:
        response = requests.get(
            url,
            params={"count": max(1, min(limit, 50))},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {"evidence": [], "diagnostic": {"status": "error", "code": "fxtwitter_failed", "error": type(exc).__name__, "itemCount": 0}}
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {"evidence": [], "diagnostic": {"status": "error", "code": "invalid_fxtwitter_response", "itemCount": 0}}

    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows[: max(1, min(limit, 50))]:
        if not isinstance(row, dict):
            continue
        author = row.get("author") if isinstance(row.get("author"), dict) else {}
        if str(author.get("screen_name") or "").strip().casefold() != handle.casefold():
            continue
        tweet_id = str(row.get("id") or "").strip()
        statement = _tweet_text(row.get("text") or row.get("raw_text"))
        if not re.fullmatch(r"\d+", tweet_id) or not statement or tweet_id in seen:
            continue
        if row.get("retweeted_status") or row.get("is_retweet") or statement.startswith("RT @"):
            continue
        seen.add(tweet_id)
        evidence.append({
            "id": f"token_x:{len(evidence) + 1}",
            "source": "token_x",
            "statement": statement,
            "observedAt": str(row.get("created_at") or ""),
            "url": f"https://x.com/{handle}/status/{tweet_id}",
            "metadata": {
                "source_name": f"@{handle}",
                "handle": handle,
                "likes": row.get("likes"),
                "reposts": row.get("reposts") or row.get("retweets"),
                "views": row.get("views"),
            },
        })
    return {
        "evidence": evidence,
        "diagnostic": {"status": "success" if evidence else "empty", "code": "", "itemCount": len(evidence), "handle": handle},
    }


def _public_host(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("unsupported_page_url")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in {80, 443}:
        raise ValueError("unsupported_page_port")
    hostname = parsed.hostname.rstrip(".")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        addresses = [ipaddress.ip_address(item[4][0]) for item in resolved]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("non_public_page_host")
    return hostname, port


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.paragraphs: list[tuple[bool, str]] = []
        self.site_name = ""
        self._ignored_depth = 0
        self._article_depth = 0
        self._title_depth = 0
        self._paragraph_depth = 0
        self._paragraph_article = False
        self._paragraph_parts: list[str] = []
        self.article_metadata = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name")).casefold()
            if key == "og:site_name" and values.get("content"):
                self.site_name = values["content"].strip()[:100]
            if key in {"og:type", "article:published_time", "article:author"}:
                self.article_metadata = True
        if tag in IGNORED_TAGS:
            self._ignored_depth += 1
        if self._ignored_depth:
            return
        if tag == "article":
            self._article_depth += 1
        elif tag == "title":
            self._title_depth += 1
        elif tag == "p":
            self._paragraph_depth += 1
            self._paragraph_article = self._article_depth > 0
            self._paragraph_parts = []
        elif tag == "br" and self._paragraph_depth:
            self._paragraph_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in IGNORED_TAGS:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "article":
            self._article_depth = max(0, self._article_depth - 1)
        elif tag == "title":
            self._title_depth = max(0, self._title_depth - 1)
        elif tag == "p" and self._paragraph_depth:
            text = re.sub(r"\s+", " ", " ".join(self._paragraph_parts)).strip()
            if text:
                self.paragraphs.append((self._paragraph_article, text))
            self._paragraph_depth = 0
            self._paragraph_parts = []

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._title_depth:
            self.title_parts.append(text)
        if self._paragraph_depth:
            self._paragraph_parts.append(text)


def _fetch_page(url: str, *, timeout: float, deadline: float) -> dict[str, Any]:
    current = url.strip()
    response = None
    try:
        for _ in range(5):
            host, _ = _public_host(current)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("website_collection_timeout")
            response = requests.get(
                current,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                timeout=max(0.1, min(timeout, remaining)),
                allow_redirects=False,
                stream=True,
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise ValueError("page_redirect_without_location")
                current = urljoin(current, location)
                continue
            response.raise_for_status()
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if "html" not in content_type and "xhtml" not in content_type:
                raise ValueError("page_not_html")
            chunks: list[bytes] = []
            byte_count = 0
            for chunk in response.iter_content(chunk_size=16_384):
                byte_count += len(chunk)
                if byte_count > MAX_PAGE_BYTES:
                    raise ValueError("page_too_large")
                chunks.append(chunk)
            html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
            parser = _PageParser()
            parser.feed(html)
            article_paragraphs = [text for in_article, text in parser.paragraphs if in_article]
            paragraphs = article_paragraphs or [text for _, text in parser.paragraphs]
            body = "\n".join(paragraphs)
            if not body:
                raise ValueError("page_has_no_text")
            title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
            statement = (f"{title}\n{body}" if title else body)[:MAX_PAGE_TEXT]
            source_name = parser.site_name or host.removeprefix("www.")
            page_type = "article" if article_paragraphs or parser.article_metadata else "website"
            return {
                "status": "success",
                "title": title,
                "page_type": page_type,
                "source_name": source_name,
                "url": current,
                "statement": statement,
            }
        raise ValueError("too_many_page_redirects")
    finally:
        if response is not None:
            response.close()


def _collect_page(value: Any, *, timeout: float, deadline: float) -> dict[str, Any]:
    url = str(value or "").strip()
    if not url:
        return {"evidence": [], "diagnostic": {"status": "skipped", "code": "no_website", "itemCount": 0}}
    if timeout <= 0:
        return {"evidence": [], "diagnostic": {"status": "error", "code": "collection_timeout", "itemCount": 0}}
    try:
        page = _fetch_page(url, timeout=timeout, deadline=deadline)
    except Exception as exc:
        return {"evidence": [], "diagnostic": {"status": "error", "code": "website_fetch_failed", "error": type(exc).__name__, "itemCount": 0}}
    evidence = [{
        "id": "token_website:1",
        "source": "token_website",
        "statement": page["statement"],
        "url": page["url"],
        "metadata": {"source_name": page["source_name"], "title": page["title"], "page_type": page["page_type"]},
    }]
    return {
        "evidence": evidence,
        "diagnostic": {
            "status": "success",
            "code": "",
            "itemCount": 1,
            "source_name": page["source_name"],
            "page_type": page["page_type"],
        },
    }


def collect(profile: dict[str, Any], *, timeout: int = 12, max_posts: int = 20) -> dict[str, Any]:
    """Collect bounded public materials from a token's GMGN-linked X and website URLs."""
    deadline = time.monotonic() + max(1, timeout)
    account = _collect_account(
        profile.get("twitter_username"),
        timeout=max(0, min(timeout, deadline - time.monotonic())),
        limit=max_posts,
    )
    remaining = max(0, min(timeout, deadline - time.monotonic()))
    page = _collect_page(profile.get("website"), timeout=remaining, deadline=deadline)
    evidence = account["evidence"] + page["evidence"]
    statuses = {account["diagnostic"]["status"], page["diagnostic"]["status"]}
    if evidence:
        status = "partial" if "error" in statuses else "success"
    elif "error" in statuses:
        status = "error"
    else:
        status = "empty"
    return {
        "evidence": evidence,
        "diagnostic": {
            "status": status,
            "x_account": account["diagnostic"],
            "website": page["diagnostic"],
            "itemCount": len(evidence),
        },
    }
