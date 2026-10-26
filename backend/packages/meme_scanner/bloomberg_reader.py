"""Read visible Bloomberg article text through the existing Meme browser profile."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from dotenv import load_dotenv

from packages.common.paths import get_paths
from .browser_lock import exclusive_browser
from .fast_evidence import _browser_lock_timeout_seconds, _fomo_profile_dir
from .fomo_login import (
    _assert_loopback_port_available,
    _stop_process,
    _validated_port,
    _wait_for_cdp,
    chromium_command,
    chromium_executable,
)


DEFAULT_TIMEOUT_SECONDS = 15 * 60
DEFAULT_CDP_PORT = 9224
_BLOOMBERG_HOSTS = {"bloomberg.com", "www.bloomberg.com"}
_ARTICLE_PARAGRAPH_SELECTOR = 'div > p[class^="ArticleBodyText_"]'


def validate_article_url(value: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower() not in _BLOOMBERG_HOSTS
        or not parsed.path.startswith("/news/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("URL must be an https://www.bloomberg.com/news/... article URL")
    return url


def visible_article_result(page: Any) -> dict[str, Any]:
    page_url = str(page.url or "")
    hostname = (urlsplit(page_url).hostname or "").lower()
    title = str(page.title() or "")
    canonical_url = page_url
    try:
        canonical_href = page.locator('link[rel="canonical"]').get_attribute("href", timeout=1000)
        canonical_url = urljoin(page_url, str(canonical_href or page_url))
    except Exception:
        pass

    def meta_content(*selectors: str) -> str | None:
        for selector in selectors:
            try:
                locator = page.locator(selector)
                if locator.count():
                    value = str(locator.first.get_attribute("content", timeout=1000) or "").strip()
                    if value:
                        return value
            except Exception:
                continue
        return None

    title = title or meta_content('meta[property="og:title"]') or ""

    paragraphs: list[str] = []
    if hostname in _BLOOMBERG_HOSTS:
        locator = page.locator(_ARTICLE_PARAGRAPH_SELECTOR)
        for index in range(locator.count()):
            paragraph = locator.nth(index)
            try:
                if not paragraph.is_visible():
                    continue
                text = str(paragraph.inner_text(timeout=1000) or "").strip()
            except Exception:
                continue
            if text and (not paragraphs or paragraphs[-1] != text):
                paragraphs.append(text)

    return {
        "status": "visible_paragraphs_found" if paragraphs else "article_body_not_visible",
        "title": title,
        "url": page_url,
        "canonical_url": canonical_url,
        "author": meta_content('meta[name="author"]', 'meta[property="article:author"]'),
        "published_at": meta_content('meta[property="article:published_time"]', 'meta[name="datePublished"]'),
        "section": meta_content('meta[property="article:section"]', 'meta[name="article:section"]'),
        "summary": meta_content('meta[name="description"]', 'meta[property="og:description"]'),
        "read_at": datetime.now(timezone.utc).isoformat(),
        "completeness": "unverified",
        "paragraph_count": len(paragraphs),
        "text": "\n\n".join(paragraphs),
    }


def _save_body(result: dict[str, Any], body: str, output_value: str | None) -> Path:
    if not body:
        raise ValueError("cannot save an empty Bloomberg article body")

    if output_value:
        output_path = Path(output_value).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with output_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body + "\n")
        except FileExistsError as exc:
            raise RuntimeError(f"output file already exists: {output_path}") from exc
        return output_path

    article_slug = urlsplit(str(result.get("canonical_url") or result.get("url") or "")).path.rstrip("/").rsplit("/", 1)[-1]
    safe_slug = "".join(character if character.isalnum() or character in "._-" else "_" for character in article_slug)
    safe_slug = safe_slug.strip("._-") or "article"
    timestamp = datetime.fromisoformat(str(result["read_at"])).strftime("%Y%m%dT%H%M%SZ")
    output_dir = get_paths().exports_dir / "bloomberg"
    output_dir.mkdir(parents=True, exist_ok=True)
    base_path = output_dir / f"{safe_slug}-{timestamp}"
    for suffix in range(100):
        output_path = base_path.with_name(f"{base_path.name}{f'-{suffix}' if suffix else ''}.txt")
        try:
            with output_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(body + "\n")
            return output_path
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a unique Bloomberg output file for {safe_slug}")


def _wait_for_operator(process: subprocess.Popen[Any], timeout_seconds: int) -> bool:
    if not sys.stdin.isatty():
        raise RuntimeError("Bloomberg access requires an interactive terminal")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Meme Chromium closed before article capture")
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))
        if ready:
            if not sys.stdin.readline():
                raise RuntimeError("interactive terminal closed before article capture")
            return True
    return False


def run(args: Any) -> int:
    load_dotenv()
    if os.name == "posix" and not os.getenv("DISPLAY"):
        raise RuntimeError("Bloomberg access needs a local display; run this command under xvfb-run -a")

    article_url = validate_article_url(getattr(args, "article_url", ""))
    timeout_seconds = max(60, int(getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS)))
    cdp_port = _validated_port(int(getattr(args, "cdp_port", DEFAULT_CDP_PORT)))
    profile_dir = _fomo_profile_dir()
    if not profile_dir.is_dir():
        raise RuntimeError("Meme FOMO browser profile is missing; initialize it with meme fomo-login first")

    command = chromium_command(
        executable=chromium_executable(),
        profile_dir=profile_dir,
        cdp_port=cdp_port,
        initial_url=article_url,
    )
    process: subprocess.Popen[Any] | None = None
    with exclusive_browser(timeout_seconds=_browser_lock_timeout_seconds()):
        try:
            _assert_loopback_port_available(cdp_port)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            ready_deadline = min(time.monotonic() + 10, time.monotonic() + timeout_seconds)
            if not _wait_for_cdp(process, cdp_port, ready_deadline):
                raise RuntimeError("Meme Chromium did not expose its local DevTools endpoint")

            from playwright.sync_api import sync_playwright

            print(f"[meme-bloomberg-read] DevTools is local-only at 127.0.0.1:{cdp_port}")
            print("[meme-bloomberg-read] Complete any Bloomberg sign-in in this existing Meme profile.")
            print(
                "[meme-bloomberg-read] Return the page to the article, then press Enter to read visible paragraphs."
            )
            if not _wait_for_operator(process, timeout_seconds):
                print("[meme-bloomberg-read] timed out; closing Chromium")
                return 124

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                context = browser.contexts[0] if browser.contexts else None
                if context is None:
                    raise RuntimeError("Meme Chromium has no browser context")
                pages = context.pages
                page = next(
                    (
                        candidate
                        for candidate in reversed(pages)
                        if (urlsplit(candidate.url).hostname or "").lower() in _BLOOMBERG_HOSTS
                        and urlsplit(candidate.url).path.rstrip("/").rsplit("/", 1)[-1]
                        == urlsplit(article_url).path.rstrip("/").rsplit("/", 1)[-1]
                    ),
                    None,
                )
                if page is None:
                    raise RuntimeError("Return the browser to the requested Bloomberg article before pressing Enter")
                result = visible_article_result(page)
                body = str(result.pop("text", ""))
                result.pop("summary", None)
                if result["paragraph_count"]:
                    output_path = _save_body(result, body, getattr(args, "output", None))
                    result["output_path"] = str(output_path)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0 if result["paragraph_count"] else 1
        except KeyboardInterrupt:
            print("[meme-bloomberg-read] stopped by operator")
            return 130
        finally:
            if process is not None:
                _stop_process(process)
