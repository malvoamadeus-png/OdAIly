from __future__ import annotations

import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from packages.common.storage import connect_sqlite


DETERMINISTIC_SOURCES = {"hyperliquid_news", "gate_market", "gate-tradfi", "gate_market_broadcast",
                         "stock_market", "stock_market_broadcast", "msx_market", "okx-stock-brief"}


def remote_ids(value: Any, key: str = "") -> set[str]:
    if isinstance(value, str) and key == "response_text":
        try:
            return remote_ids(json.loads(value))
        except (ValueError, TypeError):
            return set()
    if isinstance(value, dict):
        return set().union(*(remote_ids(v, str(k)) for k, v in value.items())) if value else set()
    if isinstance(value, list):
        return set().union(*(remote_ids(v) for v in value)) if value else set()
    return {str(value)} if value is not None and key.casefold() in {"id", "newsflashid", "newsflash_id", "source_item_id"} else set()


def linked_material(path, task: dict[str, Any]) -> dict[str, Any] | None:
    with connect_sqlite(path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_task_pipeline'").fetchone():
            return None
        candidates = conn.execute("""SELECT t.source,t.title,t.content,t.source_url,t.metadata,
                p.final_title,p.publish_completed_at,p.push_result
            FROM x_task_pipeline p JOIN tasks t ON t.id=p.task_id
            WHERE p.publish_completed_at IS NOT NULL
              AND abs((julianday(p.publish_completed_at)-julianday(?))*86400)<=300""", (task.get("published_at"),)).fetchall()
    exact = []
    for candidate in candidates:
        try:
            push = json.loads(candidate["push_result"] or "{}")
        except (ValueError, TypeError):
            continue
        if task["source_item_id"] in remote_ids(push):
            exact.append(dict(candidate))
    if len(exact) == 1:
        return exact[0]
    # Same-title/time fallback is only accepted when unambiguous, as in publisher attribution.
    matches = [dict(r) for r in candidates if r["final_title"] == task.get("title")]
    return matches[0] if len(matches) == 1 else None


def deterministic_reason(task: dict[str, Any], linked: dict[str, Any] | None) -> str:
    if linked and linked.get("source") in DETERMINISTIC_SOURCES:
        return "确定性编辑来源：" + str(linked["source"])
    # Fixed attribution used by the isolated Gate/MSX quote generators. Not MSX notices.
    body = re.sub(r"\s+", "", task["content"])
    if re.search(r"(?:据Gate(?:最新)?数据|根据MSX\.COM数据|据MSX\.COM数据)", body, re.I):
        return "确定性报价组件：Gate / MSX"
    return ""


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise ValueError("Unsupported original URL")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError("Original URL must resolve to a public address")


def fetch_original(url: str) -> str:
    current = url
    for _ in range(5):
        validate_public_url(current)
        with requests.get(current, timeout=15, allow_redirects=False, stream=True,
                          headers={"User-Agent": "Mozilla/5.0 OdailyFactReview/1.0"}) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                current = urljoin(current, response.headers.get("Location", ""))
                continue
            response.raise_for_status()
            if not any(x in response.headers.get("Content-Type", "").lower() for x in ("text/html", "text/plain", "application/xhtml")):
                raise ValueError("Original is not a text page")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError("Original page exceeds size limit")
                chunks.append(chunk)
            soup = BeautifulSoup(b"".join(chunks), "html.parser")
        for element in soup(["script", "style", "nav", "footer", "header", "form", "noscript"]):
            element.decompose()
        node = soup.find("article") or soup.find("main") or soup
        text = node.get_text("\n", strip=True)
        if len(text) < 200 or re.search(r"enable javascript|checking your browser|verify you are human|access denied", text[:1200], re.I):
            raise ValueError("Original article unavailable or blocked")
        return text[:50000]
    raise ValueError("Too many original URL redirects")


def resolve_original(path, task: dict[str, Any], linked: dict[str, Any] | None) -> dict[str, str]:
    if linked and str(linked.get("content") or "").strip():
        text = str(linked["content"])
        chain_status = "not_applicable"
        if linked.get("source") == "x":
            try:
                metadata = json.loads(linked.get("metadata") or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            chain = metadata.get("context_chain")
            chain_status = str(metadata.get("context_chain_status") or ("unknown" if chain else "complete"))
            parts = [f"【引用/回复链采集状态】{chain_status}\n【本次播报（最新帖）】\n" +
                f"作者：{metadata.get('author_username') or metadata.get('author_display_name') or '未知'}；"
                f"时间：{metadata.get('created_at') or '未知'}；链接：{linked.get('source_url') or '未知'}\n" + text]
            if isinstance(chain, list):
                for item in chain:
                    if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                        continue
                    parts.append("【引用/回复前序帖】\n" +
                        f"关系：{item.get('relation') or '前序'}；作者：{item.get('author_username') or '未知'}；"
                        f"时间：{item.get('created_at') or '未知'}；链接：{item.get('url') or item.get('id') or '未知'}\n" + str(item["text"]))
            text = "\n\n".join(parts)
        return {"status": "available", "text": text[:50000],
                "url": str(linked.get("source_url") or ""), "origin": "generation_material", "chain_status": chain_status}
    url = str(task.get("source_locator") or "").strip()
    host = (urlparse(url).hostname or "").lower()
    if not url or host == "odaily.news" or host.endswith(".odaily.news"):
        return {"status": "unavailable", "text": "", "url": "", "reason": "没有可关联的原始材料或外部原文链接"}
    try:
        if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            match = re.search(r"/status/(\d+)", url)
            if not match:
                raise ValueError("X URL has no status ID")
            response = requests.get("https://api.fxtwitter.com/status/" + match[1], timeout=15)
            response.raise_for_status()
            tweet = response.json().get("tweet") or {}
            text = str(tweet.get("text") or "")
            article = tweet.get("article") or {}
            if article:
                # Avoid claiming a truncated long-form post is complete original material.
                raise ValueError("X Article requires archived full-text material")
            if not text:
                raise ValueError("X original text unavailable")
        else:
            text = fetch_original(url)
        return {"status": "available", "text": text[:50000], "url": url, "origin": "source_link"}
    except Exception as exc:
        return {"status": "unavailable", "text": "", "url": url, "reason": str(exc)[:500]}
