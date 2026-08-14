from __future__ import annotations

import re
from typing import Any

import requests


SEARCH_URL = "https://api.fxtwitter.com/2/search"
GMGN_LINK_PATTERN = re.compile(r"(?:https?://)?(?:www\.)?gmgn\.ai/", re.IGNORECASE)


class FxTwitterSearchError(RuntimeError):
    pass


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _author(item: dict[str, Any]) -> str:
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = str(author.get("screen_name") or "").strip().lstrip("@")
    return f"@{handle}" if handle else ""


def _post(item: dict[str, Any], *, feed: str, page: int) -> dict[str, Any] | None:
    tweet_id = str(item.get("id") or "").strip()
    author = _author(item)
    text = _text(item.get("text") or item.get("raw_text"))
    if not tweet_id or not author or not text:
        return None
    return {
        "id": f"x:{tweet_id}",
        "tweet_id": tweet_id,
        "author": author,
        "text": text,
        "url": str(item.get("url") or f"https://x.com/{author.lstrip('@')}/status/{tweet_id}"),
        "timestamp": str(item.get("created_at") or "").strip(),
        "likes": item.get("likes"),
        "reposts": item.get("reposts") or item.get("retweets"),
        "views": item.get("views"),
        "search_feeds": [{"feed": feed, "page": page}],
    }


def _bottom_cursor(payload: dict[str, Any]) -> str:
    cursor = payload.get("cursor")
    if not isinstance(cursor, dict):
        return ""
    return str(cursor.get("bottom") or "").strip()


def search_ca(
    contract: str,
    *,
    pages_per_feed: int = 2,
    count_per_page: int = 20,
    timeout: int = 20,
) -> dict[str, Any]:
    contract = contract.strip()
    if not contract:
        raise ValueError("contract is empty")

    posts_by_id: dict[str, dict[str, Any]] = {}
    page_summaries: list[dict[str, Any]] = []
    for feed in ("top", "latest"):
        cursor = ""
        for page in range(1, pages_per_feed + 1):
            params: dict[str, Any] = {"q": contract, "feed": feed, "count": count_per_page}
            if cursor:
                params["cursor"] = cursor
            try:
                response = requests.get(
                    SEARCH_URL,
                    params=params,
                    headers={"User-Agent": "OdAIly-meme/1.0", "Accept": "application/json"},
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:
                raise FxTwitterSearchError(f"FxTwitter {feed} page {page} failed: {exc}") from exc
            if not isinstance(payload, dict):
                raise FxTwitterSearchError(f"FxTwitter {feed} page {page} returned a non-object response")
            results = payload.get("results")
            if not isinstance(results, list):
                raise FxTwitterSearchError(f"FxTwitter {feed} page {page} returned no results list")

            for item in results:
                if not isinstance(item, dict):
                    continue
                post = _post(item, feed=feed, page=page)
                if post is None:
                    continue
                existing = posts_by_id.get(post["id"])
                if existing is None:
                    posts_by_id[post["id"]] = post
                else:
                    existing["search_feeds"] = list(existing["search_feeds"]) + list(post["search_feeds"])

            next_cursor = _bottom_cursor(payload)
            page_summaries.append(
                {
                    "feed": feed,
                    "page": page,
                    "result_count": len(results),
                    "has_next_cursor": bool(next_cursor),
                }
            )
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

    kept: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    normalized_contract = contract.casefold()
    for post in posts_by_id.values():
        text = str(post["text"])
        if normalized_contract not in text.casefold():
            excluded.append({**post, "excluded_reason": "no_exact_ca"})
        elif GMGN_LINK_PATTERN.search(text):
            excluded.append({**post, "excluded_reason": "gmgn_link"})
        else:
            kept.append(post)

    return {
        "posts": kept,
        "excluded_posts": excluded,
        "raw": {
            "source": "fxtwitter",
            "query": contract,
            "pages_per_feed": pages_per_feed,
            "count_per_page": count_per_page,
            "pages": page_summaries,
            "unique_results": len(posts_by_id),
        },
    }
