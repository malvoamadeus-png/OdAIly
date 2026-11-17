"""Read the anonymous server-rendered Predict.fun category feed."""
from __future__ import annotations

import json
import re
from typing import Any

import requests


FEEDS = ("finance", "tech", "economy", "culture", "politics")
_FLIGHT = re.compile(r"self\.__next_f\.push\((.*?)\)</script>", re.DOTALL)
_DEHYDRATED = re.compile(r'"dehydratedAt":(\d+),"state":\{"data":')


def _flight_blob(html: str) -> str:
    parts = []
    for match in _FLIGHT.finditer(html):
        try:
            value = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            parts.append(value[1])
    return "".join(parts)


def parse_feed(html: str) -> dict[str, Any]:
    blob = _flight_blob(html)
    decoder = json.JSONDecoder()
    for match in _DEHYDRATED.finditer(blob):
        try:
            data, end = decoder.raw_decode(blob[match.end():])
        except ValueError:
            continue
        if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
            continue
        pages = data["pages"]
        if not pages or not isinstance(pages[0], dict):
            continue
        page = pages[0]
        if not isinstance(page.get("edges"), list) or not isinstance(page.get("pageInfo"), dict):
            continue
        key_at = blob.find('"queryKey":', match.end() + end)
        if key_at < 0:
            continue
        try:
            query_key, _ = decoder.raw_decode(blob[key_at + len('"queryKey":'):])
        except ValueError:
            continue
        if not isinstance(query_key, list) or query_key[:2] != ["categories", "infinite"]:
            continue
        nodes = []
        for edge in page["edges"]:
            node = edge.get("node") if isinstance(edge, dict) else None
            if not isinstance(node, dict) or not node.get("id") or not node.get("slug"):
                raise ValueError("category feed contains an invalid node")
            nodes.append(node)
        return {"nodes": nodes, "total_count": page.get("totalCount"),
                "has_next_page": page["pageInfo"].get("hasNextPage"),
                "snapshot_ms": int(match.group(1)), "query_key": query_key}
    raise ValueError("Predict.fun category feed was not found in HTML")


def fetch_feed(feed: str, *, timeout: float = 20) -> dict[str, Any]:
    if feed not in FEEDS:
        raise ValueError("unknown category feed")
    response = requests.get(
        f"https://predict.fun/zh-cn/markets/{feed}", timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"},
    )
    response.raise_for_status()
    result = parse_feed(response.text)
    result["cache_status"] = response.headers.get("cf-cache-status")
    return result


def parse_detail(html: str, slug: str) -> dict[str, Any]:
    blob = _flight_blob(html)
    decoder = json.JSONDecoder()
    for match in _DEHYDRATED.finditer(blob):
        try:
            data, _ = decoder.raw_decode(blob[match.end():])
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("slug") != slug or "description" not in data:
            continue
        description = data.get("description")
        if isinstance(description, str) and re.fullmatch(r"\$[0-9a-f]+", description):
            reference = re.search(r"(?m)^" + re.escape(description[1:]) + r":T([0-9a-f]+),", blob)
            if not reference:
                raise ValueError("missing description reference")
            length = int(reference.group(1), 16)
            raw = blob[reference.end():].encode("utf-8")[:length]
            description = raw.decode("utf-8")
        if not isinstance(description, str) or not description.strip() or description.startswith("$"):
            raise ValueError("missing official resolution rules")
        return {"description": description.strip(), "category": data}
    raise ValueError("market detail category not found")


def fetch_detail(slug: str, *, timeout: float = 20) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9-]+", slug):
        raise ValueError("invalid market slug")
    response = requests.get(f"https://predict.fun/zh-cn/market/{slug}", timeout=timeout,
                            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
    response.raise_for_status()
    return parse_detail(response.text, slug)
