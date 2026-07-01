from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from packages.non_mainstream_media.fetcher import (  # noqa: E402
    BUSINESS_INSIDER_LATEST_URL,
    discover_businessinsider_pages,
    fetch_html,
    parse_businessinsider_article,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe Business Insider latest discovery + article parsing.")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    html = fetch_html(BUSINESS_INSIDER_LATEST_URL, timeout_seconds=30, max_attempts=2, backoff_seconds=1.0)
    pages = discover_businessinsider_pages(html)
    sample = pages[: max(1, args.limit)]
    results: list[dict[str, object]] = []
    for page in sample:
        entry: dict[str, object] = {"url": page.detail_url, "title": page.title}
        try:
            article_html = fetch_html(page.detail_url, timeout_seconds=30, max_attempts=2, backoff_seconds=1.0)
            article = parse_businessinsider_article(article_html, page_url=page.detail_url, source_item_id=page.source_item_id)
            entry.update(
                {
                    "status": "ok",
                    "canonical_url": article.canonical_url,
                    "parsed_title": article.title,
                    "has_body": bool(article.content),
                    "body_length": len(article.content),
                    "authors": article.author_names,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                    "categories": article.categories,
                }
            )
        except Exception as exc:
            entry.update({"status": "error", "error": str(exc)})
        results.append(entry)

    ok_count = sum(1 for item in results if item["status"] == "ok")
    payload = {
        "list_url": BUSINESS_INSIDER_LATEST_URL,
        "discovered_count": len(pages),
        "sampled_count": len(sample),
        "ok_count": ok_count,
        "error_count": len(results) - ok_count,
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
