"""Read visible Bloomberg article text through the existing Meme browser profile."""

from __future__ import annotations

import json
import os
import select
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


DEFAULT_TIMEOUT_SECONDS = 15 * 60
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


def _write_body_file(output_path: Path, body: str) -> bool:
    try:
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body + "\n")
    return True


def _save_body(result: dict[str, Any], body: str, output_value: str | None) -> Path:
    if not body:
        raise ValueError("cannot save an empty Bloomberg article body")

    if output_value:
        output_path = Path(output_value).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if not _write_body_file(output_path, body):
            raise RuntimeError(f"output file already exists: {output_path}")
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
        if _write_body_file(output_path, body):
            return output_path
    raise RuntimeError(f"could not create a unique Bloomberg output file for {safe_slug}")


def _wait_for_operator(timeout_seconds: int) -> bool:
    if not sys.stdin.isatty():
        raise RuntimeError("Bloomberg access requires an interactive terminal")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([sys.stdin], [], [], min(1.0, remaining))
        if ready:
            if not sys.stdin.readline():
                raise RuntimeError("interactive terminal closed before article capture")
            return True
    return False


def run(args: Any) -> int:
    load_dotenv()
    if not bool(getattr(args, "confirm_authorized_use", False)):
        raise RuntimeError(
            "confirm Bloomberg account terms permit programmatic reading and personal research saving "
            "with --confirm-authorized-use"
        )
    if os.name == "posix" and not os.getenv("DISPLAY"):
        raise RuntimeError("Bloomberg access needs a local display; run this command under xvfb-run -a")

    article_url = validate_article_url(getattr(args, "article_url", ""))
    timeout_seconds = max(60, int(getattr(args, "timeout", DEFAULT_TIMEOUT_SECONDS)))
    profile_dir = _fomo_profile_dir()
    if not profile_dir.is_dir():
        raise RuntimeError("Meme FOMO browser profile is missing; initialize it with meme fomo-login first")

    with exclusive_browser(timeout_seconds=_browser_lock_timeout_seconds()):
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                launch_kwargs: dict[str, Any] = {
                    "headless": False,
                    "viewport": {"width": 1440, "height": 900},
                    "locale": "en-US",
                    "timezone_id": "Asia/Shanghai",
                }
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.goto(article_url, wait_until="domcontentloaded", timeout=60_000)
                    print("[meme-bloomberg-read] Chromium opened in the existing server-side Meme profile.")
                    print("[meme-bloomberg-read] Complete any Bloomberg sign-in in the server browser session.")
                    print("[meme-bloomberg-read] Return to the requested article and press Enter to save visible text.")
                    if not _wait_for_operator(timeout_seconds):
                        print("[meme-bloomberg-read] timed out; closing Chromium")
                        return 124
                    result = visible_article_result(page)
                    body = str(result.pop("text", ""))
                    result.pop("summary", None)
                    if result["paragraph_count"]:
                        output_path = _save_body(result, body, getattr(args, "output", None))
                        result["output_path"] = str(output_path)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0 if result["paragraph_count"] else 1
                finally:
                    context.close()
        except KeyboardInterrupt:
            print("[meme-bloomberg-read] stopped by operator")
            return 130
