from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from packages.common.time_utils import SHANGHAI_TZ


MSX_SITE_BASE_URL = "https://msx.com"
MSX_API_BASE_URL = "https://api9528mystks.mystonks.org"
MSX_NOTICE_CLASS_KEY = "system_msg"
MSX_NOTICE_LIST_PATH = "/api/v2/stat-msg/page"
MSX_NOTICE_DETAIL_PATH = "/api/v2/stat-msg/detail"


class MSXNoticeError(RuntimeError):
    """Raised when the public MSX notice API returns an unusable response."""


@dataclass(frozen=True, slots=True)
class MSXNotice:
    id: int
    alias: str
    title: str
    category: str
    published_at: str
    detail_url: str
    content_html: str | None = None
    content: str | None = None
    links: list[dict[str, str]] | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def _published_at(value: Any) -> str:
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError) as exc:
        raise MSXNoticeError(f"invalid MSX notice timestamp: {value!r}") from exc
    if timestamp_ms <= 0:
        raise MSXNoticeError(f"invalid MSX notice timestamp: {value!r}")
    return datetime.fromtimestamp(timestamp_ms / 1000, SHANGHAI_TZ).isoformat()


def html_to_text(value: str) -> str:
    """Convert the API's article HTML into stable paragraph text."""
    soup = BeautifulSoup(value or "", "html.parser")
    for tag in soup.find_all(["br", "p", "div", "li", "h1", "h2", "h3", "h4", "tr"]):
        if tag.name == "br":
            tag.replace_with("\n")
        else:
            tag.insert_after("\n")
    text = html.unescape(soup.get_text(" "))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def html_links(value: str, *, base_url: str = MSX_SITE_BASE_URL) -> list[dict[str, str]]:
    soup = BeautifulSoup(value or "", "html.parser")
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]))
        label = " ".join(anchor.get_text(" ", strip=True).split())
        key = (href, label)
        if key not in seen:
            links.append({"label": label, "url": href})
            seen.add(key)
    return links


def _api_data(payload: Any, *, endpoint: str) -> Any:
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        code = payload.get("code") if isinstance(payload, dict) else "invalid"
        message = payload.get("msg") if isinstance(payload, dict) else "invalid JSON envelope"
        raise MSXNoticeError(f"MSX {endpoint} failed: code={code}, message={message}")
    return payload.get("data")


class MSXNoticeClient:
    def __init__(
        self,
        *,
        api_base_url: str = MSX_API_BASE_URL,
        site_base_url: str = MSX_SITE_BASE_URL,
        timeout_seconds: float = 20,
        max_attempts: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.site_base_url = site_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max(1, max_attempts)
        self.session = session or requests.Session()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.post(
                    f"{self.api_base_url}{path}",
                    json=payload,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "User-Agent": "OdAIly-MSXNotice/1.0",
                        "my-stonks-lang": "zh",
                        "source": "web",
                    },
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
        raise MSXNoticeError(f"MSX request failed after {self.max_attempts} attempts: {last_error}") from last_error

    def list_notices(
        self,
        *,
        page_index: int = 1,
        page_size: int = 10,
        sub_type: int | None = None,
    ) -> tuple[int, list[MSXNotice]]:
        if page_index < 0:
            raise ValueError("page_index must be non-negative")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

        payload: dict[str, Any] = {
            "pageIndex": page_index,
            "pageSize": page_size,
            "lang": "zh",
            "classKey": MSX_NOTICE_CLASS_KEY,
        }
        if sub_type is not None:
            payload["subType"] = sub_type
        data = _api_data(self._post(MSX_NOTICE_LIST_PATH, payload), endpoint="notice list")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise MSXNoticeError("MSX notice list response has no data.list array")

        notices = [self._parse_list_item(item) for item in data["list"]]
        return int(data.get("count") or 0), notices

    def get_detail(self, notice_id: int) -> dict[str, Any]:
        data = _api_data(
            self._post(MSX_NOTICE_DETAIL_PATH, {"id": notice_id, "classKey": MSX_NOTICE_CLASS_KEY, "lang": "zh"}),
            endpoint="notice detail",
        )
        if not isinstance(data, dict):
            raise MSXNoticeError("MSX notice detail response is not an object")
        content_html = str(data.get("actualContent") or "")
        return {
            "content_html": content_html,
            "content": html_to_text(content_html),
            "links": html_links(content_html, base_url=self.site_base_url),
        }

    def fetch_page(
        self,
        *,
        page_index: int = 1,
        page_size: int = 10,
        sub_type: int | None = None,
        include_content: bool = True,
    ) -> tuple[int, list[MSXNotice]]:
        total, notices = self.list_notices(page_index=page_index, page_size=page_size, sub_type=sub_type)
        if not include_content:
            return total, notices

        detailed: list[MSXNotice] = []
        for notice in notices:
            detail = self.get_detail(notice.id)
            detailed.append(
                MSXNotice(**(notice.to_json() | detail))
            )
        return total, detailed

    def _parse_list_item(self, item: Any) -> MSXNotice:
        if not isinstance(item, dict):
            raise MSXNoticeError("MSX notice list contains a non-object item")
        try:
            notice_id = int(item["id"])
            alias = str(item.get("alias") or "")
            title = str(item["actualTitle"] or "").strip()
            category = str(item.get("subTypeName") or "").strip()
            published_at = _published_at(item["ctime"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MSXNoticeError(f"invalid MSX notice list item: {item!r}") from exc
        if not title or not alias:
            raise MSXNoticeError(f"MSX notice list item has no title or alias: {item!r}")
        return MSXNotice(
            id=notice_id,
            alias=alias,
            title=title,
            category=category,
            published_at=published_at,
            detail_url=f"{self.site_base_url}/zh-hans/notice-center-detail/{alias}",
        )
