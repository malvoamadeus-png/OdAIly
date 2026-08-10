from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


# The database-backed site display name is the primary configuration. This map
# keeps older tasks and manually-created tasks useful when metadata is absent.
DEFAULT_SITE_NAMES_BY_DOMAIN: dict[str, str] = {
    "a16zcrypto.com": "a16z crypto Posts",
    "coindesk.com": "CoinDesk",
    "cointelegraph.com": "Cointelegraph",
    "decrypt.co": "Decrypt",
    "news.bitcoin.com": "Bitcoin.com News",
    "forbes.com": "Forbes Digital Assets",
    "hk01.com": "HK01 NFT / Virtual Assets",
    "tether.io": "Tether News",
    "thelec.net": "TheElec CHINA",
    "etnews.com": "ETNews",
    "zdnet.co.kr": "ZDNet Korea Semiconductor",
    "ctee.com.tw": "CTEE Semiconductor",
    "hankyung.com": "Hankyung Premium9",
    "businessinsider.com": "Business Insider Latest",
    "ft.com": "FT Crypto",
    "wsj.com": "WSJ",
    "fortune.com": "Fortune Crypto",
    "theblock.co": "The Block",
}

_GENERIC_SITE_NAMES = {"crypto信源", "ai信源", "混合信源", "external_media", "ai_source", "mixed_source"}
_JINA_HOST = "r.jina.ai"
_TRAILING_ATTRIBUTION_PATTERN = re.compile(r"[（(]\s*(?P<name>[^（）()\n]{1,120})\s*[）)]\s*$")


def _normalize_host(value: str) -> str:
    host = value.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _source_host(source_url: str | None) -> str | None:
    if not source_url:
        return None
    parsed = urlparse(source_url.strip())
    host = _normalize_host(parsed.hostname or "")
    if host != _JINA_HOST:
        return host or None

    # Jina fallback URLs look like https://r.jina.ai/http://example.com/path.
    # Resolve the embedded original URL so fallback transport does not change
    # the displayed source name.
    embedded = unquote(parsed.path.lstrip("/"))
    if embedded.startswith(("http://", "https://")):
        return _source_host(embedded)
    return None


def _configured_site_name(value: str | None) -> str | None:
    name = str(value or "").strip()
    if not name or name.casefold() in _GENERIC_SITE_NAMES:
        return None
    return name


def resolve_source_site_name(*, source_url: str | None, configured_name: str | None = None) -> str | None:
    """Return the user-configured or hard-matched display name for an article."""

    configured = _configured_site_name(configured_name)
    if configured:
        return configured

    host = _source_host(source_url)
    if not host:
        return None
    for domain, name in DEFAULT_SITE_NAMES_BY_DOMAIN.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return None


def append_source_attribution(
    content: str,
    *,
    source_url: str | None,
    configured_name: str | None = None,
) -> str:
    """Append the standard full-width source suffix when a source is resolvable."""

    name = resolve_source_site_name(source_url=source_url, configured_name=configured_name)
    if not name:
        return content
    stripped = content.rstrip()
    match = _TRAILING_ATTRIBUTION_PATTERN.search(stripped)
    if match and match.group("name").strip().casefold() == name.casefold():
        return stripped
    return f"{stripped}（{name}）"
