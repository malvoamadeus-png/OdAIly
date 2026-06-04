from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, quote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup, Tag

from .models import DiscoveredPage, ParsedArticle, SOURCE_GROUP_AI_SOURCE, SiteDefinition


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

A16Z_CONTENT_TYPES = {"article", "podcast", "videos", "listicles", "papers"}
A16Z_BASE_URL = "https://a16zcrypto.com"
COINDESK_BASE_URL = "https://www.coindesk.com"
COINDESK_FEED_URL = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINTELEGRAPH_BASE_URL = "https://cointelegraph.com"
COINTELEGRAPH_GOOGLE_NEWS_SITEMAP_URL = "https://cointelegraph.com/sitemap/google-news.xml"
COINTELEGRAPH_FEED_URL = "https://cointelegraph.com/rss"
DECRYPT_BASE_URL = "https://decrypt.co"
DECRYPT_FEED_URL = "https://decrypt.co/feed"
FORBES_BASE_URL = "https://www.forbes.com"
FORBES_SECTION_URL = "https://www.forbes.com/sites/digital-assets/"
FORBES_FEED_URL = "https://www.forbes.com/sites/digital-assets/feed/"
HK01_BASE_URL = "https://www.hk01.com"
JINA_PROXY_URL_PREFIX = "https://r.jina.ai/http://"
TETHER_BASE_URL = "https://tether.io"
TETHER_NEWS_URL = "https://tether.io/news/"
TETHER_NEWS_API_URL = (
    "https://tether.io/wp-json/wp/v2/posts"
    "?categories=3&per_page=100&_fields=id,date_gmt,date,link,title,excerpt"
)
TETHER_NEWS_PROXY_API_URL = (
    "https://tether.io/wp-json/wp/v2/posts"
    "?categories=3&per_page=100&_fields=id,date_gmt,date,link,title"
)
FORTUNE_BASE_URL = "https://fortune.com"
FT_BASE_URL = "https://www.ft.com"
THE_BLOCK_BASE_URL = "https://www.theblock.co"
THE_BLOCK_OFFICIAL_RSS_URL = "https://www.theblock.co/rss.xml"
THE_BLOCK_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=site:theblock.co&hl=en-US&gl=US&ceid=US:en"
THELEC_BASE_URL = "https://www.thelec.net"
THELEC_CHINA_LIST_URL = "https://www.thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm"
THELEC_TIMEZONE = timezone(timedelta(hours=9))
ETNEWS_BASE_URL = "https://www.etnews.com"
ETNEWS_ELECTRONICS_LIST_URL = "https://www.etnews.com/news/section.html?id1=06"
ETNEWS_SW_LIST_URL = "https://www.etnews.com/news/section.html?id1=04"
ETNEWS_TIMEZONE = timezone(timedelta(hours=9))
ZDNET_KOREA_BASE_URL = "https://zdnet.co.kr"
ZDNET_KOREA_SEMICONDUCTOR_LIST_URL = "https://zdnet.co.kr/newskey/?lstcode=%EB%B0%98%EB%8F%84%EC%B2%B4"
ZDNET_KOREA_TIMEZONE = timezone(timedelta(hours=9))
WSJ_BASE_URL = "https://www.wsj.com"
BLOOMBERG_BASE_URL = "https://www.bloomberg.com"
GOOGLE_NEWS_BASE_URL = "https://news.google.com"
FT_GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=site:ft.com+crypto&hl=en-US&gl=US&ceid=US:en"
FT_GOOGLE_NEWS_MAX_ITEMS = 25
FORTUNE_CRYPTO_PATTERN = re.compile(
    r"crypto|bitcoin|ethereum|stablecoin|token|tokenized|blockchain|web3|defi|nft|dao|airdrop|staking|"
    r"btc|eth|sol|bnb|usdt|usdc|layer\s*2|l2|onchain|wallet|exchange|miner|mining|"
    r"比特币|以太坊|稳定币|代币|区块链|加密|公链|链上|钱包|交易所|矿工|挖矿",
    re.IGNORECASE,
)
HK01_ISSUE_URL = (
    "https://www.hk01.com/issue/10154/"
    "nft%E8%99%9B%E6%93%AC%E8%B2%A8%E5%B9%A3-"
    "%E5%B0%88%E5%8D%80-%E6%AF%94%E7%89%B9%E5%B9%A3-"
    "%E4%BB%A5%E5%A4%AA%E5%B9%A3-%E5%8D%80%E5%A1%8A"
    "%E9%8F%88%E4%B8%AD%E4%BD%A0%E9%9C%80%E9%97%9C%E6%B3%A8"
    "%E7%9A%84%E4%B8%80%E5%88%87"
)
DISCLAIMER_MARKERS = [
    "the views expressed here are those of the individual ah capital management",
    "the views expressed here are those of the individual a16z personnel",
    "this material is for informational purposes only",
    "nothing in this post constitutes investment, legal, or tax advice",
    "certain information contained herein has been obtained from third-party sources",
    "see a16z.com/disclosures",
]


SITE_REGISTRY: dict[str, SiteDefinition] = {
    "a16z_crypto_posts": SiteDefinition(
        site_key="a16z_crypto_posts",
        display_name="a16z crypto Posts",
        homepage_url="https://a16zcrypto.com/posts/",
        list_url="https://a16zcrypto.com/posts/",
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "coindesk": SiteDefinition(
        site_key="coindesk",
        display_name="CoinDesk",
        homepage_url=COINDESK_BASE_URL,
        list_url=COINDESK_FEED_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "cointelegraph": SiteDefinition(
        site_key="cointelegraph",
        display_name="Cointelegraph",
        homepage_url=COINTELEGRAPH_BASE_URL,
        list_url=COINTELEGRAPH_GOOGLE_NEWS_SITEMAP_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "decrypt": SiteDefinition(
        site_key="decrypt",
        display_name="Decrypt",
        homepage_url=DECRYPT_BASE_URL,
        list_url=DECRYPT_FEED_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "forbes_digital_assets": SiteDefinition(
        site_key="forbes_digital_assets",
        display_name="Forbes Digital Assets",
        homepage_url=FORBES_SECTION_URL,
        list_url=FORBES_FEED_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "hk01_virtual_assets": SiteDefinition(
        site_key="hk01_virtual_assets",
        display_name="HK01 NFT / Virtual Assets",
        homepage_url=HK01_ISSUE_URL,
        list_url=HK01_ISSUE_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "tether_news": SiteDefinition(
        site_key="tether_news",
        display_name="Tether News",
        homepage_url=TETHER_NEWS_URL,
        list_url=TETHER_NEWS_API_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
    ),
    "thelec_china": SiteDefinition(
        site_key="thelec_china",
        display_name="TheElec CHINA",
        homepage_url=THELEC_BASE_URL,
        list_url=THELEC_CHINA_LIST_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
        source_group=SOURCE_GROUP_AI_SOURCE,
        interval_seconds=300,
    ),
    "etnews_electronics": SiteDefinition(
        site_key="etnews_electronics",
        display_name="ETNews Electronics",
        homepage_url=ETNEWS_BASE_URL,
        list_url=ETNEWS_ELECTRONICS_LIST_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
        source_group=SOURCE_GROUP_AI_SOURCE,
        interval_seconds=300,
    ),
    "etnews_sw": SiteDefinition(
        site_key="etnews_sw",
        display_name="ETNews SW",
        homepage_url=ETNEWS_BASE_URL,
        list_url=ETNEWS_SW_LIST_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
        source_group=SOURCE_GROUP_AI_SOURCE,
        interval_seconds=300,
    ),
    "zdnet_korea_semiconductor": SiteDefinition(
        site_key="zdnet_korea_semiconductor",
        display_name="ZDNet Korea Semiconductor",
        homepage_url=ZDNET_KOREA_BASE_URL,
        list_url=ZDNET_KOREA_SEMICONDUCTOR_LIST_URL,
        capture_method="html_request",
        pipeline_mode="write_flow",
        source_group=SOURCE_GROUP_AI_SOURCE,
        interval_seconds=300,
    ),
    "ft_crypto": SiteDefinition(
        site_key="ft_crypto",
        display_name="FT Crypto",
        homepage_url="https://www.ft.com/crypto",
        list_url="https://www.ft.com/crypto",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_business": SiteDefinition(
        site_key="wsj_business",
        display_name="WSJ Business",
        homepage_url="https://www.wsj.com/business?mod=nav_top_section",
        list_url="https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_economy": SiteDefinition(
        site_key="wsj_economy",
        display_name="WSJ Economy",
        homepage_url="https://www.wsj.com/economy?mod=nav_top_section",
        list_url="https://feeds.content.dowjones.io/public/rss/socialeconomyfeed",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "wsj_finance": SiteDefinition(
        site_key="wsj_finance",
        display_name="WSJ Finance",
        homepage_url="https://www.wsj.com/finance?mod=nav_top_section",
        list_url="https://feeds.content.dowjones.io/public/rss/socialmarketsfeed",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "fortune_crypto": SiteDefinition(
        site_key="fortune_crypto",
        display_name="Fortune Crypto",
        homepage_url="https://fortune.com/section/crypto/",
        list_url="https://fortune.com/section/crypto/",
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
    "the_block": SiteDefinition(
        site_key="the_block",
        display_name="The Block",
        homepage_url=THE_BLOCK_BASE_URL,
        list_url=THE_BLOCK_OFFICIAL_RSS_URL,
        capture_method="html_request",
        pipeline_mode="alert_only",
    ),
}


def get_site_registry() -> dict[str, SiteDefinition]:
    return dict(SITE_REGISTRY)


def fetch_discovered_pages(
    site: SiteDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    if site.site_key == "a16z_crypto_posts":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_a16z_pages(html, base_url=site.homepage_url)
    if site.site_key == "coindesk":
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_coindesk_pages(xml, base_url=site.homepage_url)
    if site.site_key == "cointelegraph":
        return fetch_cointelegraph_discovered_pages(
            site,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    if site.site_key == "decrypt":
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_decrypt_pages(xml, base_url=site.homepage_url)
    if site.site_key == "forbes_digital_assets":
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_forbes_pages(xml, base_url=site.homepage_url)
    if site.site_key == "hk01_virtual_assets":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_hk01_pages(html, base_url=site.homepage_url)
    if site.site_key == "thelec_china":
        return fetch_thelec_discovered_pages(
            site,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    if site.site_key in {"etnews_electronics", "etnews_sw"}:
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_etnews_pages(html, base_url=site.homepage_url)
    if site.site_key == "zdnet_korea_semiconductor":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_zdnet_korea_pages(html, base_url=site.homepage_url)
    if site.site_key == "tether_news":
        try:
            payload = fetch_json(
                site.list_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            return discover_tether_pages(payload, base_url=site.homepage_url)
        except Exception:
            try:
                wrapped_payload = fetch_html(
                    build_jina_proxy_url(TETHER_NEWS_PROXY_API_URL),
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                )
                return discover_tether_pages(
                    parse_jina_wrapped_json_payload(wrapped_payload),
                    base_url=site.homepage_url,
                )
            except Exception:
                markdown = fetch_html(
                    build_jina_proxy_url(TETHER_NEWS_URL),
                    timeout_seconds=timeout_seconds,
                    max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds,
                )
                return discover_tether_pages_from_markdown(markdown, base_url=site.homepage_url)
    if site.site_key == "ft_crypto":
        xml = fetch_html(
            FT_GOOGLE_NEWS_RSS_URL,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        return discover_ft_pages(
            xml,
            base_url=site.homepage_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    if site.site_key in {"wsj_business", "wsj_economy", "wsj_finance"}:
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_wsj_pages(xml, base_url=site.homepage_url)
    if site.site_key == "fortune_crypto":
        html = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        return discover_fortune_pages(html, base_url=site.homepage_url)
    if site.site_key == "the_block":
        return fetch_the_block_discovered_pages(
            site,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
    raise ValueError(f"unsupported site registry entry: {site.site_key}")


def fetch_article(
    site: SiteDefinition,
    page: DiscoveredPage,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> ParsedArticle:
    article: ParsedArticle
    if site.site_key == "a16z_crypto_posts":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_a16z_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "coindesk":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_coindesk_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "cointelegraph":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_cointelegraph_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "decrypt":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_decrypt_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "forbes_digital_assets":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_forbes_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "hk01_virtual_assets":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_hk01_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "thelec_china":
        html = fetch_html_with_fallbacks(
            page.detail_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
            fallback_urls=build_thelec_fallback_urls(page.detail_url),
        )
        article = parse_thelec_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key in {"etnews_electronics", "etnews_sw"}:
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_etnews_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "zdnet_korea_semiconductor":
        html = fetch_html(page.detail_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        article = parse_zdnet_korea_article(html, page_url=page.detail_url, source_item_id=page.source_item_id)
    elif site.site_key == "tether_news":
        slug = pick_tether_post_slug(page.detail_url)
        try:
            payload = fetch_json(
                f"{TETHER_BASE_URL}/wp-json/wp/v2/posts?slug={quote(slug)}&_embed=wp:term",
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            article = parse_tether_article(payload, page_url=page.detail_url, source_item_id=page.source_item_id)
        except Exception:
            markdown = fetch_html(
                build_jina_proxy_url(page.detail_url),
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
            article = parse_tether_markdown_article(
                markdown,
                page_url=page.detail_url,
                source_item_id=page.source_item_id,
            )
    else:
        raise ValueError(f"unsupported site registry entry: {site.site_key}")
    return apply_discovery_page_fallback(article, page)


def fetch_html(
    url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            return response.text
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(max(0.0, backoff_seconds) * attempt)
    raise RuntimeError(f"request failed url={url}: {last_error}") from last_error


def fetch_json(
    url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> Any:
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(max(0.0, backoff_seconds) * attempt)
    raise RuntimeError(f"request failed url={url}: {last_error}") from last_error


def fetch_html_with_fallbacks(
    url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    fallback_urls: list[str] | None = None,
) -> str:
    candidates = [url, *(fallback_urls or [])]
    errors: list[str] = []
    for candidate in candidates:
        try:
            return fetch_html(
                candidate,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
        except Exception as exc:
            errors.append(f"{candidate}: {exc}")
    raise RuntimeError("; ".join(errors))


def build_jina_proxy_url(url: str) -> str:
    return f"{JINA_PROXY_URL_PREFIX}{url}"


def extract_jina_markdown_payload(payload: str) -> str:
    marker = "Markdown Content:"
    if marker not in payload:
        raise ValueError("invalid Jina payload: missing Markdown Content marker")
    content = payload.split(marker, 1)[1]
    return content.lstrip("\r\n").strip()


def parse_jina_wrapped_json_payload(payload: str) -> Any:
    text = extract_jina_markdown_payload(payload)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        obj, _ = decoder.raw_decode(text[index:])
        return obj
    raise ValueError("invalid Jina payload: missing JSON body")


def fetch_cointelegraph_discovered_pages(
    site: SiteDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    primary_error: str | None = None
    try:
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=max_attempts, backoff_seconds=backoff_seconds)
        pages = discover_cointelegraph_sitemap_pages(xml, base_url=site.homepage_url)
        if pages:
            return pages
        primary_error = "cointelegraph google-news sitemap returned no pages"
    except Exception as exc:
        primary_error = f"cointelegraph google-news sitemap failed: {exc}"

    fallback_error: str | None = None
    try:
        xml = fetch_html(
            COINTELEGRAPH_FEED_URL,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        pages = discover_cointelegraph_rss_pages(xml, base_url=site.homepage_url)
        if pages:
            return pages
        fallback_error = "cointelegraph rss fallback returned no pages"
    except Exception as exc:
        fallback_error = f"cointelegraph rss fallback failed: {exc}"

    message_parts = [part for part in (primary_error, fallback_error) if part]
    if message_parts:
        raise RuntimeError("; ".join(message_parts))
    return []


def fetch_the_block_discovered_pages(
    site: SiteDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    primary_error: str | None = None
    try:
        xml = fetch_html(site.list_url, timeout_seconds=timeout_seconds, max_attempts=1, backoff_seconds=0.0)
        pages = discover_the_block_pages(xml, source_name=site.display_name)
        if pages:
            return pages
        primary_error = "the block official rss returned no pages"
    except Exception as exc:
        primary_error = f"the block official rss failed: {exc}"

    proxy_error: str | None = None
    try:
        markdown = fetch_html(
            build_jina_proxy_url(site.list_url),
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        pages = discover_the_block_pages_from_jina_rss(markdown, base_url=site.homepage_url)
        if pages:
            return pages
        proxy_error = "the block official rss via jina returned no pages"
    except Exception as exc:
        proxy_error = f"the block official rss via jina failed: {exc}"

    fallback_error: str | None = None
    try:
        xml = fetch_html(
            THE_BLOCK_GOOGLE_NEWS_RSS_URL,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        pages = discover_the_block_pages(xml, source_name=site.display_name)
        if pages:
            return pages
        fallback_error = "the block google-news fallback returned no pages"
    except Exception as exc:
        fallback_error = f"the block google-news fallback failed: {exc}"

    message_parts = [part for part in (primary_error, proxy_error, fallback_error) if part]
    if message_parts:
        raise RuntimeError("; ".join(message_parts))
    return []


def fetch_thelec_discovered_pages(
    site: SiteDefinition,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    html = fetch_html_with_fallbacks(
        site.list_url,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        fallback_urls=build_thelec_fallback_urls(site.list_url),
    )
    return discover_thelec_pages(html, base_url=site.homepage_url)


def discover_a16z_pages(html: str, *, base_url: str = A16Z_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        parsed = urlparse(detail_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 3 or parts[0] != "posts":
            continue
        if parts[1] not in A16Z_CONTENT_TYPES:
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url))
    return results


def discover_coindesk_pages(xml_text: str, *, base_url: str = COINDESK_BASE_URL) -> list[DiscoveredPage]:
    return discover_rss_pages(
        xml_text,
        base_url=base_url,
        href_patterns=(r"^https://www\.coindesk\.com/.+/$",),
        excluded_patterns=(r"/tv/", r"/video/", r"/podcasts?/", r"/livewire/"),
    )


def discover_cointelegraph_pages(xml_text: str, *, base_url: str = COINTELEGRAPH_BASE_URL) -> list[DiscoveredPage]:
    pages = discover_cointelegraph_sitemap_pages(xml_text, base_url=base_url)
    if pages:
        return pages
    return discover_cointelegraph_rss_pages(xml_text, base_url=base_url)


def discover_cointelegraph_sitemap_pages(xml_text: str, *, base_url: str = COINTELEGRAPH_BASE_URL) -> list[DiscoveredPage]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid Cointelegraph sitemap payload") from exc
    ns = {
        "sm": "http://www.sitemaps.org/schemas/sitemap/0.9",
        "news": "http://www.google.com/schemas/sitemap-news/0.9",
    }
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for item in root.findall(".//sm:url", ns):
        link = clean_inline_text(item.findtext("sm:loc", default="", namespaces=ns))
        title = clean_inline_text(item.findtext("news:news/news:title", default="", namespaces=ns))
        published_at_raw = clean_inline_text(
            item.findtext("news:news/news:publication_date", default="", namespaces=ns)
        )
        if not link:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if not re.match(r"^https://cointelegraph\.com/news/.+$", detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                published_at=parse_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_cointelegraph_rss_pages(xml_text: str, *, base_url: str = COINTELEGRAPH_BASE_URL) -> list[DiscoveredPage]:
    return discover_rss_pages(
        xml_text,
        base_url=base_url,
        href_patterns=(r"^https://cointelegraph\.com/news/.+$",),
    )


def discover_decrypt_pages(xml_text: str, *, base_url: str = DECRYPT_BASE_URL) -> list[DiscoveredPage]:
    return discover_rss_pages(
        xml_text,
        base_url=base_url,
        href_patterns=(r"^https://decrypt\.co/\d+/.+$",),
    )


def discover_the_block_pages(xml_text: str, *, source_name: str = "The Block") -> list[DiscoveredPage]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid The Block discovery RSS payload") from exc
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for item in root.findall(".//item"):
        link = normalize_the_block_discovery_url(clean_inline_text(item.findtext("link", default="")))
        title = clean_inline_text(item.findtext("title", default=""))
        item_source = clean_inline_text(item.findtext("source", default=""))
        description_html = item.findtext("description", default="")
        published_at_raw = clean_inline_text(item.findtext("pubDate", default="") or item.findtext("published", default=""))
        if not link or not title:
            continue
        if item_source and item_source != source_name:
            continue
        if link in seen:
            continue
        seen.add(link)
        clean_title = strip_google_news_source_suffix(title, item_source or source_name) if item_source else title
        excerpt = (
            extract_google_news_excerpt(description_html, title=title, source_name=item_source or source_name)
            if item_source
            else extract_feed_excerpt(description_html, title=clean_title)
        )
        results.append(
            DiscoveredPage(
                source_item_id=link,
                detail_url=link,
                title=clean_title,
                excerpt=excerpt or None,
                published_at=parse_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_the_block_pages_from_jina_rss(payload: str, *, base_url: str = THE_BLOCK_BASE_URL) -> list[DiscoveredPage]:
    text = extract_jina_markdown_payload(payload)
    lines = [line.strip() for line in text.splitlines()]
    heading_pattern = re.compile(r"^### \[(?P<title>.+?)\]\((?P<link>https://www\.theblock\.co/post/[^)]+)\)$")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    index = 0
    while index < len(lines):
        match = heading_pattern.fullmatch(lines[index])
        if not match:
            index += 1
            continue
        title = clean_inline_text(match.group("title"))
        detail_url = normalize_the_block_discovery_url(normalize_url(urljoin(base_url, match.group("link"))))
        published_index = next_nonempty_line(lines, index + 1)
        published_at_raw = ""
        while published_index is not None and published_index < len(lines):
            candidate = clean_inline_text(lines[published_index])
            if candidate.startswith("### ["):
                break
            if parse_published_at(candidate) is not None:
                published_at_raw = candidate
                break
            published_index = next_nonempty_line(lines, published_index + 1)
        if detail_url and title and detail_url not in seen:
            seen.add(detail_url)
            results.append(
                DiscoveredPage(
                    source_item_id=detail_url,
                    detail_url=detail_url,
                    title=title,
                    published_at=parse_published_at(published_at_raw),
                    published_at_raw=published_at_raw or None,
                )
            )
        index += 1
    return results


def discover_forbes_pages(xml_text: str, *, base_url: str = FORBES_BASE_URL) -> list[DiscoveredPage]:
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid Forbes RSS payload") from exc
    for item in root.findall(".//item"):
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        excerpt = clean_inline_text(item.findtext("description", default=""))
        if not link:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if not is_forbes_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                excerpt=excerpt or None,
            )
        )
    return results


def discover_rss_pages(
    xml_text: str,
    *,
    base_url: str,
    href_patterns: tuple[str, ...],
    excluded_patterns: tuple[str, ...] = (),
) -> list[DiscoveredPage]:
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid RSS payload") from exc
    for item in root.findall(".//item"):
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        description = item.findtext("description", default="")
        published_at_raw = clean_inline_text(
            item.findtext("pubDate", default="")
            or item.findtext("published", default="")
            or item.findtext("{http://www.w3.org/2005/Atom}published", default="")
            or item.findtext("{http://www.w3.org/2005/Atom}updated", default="")
        )
        if not link or not title:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if href_patterns and not any(re.match(pattern, detail_url) for pattern in href_patterns):
            continue
        if excluded_patterns and any(re.search(pattern, detail_url) for pattern in excluded_patterns):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                excerpt=extract_feed_excerpt(description, title=title) or None,
                published_at=parse_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_hk01_pages(html: str, *, base_url: str = HK01_BASE_URL) -> list[DiscoveredPage]:
    payload = extract_next_data_payload(html)
    props = payload.get("props", {})
    page_props = props.get("pageProps") or props.get("initialProps", {}).get("pageProps", {})
    issue = page_props.get("issue") or {}
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for block in issue.get("blocks", []):
        for article in block.get("articles", []):
            article_data = article.get("data") or {}
            detail_path = article_data.get("publishUrl") or article_data.get("canonicalUrl")
            if not detail_path:
                continue
            detail_url = normalize_url(urljoin(base_url, str(detail_path)))
            if detail_url in seen:
                continue
            seen.add(detail_url)
            title = clean_inline_text(str(article_data.get("title") or article_data.get("name") or ""))
            results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title or None))
    return results


def discover_thelec_pages(html: str, *, base_url: str = THELEC_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    containers = [
        nearest_article_container(anchor) or anchor.parent
        for anchor in soup.select("a[href*='articleView.html?idxno=']")
    ]
    for container in containers:
        if not isinstance(container, Tag):
            continue
        page = extract_thelec_discovered_page(container, base_url=base_url)
        if page is None or page.detail_url in seen:
            continue
        seen.add(page.detail_url)
        results.append(page)
    return results or discover_thelec_pages_from_markdown(html, base_url=base_url)


def discover_thelec_pages_from_markdown(payload: str, *, base_url: str = THELEC_BASE_URL) -> list[DiscoveredPage]:
    text = payload
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    link_pattern = re.compile(
        r"(?:^|\n)\s*(?:#{1,6}\s*)?\[([^\]\n]+)\]\((https?://(?:www\.)?thelec\.net/news/articleView\.html\?idxno=\d+)[^)]*\)",
        re.IGNORECASE,
    )
    for match in link_pattern.finditer(text):
        raw_title = clean_inline_text(strip_markdown_formatting(match.group(1)))
        if not raw_title or raw_title.startswith("!["):
            continue
        detail_url = normalize_thelec_url(urljoin(base_url, match.group(2)))
        if detail_url in seen:
            continue
        seen.add(detail_url)
        following_text = text[match.end() : match.end() + 500]
        published_at_raw = extract_first_datetime_text(following_text)
        excerpt = extract_thelec_markdown_excerpt(following_text)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=raw_title,
                excerpt=excerpt or None,
                published_at=parse_thelec_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_etnews_pages(html: str, *, base_url: str = ETNEWS_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        detail_url = normalize_etnews_url(urljoin(base_url, href))
        if not is_etnews_article_url(detail_url) or detail_url in seen:
            continue
        seen.add(detail_url)
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        container = nearest_article_container(anchor)
        if not title and container is not None:
            title = extract_etnews_listing_title(container, detail_url=detail_url)
        excerpt = extract_etnews_listing_excerpt(container, title=title) if container is not None else ""
        published_at_raw = extract_first_datetime_text(container.get_text(" ", strip=True)) if container is not None else ""
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                excerpt=excerpt or None,
                published_at=parse_etnews_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_zdnet_korea_pages(html: str, *, base_url: str = ZDNET_KOREA_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        detail_url = normalize_zdnet_korea_url(urljoin(base_url, href))
        if not is_zdnet_korea_article_url(detail_url) or detail_url in seen:
            continue
        seen.add(detail_url)
        container = nearest_article_container(anchor)
        title = ""
        if container is not None:
            title = extract_zdnet_korea_listing_title(container, detail_url=detail_url)
        if not title:
            title = clean_inline_text(anchor.get_text(" ", strip=True))
        excerpt = extract_zdnet_korea_listing_excerpt(container, title=title) if container is not None else ""
        published_at_raw = extract_zdnet_korea_listing_published_at_text(container) if container is not None else ""
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title or None,
                excerpt=excerpt or None,
                published_at=parse_zdnet_korea_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_tether_pages(payload: Any, *, base_url: str = TETHER_BASE_URL) -> list[DiscoveredPage]:
    if not isinstance(payload, list):
        raise ValueError("invalid Tether discovery payload")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        link = normalize_url(urljoin(base_url, str(item.get("link") or "")))
        title = extract_wp_rendered_text(item.get("title"))
        excerpt = extract_wp_rendered_text(item.get("excerpt"))
        if not link or not title:
            continue
        if link in seen:
            continue
        seen.add(link)
        published_at_raw = str(item.get("date_gmt") or item.get("date") or "").strip()
        results.append(
            DiscoveredPage(
                source_item_id=link,
                detail_url=link,
                title=title,
                excerpt=excerpt or None,
                published_at=parse_published_at(published_at_raw),
                published_at_raw=published_at_raw or None,
            )
        )
    return results


def discover_tether_pages_from_markdown(payload: str, *, base_url: str = TETHER_BASE_URL) -> list[DiscoveredPage]:
    text = extract_jina_markdown_payload(payload)
    lines = [line.strip() for line in text.splitlines()]
    date_pattern = re.compile(r"^[A-Z][a-z]+ \d{1,2}, \d{4}$")
    read_more_pattern = re.compile(r"^\[Read more\]\((?P<url>[^)]+)\)$")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not date_pattern.fullmatch(line):
            index += 1
            continue
        published_at_raw = line
        cursor = next_nonempty_line(lines, index + 1)
        if cursor is None:
            break
        if re.search(r"\bread\b$", lines[cursor], re.IGNORECASE):
            cursor = next_nonempty_line(lines, cursor + 1)
        if cursor is None:
            break
        title = clean_inline_text(lines[cursor])
        excerpt_index = next_nonempty_line(lines, cursor + 1)
        if excerpt_index is None:
            break
        excerpt = clean_inline_text(lines[excerpt_index])
        read_more_index = next_nonempty_line(lines, excerpt_index + 1)
        if read_more_index is None:
            break
        match = read_more_pattern.fullmatch(lines[read_more_index])
        if not match:
            index = read_more_index + 1
            continue
        detail_url = normalize_url(urljoin(base_url, match.group("url")))
        if detail_url and title and detail_url not in seen:
            seen.add(detail_url)
            results.append(
                DiscoveredPage(
                    source_item_id=detail_url,
                    detail_url=detail_url,
                    title=title,
                    excerpt=excerpt or None,
                    published_at=parse_tether_human_date(published_at_raw),
                    published_at_raw=published_at_raw,
                )
            )
        index = read_more_index + 1
    return results


def discover_fortune_pages(html: str, *, base_url: str = FORTUNE_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        if not is_fortune_article_url(detail_url):
            continue
        if not is_fortune_crypto_relevant(title, detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title))
    return results


def discover_ft_pages(
    xml_text: str,
    *,
    base_url: str = FT_BASE_URL,
    timeout_seconds: float = 20.0,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> list[DiscoveredPage]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid FT discovery RSS payload") from exc
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for item in root.findall(".//item")[:FT_GOOGLE_NEWS_MAX_ITEMS]:
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        source_name = clean_inline_text(item.findtext("source", default=""))
        description_html = item.findtext("description", default="")
        if not link or not title:
            continue
        if source_name and source_name != "Financial Times":
            continue
        decoded_url = decode_google_news_url(
            link,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            backoff_seconds=backoff_seconds,
        )
        if not decoded_url:
            continue
        detail_url = normalize_url(urljoin(base_url, decoded_url))
        if not is_ft_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        excerpt = extract_google_news_excerpt(description_html, title=title, source_name=source_name or "Financial Times")
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=strip_google_news_source_suffix(title, "Financial Times"),
                excerpt=excerpt or None,
            )
        )
    return results


def discover_wsj_pages(xml_text: str, *, base_url: str = WSJ_BASE_URL) -> list[DiscoveredPage]:
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError("invalid WSJ RSS payload") from exc
    for item in root.findall(".//item"):
        link = clean_inline_text(item.findtext("link", default=""))
        title = clean_inline_text(item.findtext("title", default=""))
        excerpt = clean_inline_text(item.findtext("description", default=""))
        if not link or not title:
            continue
        detail_url = normalize_url(urljoin(base_url, link))
        if not is_wsj_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(
            DiscoveredPage(
                source_item_id=detail_url,
                detail_url=detail_url,
                title=title,
                excerpt=excerpt or None,
            )
        )
    return results


def discover_bloomberg_pages(html: str, *, base_url: str = BLOOMBERG_BASE_URL) -> list[DiscoveredPage]:
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[DiscoveredPage] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not href:
            continue
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        if len(title) < 8:
            continue
        detail_url = normalize_url(urljoin(base_url, href))
        if not is_bloomberg_article_url(detail_url):
            continue
        if detail_url in seen:
            continue
        seen.add(detail_url)
        results.append(DiscoveredPage(source_item_id=detail_url, detail_url=detail_url, title=title))
    return results


def parse_a16z_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    structured = find_structured_content(soup)
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(structured.get("url") or "")
        or page_url
    )
    title = clean_inline_text(
        str(
            structured.get("headline")
            or structured.get("name")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_published_at(structured.get("datePublished") or structured.get("dateCreated"))
    author_names = normalize_string_list(structured.get("author"), field_name="name")
    categories = normalize_string_list(structured.get("articleSection"))
    tags = normalize_keywords(structured.get("keywords"))
    body = clean_body_text(str(structured.get("articleBody") or "") or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            structured.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    content_format = infer_content_format(canonical)
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": structured.get("datePublished") or structured.get("dateCreated"),
        "structured_type": structured.get("@type"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=published_at,
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format=content_format,
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "structured_type": structured.get("@type"),
            "structured_headline": structured.get("headline") or structured.get("name"),
        },
        metadata=metadata,
    )


def parse_coindesk_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    structured = find_structured_content(soup)
    published_at_raw = (
        structured.get("datePublished")
        or select_meta_content(soup, "property", "article:published_time")
        or structured.get("dateCreated")
    )
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(structured.get("url") or "")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            structured.get("headline")
            or structured.get("name")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    author_names = normalize_string_list(structured.get("author"), field_name="name")
    categories = normalize_string_list(structured.get("articleSection"))
    tags = normalize_keywords(structured.get("keywords"))
    body = clean_body_text(extract_coindesk_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            structured.get("description")
            or structured.get("abstract")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
        "structured_type": structured.get("@type"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_published_at(published_at_raw),
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format="coindesk_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "structured_type": structured.get("@type"),
            "structured_headline": structured.get("headline") or structured.get("name"),
        },
        metadata=metadata,
    )


def parse_cointelegraph_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    published_at_raw = (
        select_meta_content(soup, "property", "article:published_time")
        or select_meta_content(soup, "name", "article:published_time")
        or select_meta_content(soup, "property", "og:updated_time")
        or extract_cointelegraph_embedded_published_at(html)
    )
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    author_names = extract_cointelegraph_authors(soup)
    categories = normalize_string_list(select_meta_content(soup, "property", "article:section"))
    tags = normalize_keywords(
        select_meta_content(soup, "property", "article:tag") or select_meta_content(soup, "name", "keywords")
    )
    body = clean_body_text(extract_cointelegraph_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
        "structured_type": None,
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_published_at(published_at_raw),
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format="cointelegraph_news",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "byline": extract_cointelegraph_byline_text(soup),
        },
        metadata=metadata,
    )


def apply_discovery_page_fallback(article: ParsedArticle, page: DiscoveredPage) -> ParsedArticle:
    published_at = article.published_at or page.published_at
    metadata = dict(article.metadata)
    changed = published_at != article.published_at
    if not metadata.get("published_at_raw") and page.published_at_raw:
        metadata["published_at_raw"] = page.published_at_raw
        changed = True
    if not changed:
        return article
    return replace(article, published_at=published_at, metadata=metadata)


def parse_decrypt_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    structured = find_structured_content(soup)
    published_at_raw = (
        structured.get("datePublished")
        or select_meta_content(soup, "property", "article:published_time")
        or structured.get("dateCreated")
    )
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(structured.get("url") or "")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            structured.get("headline")
            or structured.get("name")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    author_names = normalize_string_list(structured.get("author"), field_name="name") or normalize_string_list(
        extract_decrypt_author(soup)
    )
    categories = normalize_string_list(structured.get("articleSection"))
    tags = normalize_keywords(structured.get("keywords"))
    body = clean_body_text(extract_decrypt_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            structured.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
        "structured_type": structured.get("@type"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_published_at(published_at_raw),
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format="decrypt_news",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "structured_type": structured.get("@type"),
            "structured_headline": structured.get("headline") or structured.get("name"),
        },
        metadata=metadata,
    )


def parse_forbes_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    payload = extract_next_data_payload(html)
    article = (((payload.get("props") or {}).get("pageProps") or {}).get("data") or {}).get("article") or {}
    canonical = normalize_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or str(article.get("canonicalUrl") or "")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            article.get("title")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_published_at(article.get("date"))
    author_names = normalize_string_list(article.get("authorsList"), field_name="name") or normalize_string_list(
        article.get("author"),
        field_name="name",
    )
    categories = normalize_string_list(
        [
            article.get("displayChannel"),
            article.get("displaySection"),
            article.get("channelSection"),
        ]
    )
    body = clean_body_text(extract_forbes_body(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            article.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "article_id": article.get("articleId"),
        "blog_name": article.get("blogName"),
        "date_raw": article.get("date"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=published_at,
        author_names=author_names,
        tags=[],
        categories=categories,
        excerpt=excerpt,
        content_format="forbes_digital_assets",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "article_id": article.get("articleId"),
            "blog_name": article.get("blogName"),
        },
        metadata=metadata,
    )


def parse_hk01_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    payload = extract_next_data_payload(html)
    props = payload.get("props", {})
    page_props = props.get("pageProps") or props.get("initialProps", {}).get("pageProps", {})
    article = page_props.get("article") or {}
    canonical = normalize_url(
        urljoin(
            HK01_BASE_URL,
            str(article.get("canonicalUrl") or article.get("publishUrl") or select_attr(soup, "link[rel='canonical']", "href") or page_url),
        )
    )
    title = clean_inline_text(
        str(
            article.get("title")
            or select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at = parse_unix_timestamp(article.get("publishTime")) or parse_published_at(article.get("publishTime"))
    author_names = normalize_string_list(article.get("authors"), field_name="name")
    tags = normalize_string_list(article.get("tags"), field_name="name")
    categories = normalize_string_list(
        [
            article.get("mainCategory"),
            article.get("categories"),
        ],
        field_name="name",
    )
    body = clean_body_text(extract_hk01_body(article.get("blocks", [])) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            article.get("description")
            or select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body[:240]
        )
    )
    metadata = {
        "article_id": article.get("articleId"),
        "publish_time_raw": article.get("publishTime"),
        "content_type": article.get("contentType"),
        "main_category_id": article.get("mainCategoryId"),
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=published_at,
        author_names=author_names,
        tags=tags,
        categories=categories,
        excerpt=excerpt,
        content_format="hk01_issue_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "article_id": article.get("articleId"),
            "content_type": article.get("contentType"),
        },
        metadata=metadata,
    )


def parse_thelec_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    canonical = normalize_thelec_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at_raw = (
        select_meta_content(soup, "property", "article:published_time")
        or select_meta_content(soup, "name", "article:published_time")
        or extract_thelec_published_at_text(soup)
    )
    author_names = extract_thelec_author_names(soup)
    categories = extract_thelec_categories(soup)
    body = clean_body_text(extract_thelec_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or extract_thelec_subtitle(soup)
            or body.split("\n\n", 1)[0][:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_thelec_published_at(published_at_raw),
        author_names=author_names,
        tags=[],
        categories=categories,
        excerpt=excerpt,
        content_format="thelec_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
        },
        metadata=metadata,
    )


def parse_etnews_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    canonical = normalize_etnews_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or select_text(soup, "#article_title_h2")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at_raw = (
        select_meta_content(soup, "property", "article:published_time")
        or select_meta_content(soup, "name", "article:published_time")
        or extract_etnews_published_at_text(soup)
    )
    author_names = extract_etnews_author_names(soup)
    categories = extract_etnews_categories(soup)
    body = clean_body_text(extract_etnews_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or body.split("\n\n", 1)[0][:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_etnews_published_at(published_at_raw),
        author_names=author_names,
        tags=[],
        categories=categories,
        excerpt=excerpt,
        content_format="etnews_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
        },
        metadata=metadata,
    )


def parse_zdnet_korea_article(html: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    soup = BeautifulSoup(html, "html.parser")
    canonical = normalize_zdnet_korea_url(
        select_attr(soup, "link[rel='canonical']", "href")
        or select_meta_content(soup, "property", "og:url")
        or page_url
    )
    title = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:title")
            or select_meta_content(soup, "name", "twitter:title")
            or select_meta_content(soup, "property", "dd:title")
            or select_text(soup, ".news_head h1")
            or pick_heading_text(soup)
            or canonical
        )
    )
    published_at_raw = (
        select_meta_content(soup, "property", "article:published_time")
        or select_meta_content(soup, "property", "dd:published_time")
        or extract_zdnet_korea_published_at_text(soup)
    )
    author_names = extract_zdnet_korea_author_names(soup)
    categories = extract_zdnet_korea_categories(soup)
    body = clean_body_text(extract_zdnet_korea_body(soup) or extract_body_text(soup))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = clean_inline_text(
        str(
            select_meta_content(soup, "property", "og:description")
            or select_meta_content(soup, "name", "description")
            or select_text(soup, ".news_head .summary")
            or body.split("\n\n", 1)[0][:240]
        )
    )
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_zdnet_korea_published_at(published_at_raw),
        author_names=author_names,
        tags=[],
        categories=categories,
        excerpt=excerpt,
        content_format="zdnet_korea_article",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
        },
        metadata=metadata,
    )


def parse_tether_article(payload: Any, *, page_url: str, source_item_id: str) -> ParsedArticle:
    if isinstance(payload, list):
        if not payload:
            raise ValueError(f"Tether article payload is empty for {page_url}")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError(f"invalid Tether article payload for {page_url}")
    canonical = normalize_url(urljoin(TETHER_BASE_URL, str(payload.get("link") or page_url)))
    title = extract_wp_rendered_text(payload.get("title")) or canonical
    body = clean_body_text(extract_wp_rendered_body(str((payload.get("content") or {}).get("rendered") or "")))
    if not body:
        raise ValueError(f"article body is empty for {page_url}")
    excerpt = extract_wp_rendered_text(payload.get("excerpt")) or body[:240]
    published_at_raw = payload.get("date_gmt") or payload.get("date")
    metadata = {
        "canonical_url": canonical,
        "post_id": payload.get("id"),
        "published_at_raw": published_at_raw,
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=parse_published_at(published_at_raw),
        author_names=[],
        tags=extract_wp_term_names(payload, taxonomy="post_tag"),
        categories=extract_wp_term_names(payload, taxonomy="category"),
        excerpt=excerpt,
        content_format="tether_news_post",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "post_id": payload.get("id"),
        },
        metadata=metadata,
    )


def parse_tether_markdown_article(payload: str, *, page_url: str, source_item_id: str) -> ParsedArticle:
    text = extract_jina_markdown_payload(payload)
    lines = [line.strip() for line in text.splitlines()]
    title = ""
    for line in lines:
        if not line.startswith("# "):
            continue
        heading = clean_inline_text(line[2:])
        if heading.endswith(" - Tether.io"):
            title = heading[: -len(" - Tether.io")].strip()
            break
    title = title or normalize_url(page_url)
    anchor_index = -1
    for index, line in enumerate(lines):
        if clean_inline_text(strip_markdown_formatting(line)) == title:
            anchor_index = index
            break
    body_start = next_nonempty_line(lines, anchor_index + 1 if anchor_index >= 0 else 0)
    if body_start is None:
        raise ValueError(f"Tether markdown article body is empty for {page_url}")
    body_lines: list[str] = []
    for line in lines[body_start:]:
        if "BACK TO NEWS" in line or line.startswith("## latest news"):
            break
        if not line:
            continue
        cleaned = clean_inline_text(strip_markdown_formatting(line))
        if not cleaned:
            continue
        body_lines.append(cleaned)
    body = clean_body_text("\n\n".join(body_lines))
    if not body:
        raise ValueError(f"Tether markdown article body is empty for {page_url}")
    canonical = normalize_url(page_url)
    excerpt = body.split("\n\n", 1)[0][:240]
    published_at_raw = None
    if body_lines:
        date_match = re.match(r"^(?P<date>\d{1,2}\s+[A-Z][a-z]+,\s+\d{4})", body_lines[0])
        if date_match:
            published_at_raw = date_match.group("date")
    metadata = {
        "canonical_url": canonical,
        "published_at_raw": published_at_raw,
        "proxy_fallback": "jina_markdown",
    }
    return ParsedArticle(
        source_item_id=source_item_id,
        canonical_url=canonical,
        title=title,
        content=body,
        published_at=None,
        excerpt=excerpt,
        content_format="tether_news_post",
        raw_payload={
            "page_url": page_url,
            "canonical_url": canonical,
            "proxy_fallback": "jina_markdown",
        },
        metadata=metadata,
    )


def find_structured_content(soup: BeautifulSoup) -> dict[str, Any]:
    for script in soup.select("script[type='application/ld+json']"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        match = pick_structured_article(payload)
        if match:
            return match
    return {}


def pick_structured_article(payload: Any) -> dict[str, Any] | None:
    article_types = {
        "Article",
        "NewsArticle",
        "BlogPosting",
        "VideoObject",
        "PodcastEpisode",
        "PodcastSeries",
        "CreativeWork",
    }
    if isinstance(payload, dict):
        payload_type = payload.get("@type")
        type_values = payload_type if isinstance(payload_type, list) else [payload_type]
        if any(value in article_types for value in type_values):
            if payload.get("headline") or payload.get("name") or payload.get("articleBody"):
                return payload
        for value in payload.values():
            match = pick_structured_article(value)
            if match:
                return match
    elif isinstance(payload, list):
        for item in payload:
            match = pick_structured_article(item)
            if match:
                return match
    return None


def extract_cointelegraph_authors(soup: BeautifulSoup) -> list[str]:
    byline = extract_cointelegraph_byline_text(soup)
    if not byline:
        return []
    matches = re.findall(r"(?:Written by|Reviewed by)\s+(.+?)\s*(?:,|$)", byline)
    return unique_preserve_order(clean_inline_text(match.replace("\u2060", " ")) for match in matches if clean_inline_text(match))


def extract_cointelegraph_byline_text(soup: BeautifulSoup) -> str:
    node = soup.select_one("[data-testid='post-byline']")
    if node is None:
        return ""
    return clean_inline_text(node.get_text(" ", strip=True).replace("\u2060", " "))


def extract_cointelegraph_embedded_published_at(html: str) -> str | None:
    for pattern in (
        r'"publishedAt"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
    ):
        match = re.search(pattern, html)
        if match:
            return clean_inline_text(match.group(1))
    return None


def extract_next_data_payload(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if match is None:
        return {}
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}


def decode_google_news_url(
    source_url: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str | None:
    parsed = urlparse(source_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if parsed.netloc.lower() != "news.google.com" or len(path_parts) < 2 or path_parts[-2] not in {"articles", "read"}:
        return source_url
    base64_str = path_parts[-1]
    params = get_google_news_decoding_params(
        base64_str,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )
    if not params:
        return None
    signature, timestamp = params
    return execute_google_news_decode(
        base64_str,
        signature=signature,
        timestamp=timestamp,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
    )


def get_google_news_decoding_params(
    base64_str: str,
    *,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> tuple[str, str] | None:
    for candidate_url in (
        f"{GOOGLE_NEWS_BASE_URL}/articles/{base64_str}",
        f"{GOOGLE_NEWS_BASE_URL}/rss/articles/{base64_str}",
    ):
        try:
            html = fetch_html(
                candidate_url,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
            )
        except RuntimeError:
            continue
        signature_match = re.search(r'data-n-a-sg="([^"]+)"', html)
        timestamp_match = re.search(r'data-n-a-ts="([^"]+)"', html)
        if signature_match and timestamp_match:
            return signature_match.group(1), timestamp_match.group(1)
    return None


def execute_google_news_decode(
    base64_str: str,
    *,
    signature: str,
    timestamp: str,
    timeout_seconds: float,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
) -> str | None:
    payload = [
        "Fbv4je",
        (
            f'["garturlreq",[["X","X",["X","X"],null,null,1,1,"US:en",null,1,null,null,null,null,null,0,1],'
            f'"X","X",1,[1,1,1],1,1,null,0,0,null,0],"{base64_str}",{timestamp},"{signature}"]'
        ),
    ]
    last_error: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            response = requests.post(
                f"{GOOGLE_NEWS_BASE_URL}/_/DotsSplashUi/data/batchexecute",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                    "User-Agent": REQUEST_HEADERS["User-Agent"],
                },
                data=f"f.req={quote(json.dumps([[payload]]))}",
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            parts = response.text.split("\n\n")
            if len(parts) < 2:
                return None
            parsed = json.loads(parts[1])[:-2]
            decoded = json.loads(parsed[0][2])[1]
            return normalize_url(decoded)
        except Exception as exc:
            last_error = exc
            if attempt >= max_attempts:
                break
            time.sleep(max(0.0, backoff_seconds) * attempt)
    if last_error:
        return None
    return None


def strip_google_news_source_suffix(title: str, source_name: str) -> str:
    suffix = f" - {source_name}".strip()
    if title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title.strip()


def extract_google_news_excerpt(description_html: str, *, title: str, source_name: str) -> str:
    if not description_html:
        return ""
    text = clean_inline_text(BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True))
    clean_title = strip_google_news_source_suffix(title, source_name)
    if text.startswith(clean_title):
        text = text[len(clean_title) :].strip()
    if text.endswith(source_name):
        text = text[: -len(source_name)].strip()
    return text.strip(" -\u00a0")


def extract_feed_excerpt(description_html: str, *, title: str = "") -> str:
    if not description_html:
        return ""
    text = clean_inline_text(BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True))
    if title and text.startswith(title):
        text = text[len(title) :].strip()
    return text.strip(" -\u00a0")


def is_forbes_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("forbes.com") and parsed.path.startswith("/sites/digital-assets/") and not parsed.path.endswith("/feed/")


def is_ft_article_url(url: str) -> bool:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    return parsed.netloc.lower().endswith("ft.com") and len(parts) == 2 and parts[0] == "content"


def is_fortune_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("fortune.com"):
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return False
    if parts[0] == "section":
        return False
    return all(part.isdigit() for part in parts[:3])


def is_fortune_crypto_relevant(title: str, url: str) -> bool:
    if "/crypto/" in urlparse(url).path.lower():
        return True
    return bool(FORTUNE_CRYPTO_PATTERN.search(title))


def is_wsj_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("wsj.com"):
        return False
    path = parsed.path.rstrip("/")
    if path.startswith("/articles/"):
        return True
    parts = [part for part in path.split("/") if part]
    return len(parts) >= 2 and parts[0] in {"business", "economy", "finance"}


def is_bloomberg_article_url(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("bloomberg.com"):
        return False
    return parsed.path.startswith(("/news/articles/", "/opinion/articles/", "/graphics/"))


def extract_forbes_body(soup: BeautifulSoup) -> str:
    best_text = ""
    for selector in (".article-body", ".fs-article", "article"):
        node = soup.select_one(selector)
        if node is None:
            continue
        fragment = BeautifulSoup(str(node), "html.parser")
        drop_noise(fragment)
        parts: list[str] = []
        for part in collect_text_parts(fragment):
            lower_part = part.lower()
            if lower_part.startswith("sign up now for cryptocodex"):
                continue
            if lower_part.startswith("sign up now for the free cryptocodex"):
                continue
            if lower_part.startswith("this voice experience is generated by ai"):
                continue
            parts.append(part)
        text = "\n\n".join(parts).strip()
        if len(text) > len(best_text):
            best_text = text
    return best_text


def extract_coindesk_body(soup: BeautifulSoup) -> str:
    node = soup.select_one("div.document-body")
    if node is None:
        return ""
    return extract_body_from_node(node)


def extract_cointelegraph_body(soup: BeautifulSoup) -> str:
    for selector in ("[data-testid='post'] .ct-prose-2", "[data-testid='post']", "main"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = extract_body_from_node(node)
        if text:
            return text
    return ""


def extract_decrypt_body(soup: BeautifulSoup) -> str:
    node = soup.select_one(".post-content")
    if node is None:
        return ""
    return extract_body_from_node(node)


def extract_hk01_body(blocks: list[dict[str, Any]]) -> str:
    html_token_parts: list[str] = []
    summary_parts: list[str] = []
    for block in blocks:
        token_parts = extract_hk01_text_nodes(block.get("htmlTokens"))
        if token_parts:
            html_token_parts.extend(token_parts)
            continue
        summary_parts.extend(extract_hk01_text_nodes(block.get("summary")))
    parts = html_token_parts or summary_parts
    return "\n\n".join(unique_preserve_order(parts)).strip()


def extract_thelec_body(soup: BeautifulSoup) -> str:
    best_text = ""
    for selector in (
        "#article-view-content-div",
        ".article-view-content-div",
        "#article-view-content",
        ".article-view-content",
        ".view-content",
        ".article_txt",
        ".article-body",
        "article",
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        fragment = BeautifulSoup(str(node), "html.parser")
        for drop_selector in ("h1", ".article-sub-title", ".article-meta", ".article-head-title-list"):
            for drop_node in fragment.select(drop_selector):
                drop_node.decompose()
        text = extract_body_from_node(fragment)
        if len(text) > len(best_text):
            best_text = text
    return best_text


def extract_thelec_author_names(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    for selector in (
        "meta[name='author']",
        "[itemprop='author']",
        ".article-meta .name",
        ".article-head .name",
        ".article_writer",
        ".article-byline .name",
        ".byline .name",
    ):
        if selector.startswith("meta"):
            text = clean_inline_text(select_meta_content(soup, "name", "author") or "")
        else:
            node = soup.select_one(selector)
            text = clean_inline_text(node.get_text(" ", strip=True)) if node else ""
        text = re.sub(r"^(?:By|기사\s*입력)\s+", "", text, flags=re.IGNORECASE).strip()
        if text and not re.search(r"승인|입력|댓글", text):
            candidates.append(text)
    if candidates:
        return unique_preserve_order(candidates)
    page_text = clean_inline_text(soup.get_text(" ", strip=True))
    match = re.search(r"(?P<author>[A-Za-z][A-Za-z\s,.'-]{1,80})\s*승인\s*\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}", page_text)
    if match:
        return [clean_inline_text(match.group("author"))]
    return []


def extract_thelec_categories(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    for selector in (
        ".article-head-title-list a",
        ".breadcrumb a",
        ".location a",
        ".article-category",
    ):
        for node in soup.select(selector):
            text = clean_inline_text(node.get_text(" ", strip=True))
            if text and text.upper() != "HOME":
                candidates.append(text)
    return unique_preserve_order(candidates)


def extract_thelec_subtitle(soup: BeautifulSoup) -> str:
    for selector in (
        ".article-sub-title",
        ".article-summary",
        ".summary",
        ".view-subtitle",
        "article h2",
    ):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = clean_inline_text(node.get_text(" ", strip=True))
        if text:
            return text
    return ""


def extract_thelec_published_at_text(soup: BeautifulSoup) -> str | None:
    for selector in (".article-meta", ".article-head", ".view-side", "article"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = extract_first_datetime_text(node.get_text(" ", strip=True))
        if text:
            return text
    fallback = extract_first_datetime_text(soup.get_text(" ", strip=True))
    return fallback or None


def extract_first_datetime_text(value: str) -> str:
    match = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?", value)
    return match.group(0) if match else ""


def parse_thelec_published_at(value: Any) -> datetime | None:
    text = clean_inline_text(str(value or ""))
    if not text:
        return None
    for fmt in ("%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=THELEC_TIMEZONE).astimezone(UTC)
        except ValueError:
            continue
    return parse_published_at(text)


def extract_hk01_text_nodes(value: Any) -> list[str]:
    parts: list[str] = []
    if isinstance(value, str):
        text = clean_inline_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))
        if text:
            parts.append(text)
        return parts
    if isinstance(value, list):
        for item in value:
            parts.extend(extract_hk01_text_nodes(item))
        return parts
    if isinstance(value, dict):
        for key in ("content", "text", "summary", "title"):
            if key in value:
                parts.extend(extract_hk01_text_nodes(value.get(key)))
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                parts.extend(extract_hk01_text_nodes(nested))
        return parts
    return parts


def extract_thelec_discovered_page(container: Tag, *, base_url: str) -> DiscoveredPage | None:
    anchors: list[tuple[int, str, str]] = []
    for anchor in container.select("a[href*='articleView.html?idxno=']"):
        href = anchor.get("href")
        if not href:
            continue
        detail_url = normalize_thelec_url(urljoin(base_url, href))
        title = clean_inline_text(anchor.get_text(" ", strip=True))
        anchors.append((len(title), title, detail_url))
    if not anchors:
        return None
    _, title, detail_url = sorted(anchors, key=lambda item: (item[0], item[1]), reverse=True)[0]
    if not detail_url or not title:
        return None
    metadata_text = clean_inline_text(container.get_text(" ", strip=True))
    published_at_raw = extract_first_datetime_text(metadata_text)
    excerpt = extract_thelec_listing_excerpt(container, title=title, published_at_raw=published_at_raw)
    return DiscoveredPage(
        source_item_id=detail_url,
        detail_url=detail_url,
        title=title,
        excerpt=excerpt or None,
        published_at=parse_thelec_published_at(published_at_raw),
        published_at_raw=published_at_raw or None,
    )


def extract_thelec_listing_excerpt(container: Tag, *, title: str, published_at_raw: str) -> str:
    for selector in (".lead", ".summary", ".description", "p"):
        node = container.select_one(selector)
        if node is None:
            continue
        text = clean_inline_text(node.get_text(" ", strip=True))
        if text and text != title and text != published_at_raw:
            return text
    candidates: list[str] = []
    for node in container.select("p, span, div"):
        text = clean_inline_text(node.get_text(" ", strip=True))
        if not text or text == title or text == published_at_raw:
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z\s,.'-]+\s*\|\s*\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}", text):
            continue
        if re.fullmatch(r"[A-Z][A-Z\s]+\s*\|\s*[A-Za-z][A-Za-z\s,.'-]+\s*\|\s*\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}", text):
            continue
        candidates.append(text)
    for candidate in candidates:
        if len(candidate) >= 30 and candidate != title:
            return candidate
    return ""


def extract_thelec_markdown_excerpt(value: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    cleaned = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"(?:^|\n)\s*#{1,6}\s+", "\n", cleaned)
    candidates: list[str] = []
    for line in cleaned.splitlines():
        text = clean_inline_text(strip_markdown_formatting(line))
        if not text:
            continue
        if re.fullmatch(r"\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?", text):
            continue
        if re.fullmatch(r"\d{2}[./-]\d{2}\s+\d{2}:\d{2}", text):
            continue
        candidates.append(text)
    return candidates[0][:240] if candidates else ""


def extract_wp_rendered_text(value: Any) -> str:
    if isinstance(value, dict):
        rendered = value.get("rendered")
        if rendered is not None:
            value = rendered
    if not isinstance(value, str):
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return clean_inline_text(text)


def extract_wp_rendered_body(rendered_html: str) -> str:
    if not rendered_html.strip():
        return ""
    normalized_html = re.sub(r"(<br\s*/?>\s*){2,}", "</p><p>", rendered_html, flags=re.IGNORECASE)
    normalized_html = re.sub(r"<br\s*/?>", "\n", normalized_html, flags=re.IGNORECASE)
    fragment = BeautifulSoup(normalized_html, "html.parser")
    drop_noise(fragment)
    parts = collect_text_parts(fragment)
    return "\n\n".join(part for part in parts if part).strip()


def extract_wp_term_names(payload: dict[str, Any], *, taxonomy: str) -> list[str]:
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        return []
    groups = embedded.get("wp:term")
    if not isinstance(groups, list):
        return []
    terms: list[str] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict):
                continue
            if str(item.get("taxonomy") or "") != taxonomy:
                continue
            name = clean_inline_text(str(item.get("name") or ""))
            if name:
                terms.append(name)
    return unique_preserve_order(terms)


def pick_tether_post_slug(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2 or parts[0] != "news":
        raise ValueError(f"invalid Tether news url: {url}")
    return parts[-1]


def parse_tether_human_date(value: Any) -> datetime | None:
    text = clean_inline_text(str(value or ""))
    if not text:
        return None
    for fmt in ("%B %d, %Y", "%d %B, %Y", "%b %d, %Y", "%d %b, %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return parse_published_at(text)


def next_nonempty_line(lines: list[str], start_index: int) -> int | None:
    for index in range(max(0, start_index), len(lines)):
        if lines[index].strip():
            return index
    return None


def strip_markdown_formatting(value: str) -> str:
    text = re.sub(r"!\[[^\]]*]\([^)]+\)", " ", value)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^[#>\-\*\s]+", "", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("–", " – ")
    return text


def build_thelec_fallback_urls(url: str) -> list[str]:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("thelec.net"):
        return []
    host = parsed.netloc.lower()
    alternate_host = "thelec.net" if host.startswith("www.") else "www.thelec.net"
    alternate_url = urlunparse(parsed._replace(netloc=alternate_host))
    return unique_preserve_order([alternate_url, build_jina_proxy_url(alternate_url)])


def normalize_thelec_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("thelec.net"):
        return url.strip()
    idxno = parse_qs(parsed.query).get("idxno", [])
    query = f"idxno={idxno[0]}" if idxno and idxno[0] else parsed.query
    path = parsed.path or "/"
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), path, "", query, ""))


def normalize_etnews_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.netloc.lower().endswith("etnews.com"):
        return url.strip()
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), path, "", "", ""))


def is_etnews_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.lower().endswith("etnews.com") and re.fullmatch(r"/\d{12,}", parsed.path.rstrip("/") or "")


def normalize_zdnet_korea_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if not parsed.netloc.lower().endswith("zdnet.co.kr"):
        return url.strip()
    no_value = parse_qs(parsed.query).get("no", [])
    query = f"no={no_value[0]}" if no_value and no_value[0] else parsed.query
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme or "https", parsed.netloc.lower(), path, "", query, ""))


def is_zdnet_korea_article_url(url: str) -> bool:
    parsed = urlparse(url)
    no_value = parse_qs(parsed.query).get("no", [])
    return parsed.netloc.lower().endswith("zdnet.co.kr") and parsed.path.rstrip("/") == "/view" and bool(no_value and re.fullmatch(r"\d{8,}", no_value[0]))


def normalize_the_block_discovery_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("theblock.co"):
        return url.strip()
    normalized = normalize_url(url)
    normalized_parsed = urlparse(normalized)
    normalized = urlunparse(normalized_parsed._replace(query="", fragment=""))
    if not normalized.endswith("/"):
        normalized += "/"
    return normalized


def extract_decrypt_author(soup: BeautifulSoup) -> str:
    author = select_meta_content(soup, "name", "author") or ""
    if " / " in author:
        return author.split(" / ", 1)[1].strip()
    return author.strip()


def nearest_article_container(anchor: Tag) -> Tag | None:
    for parent in anchor.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name in {"li", "article", "section", "tr"}:
            return parent
        if parent.name == "div" and parent.select("a[href]"):
            text = clean_inline_text(parent.get_text(" ", strip=True))
            if len(text) >= 20:
                return parent
    return anchor.parent if isinstance(anchor.parent, Tag) else None


def extract_etnews_listing_title(container: Tag, *, detail_url: str) -> str:
    for anchor in container.select("a[href]"):
        href = str(anchor.get("href") or "")
        if normalize_etnews_url(urljoin(ETNEWS_BASE_URL, href)) != detail_url:
            continue
        text = clean_inline_text(anchor.get_text(" ", strip=True))
        if text:
            return text
    for selector in ("h1", "h2", "h3", "strong", ".tit", ".title", ".subject"):
        node = container.select_one(selector)
        text = clean_inline_text(node.get_text(" ", strip=True)) if node else ""
        if text:
            return text
    return ""


def extract_etnews_listing_excerpt(container: Tag, *, title: str) -> str:
    text = clean_inline_text(container.get_text(" ", strip=True))
    if title and title in text:
        text = text.replace(title, " ", 1)
    text = re.sub(r"\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}(?::\d{2})?", " ", text)
    text = clean_inline_text(text)
    return text[:240]


def extract_zdnet_korea_listing_title(container: Tag, *, detail_url: str) -> str:
    for selector in ("h1", "h2", "h3", "strong", ".title", ".subject", ".tit"):
        node = container.select_one(selector)
        text = clean_inline_text(node.get_text(" ", strip=True)) if node else ""
        if text:
            return text
    for anchor in container.select("a[href]"):
        href = str(anchor.get("href") or "")
        if normalize_zdnet_korea_url(urljoin(ZDNET_KOREA_BASE_URL, href)) != detail_url:
            continue
        text = clean_inline_text(anchor.get_text(" ", strip=True))
        if text:
            return text
    return ""


def extract_zdnet_korea_listing_excerpt(container: Tag, *, title: str) -> str:
    for selector in (".assetText p:not(.byline)", ".top_summary", "p:not(.byline)"):
        for node in container.select(selector):
            text = clean_inline_text(node.get_text(" ", strip=True))
            if text and text != title:
                return text[:240]
    text = clean_inline_text(container.get_text(" ", strip=True))
    if title and title in text:
        text = text.replace(title, " ", 1)
    text = re.sub(r"\d{4}[./-]\d{2}[./-]\d{2}\s*(?:AM|PM)?\s*\d{2}:\d{2}", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[가-힣A-Za-z][가-힣A-Za-z\s.'-]{1,40}\s*기자", " ", text)
    return clean_inline_text(text)[:240]


def extract_zdnet_korea_listing_published_at_text(container: Tag) -> str | None:
    for selector in (".byline span", ".meta span", ".top_reporter"):
        node = container.select_one(selector)
        if node is None:
            continue
        text = clean_inline_text(node.get_text(" ", strip=True))
        match = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}\s*(?:AM|PM)?\s*\d{2}:\d{2}", text, re.IGNORECASE)
        if match:
            return match.group(0)
    text = clean_inline_text(container.get_text(" ", strip=True))
    match = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}\s*(?:AM|PM)?\s*\d{2}:\d{2}", text, re.IGNORECASE)
    return match.group(0) if match else None


def extract_etnews_body(soup: BeautifulSoup) -> str:
    node = soup.select_one("#articleBody") or soup.select_one(".article_body")
    if node is None:
        return ""
    fragment = BeautifulSoup(str(node), "html.parser")
    drop_noise(fragment)
    for selector in (".article_image", ".reporter_info", ".copyright", ".ad", ".advertisement"):
        for item in fragment.select(selector):
            item.decompose()
    parts = collect_text_parts(fragment)
    if parts:
        return "\n\n".join(parts)
    return fragment.get_text("\n\n", strip=True)


def extract_zdnet_korea_body(soup: BeautifulSoup) -> str:
    node = soup.select_one("#articleBody > div[id^='content-']") or soup.select_one("#articleBody") or soup.select_one(".view_cont")
    if node is None:
        return ""
    fragment = BeautifulSoup(str(node), "html.parser")
    drop_noise(fragment)
    for selector in (".view_ad", ".news_box.connect", ".mt_bn_box", ".reporter_list2", ".like_box", ".like_under_box", ".reporter_naver_box", ".tags"):
        for item in fragment.select(selector):
            item.decompose()
    for heading in fragment.select("h1, h2, h3, h4"):
        text = clean_inline_text(heading.get_text(" ", strip=True))
        if text == "관련기사":
            heading.decompose()
    parts = collect_text_parts(fragment)
    if parts:
        return "\n\n".join(parts)
    return fragment.get_text("\n\n", strip=True)


def extract_etnews_author_names(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    for selector in ("meta[name='author']", ".reporter_info", ".byline", ".reporter"):
        if selector.startswith("meta"):
            text = clean_inline_text(select_meta_content(soup, "name", "author") or "")
        else:
            node = soup.select_one(selector)
            text = clean_inline_text(node.get_text(" ", strip=True)) if node else ""
        text = re.sub(r"\s*기자\s*기사\s*더보기.*$", "", text).strip()
        match = re.search(r"([가-힣A-Za-z][가-힣A-Za-z\s.'-]{1,40})\s*기자", text)
        if match:
            text = clean_inline_text(match.group(1))
        if text and not re.search(r"기사\s*더보기|공유하기|발행일", text):
            candidates.append(text)
    return unique_preserve_order(candidates)


def extract_zdnet_korea_author_names(soup: BeautifulSoup) -> list[str]:
    candidates: list[str] = []
    for value in (
        select_meta_content(soup, "property", "dd:author"),
        select_meta_content(soup, "property", "dable:author"),
        select_text(soup, ".reporter_info strong"),
        select_text(soup, ".reporter_name strong"),
    ):
        text = clean_inline_text(str(value or ""))
        text = re.sub(r"\s*기자$", "", text).strip()
        if text:
            candidates.append(text)
    return unique_preserve_order(candidates)


def extract_etnews_categories(soup: BeautifulSoup) -> list[str]:
    header = soup.select_one(".article_header")
    if header is None:
        return []
    title = select_text(soup, "#article_title_h2")
    text = clean_inline_text(header.get_text(" ", strip=True))
    if title and title in text:
        text = text.split(title, 1)[0]
    categories = [part for part in re.split(r"\s+", text) if part and part not in {"뉴스"}]
    return unique_preserve_order(categories[:3])


def extract_zdnet_korea_categories(soup: BeautifulSoup) -> list[str]:
    candidates = normalize_string_list(
        select_meta_content(soup, "property", "article:section")
        or select_meta_content(soup, "property", "dd:category")
    )
    if candidates:
        return candidates
    text = select_text(soup, ".news_head .meta a")
    return [text] if text else []


def extract_etnews_published_at_text(soup: BeautifulSoup) -> str | None:
    for selector in (".article_header", "#articleBody", "article"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = extract_first_datetime_text(node.get_text(" ", strip=True))
        if text:
            return text
    return extract_first_datetime_text(soup.get_text(" ", strip=True)) or None


def extract_zdnet_korea_published_at_text(soup: BeautifulSoup) -> str | None:
    for selector in (".news_head .meta", "#articleBody", "article"):
        node = soup.select_one(selector)
        if node is None:
            continue
        text = clean_inline_text(node.get_text(" ", strip=True))
        match = re.search(r"\d{4}[./-]\d{2}[./-]\d{2}\s+\d{2}:\d{2}", text)
        if match:
            return match.group(0)
    return extract_first_datetime_text(soup.get_text(" ", strip=True)) or None


def parse_etnews_published_at(value: Any) -> datetime | None:
    text = clean_inline_text(str(value or ""))
    if not text:
        return None
    if re.search(r"[+-]\d{2}:?\d{2}|Z$", text):
        return parse_published_at(text)
    for fmt in ("%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ETNEWS_TIMEZONE).astimezone(UTC)
        except ValueError:
            continue
    return parse_published_at(text)


def parse_zdnet_korea_published_at(value: Any) -> datetime | None:
    text = clean_inline_text(str(value or ""))
    if not text:
        return None
    if re.search(r"[+-]\d{2}:?\d{2}|Z$", text):
        return parse_published_at(text)
    match = re.fullmatch(r"(\d{4})[./-](\d{2})[./-](\d{2})\s+(AM|PM)\s+(\d{2}):(\d{2})", text, re.IGNORECASE)
    if match:
        year, month, day, meridiem, hour, minute = match.groups()
        hour_value = int(hour) % 12
        if meridiem.upper() == "PM":
            hour_value += 12
        return datetime(int(year), int(month), int(day), hour_value, int(minute), tzinfo=ZDNET_KOREA_TIMEZONE).astimezone(UTC)
    for fmt in ("%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=ZDNET_KOREA_TIMEZONE).astimezone(UTC)
        except ValueError:
            continue
    return parse_published_at(text)


def extract_body_from_node(node: Tag) -> str:
    fragment = BeautifulSoup(str(node), "html.parser")
    drop_noise(fragment)
    return "\n\n".join(part for part in collect_text_parts(fragment) if part).strip()


def extract_body_text(soup: BeautifulSoup) -> str:
    candidates: list[Tag] = []
    for selector in (
        "article",
        "main article",
        "main",
        "[itemprop='articleBody']",
        ".entry-content",
        ".post-content",
        ".content",
        ".prose",
        ".wysiwyg",
    ):
        candidates.extend(soup.select(selector))
    if not candidates:
        candidates = [tag for tag in soup.select("div, section") if len(tag.select("p")) >= 4]
    best_text = ""
    for candidate in candidates:
        fragment = BeautifulSoup(str(candidate), "html.parser")
        drop_noise(fragment)
        parts = collect_text_parts(fragment)
        text = "\n\n".join(part for part in parts if part)
        if len(text) > len(best_text):
            best_text = text
    return best_text


def drop_noise(fragment: BeautifulSoup) -> None:
    for selector in (
        "script",
        "style",
        "noscript",
        "nav",
        "header",
        "footer",
        "form",
        "button",
        "svg",
        "aside",
        "picture",
        "img",
        "figure",
        "figcaption",
    ):
        for node in fragment.select(selector):
            node.decompose()
    pattern = re.compile(
        r"(share|social|related|recommend|newsletter|subscribe|breadcrumb|footer|header|nav|search|topic)",
        re.IGNORECASE,
    )
    for node in fragment.find_all(True):
        if not isinstance(node, Tag) or getattr(node, "attrs", None) is None:
            continue
        attrs = " ".join(
            str(value)
            for key in ("id", "class", "aria-label", "data-testid")
            for value in ([node.get(key)] if node.get(key) is not None else [])
        )
        if pattern.search(attrs):
            node.decompose()


def collect_text_parts(fragment: BeautifulSoup) -> list[str]:
    parts: list[str] = []
    for node in fragment.select("h1, h2, h3, h4, p, li, blockquote"):
        text = clean_inline_text(node.get_text(" ", strip=True))
        if not text:
            continue
        if len(text) <= 2:
            continue
        parts.append(text)
    return parts


def clean_body_text(value: str) -> str:
    text = value.replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()
    lower_text = text.lower()
    cut_points = [lower_text.find(marker) for marker in DISCLAIMER_MARKERS if marker in lower_text]
    cut_points = [index for index in cut_points if index >= 0]
    if cut_points:
        text = text[: min(cut_points)].strip()
    paragraphs: list[str] = []
    for chunk in re.split(r"\n{2,}", text):
        normalized = clean_inline_text(chunk)
        if not normalized:
            continue
        lower_normalized = normalized.lower()
        if lower_normalized.startswith("sign up now for cryptocodex"):
            continue
        if lower_normalized.startswith("sign up now for the free cryptocodex"):
            continue
        if lower_normalized.startswith("this voice experience is generated by ai"):
            continue
        if lower_normalized.startswith("daily debrief newsletter"):
            continue
        if normalized.lower().startswith(("related posts", "read more", "recommended")):
            continue
        paragraphs.append(normalized)
    return "\n\n".join(paragraphs).strip()


def infer_content_format(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith("forbes.com") and parsed.path.startswith("/sites/digital-assets/"):
        return "forbes_digital_assets"
    if parsed.netloc.lower().endswith("hk01.com"):
        return "hk01_issue_article"
    if parsed.netloc.lower().endswith("thelec.net") and parsed.path == "/news/articleView.html":
        return "thelec_article"
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "posts":
        return parts[1]
    return None


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") + "/"
    return urlunparse((parsed.scheme, parsed.netloc.lower(), path, "", "", ""))


def parse_published_at(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y.%m.%d %H:%M", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=UTC)
            except ValueError:
                continue
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_unix_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    if number > 10_000_000_000:
        number = number / 1000
    return datetime.fromtimestamp(number, tz=UTC)


def pick_heading_text(soup: BeautifulSoup) -> str:
    for selector in ("main h1", "article h1", "h1"):
        tag = soup.select_one(selector)
        if tag:
            return tag.get_text(" ", strip=True)
    return ""


def select_attr(soup: BeautifulSoup, selector: str, attr: str) -> str | None:
    node = soup.select_one(selector)
    if node is None:
        return None
    value = node.get(attr)
    return str(value).strip() if value else None


def select_text(soup: BeautifulSoup, selector: str) -> str:
    node = soup.select_one(selector)
    return clean_inline_text(node.get_text(" ", strip=True)) if node else ""


def select_meta_content(soup: BeautifulSoup, attr_name: str, attr_value: str) -> str | None:
    node = soup.find("meta", attrs={attr_name: attr_value})
    if node is None:
        return None
    content = node.get("content")
    return str(content).strip() if content else None


def normalize_string_list(value: Any, *, field_name: str | None = None) -> list[str]:
    items: list[str] = []
    if isinstance(value, list):
        for item in value:
            items.extend(normalize_string_list(item, field_name=field_name))
    elif isinstance(value, dict):
        if field_name and value.get(field_name):
            items.extend(normalize_string_list(value.get(field_name), field_name=field_name))
        elif value.get("name"):
            items.extend(normalize_string_list(value.get("name"), field_name=field_name))
    elif isinstance(value, str):
        cleaned = clean_inline_text(value)
        if cleaned:
            items.append(cleaned)
    return unique_preserve_order(items)


def normalize_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = re.split(r"[,|/]", value)
        return unique_preserve_order(clean_inline_text(part) for part in parts if clean_inline_text(part))
    return normalize_string_list(value)


def clean_inline_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def unique_preserve_order(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
