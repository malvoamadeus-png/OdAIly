from __future__ import annotations

import json
import re
import threading
import urllib.parse
from datetime import datetime
from typing import Any

import requests

from .models import CaptureRecord, TimelineAttempt, TweetCandidate
from .token_identity import (
    TokenSymbolResolver,
    normalize_token_symbol,
    replace_token_ca_tokens,
    resolve_token_symbol_with_gmgn,
)


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


def _find_articles(payload: dict[str, Any]) -> list[dict[str, Any]]:
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

    # Historical posts' Articles belong to their own context layers.
    visit({key: value for key, value in payload.items() if key not in {*QUOTE_KEYS, "replying_to_status"}})
    return articles


QUOTE_KEYS = ("quote", "quoted_tweet", "quote_tweet", "quoted_status")
QUOTE_ID_KEYS = ("quote_id", "quoted_tweet_id", "quoted_status_id")


def has_context_reference(payload: dict[str, Any]) -> bool:
    replying_to = payload.get("replying_to")
    return bool(
        any(payload.get(key) for key in (*QUOTE_KEYS, *QUOTE_ID_KEYS, "replying_to_status"))
        or (isinstance(replying_to, dict) and replying_to.get("status"))
    )


def _context_ref(payload: dict[str, Any]) -> tuple[dict[str, Any], str, str] | None:
    for key in QUOTE_KEYS:
        value = payload.get(key)
        if isinstance(value, dict) and value:
            quote_id = str(value.get("id") or value.get("id_str") or "").strip()
            if quote_id:
                return value, quote_id, "quote"
        elif isinstance(value, (str, int)) and str(value).strip():
            return {"id": str(value)}, str(value), "quote"
    for key in QUOTE_ID_KEYS:
        value = str(payload.get(key) or "").strip()
        if value:
            return {"id": value}, value, "quote"
    reply = payload.get("replying_to")
    status = payload.get("replying_to_status")
    embedded = status if isinstance(status, dict) else {}
    reply_id = str(embedded.get("id") or embedded.get("status") or (status if not isinstance(status, dict) else "") or "").strip()
    if not reply_id and isinstance(reply, dict):
        reply_id = str(reply.get("status") or "").strip()
    if reply_id:
        embedded = {**embedded, "id": reply_id}
        if isinstance(reply, dict):
            embedded.setdefault("url", reply.get("url"))
            embedded.setdefault("author", {"screen_name": reply.get("screen_name")})
        elif isinstance(reply, str):
            embedded.setdefault("author", {"screen_name": reply})
        return embedded, reply_id, "reply"
    return None


def _context_item(payload: dict[str, Any], tweet_id: str, relation: str) -> dict[str, Any]:
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    username = str(author.get("screen_name") or payload.get("author_username") or "").strip().lstrip("@")
    text, _ = _compose_x_content(_text(payload.get("text") or payload.get("raw_text")), _find_articles(payload))
    return {
        "id": tweet_id,
        "relation": relation,
        "url": str(payload.get("url") or (f"https://x.com/{username}/status/{tweet_id}" if username else "")),
        "author_username": username,
        "author_display_name": str(author.get("name") or "").strip(),
        "created_at": str(payload.get("created_at") or "").strip(),
        "text": text,
    }


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
        token_symbol_resolver: TokenSymbolResolver = resolve_token_symbol_with_gmgn,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.token_symbol_resolver = token_symbol_resolver
        self._token_symbol_cache: dict[tuple[tuple[str, ...], str], str | None] = {}
        self._token_symbol_cache_lock = threading.Lock()

    def _resolve_token_symbol(self, chains: tuple[str, ...], address: str) -> str | None:
        cache_key = (chains, address.casefold())
        with self._token_symbol_cache_lock:
            if cache_key in self._token_symbol_cache:
                return self._token_symbol_cache[cache_key]
            try:
                symbol = normalize_token_symbol(self.token_symbol_resolver(chains, address))
            except Exception as exc:
                print(
                    "[odaily] x-capture GMGN token identity lookup failed "
                    f"address={address} error={type(exc).__name__}: {str(exc)[:200]}"
                )
                symbol = None
            self._token_symbol_cache[cache_key] = symbol
            return symbol

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

    def collect_context_chain(
        self, detail: dict[str, Any], *, root_id: str, author_hint: str = "",
        raw_layers: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str, str | None]:
        chain: list[dict[str, Any]] = []
        visited = {root_id}
        current = detail
        partial_error = None
        if has_context_reference(current) and _context_ref(current) is None:
            return chain, "partial", "context reference has no id"
        while ref := _context_ref(current):
            embedded, tweet_id, relation = ref
            if tweet_id in visited:
                return chain, "partial", f"context cycle at {tweet_id}"
            visited.add(tweet_id)
            author = embedded.get("author") if isinstance(embedded.get("author"), dict) else {}
            username = str(author.get("screen_name") or embedded.get("author_username") or "").strip().lstrip("@")
            if not username:
                url = str(embedded.get("url") or "")
                match = re.search(r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)/status/" + re.escape(tweet_id), url)
                username = match.group(1) if match else ""
            if not username:
                current_author = current.get("author") if isinstance(current.get("author"), dict) else {}
                username = str(current_author.get("screen_name") or author_hint or "").strip().lstrip("@")
            if not username:
                return chain, "partial", f"context author missing for {tweet_id}"
            try:
                fetched = self.fetch_detail(username, tweet_id)
                if not fetched or str(fetched.get("id") or tweet_id) != tweet_id:
                    raise ValueError("context detail missing or id mismatch")
            except Exception as exc:
                if raw_layers is not None:
                    raw_layers.append({"id": tweet_id, "relation": relation, "embedded": embedded, "fetch_error": str(exc)})
                item = _context_item(embedded, tweet_id, relation)
                if item["text"]:
                    chain.append(item)
                partial_error = f"context {tweet_id}: {type(exc).__name__}: {exc}"
                if has_context_reference(embedded):
                    current = embedded
                    continue
                return chain, "partial", partial_error
            if raw_layers is not None:
                raw_layers.append({"id": tweet_id, "relation": relation, "detail": fetched})
            item = _context_item(fetched, tweet_id, relation)
            if not item["text"]:
                return chain, "partial", f"context text missing for {tweet_id}"
            item["text"] = replace_token_ca_tokens(item["text"], self._resolve_token_symbol)
            chain.append(item)
            current = fetched if has_context_reference(fetched) else embedded
            if has_context_reference(current) and _context_ref(current) is None:
                return chain, "partial", f"context reference has no id in {tweet_id}"
        return chain, "partial" if partial_error else "complete", partial_error

    def build_record(
        self,
        username: str,
        candidate: TweetCandidate,
        *,
        detail: dict[str, Any] | None = None,
        detail_error: str | None = None,
        context_chain: list[dict[str, Any]] | None = None,
        context_chain_status: str = "complete",
        context_chain_error: str | None = None,
        context_raw_layers: list[dict[str, Any]] | None = None,
    ) -> CaptureRecord:
        detail = detail or {}
        author = detail.get("author") if isinstance(detail.get("author"), dict) else {}
        post_text = _text(detail.get("text") or detail.get("raw_text")) or candidate.text
        articles = _find_articles(detail)
        if not articles:
            articles = _find_articles(candidate.raw_payload)
        text, article_titles = _compose_x_content(post_text, articles)
        text = replace_token_ca_tokens(text, self._resolve_token_symbol)
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
        if context_chain:
            metadata["context_chain"] = context_chain
        if context_chain or context_chain_status != "complete":
            metadata["context_chain_status"] = context_chain_status
        if context_chain_error:
            metadata["context_chain_error"] = context_chain_error

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
            raw_payload={
                "timeline": candidate.raw_payload,
                "detail": detail,
                **({"context_layers": context_raw_layers} if context_raw_layers else {}),
            },
        )
