from __future__ import annotations

import json
import re
import urllib.parse
from datetime import datetime
from typing import Any

import requests

from .models import CaptureRecord, TimelineAttempt, TweetCandidate


USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
RESERVED_PATHS = {
    "home",
    "explore",
    "i",
    "search",
    "messages",
    "notifications",
    "settings",
    "tos",
    "privacy",
    "compose",
}


def normalize_username(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("username is empty")
    if raw.startswith("@"):
        raw = raw[1:]
    if "://" not in raw and "/" not in raw:
        username = raw
    else:
        if "://" not in raw:
            raw = "https://" + raw.lstrip("/")
        parsed = urllib.parse.urlparse(raw)
        host = parsed.netloc.lower()
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise ValueError("profile URL must point to x.com or twitter.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("profile URL must be a direct user profile URL")
        username = parts[0].lstrip("@")
    if username.lower() in RESERVED_PATHS or not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(f"invalid Twitter/X username: {username!r}")
    return username


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(value.get("text") or "").strip()
    return ""


def _media_urls(payload: dict[str, Any]) -> list[str]:
    media = payload.get("media")
    items: list[Any] = []
    if isinstance(media, dict) and isinstance(media.get("all"), list):
        items = media["all"]
    elif isinstance(media, list):
        items = media

    urls: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("thumbnail_url") or "").strip()
        if url and url not in urls:
            urls.append(url)
    return urls


def _article_content_blocks(article: dict[str, Any]) -> list[dict[str, Any]]:
    content = article.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            content = None
    if not isinstance(content, dict):
        return []
    blocks = content.get("blocks")
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


def _article_text(article: dict[str, Any]) -> str:
    lines: list[str] = []
    for block in _article_content_blocks(article):
        text = _text(block.get("text"))
        if not text:
            continue
        block_type = str(block.get("type") or "unstyled")
        if block_type == "header-one":
            text = f"# {text}"
        elif block_type == "header-two":
            text = f"## {text}"
        elif block_type == "header-three":
            text = f"### {text}"
        elif block_type == "blockquote":
            text = f"> {text}"
        elif block_type == "unordered-list-item":
            text = f"- {text}"
        elif block_type == "ordered-list-item":
            text = f"1. {text}"
        lines.append(text)

    if lines:
        return "\n".join(lines)
    return _text(article.get("body") or article.get("text") or article.get("preview_text"))


def _find_articles(payload: Any) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            article = value.get("article")
            if isinstance(article, dict):
                identity = (
                    str(article.get("id") or ""),
                    _text(article.get("title")),
                    _article_text(article),
                )
                if identity not in seen and (identity[0] or identity[1] or identity[2]):
                    seen.add(identity)
                    articles.append(article)
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(payload)
    return articles


def _compose_x_content(post_text: str, articles: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not articles:
        return post_text, []

    sections: list[str] = []
    if post_text:
        sections.append(f"【普通帖子】\n{post_text}")

    article_titles: list[str] = []
    for article in articles:
        title = _text(article.get("title"))
        body = _article_text(article)
        if title:
            article_titles.append(title)
        if not title and not body:
            continue
        article_lines = ["【X文章】"]
        if title:
            article_lines.append(f"标题：{title}")
        if body:
            article_lines.append(f"正文：{body}")
        sections.append("\n".join(article_lines))

    return "\n".join(sections), article_titles


def parse_twitter_created_at(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    try:
        return datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y").isoformat()
    except ValueError:
        return None


def candidate_from_fxtwitter(payload: dict[str, Any]) -> TweetCandidate | None:
    tweet_id = str(payload.get("id") or "").strip()
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    username = str(author.get("screen_name") or "").strip().lstrip("@")
    if not tweet_id or not username:
        return None
    return TweetCandidate(
        tweet_id=tweet_id,
        author_username=username,
        author_display_name=str(author.get("name") or username).strip(),
        text=_text(payload.get("text") or payload.get("raw_text")),
        created_at_raw=str(payload.get("created_at") or "").strip() or None,
        reply_count=_int(payload.get("replies")),
        retweet_count=_int(payload.get("reposts") or payload.get("retweets")),
        like_count=_int(payload.get("likes")),
        bookmark_count=_int(payload.get("bookmarks")),
        view_count=_int(payload.get("views")),
        media_urls=_media_urls(payload),
        raw_payload=payload,
    )


class FXTwitterClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        user_agent: str = "odaily-x-capture/1.0",
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(
            url,
            params=params,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=self.timeout_seconds,
        )
        if response.status_code == 204:
            return {}
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def fetch_timeline(self, username: str, *, count: int = 20) -> tuple[list[TweetCandidate], TimelineAttempt]:
        normalized = normalize_username(username)
        url = f"https://api.fxtwitter.com/2/profile/{normalized}/statuses"
        try:
            payload = self._get_json(url, params={"count": max(1, min(count, 100))})
        except Exception as exc:
            return [], TimelineAttempt("fxtwitter", "fetch_failed", url, str(exc))

        results = payload.get("results")
        if not isinstance(results, list):
            return [], TimelineAttempt("fxtwitter", "parse_empty", url, "empty response")

        candidates: list[TweetCandidate] = []
        target = normalized.lower()
        for item in results:
            if not isinstance(item, dict):
                continue
            candidate = candidate_from_fxtwitter(item)
            if candidate and candidate.author_username.lower() == target:
                candidates.append(candidate)

        status = "success" if candidates else "parse_empty"
        error = None if candidates else "no target-author posts"
        return candidates, TimelineAttempt("fxtwitter", status, url, error, len(candidates))

    def fetch_detail(self, username: str, tweet_id: str) -> dict[str, Any]:
        normalized = normalize_username(username)
        payload = self._get_json(f"https://api.fxtwitter.com/{normalized}/status/{tweet_id}")
        tweet = payload.get("tweet")
        return tweet if isinstance(tweet, dict) else {}

    def build_record(
        self,
        username: str,
        candidate: TweetCandidate,
        *,
        detail: dict[str, Any] | None = None,
        detail_error: str | None = None,
    ) -> CaptureRecord:
        detail = detail or {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        post_text = _text(detail.get("text") or detail.get("raw_text")) or candidate.text
        articles = _find_articles(detail)
        if not articles:
            articles = _find_articles(candidate.raw_payload)
        text, article_titles = _compose_x_content(post_text, articles)
        created_at = parse_twitter_created_at(str(detail.get("created_at") or candidate.created_at_raw or ""))
        media_urls = _media_urls(detail) or list(candidate.media_urls)
        metadata: dict[str, Any] = {
            "source": candidate.source,
            "detail_fetched": bool(detail),
        }
        if articles:
            metadata["content_format"] = "x_post_with_article" if post_text else "x_article"
            metadata["article_count"] = len(articles)
            metadata["article_titles"] = article_titles
        if detail_error:
            metadata["detail_error"] = detail_error

        return CaptureRecord(
            platform="x",
            tweet_id=candidate.tweet_id,
            author_username=str(author.get("screen_name") or candidate.author_username).lstrip("@"),
            author_display_name=str(author.get("name") or candidate.author_display_name),
            url=str(detail.get("url") or f"https://x.com/{username}/status/{candidate.tweet_id}"),
            text=text,
            created_at=created_at,
            reply_count=_int(detail.get("replies") or candidate.reply_count),
            retweet_count=_int(detail.get("retweets") or detail.get("reposts") or candidate.retweet_count),
            like_count=_int(detail.get("likes") or candidate.like_count),
            bookmark_count=_int(detail.get("bookmarks") or candidate.bookmark_count),
            view_count=_int(detail.get("views") or candidate.view_count),
            media_urls=media_urls,
            metadata=metadata,
            raw_payload={"timeline": candidate.raw_payload, "detail": detail},
        )
