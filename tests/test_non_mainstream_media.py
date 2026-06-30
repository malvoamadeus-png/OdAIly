from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from packages.non_mainstream_media.fetcher import (
    clean_body_text,
    discover_a16z_pages,
    discover_bloomberg_pages,
    discover_coindesk_pages,
    discover_cointelegraph_pages,
    discover_ctee_pages,
    discover_decrypt_pages,
    discover_etnews_pages,
    discover_ft_pages,
    discover_fortune_pages,
    discover_forbes_pages,
    discover_hankyung_pages,
    fetch_discovered_pages,
    fetch_article,
    discover_hk01_pages,
    discover_thelec_pages,
    discover_zdnet_korea_pages,
    discover_tether_pages,
    discover_the_block_pages,
    discover_wsj_pages,
    get_site_registry,
    parse_a16z_article,
    parse_coindesk_article,
    parse_cointelegraph_article,
    parse_ctee_article,
    parse_decrypt_article,
    parse_etnews_article,
    parse_forbes_article,
    parse_hankyung_article,
    parse_hk01_article,
    parse_thelec_article,
    parse_zdnet_korea_article,
    parse_tether_article,
)
from packages.non_mainstream_media.models import (
    DISCOVERY_MODE_DIRECT,
    DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK,
    SOURCE_GROUP_AI_SOURCE,
    DiscoveredPage,
    ParsedArticle,
    SiteDefinition,
)
from packages.non_mainstream_media.repository import InMemoryNonMainstreamMediaRepository
from packages.non_mainstream_media.telegram_discovery import TelegramDiscoveryWorker, extract_candidate_url
from packages.non_mainstream_media.worker import NonMainstreamMediaWorker
from packages.common.config import TelegramDiscoverySettings


def test_discover_a16z_pages_filters_taxonomy_and_keeps_content_pages() -> None:
    html = """
    <html>
      <body>
        <a href="/posts/">All posts</a>
        <a href="/posts/focus-areas/ai/">Focus area</a>
        <a href="/posts/tags/defi/">Tag page</a>
        <a href="/posts/article/token-launch/">Article</a>
        <a href="https://a16zcrypto.com/posts/videos/founder-talk/?utm_source=test">Video</a>
        <a href="/posts/podcast/market-cycle/">Podcast</a>
        <a href="/posts/article/token-launch/">Duplicate article</a>
      </body>
    </html>
    """

    pages = discover_a16z_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://a16zcrypto.com/posts/article/token-launch/",
        "https://a16zcrypto.com/posts/videos/founder-talk/",
        "https://a16zcrypto.com/posts/podcast/market-cycle/",
    ]


def test_parse_a16z_article_extracts_structured_fields_and_cleans_disclaimer() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://a16zcrypto.com/posts/article/token-launch/" />
        <meta property="og:description" content="A16z outlines a new token launch playbook." />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": "Token Launch Playbook",
            "url": "https://a16zcrypto.com/posts/article/token-launch/",
            "datePublished": "2026-05-17T10:00:00Z",
            "author": [{"name": "Alice"}, {"name": "Bob"}],
            "keywords": "DeFi, Token Design",
            "articleSection": ["Research", "Markets"],
            "articleBody": "First paragraph.\\n\\nSecond paragraph.\\n\\nThis material is for informational purposes only. Extra legal copy."
          }
        </script>
      </head>
      <body></body>
    </html>
    """

    article = parse_a16z_article(
        html,
        page_url="https://a16zcrypto.com/posts/article/token-launch/",
        source_item_id="https://a16zcrypto.com/posts/article/token-launch/",
    )

    assert article.canonical_url == "https://a16zcrypto.com/posts/article/token-launch/"
    assert article.title == "Token Launch Playbook"
    assert article.published_at == datetime(2026, 5, 17, 10, 0, tzinfo=UTC)
    assert article.author_names == ["Alice", "Bob"]
    assert article.tags == ["DeFi", "Token Design"]
    assert article.categories == ["Research", "Markets"]
    assert article.content == "First paragraph.\n\nSecond paragraph."
    assert article.content_format == "article"
    assert article.metadata["structured_type"] == "Article"


def test_registry_assigns_new_external_media_pipeline_modes() -> None:
    registry = get_site_registry()

    assert registry["coindesk"].pipeline_mode == "write_flow"
    assert registry["cointelegraph"].pipeline_mode == "write_flow"
    assert registry["decrypt"].pipeline_mode == "write_flow"
    assert registry["tether_news"].pipeline_mode == "write_flow"
    assert registry["the_block"].pipeline_mode == "alert_only"


def test_registry_assigns_discovery_modes_for_telegram_primary_sites() -> None:
    registry = get_site_registry()

    assert registry["coindesk"].discovery_mode == DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK
    assert registry["cointelegraph"].discovery_mode == DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK
    assert registry["decrypt"].discovery_mode == DISCOVERY_MODE_TELEGRAM_PRIMARY_DIRECT_FALLBACK
    assert registry["the_block"].discovery_mode == DISCOVERY_MODE_DIRECT
    assert registry["fortune_crypto"].discovery_mode == DISCOVERY_MODE_DIRECT


def test_discover_coindesk_pages_reads_rss_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Bitcoin ETFs add to inflow streak</title>
          <link>https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak</link>
          <description><![CDATA[Bitcoin ETFs add to inflow streak - Spot funds logged a fifth straight day of net inflows.]]></description>
        </item>
        <item>
          <title>Duplicate title</title>
          <link>https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak?utm_source=rss</link>
        </item>
        <item>
          <title>Ignore TV</title>
          <link>https://www.coindesk.com/tv/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_coindesk_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak/"
    ]
    assert pages[0].excerpt == "Spot funds logged a fifth straight day of net inflows."


def test_parse_coindesk_article_extracts_structured_fields_and_body() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak" />
        <meta property="og:description" content="Spot Bitcoin ETFs extended their inflow streak for a fifth day." />
        <script type="application/ld+json">
          {
            "@context": "http://schema.org",
            "@type": "NewsArticle",
            "headline": "Bitcoin ETFs add to inflow streak",
            "url": "https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak",
            "datePublished": "2026-05-24T10:00:00Z",
            "articleSection": ["Markets"],
            "author": [{"@type": "Person", "name": "Margaux Nijkerk"}],
            "keywords": "Bitcoin, ETF"
          }
        </script>
      </head>
      <body>
        <h1>Bitcoin ETFs add to inflow streak</h1>
        <div class="document-body">
          <p>Spot Bitcoin ETFs logged a fifth straight day of inflows.</p>
          <p>BlackRock's IBIT led the move.</p>
        </div>
      </body>
    </html>
    """

    article = parse_coindesk_article(
        html,
        page_url="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak",
        source_item_id="https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak/",
    )

    assert article.canonical_url == "https://www.coindesk.com/markets/2026/05/24/bitcoin-etfs-add-to-inflow-streak/"
    assert article.title == "Bitcoin ETFs add to inflow streak"
    assert article.author_names == ["Margaux Nijkerk"]
    assert article.categories == ["Markets"]
    assert article.tags == ["Bitcoin", "ETF"]
    assert article.content == "Spot Bitcoin ETFs logged a fifth straight day of inflows.\n\nBlackRock's IBIT led the move."
    assert article.content_format == "coindesk_article"


def test_discover_cointelegraph_pages_reads_rss_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Crypto Today</title>
          <link>https://cointelegraph.com/news/crypto-today?utm_source=rss_feed</link>
          <description><![CDATA[Crypto Today - A roundup of the latest crypto headlines.]]></description>
          <pubDate>Sat, 24 May 2026 11:00:00 +0000</pubDate>
        </item>
        <item>
          <title>Ignore markets category</title>
          <link>https://cointelegraph.com/magazine/feature-story</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_cointelegraph_pages(xml)

    assert [page.detail_url for page in pages] == ["https://cointelegraph.com/news/crypto-today/"]
    assert pages[0].excerpt == "A roundup of the latest crypto headlines."
    assert pages[0].published_at == datetime(2026, 5, 24, 11, 0, tzinfo=UTC)


def test_parse_cointelegraph_article_extracts_body_and_byline() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://cointelegraph.com/news/crypto-today" />
        <meta property="og:title" content="Crypto Today" />
        <meta property="og:description" content="A roundup of the latest crypto headlines." />
        <meta property="article:published_time" content="2026-05-24T11:00:00Z" />
      </head>
      <body>
        <main>
          <div data-testid="post">
            <div data-testid="post-byline">Written by Cointelegraph, Staff Writer. Reviewed by Bryan O'Shea, Staff Editor.</div>
            <div class="ct-prose-2 mt-4 pb-10">
              <p>Need to know what happened in crypto today?</p>
              <p>Bitcoin ETFs kept their inflow streak alive.</p>
              <p>Solana fees also rebounded.</p>
            </div>
          </div>
        </main>
      </body>
    </html>
    """

    article = parse_cointelegraph_article(
        html,
        page_url="https://cointelegraph.com/news/crypto-today",
        source_item_id="https://cointelegraph.com/news/crypto-today/",
    )

    assert article.canonical_url == "https://cointelegraph.com/news/crypto-today/"
    assert article.title == "Crypto Today"
    assert article.author_names == ["Cointelegraph", "Bryan O'Shea"]
    assert article.content == (
        "Need to know what happened in crypto today?\n\n"
        "Bitcoin ETFs kept their inflow streak alive.\n\n"
        "Solana fees also rebounded."
    )
    assert article.content_format == "cointelegraph_news"


def test_discover_cointelegraph_pages_reads_google_news_sitemap_publication_time() -> None:
    xml = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
      <url>
        <loc>https://cointelegraph.com/news/kalshi-launches-advocacy-group-to-counter-anti-prediction-market-lobbying</loc>
        <news:news>
          <news:publication>
            <news:name>Cointelegraph</news:name>
            <news:language>en</news:language>
          </news:publication>
          <news:publication_date>2026-05-25T04:07:32.071Z</news:publication_date>
          <news:title>Kalshi backs prediction markets lobby group with former Trump official</news:title>
        </news:news>
      </url>
      <url>
        <loc>https://cointelegraph.com/magazine/feature-story</loc>
        <news:news>
          <news:publication_date>2026-05-25T04:08:00.000Z</news:publication_date>
          <news:title>Ignore non news path</news:title>
        </news:news>
      </url>
    </urlset>
    """

    pages = discover_cointelegraph_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://cointelegraph.com/news/kalshi-launches-advocacy-group-to-counter-anti-prediction-market-lobbying/"
    ]
    assert pages[0].title == "Kalshi backs prediction markets lobby group with former Trump official"
    assert pages[0].published_at == datetime(2026, 5, 25, 4, 7, 32, 71000, tzinfo=UTC)
    assert pages[0].published_at_raw == "2026-05-25T04:07:32.071Z"


def test_fetch_discovered_pages_cointelegraph_falls_back_to_rss(monkeypatch) -> None:
    site = get_site_registry()["cointelegraph"]
    calls: list[str] = []
    rss = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Crypto Today</title>
          <link>https://cointelegraph.com/news/crypto-today?utm_source=rss_feed</link>
          <description><![CDATA[Crypto Today - A roundup of the latest crypto headlines.]]></description>
          <pubDate>Sat, 24 May 2026 11:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>
    """

    def fake_fetch_html(url: str, **_: object) -> str:
        calls.append(url)
        if url == site.list_url:
            raise RuntimeError("primary sitemap unavailable")
        if url == "https://cointelegraph.com/rss":
            return rss
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert calls == [site.list_url, "https://cointelegraph.com/rss"]
    assert [page.detail_url for page in pages] == ["https://cointelegraph.com/news/crypto-today/"]
    assert pages[0].published_at == datetime(2026, 5, 24, 11, 0, tzinfo=UTC)


def test_fetch_article_uses_discovery_published_at_when_cointelegraph_html_has_no_time(monkeypatch) -> None:
    page = DiscoveredPage(
        source_item_id="https://cointelegraph.com/news/crypto-today/",
        detail_url="https://cointelegraph.com/news/crypto-today/",
        title="Crypto Today",
        published_at=datetime(2026, 5, 24, 11, 0, tzinfo=UTC),
        published_at_raw="2026-05-24T11:00:00Z",
    )
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://cointelegraph.com/news/crypto-today" />
        <meta property="og:title" content="Crypto Today" />
        <meta property="og:description" content="A roundup of the latest crypto headlines." />
      </head>
      <body>
        <main>
          <div data-testid="post">
            <div data-testid="post-byline">Written by Cointelegraph, Staff Writer.</div>
            <div class="ct-prose-2 mt-4 pb-10">
              <p>Need to know what happened in crypto today?</p>
              <p>Bitcoin ETFs kept their inflow streak alive.</p>
            </div>
          </div>
        </main>
      </body>
    </html>
    """

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", lambda *_args, **_kwargs: html)

    parsed = fetch_article(
        get_site_registry()["cointelegraph"],
        page,
        timeout_seconds=20,
        max_attempts=1,
        backoff_seconds=0,
    )

    assert parsed.published_at == datetime(2026, 5, 24, 11, 0, tzinfo=UTC)
    assert parsed.metadata["published_at_raw"] == "2026-05-24T11:00:00Z"


def test_discover_decrypt_pages_reads_rss_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Bitcoin Dives Below $75K</title>
          <link>https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion?utm_source=feed</link>
          <description><![CDATA[Bitcoin Dives Below $75K - Liquidations neared $1 billion during the slide.]]></description>
        </item>
        <item>
          <title>Ignore non-article link</title>
          <link>https://decrypt.co/news</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_decrypt_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion/"
    ]
    assert pages[0].excerpt == "Liquidations neared $1 billion during the slide."


def test_parse_decrypt_article_extracts_body_and_author() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion" />
        <meta name="author" content="Decrypt / Andrew Hayward" />
        <meta property="og:description" content="Bitcoin touched its lowest price in a month overnight." />
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "NewsArticle",
            "headline": "Bitcoin Dives Below $75K",
            "url": "https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion",
            "datePublished": "2026-05-24T12:00:00Z",
            "articleSection": ["Markets"],
            "keywords": "Bitcoin, Liquidations"
          }
        </script>
      </head>
      <body>
        <main>
          <div class="grid grid-cols-1 md:grid-cols-8 unreset post-content md:pb-20">
            <p>Bitcoin is starting to rebound after a rocky night.</p>
            <p>The coin is currently trading around $75,500.</p>
            <p>Daily Debrief Newsletter</p>
          </div>
        </main>
      </body>
    </html>
    """

    article = parse_decrypt_article(
        html,
        page_url="https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion",
        source_item_id="https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion/",
    )

    assert article.canonical_url == "https://decrypt.co/368912/bitcoin-drops-75k-crypto-liquidations-near-1-billion/"
    assert article.title == "Bitcoin Dives Below $75K"
    assert article.author_names == ["Andrew Hayward"]
    assert article.categories == ["Markets"]
    assert article.tags == ["Bitcoin", "Liquidations"]
    assert article.content == "Bitcoin is starting to rebound after a rocky night.\n\nThe coin is currently trading around $75,500."
    assert article.content_format == "decrypt_news"


def test_discover_the_block_pages_reads_official_rss_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>First The Block Title</title>
          <link>https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc?utm_source=rss&amp;utm_medium=rss</link>
          <description><![CDATA[<p>Lead paragraph from official rss.</p>]]></description>
          <pubDate>Mon, 25 May 2026 04:35:03 -0400</pubDate>
        </item>
      </channel>
    </rss>
    """

    pages = discover_the_block_pages(xml)

    assert [page.detail_url for page in pages] == ["https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc/"]
    assert pages[0].title == "First The Block Title"
    assert pages[0].excerpt == "Lead paragraph from official rss."
    assert pages[0].published_at == datetime(2026, 5, 25, 8, 35, 3, tzinfo=UTC)
    assert pages[0].published_at_raw == "Mon, 25 May 2026 04:35:03 -0400"


def test_discover_the_block_pages_reads_google_news_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>First The Block Title - The Block</title>
          <link>https://news.google.com/rss/articles/CBMiTestStory?oc=5</link>
          <source url="https://www.theblock.co/">The Block</source>
          <description><![CDATA[First The Block Title - Lead paragraph from rss. The Block]]></description>
        </item>
        <item>
          <title>Ignore other source - Bloomberg</title>
          <link>https://news.google.com/rss/articles/CBMiIgnoreOtherSource?oc=5</link>
          <source url="https://www.bloomberg.com/">Bloomberg</source>
        </item>
      </channel>
    </rss>
    """

    pages = discover_the_block_pages(xml)

    assert [page.detail_url for page in pages] == ["https://news.google.com/rss/articles/CBMiTestStory?oc=5"]
    assert pages[0].title == "First The Block Title"
    assert pages[0].excerpt == "Lead paragraph from rss."


def test_fetch_discovered_pages_dispatches_the_block_with_jina_official_rss(monkeypatch) -> None:
    site = get_site_registry()["the_block"]
    markdown = """
Title: The Block

URL Source: https://www.theblock.co/rss.xml

Markdown Content:
# The Block

### [First The Block Title](https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc?utm_source=rss&utm_medium=rss)

[https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc?utm_source=rss&utm_medium=rss](https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc?utm_source=rss&utm_medium=rss)

Mon, 25 May 2026 04:35:03 -0400
""".strip()

    def fake_fetch_html(url: str, **_: object) -> str:
        if url == site.list_url:
            raise RuntimeError("request failed url=https://www.theblock.co/rss.xml: 403")
        assert url == f"https://r.jina.ai/http://{site.list_url}"
        return markdown

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert [page.detail_url for page in pages] == ["https://www.theblock.co/post/402447/bitcoin-og-moves-2650-btc/"]
    assert pages[0].published_at == datetime(2026, 5, 25, 8, 35, 3, tzinfo=UTC)


def test_fetch_discovered_pages_dispatches_the_block_to_google_news_fallback(monkeypatch) -> None:
    site = get_site_registry()["the_block"]
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>First The Block Title - The Block</title>
          <link>https://news.google.com/rss/articles/CBMiDispatchStory?oc=5</link>
          <source url="https://www.theblock.co/">The Block</source>
          <description><![CDATA[First The Block Title - Lead paragraph from rss. The Block]]></description>
          <pubDate>Mon, 25 May 2026 08:35:03 GMT</pubDate>
        </item>
      </channel>
    </rss>
    """

    def fake_fetch_html(url: str, **_: object) -> str:
        if url == site.list_url:
            raise RuntimeError("request failed url=https://www.theblock.co/rss.xml: 403")
        if url == f"https://r.jina.ai/http://{site.list_url}":
            raise RuntimeError("jina official rss unavailable")
        assert url == "https://news.google.com/rss/search?q=site:theblock.co&hl=en-US&gl=US&ceid=US:en"
        return xml

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert [page.detail_url for page in pages] == ["https://news.google.com/rss/articles/CBMiDispatchStory?oc=5"]
    assert pages[0].published_at == datetime(2026, 5, 25, 8, 35, 3, tzinfo=UTC)


def test_discover_forbes_pages_reads_digital_assets_rss() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Strategy article</title>
          <link>https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/</link>
        </item>
        <item>
          <title>Duplicate</title>
          <link>https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/?utm_source=test</link>
        </item>
        <item>
          <title>Other section</title>
          <link>https://www.forbes.com/sites/other/2026/05/16/not-included/</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_forbes_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/"
    ]
    assert [page.title for page in pages] == ["Strategy article"]


def test_discover_hk01_pages_reads_issue_blocks() -> None:
    payload = {
        "props": {
            "initialProps": {
                "pageProps": {
                    "issue": {
                        "blocks": [
                            {
                                "articles": [
                                    {
                                        "data": {
                                            "publishUrl": "/topic/60347049/sample-story",
                                            "canonicalUrl": "/topic/60347049/sample-story",
                                            "title": "HK01 sample story",
                                        }
                                    },
                                    {
                                        "data": {
                                            "publishUrl": "/topic/60347049/sample-story?utm_source=test",
                                            "canonicalUrl": "/topic/60347049/sample-story",
                                            "title": "HK01 duplicate story",
                                        }
                                    },
                                ]
                            }
                        ]
                    }
                }
            }
        }
    }
    html = (
        '<html><head><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script></head></html>"
    )

    pages = discover_hk01_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://www.hk01.com/topic/60347049/sample-story/"
    ]
    assert [page.title for page in pages] == ["HK01 sample story"]


def test_discover_thelec_pages_reads_list_titles_and_keeps_idxno() -> None:
    html = """
    <html>
      <body>
        <div class="list-container">
          <div class="article-list-item">
            <span class="category">CHINA</span>
            <a href="/news/articleView.html?idxno=10961">Nvidia to source HBM3E from Samsung by end-June, likely for China market</a>
            <p class="lead">Samsung's HBM3E products are likely headed to China-bound accelerators.</p>
            <span class="writer">Kim Min-su</span>
            <span class="date">2024.05.17 14:03</span>
          </div>
          <div class="article-list-item">
            <span class="category">CHINA</span>
            <a href="https://www.thelec.net/news/articleView.html?idxno=10961&utm_source=test">Duplicate story</a>
            <p class="lead">Duplicate excerpt should not create another item.</p>
            <span class="writer">Kim Min-su</span>
            <span class="date">2024.05.17 14:03</span>
          </div>
          <div class="article-list-item">
            <span class="category">CHINA</span>
            <a href="/news/articleView.html?idxno=10962">China smartphone OLED shipments rebound in April</a>
            <p class="lead">Shipment growth returned as domestic demand stabilized.</p>
            <span class="writer">Park Jae-hyuk</span>
            <span class="date">2024.05.18 09:10</span>
          </div>
        </div>
      </body>
    </html>
    """

    pages = discover_thelec_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://www.thelec.net/news/articleView.html?idxno=10961",
        "https://www.thelec.net/news/articleView.html?idxno=10962",
    ]
    assert pages[0].title == "Nvidia to source HBM3E from Samsung by end-June, likely for China market"
    assert pages[0].excerpt == "Samsung's HBM3E products are likely headed to China-bound accelerators."
    assert pages[0].published_at == datetime(2024, 5, 17, 5, 3, tzinfo=UTC)
    assert pages[0].published_at_raw == "2024.05.17 14:03"


def test_discover_thelec_pages_reads_jina_markdown_links() -> None:
    payload = """
    Title: Semiconductor - The Elec Inc.

    URL Source: https://www.thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm

    Markdown Content:
    * [![Image 1: Intel Says x86 to Power 80% of Data Centers by 2030 Amid Agentic AI Shift](https://cdn.thelec.net/news/thumbnail/202606/10998_10986_2935_v150.jpg)](https://www.thelec.net/news/articleView.html?idxno=10998)## [Intel Says x86 to Power 80% of Data Centers by 2030 Amid Agentic AI Shift](https://www.thelec.net/news/articleView.html?idxno=10998)

    Intel expressed confidence that x86 architecture-based central processing units will regain leadership.

    * [![Image 2: Samsung Quietly Unveils HBM5 Mock-Up at Computex 2026](https://cdn.thelec.net/news/thumbnail/202606/10996_10984_2235_v150.jpg)](https://www.thelec.net/news/articleView.html?idxno=10996)## [Samsung Quietly Unveils HBM5 Mock-Up at Computex 2026](https://www.thelec.net/news/articleView.html?idxno=10996)

    Samsung Electronics quietly displayed an HBM5 mock-up at Computex.
    """

    pages = discover_thelec_pages(payload)

    assert [page.detail_url for page in pages] == [
        "https://www.thelec.net/news/articleView.html?idxno=10998",
        "https://www.thelec.net/news/articleView.html?idxno=10996",
    ]
    assert pages[0].title == "Intel Says x86 to Power 80% of Data Centers by 2030 Amid Agentic AI Shift"
    assert pages[0].excerpt == (
        "Intel expressed confidence that x86 architecture-based central processing units will regain leadership."
    )


def test_parse_thelec_article_extracts_title_author_time_and_body() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.thelec.net/news/articleView.html?idxno=10961" />
        <meta property="og:title" content="Nvidia to source HBM3E from Samsung by end-June, likely for China market" />
        <meta property="og:description" content="Samsung's HBM3E output is expected to support Nvidia accelerators for China." />
      </head>
      <body>
        <article>
          <div class="article-head-title-list"><a href="/news/articleList.html?sc_section_code=S1N2">CHINA</a></div>
          <h1>Nvidia to source HBM3E from Samsung by end-June, likely for China market</h1>
          <div class="article-sub-title">Samsung's HBM3E output is expected to support Nvidia accelerators for China.</div>
          <div class="article-meta">
            <span class="name">Kim Min-su</span>
            <span class="time">승인 2024.05.17 14:03</span>
          </div>
          <div id="article-view-content-div">
            <p>Nvidia is expected to qualify Samsung Electronics' HBM3E by the end of June.</p>
            <p>The chips are likely to be used in AI accelerators bound for the China market, TheElec has learned.</p>
            <p>Sources said volume shipments could begin in the second half of the year.</p>
          </div>
        </article>
      </body>
    </html>
    """

    article = parse_thelec_article(
        html,
        page_url="https://www.thelec.net/news/articleView.html?idxno=10961",
        source_item_id="https://www.thelec.net/news/articleView.html?idxno=10961",
    )

    assert article.canonical_url == "https://www.thelec.net/news/articleView.html?idxno=10961"
    assert article.title == "Nvidia to source HBM3E from Samsung by end-June, likely for China market"
    assert article.author_names == ["Kim Min-su"]
    assert article.categories == ["CHINA"]
    assert article.published_at == datetime(2024, 5, 17, 5, 3, tzinfo=UTC)
    assert article.content == (
        "Nvidia is expected to qualify Samsung Electronics' HBM3E by the end of June.\n\n"
        "The chips are likely to be used in AI accelerators bound for the China market, TheElec has learned.\n\n"
        "Sources said volume shipments could begin in the second half of the year."
    )
    assert article.excerpt == "Samsung's HBM3E output is expected to support Nvidia accelerators for China."
    assert article.content_format == "thelec_article"


def test_fetch_discovered_pages_thelec_uses_host_fallback(monkeypatch) -> None:
    site = get_site_registry()["thelec_china"]
    html = """
    <html>
      <body>
        <div class="article-list-item">
          <a href="/news/articleView.html?idxno=10961">Sample TheElec story</a>
          <p>Sample excerpt line for TheElec fallback.</p>
          <span>2024.05.17 14:03</span>
        </div>
      </body>
    </html>
    """
    calls: list[str] = []

    def fake_fetch_html(url: str, **_: object) -> str:
        calls.append(url)
        if url == site.list_url:
            raise RuntimeError("primary host timeout")
        if url == "https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm":
            return html
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert calls[:2] == [
        site.list_url,
        "https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm",
    ]
    assert [page.detail_url for page in pages] == ["https://www.thelec.net/news/articleView.html?idxno=10961"]


def test_fetch_discovered_pages_thelec_uses_jina_fallback_when_hosts_block(monkeypatch) -> None:
    site = get_site_registry()["thelec_china"]
    payload = """
    Markdown Content:
    ## [Intel Says x86 to Power 80% of Data Centers by 2030 Amid Agentic AI Shift](https://www.thelec.net/news/articleView.html?idxno=10998)

    Intel expressed confidence that x86 architecture-based central processing units will regain leadership.
    """
    calls: list[str] = []

    def fake_fetch_html(url: str, **_: object) -> str:
        calls.append(url)
        if url in {
            site.list_url,
            "https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm",
        }:
            raise RuntimeError("403 forbidden")
        if url == "https://r.jina.ai/http://https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm":
            return payload
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert calls == [
        site.list_url,
        "https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm",
        "https://r.jina.ai/http://https://thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm",
    ]
    assert [page.detail_url for page in pages] == ["https://www.thelec.net/news/articleView.html?idxno=10998"]


def test_fetch_article_thelec_parses_jina_markdown_detail_fallback(monkeypatch) -> None:
    site = get_site_registry()["thelec_china"]
    page = DiscoveredPage(
        source_item_id="https://www.thelec.net/news/articleView.html?idxno=11043",
        detail_url="https://www.thelec.net/news/articleView.html?idxno=11043",
        title="Chey Tae-won Meets Young Liu in Taiwan to Discuss AI Infrastructure Cooperation",
    )
    payload = """
    Title: Chey Tae-won Meets Young Liu in Taiwan to Discuss AI Infrastructure Cooperation

    URL Source: https://thelec.net/news/articleView.html?idxno=11043

    Published Time: 2026-06-05T07:33:24+09:00

    Markdown Content:
    # Chey Tae-won Meets Young Liu in Taiwan to Discuss AI Infrastructure Cooperation < Semiconductor < 기사본문 - The Elec Inc.

    [Semiconductor](https://www.thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm)

    # Chey Tae-won Meets Young Liu in Taiwan to Discuss AI Infrastructure Cooperation

    ## Third major AI partnership meeting following Nvidia and TSMC

    *    JEONG IL JOO
    *   Published 2026.06.05 07:33

    ![Image 1: SK Group Chairman Chey Tae-won meets with Foxconn executives in Taiwan to discuss cooperation.](https://cdn.thelec.net/news/photo/202606/11043_11033_241.jpg)

    SK Group Chairman Chey Tae-won meets with Foxconn executives in Taiwan to discuss cooperation.

    Chey Tae-won met with Young Liu and Foxconn executives in Taiwan to discuss cooperation in next-generation artificial intelligence (AI) infrastructure.

    SK hynix said the meeting took place in Taipei on June 3. The two sides discussed ways to strengthen competitiveness in next-generation AI infrastructure.

    [JEONG IL JOO](https://www.thelec.net/news/articleList.html?sc_area=I&sc_word=1week&view_type=sm)
    [View other news](https://www.thelec.net/news/articleList.html?sc_area=I&sc_word=1week&view_type=sm)
    """
    calls: list[str] = []

    def fake_fetch_html(url: str, **_: object) -> str:
        calls.append(url)
        if url in {
            page.detail_url,
            "https://thelec.net/news/articleView.html?idxno=11043",
        }:
            raise RuntimeError("403 forbidden")
        if url == "https://r.jina.ai/http://https://thelec.net/news/articleView.html?idxno=11043":
            return payload
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    article = fetch_article(site, page, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert calls == [
        page.detail_url,
        "https://thelec.net/news/articleView.html?idxno=11043",
        "https://r.jina.ai/http://https://thelec.net/news/articleView.html?idxno=11043",
    ]
    assert article.title == "Chey Tae-won Meets Young Liu in Taiwan to Discuss AI Infrastructure Cooperation"
    assert article.author_names == ["JEONG IL JOO"]
    assert article.categories == ["Semiconductor"]
    assert article.published_at == datetime(2026, 6, 4, 22, 33, 24, tzinfo=UTC)
    assert article.content == (
        "Chey Tae-won met with Young Liu and Foxconn executives in Taiwan to discuss cooperation in next-generation "
        "artificial intelligence (AI) infrastructure.\n\n"
        "SK hynix said the meeting took place in Taipei on June 3. The two sides discussed ways to strengthen "
        "competitiveness in next-generation AI infrastructure."
    )
    assert article.metadata["proxy_fallback"] == "jina_markdown"


def test_thelec_registered_as_ai_source_with_five_minute_interval() -> None:
    site = get_site_registry()["thelec_china"]

    assert site.source_group == SOURCE_GROUP_AI_SOURCE
    assert site.pipeline_mode == "write_flow"
    assert site.interval_seconds == 300


def test_etnews_sections_registered_as_ai_sources_with_five_minute_interval() -> None:
    registry = get_site_registry()

    assert registry["etnews_electronics"].source_group == SOURCE_GROUP_AI_SOURCE
    assert registry["etnews_electronics"].pipeline_mode == "write_flow"
    assert registry["etnews_electronics"].interval_seconds == 300
    assert registry["etnews_electronics"].list_url == "https://www.etnews.com/news/section.html?id1=06"

    assert registry["etnews_sw"].source_group == SOURCE_GROUP_AI_SOURCE
    assert registry["etnews_sw"].pipeline_mode == "write_flow"
    assert registry["etnews_sw"].interval_seconds == 300
    assert registry["etnews_sw"].list_url == "https://www.etnews.com/news/section.html?id1=04"


def test_zdnet_korea_semiconductor_registered_as_ai_source_with_five_minute_interval() -> None:
    site = get_site_registry()["zdnet_korea_semiconductor"]

    assert site.source_group == SOURCE_GROUP_AI_SOURCE
    assert site.pipeline_mode == "write_flow"
    assert site.interval_seconds == 300
    assert site.list_url == "https://zdnet.co.kr/newskey/?lstcode=%EB%B0%98%EB%8F%84%EC%B2%B4"


def test_ctee_semiconductor_registered_as_ai_source_with_five_minute_interval() -> None:
    site = get_site_registry()["ctee_semiconductor"]

    assert site.source_group == SOURCE_GROUP_AI_SOURCE
    assert site.pipeline_mode == "write_flow"
    assert site.interval_seconds == 300
    assert site.list_url == "https://www.ctee.com.tw/industry/semi"


def build_telegram_settings() -> TelegramDiscoverySettings:
    return TelegramDiscoverySettings(
        api_id=1,
        api_hash="hash",
        session_path="telegram-discovery.session",
        channel="https://t.me/PhoenixNewsEN",
        proxy="none",
        poll_interval_seconds=30,
        retry_delay_seconds=120,
        retry_max_attempts=3,
        timeout_seconds=20.0,
        connection_retries=3,
    )


def build_telegram_worker(
    repository: InMemoryNonMainstreamMediaRepository,
    *,
    fetch_article_fn,
    clock=None,
) -> TelegramDiscoveryWorker:
    repository.sync_sources(list(get_site_registry().values()))
    return TelegramDiscoveryWorker(
        repository=repository,
        settings=build_telegram_settings(),
        fetch_article_fn=fetch_article_fn,
        pipeline_client=None,
        clock=clock,
    )


def test_extract_candidate_url_from_plain_text_message() -> None:
    message = SimpleNamespace(
        message=(
            "CoinDesk: Bitcoin rallies again "
            "https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again"
        ),
        entities=[],
        media=None,
    )

    assert (
        extract_candidate_url(message, "coindesk")
        == "https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again"
    )


def test_extract_candidate_url_from_hidden_entity_link() -> None:
    entity = type("MessageEntityTextUrl", (), {"url": "https://decrypt.co/123/test"})()
    message = SimpleNamespace(
        message="Decrypt: hidden link",
        entities=[entity],
        media=None,
    )

    assert extract_candidate_url(message, "decrypt") == "https://decrypt.co/123/test"


def test_telegram_worker_success_marks_seen_and_saves_task() -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    article = ParsedArticle(
        source_item_id="https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again/",
        canonical_url="https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again/",
        title="Bitcoin rallies again",
        content="Body text",
        published_at=datetime(2026, 6, 30, 7, 0, tzinfo=UTC),
        author_names=["Author"],
    )

    def fake_fetch_article(site, page, **_kwargs):
        assert site.site_key == "coindesk"
        assert page.detail_url == "https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again"
        return article

    worker = build_telegram_worker(repository, fetch_article_fn=fake_fetch_article)
    source = next(source for source in repository.list_sources() if source.site_key == "coindesk")
    message = SimpleNamespace(
        id=1001,
        message=(
            "CoinDesk: Bitcoin rallies again "
            "https://www.coindesk.com/markets/2026/06/30/bitcoin-rallies-again"
        ),
        entities=[],
        media=None,
    )

    event = worker.handle_message(message, sources={"coindesk": source})

    assert event["status"] == "success"
    assert len(repository.tasks) == 1
    assert repository.tasks[0]["source_item_id"] == article.canonical_url
    assert ("coindesk", article.canonical_url) in repository.seen


def test_telegram_worker_failure_does_not_mark_seen() -> None:
    repository = InMemoryNonMainstreamMediaRepository()

    def fake_fetch_article(_site, _page, **_kwargs):
        raise RuntimeError("parse failed irrecoverably")

    worker = build_telegram_worker(repository, fetch_article_fn=fake_fetch_article)
    source = next(source for source in repository.list_sources() if source.site_key == "coindesk")
    message = SimpleNamespace(
        id=1002,
        message=(
            "CoinDesk: bad message "
            "https://www.coindesk.com/markets/2026/06/30/bad-message"
        ),
        entities=[],
        media=None,
    )

    event = worker.handle_message(message, sources={"coindesk": source})

    assert event["status"] == "failed"
    assert repository.tasks == []
    assert repository.seen == {}


def test_telegram_worker_404_retries_and_only_saves_once() -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    clock_state = {"now": 0.0}
    attempts = {"count": 0}
    article = ParsedArticle(
        source_item_id="https://cointelegraph.com/news/retry-story/",
        canonical_url="https://cointelegraph.com/news/retry-story/",
        title="Retry story",
        content="Recovered body",
        published_at=datetime(2026, 6, 30, 8, 0, tzinfo=UTC),
    )

    def fake_fetch_article(_site, _page, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError(
                "request failed url=https://cointelegraph.com/news/retry-story: 404 Client Error: Not Found"
            )
        return article

    worker = build_telegram_worker(
        repository,
        fetch_article_fn=fake_fetch_article,
        clock=lambda: clock_state["now"],
    )
    source = next(source for source in repository.list_sources() if source.site_key == "cointelegraph")
    message = SimpleNamespace(
        id=1003,
        message="Cointelegraph: retry story https://cointelegraph.com/news/retry-story",
        entities=[],
        media=None,
    )

    first = worker.handle_message(message, sources={"cointelegraph": source})
    assert first["status"] == "retry_scheduled"
    assert repository.tasks == []
    assert repository.seen == {}

    clock_state["now"] = 119.0
    assert worker.process_retry_tasks() == []

    clock_state["now"] = 120.0
    retry_events = worker.process_retry_tasks()

    assert retry_events == [
        {
            "status": "retry_success",
            "site_key": "cointelegraph",
            "url": "https://cointelegraph.com/news/retry-story",
            "message_id": 1003,
            "retry_attempt": 1,
        }
    ]
    assert attempts["count"] == 2
    assert len(repository.tasks) == 1
    assert repository.tasks[0]["source_item_id"] == article.canonical_url
    assert ("cointelegraph", article.canonical_url) in repository.seen
    assert worker.process_retry_tasks() == []


def test_telegram_worker_skips_non_target_message() -> None:
    repository = InMemoryNonMainstreamMediaRepository()

    def fake_fetch_article(_site, _page, **_kwargs):
        raise AssertionError("should not fetch")

    worker = build_telegram_worker(repository, fetch_article_fn=fake_fetch_article)
    message = SimpleNamespace(
        id=1004,
        message="BINANCE: Update on leverage tiers",
        entities=[],
        media=None,
    )

    assert worker.handle_message(message)["status"] == "skip"


def test_discover_etnews_pages_reads_section_article_ids() -> None:
    html = """
    <html>
      <body>
        <ul>
          <li>
            <a href="/20260603000046">BOE OLED production ceremony</a>
            <p>Display panel update</p>
          </li>
          <li>
            <a href="https://www.etnews.com/20260602000291?utm_source=test"></a>
            <strong>AI legaltech survey</strong>
            <span>2026-06-03 12:00</span>
          </li>
          <li><a href="/news/section.html?id1=04">Ignore section</a></li>
          <li><a href="/20260603000046">Duplicate story</a></li>
        </ul>
      </body>
    </html>
    """

    pages = discover_etnews_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://www.etnews.com/20260603000046",
        "https://www.etnews.com/20260602000291",
    ]
    assert pages[0].title == "BOE OLED production ceremony"
    assert pages[1].title == "AI legaltech survey"
    assert pages[1].published_at == datetime(2026, 6, 3, 3, 0, tzinfo=UTC)


def test_parse_etnews_article_extracts_title_time_author_and_body() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.etnews.com/20260602000291" />
        <meta property="og:title" content="10명 중 6명이 AI로 법률 분야 활용" />
        <meta property="og:description" content="국내 성인 설문조사 결과" />
        <meta property="article:published_time" content="2026-06-03T12:00:00+09:00" />
      </head>
      <body>
        <div class="article_header">
          <span>플랫폼/유통</span>
          <h2 id="article_title_h2">10명 중 6명이 AI로 법률 분야 활용</h2>
          <span>발행일 : 2026-06-03 12:00</span>
        </div>
        <div id="articleBody" class="article_body">
          <figure class="article_image">기사 이해를 돕기 위한 AI 생성 이미지</figure>
          <p>국내 성인 10명 중 6명은 생성형 AI를 법률분야에 활용하고 있다.</p>
          <p>토종 리걸테크는 변호사법 때문에 서비스를 제공하기 어렵다.</p>
        </div>
        <div class="reporter_info">현대인 기자 기사 더보기</div>
      </body>
    </html>
    """

    article = parse_etnews_article(
        html,
        page_url="https://www.etnews.com/20260602000291",
        source_item_id="https://www.etnews.com/20260602000291",
    )

    assert article.canonical_url == "https://www.etnews.com/20260602000291"
    assert article.title == "10명 중 6명이 AI로 법률 분야 활용"
    assert article.published_at == datetime(2026, 6, 3, 3, 0, tzinfo=UTC)
    assert article.author_names == ["현대인"]
    assert article.categories == ["플랫폼/유통"]
    assert article.content == (
        "국내 성인 10명 중 6명은 생성형 AI를 법률분야에 활용하고 있다.\n\n"
        "토종 리걸테크는 변호사법 때문에 서비스를 제공하기 어렵다."
    )
    assert article.excerpt == "국내 성인 설문조사 결과"
    assert article.content_format == "etnews_article"


def test_fetch_discovered_pages_ctee_uses_jina_fallback_when_direct_unavailable(monkeypatch) -> None:
    site = get_site_registry()["ctee_semiconductor"]
    payload = """
    Markdown Content:
    [![Image 1: 魏哲家：未來幾年都很好 買台積股票「請繼續」](https://images.ctee.com.tw/newsphoto/2026-06-05/330/A01AA1_PictureItem_Clipping_01_5.jpg)](https://www.ctee.com.tw/news/20260605700040-430501)
    [產業](https://www.ctee.com.tw/industry)

    ### [魏哲家：未來幾年都很好 買台積股票「請繼續」](https://www.ctee.com.tw/news/20260605700040-430501)

    AI浪潮持續推升全球半導體需求。

    2026.06.05
    """
    calls: list[str] = []

    def fake_fetch_html(url: str, **_: object) -> str:
        calls.append(url)
        if url == site.list_url:
            raise RuntimeError("connect timeout")
        if url == "https://r.jina.ai/http://https://www.ctee.com.tw/industry/semi":
            return payload
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert calls == [
        site.list_url,
        "https://r.jina.ai/http://https://www.ctee.com.tw/industry/semi",
    ]
    assert [page.detail_url for page in pages] == ["https://www.ctee.com.tw/news/20260605700040-430501"]
    assert pages[0].title == "魏哲家：未來幾年都很好 買台積股票「請繼續」"
    assert pages[0].excerpt == "AI浪潮持續推升全球半導體需求。"
    assert pages[0].published_at == datetime(2026, 6, 4, 16, 0, tzinfo=UTC)


def test_discover_ctee_pages_reads_news_cards_and_ignores_sidebar() -> None:
    html = """
    <html>
      <body>
        <div class="content__body">
          <div class="newslist">
            <div class="newslist__card">
              <figure class="picture--thumb">
                <a href="/news/20260605700040-430501"></a>
              </figure>
              <h3 class="news-title">魏哲家：未來幾年都很好 買台積股票「請繼續」</h3>
              <p>AI浪潮持續推升全球半導體需求。</p>
              <span>2026.06.05</span>
            </div>
            <div class="newslist__card">
              <a href="https://www.ctee.com.tw/news/20260605700071-430501?utm_source=test"></a>
              <h3 class="news-title">台積電世界2成股權 持股不再降</h3>
              <p>台積電仍將持有世界先進約2成股權。</p>
              <span>2026.06.05</span>
            </div>
          </div>
        </div>
        <div class="list-box editor-select">
          <ul>
            <li><h4 class="list-title"><a href="/news/20260529700050-430501">黃仁勳兆元宴 台鏈大點兵</a></h4></li>
          </ul>
        </div>
      </body>
    </html>
    """

    pages = discover_ctee_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://www.ctee.com.tw/news/20260605700040-430501",
        "https://www.ctee.com.tw/news/20260605700071-430501",
    ]
    assert pages[0].title == "魏哲家：未來幾年都很好 買台積股票「請繼續」"
    assert pages[0].excerpt == "AI浪潮持續推升全球半導體需求。"
    assert pages[0].published_at == datetime(2026, 6, 4, 16, 0, tzinfo=UTC)


def test_discover_zdnet_korea_pages_reads_newskey_articles() -> None:
    html = """
    <html>
      <body>
        <div class="newsPost">
          <div class="assetThumb">
            <a href="/view/?no=20260604082845" title=""></a>
          </div>
          <div class="assetText">
            <a href="/view/?no=20260604082845">
              <h3>최태원 SK-웨이저자 TSMC 회장 "차세대 HBM 개발 협력 강화"</h3>
              <p>2년 만에 대만서 회동..."커스텀 HBM 등 AI 메모리 시장 선점 속도"</p>
            </a>
            <p class="byline"><span>2026.06.04 AM 09:44</span><a href="/reporter/?lstcode=jkyoon">장경윤 기자</a></p>
          </div>
        </div>
        <div class="newsPost">
          <div class="assetText">
            <a href="https://zdnet.co.kr/view/?no=20260602110030&utm_source=test"></a>
            <strong>엔비디아가 한국에 손을 내민 이유</strong>
            <p>AI 시대의 핵심 파트너로 다시 마주하게 된 사연.</p>
            <p class="byline"><span>2026.06.02 PM 10:08</span><a href="/reporter/?lstcode=ameet">AMEET</a></p>
          </div>
        </div>
        <a href="/newskey/?lstcode=반도체">ignore section</a>
        <a href="/view/?no=20260604082845">duplicate story</a>
      </body>
    </html>
    """

    pages = discover_zdnet_korea_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://zdnet.co.kr/view?no=20260604082845",
        "https://zdnet.co.kr/view?no=20260602110030",
    ]
    assert pages[0].title == '최태원 SK-웨이저자 TSMC 회장 "차세대 HBM 개발 협력 강화"'
    assert pages[0].excerpt == '2년 만에 대만서 회동..."커스텀 HBM 등 AI 메모리 시장 선점 속도"'
    assert pages[0].published_at == datetime(2026, 6, 4, 0, 44, tzinfo=UTC)
    assert pages[1].title == "엔비디아가 한국에 손을 내민 이유"
    assert pages[1].published_at == datetime(2026, 6, 2, 13, 8, tzinfo=UTC)


def test_parse_zdnet_korea_article_extracts_title_time_author_category_and_body() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://zdnet.co.kr/view/?no=20260604082845" />
        <meta property="og:title" content="최태원 SK-웨이저자 TSMC 회장 &quot;차세대 HBM 개발 협력 강화&quot;" />
        <meta property="og:description" content="최태원 SK그룹 회장과 웨이저자 TSMC 회장이 만났다." />
        <meta property="dd:author" content="장경윤 기자" />
        <meta property="article:section" content="반도체ㆍ디스플레이" />
        <meta property="article:published_time" content="2026-06-04T09:44:14+09:00" />
      </head>
      <body>
        <div class="news_head">
          <h1>최태원 SK-웨이저자 TSMC 회장 "차세대 HBM 개발 협력 강화"</h1>
          <p class="summary">2년 만에 대만서 회동..."커스텀 HBM 등 AI 메모리 시장 선점 속도"</p>
          <p class="meta"><a href="/news/?lstcode=0050&page=1">반도체ㆍ디스플레이</a><span>입력 :2026/06/04 09:44 수정: 2026/06/04 09:54</span></p>
        </div>
        <div class="reporter_info">
          <strong>장경윤 기자</strong>
        </div>
        <div class="view_cont" id="articleBody" itemprop="articleBody">
          <div id="content-20260604082845" style="font-size: 16px;">
            <div class="view_ad">광고</div>
            <p>최태원 SK그룹 회장과 웨이저자 TSMC 회장이 지난 3일 대만에서 만났다.</p>
            <p>양사는 차세대 고대역폭메모리(HBM) 개발 협력을 강화하기로 했다.</p>
            <h2><span>관련기사</span></h2>
            <div class="news_box connect">
              <ul>
                <li><a href="/view/?no=20260602180849">젠슨 황, SK하이닉스 부스 깜짝 방문</a><span>2026.06.02</span></li>
              </ul>
            </div>
            <p>향후 양사는 고객 맞춤형 AI 메모리 시장 선점에 속도를 낼 계획이다.</p>
            <div class="mt_bn_box">배너</div>
          </div>
        </div>
      </body>
    </html>
    """

    article = parse_zdnet_korea_article(
        html,
        page_url="https://zdnet.co.kr/view/?no=20260604082845",
        source_item_id="https://zdnet.co.kr/view/?no=20260604082845",
    )

    assert article.canonical_url == "https://zdnet.co.kr/view?no=20260604082845"
    assert article.title == '최태원 SK-웨이저자 TSMC 회장 "차세대 HBM 개발 협력 강화"'
    assert article.published_at == datetime(2026, 6, 4, 0, 44, 14, tzinfo=UTC)
    assert article.author_names == ["장경윤"]
    assert article.categories == ["반도체ㆍ디스플레이"]
    assert article.content == (
        "최태원 SK그룹 회장과 웨이저자 TSMC 회장이 지난 3일 대만에서 만났다.\n\n"
        "양사는 차세대 고대역폭메모리(HBM) 개발 협력을 강화하기로 했다.\n\n"
        "향후 양사는 고객 맞춤형 AI 메모리 시장 선점에 속도를 낼 계획이다."
    )
    assert "관련기사" not in article.content
    assert article.excerpt == "최태원 SK그룹 회장과 웨이저자 TSMC 회장이 만났다."
    assert article.content_format == "zdnet_korea_article"


def test_parse_ctee_article_extracts_structured_fields_and_body() -> None:
    structured = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "@id": "https://www.ctee.com.tw/news/20260605700040-439901",
        "headline": "魏哲家：未來幾年都很好 買台積股票「請繼續」",
        "description": "AI浪潮持續推升全球半導體需求。",
        "datePublished": "2026-06-05T03:00:00+0800",
        "author": {"@type": "Person", "name": "張珈睿"},
        "keywords": "台積電股東會,鴻海,華碩,緯創,半導體,台積電",
        "about": [{"@type": "Thing", "name": "台積電股東會"}, {"@type": "Thing", "name": "半導體"}],
        "articleBody": (
            "AI浪潮持續推升全球半導體需求。 "
            "魏哲家指出，台積電過去一年營運表現亮眼。 "
            "台積電將持續與股東分享經營成果。"
        ),
    }
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.ctee.com.tw/news/20260605700040-439901" />
        <meta property="og:title" content="魏哲家：未來幾年都很好 買台積股票「請繼續」" />
        <meta property="og:description" content="AI浪潮持續推升全球半導體需求。" />
        <meta property="article:published_time" content="2026-06-05 03:00:00" />
        <script type="application/ld+json">""" + json.dumps(structured, ensure_ascii=False) + """</script>
      </head>
      <body>
        <div class="content__header">
          <h1 class="main-title">魏哲家：未來幾年都很好 買台積股票「請繼續」</h1>
          <h2 class="sub-title">股東會喊話，AI需求強，今年營收仍將增逾30％</h2>
          <ul class="news-credit">
            <li class="publish-date"><time>2026.06.05</time></li>
            <li class="publish-time"><time>03:00</time></li>
            <li class="publish-author"><span class="organization">工商時報</span><span class="name"><a href="/reporter/60173">張珈睿</a></span></li>
          </ul>
          <div class="list-box taglist">
            <ul>
              <li class="taglist__item"><a href="/search/台積電股東會">台積電股東會</a></li>
              <li class="taglist__item"><a href="/search/半導體">半導體</a></li>
              <li class="taglist__item"><a href="/search/台積電">台積電</a></li>
            </ul>
          </div>
        </div>
        <div class="content__body">
          <article>
            <p>AI浪潮持續推升全球半導體需求。</p>
            <p>魏哲家指出，台積電過去一年營運表現亮眼。</p>
            <p>台積電將持續與股東分享經營成果。</p>
          </article>
        </div>
      </body>
    </html>
    """

    article = parse_ctee_article(
        html,
        page_url="https://www.ctee.com.tw/news/20260605700040-430501",
        source_item_id="https://www.ctee.com.tw/news/20260605700040-430501",
    )

    assert article.canonical_url == "https://www.ctee.com.tw/news/20260605700040-439901"
    assert article.title == "魏哲家：未來幾年都很好 買台積股票「請繼續」"
    assert article.published_at == datetime(2026, 6, 4, 19, 0, tzinfo=UTC)
    assert article.author_names == ["張珈睿"]
    assert article.categories == ["半導體"]
    assert article.tags == ["台積電股東會", "鴻海", "華碩", "緯創", "台積電"]
    assert article.content == (
        "AI浪潮持續推升全球半導體需求。 魏哲家指出，台積電過去一年營運表現亮眼。 "
        "台積電將持續與股東分享經營成果。"
    )
    assert article.excerpt == "AI浪潮持續推升全球半導體需求。"
    assert article.content_format == "ctee_article"


def test_discover_tether_pages_reads_wordpress_posts_payload() -> None:
    payload = [
        {
            "id": 2663,
            "date_gmt": "2026-05-25T07:30:00",
            "link": "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
            "title": {
                "rendered": "Tether and the Government of Georgia to Launch GEL₮, the Official Stablecoin of Georgia"
            },
            "excerpt": {
                "rendered": (
                    "<p>25 May, 2026 &#8211; Tether today announced plans to launch GEL&#x20AE; in Georgia [&hellip;]</p>"
                )
            },
        },
        {
            "id": 2664,
            "date_gmt": "2026-05-25T07:30:00",
            "link": "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/?utm_source=test",
            "title": {"rendered": "Duplicate copy"},
            "excerpt": {"rendered": "<p>Duplicate excerpt</p>"},
        },
    ]

    pages = discover_tether_pages(payload)

    assert [page.detail_url for page in pages] == [
        "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
    ]
    assert pages[0].source_item_id == pages[0].detail_url
    assert pages[0].title == "Tether and the Government of Georgia to Launch GEL₮, the Official Stablecoin of Georgia"
    assert pages[0].excerpt == "25 May, 2026 – Tether today announced plans to launch GEL₮ in Georgia […]"


def test_discover_tether_pages_keeps_published_at_metadata() -> None:
    payload = [
        {
            "id": 2663,
            "date_gmt": "2026-05-25T07:30:00",
            "link": "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
            "title": {"rendered": "Tether Georgia stablecoin launch"},
            "excerpt": {"rendered": "<p>Excerpt</p>"},
        }
    ]

    pages = discover_tether_pages(payload)

    assert pages[0].published_at == datetime(2026, 5, 25, 7, 30, tzinfo=UTC)
    assert pages[0].published_at_raw == "2026-05-25T07:30:00"


def test_fetch_discovered_pages_tether_falls_back_to_jina_payload(monkeypatch) -> None:
    site = get_site_registry()["tether_news"]
    wrapped = """
Title:

URL Source: https://tether.io/wp-json/wp/v2/posts?categories=3&per_page=100&_fields=id,date_gmt,date,link,title,excerpt

Markdown Content:
[{"id":2663,"date_gmt":"2026-05-25T07:30:00","link":"https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/","title":{"rendered":"Tether and the Government of Georgia to Launch GEL of Georgia"},"excerpt":{"rendered":"<p>25 May, 2026 &#8211; Tether today announced plans to launch GEL in Georgia [&hellip;]</p>"}}]
""".strip()

    def fake_fetch_json(url: str, **_: object) -> object:
        assert url == site.list_url
        raise RuntimeError("request failed url=https://tether.io/wp-json/wp/v2/posts: 403")

    def fake_fetch_html(url: str, **_: object) -> str:
        assert (
            url
            == "https://r.jina.ai/http://https://tether.io/wp-json/wp/v2/posts"
            "?categories=3&per_page=100&_fields=id,date_gmt,date,link,title"
        )
        return wrapped

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_json", fake_fetch_json)
    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    pages = fetch_discovered_pages(site, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert [page.detail_url for page in pages] == [
        "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
    ]
    assert pages[0].published_at == datetime(2026, 5, 25, 7, 30, tzinfo=UTC)


def test_fetch_article_tether_falls_back_to_jina_markdown(monkeypatch) -> None:
    site = get_site_registry()["tether_news"]
    page = DiscoveredPage(
        source_item_id="https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
        detail_url="https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
        title="Tether and the Government of Georgia to Launch GEL of Georgia",
        published_at=datetime(2026, 5, 25, 7, 30, tzinfo=UTC),
        published_at_raw="2026-05-25T07:30:00",
    )
    wrapped = """
Title: Tether and the Government of Georgia to Launch GEL of Georgia - Tether.io

URL Source: https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/

Markdown Content:
# Tether and the Government of Georgia to Launch GEL of Georgia - Tether.io

Tether and the Government of Georgia to Launch GEL of Georgia

**25 May, 2026** - [Tether,](https://tether.io/) today announced plans to launch GEL in Georgia.

The stablecoin is designed to support faster payments and cross-border transfers.

Officials said the project will operate under a stablecoin regulatory framework.

[![Image](https://tether.io/wp-content/uploads/return.svg) BACK TO NEWS](https://tether.io/news "News")

## latest news
""".strip()

    def fake_fetch_json(url: str, **_: object) -> object:
        assert "slug=tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia" in url
        raise RuntimeError("request failed url=https://tether.io/wp-json/wp/v2/posts?slug=...: 403")

    def fake_fetch_html(url: str, **_: object) -> str:
        assert url == f"https://r.jina.ai/http://{page.detail_url}"
        return wrapped

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_json", fake_fetch_json)
    monkeypatch.setattr("packages.non_mainstream_media.fetcher.fetch_html", fake_fetch_html)

    article = fetch_article(site, page, timeout_seconds=20, max_attempts=2, backoff_seconds=1)

    assert article.title == "Tether and the Government of Georgia to Launch GEL of Georgia"
    assert article.content == (
        "25 May, 2026 - Tether, today announced plans to launch GEL in Georgia.\n\n"
        "The stablecoin is designed to support faster payments and cross-border transfers.\n\n"
        "Officials said the project will operate under a stablecoin regulatory framework."
    )
    assert article.published_at == datetime(2026, 5, 25, 7, 30, tzinfo=UTC)
    assert article.metadata["proxy_fallback"] == "jina_markdown"


def test_discover_fortune_pages_reads_section_story_titles() -> None:
    html = """
    <html>
      <body>
        <a href="/section/crypto/">Crypto section</a>
        <a href="/2026/05/18/bitcoin-funds-hit-record-inflows/">Bitcoin funds hit record inflows</a>
        <a href="/2026/05/18/bitcoin-funds-hit-record-inflows/?utm_source=test">Duplicate story</a>
        <a href="/2026/05/18/ether-l2-fees-slide/">Ether L2 fees slide</a>
        <a href="/2026/05/19/coach-handbags-comeback-millennials-gen-z/">How Coach became Gen Z’s favorite affordable luxury handbag brand</a>
        <a href="/2026/05/19/us-israel-iran-war-energy-gas-prices-coal-oil/">High gas prices are just the beginning</a>
        <a href="/video/crypto-roundup/">Video roundup</a>
      </body>
    </html>
    """

    pages = discover_fortune_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://fortune.com/2026/05/18/bitcoin-funds-hit-record-inflows/",
        "https://fortune.com/2026/05/18/ether-l2-fees-slide/",
    ]
    assert [page.title for page in pages] == [
        "Bitcoin funds hit record inflows",
        "Ether L2 fees slide",
    ]


def test_discover_ft_pages_reads_google_news_rss_and_decodes_ft_links(monkeypatch) -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Bitcoin ETFs draw in new inflows - Financial Times</title>
          <link>https://news.google.com/rss/articles/AAA?oc=5</link>
          <description><![CDATA[<a href=\"https://news.google.com/rss/articles/AAA?oc=5\">Bitcoin ETFs draw in new inflows</a>&nbsp;&nbsp;<font color=\"#6f6f6f\">Financial Times</font>]]></description>
          <source url="https://www.ft.com">Financial Times</source>
        </item>
        <item>
          <title>Duplicate story - Financial Times</title>
          <link>https://news.google.com/rss/articles/BBB?oc=5</link>
          <description><![CDATA[<a href=\"https://news.google.com/rss/articles/BBB?oc=5\">Duplicate story</a>&nbsp;&nbsp;<font color=\"#6f6f6f\">Financial Times</font>]]></description>
          <source url="https://www.ft.com">Financial Times</source>
        </item>
        <item>
          <title>Ignore other publisher - Other News</title>
          <link>https://news.google.com/rss/articles/CCC?oc=5</link>
          <description><![CDATA[<a href=\"https://news.google.com/rss/articles/CCC?oc=5\">Ignore other publisher</a>&nbsp;&nbsp;<font color=\"#6f6f6f\">Other News</font>]]></description>
          <source url="https://example.com">Other News</source>
        </item>
      </channel>
    </rss>
    """

    decoded = {
        "https://news.google.com/rss/articles/AAA?oc=5": "https://www.ft.com/content/bd1544e9-67ac-4132-b7a0-7d9dab1723c2",
        "https://news.google.com/rss/articles/BBB?oc=5": "https://www.ft.com/content/bd1544e9-67ac-4132-b7a0-7d9dab1723c2?shareType=nongift",
        "https://news.google.com/rss/articles/CCC?oc=5": "https://example.com/content/not-ft",
    }

    def fake_decode_google_news_url(source_url: str, **_: object) -> str | None:
        return decoded[source_url]

    monkeypatch.setattr("packages.non_mainstream_media.fetcher.decode_google_news_url", fake_decode_google_news_url)

    pages = discover_ft_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://www.ft.com/content/bd1544e9-67ac-4132-b7a0-7d9dab1723c2/",
    ]
    assert [page.title for page in pages] == [
        "Bitcoin ETFs draw in new inflows",
    ]


def test_discover_wsj_pages_reads_rss_titles() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Crypto treasuries drive new debt wave</title>
          <link>https://www.wsj.com/articles/crypto-treasuries-drive-new-debt-wave-abc123?mod=rss_markets_main</link>
        </item>
        <item>
          <title>Duplicate</title>
          <link>https://www.wsj.com/articles/crypto-treasuries-drive-new-debt-wave-abc123?mod=duplicate</link>
        </item>
        <item>
          <title>Ignore section link</title>
          <link>https://www.wsj.com/finance</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_wsj_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://www.wsj.com/articles/crypto-treasuries-drive-new-debt-wave-abc123/"
    ]
    assert [page.title for page in pages] == ["Crypto treasuries drive new debt wave"]


def test_discover_wsj_pages_accepts_section_style_links() -> None:
    xml = """
    <rss version="2.0">
      <channel>
        <item>
          <title>Central bank path keeps traders guessing</title>
          <link>https://www.wsj.com/economy/central-banking/central-bank-path-keeps-traders-guessing-87ae9ed5</link>
        </item>
        <item>
          <title>Bank funding costs keep climbing</title>
          <link>https://www.wsj.com/finance/banking/bank-funding-costs-keep-climbing-83478ea2</link>
        </item>
        <item>
          <title>Ignore bare section</title>
          <link>https://www.wsj.com/economy</link>
        </item>
      </channel>
    </rss>
    """

    pages = discover_wsj_pages(xml)

    assert [page.detail_url for page in pages] == [
        "https://www.wsj.com/economy/central-banking/central-bank-path-keeps-traders-guessing-87ae9ed5/",
        "https://www.wsj.com/finance/banking/bank-funding-costs-keep-climbing-83478ea2/",
    ]
    assert [page.title for page in pages] == [
        "Central bank path keeps traders guessing",
        "Bank funding costs keep climbing",
    ]


def test_discover_bloomberg_pages_keeps_article_links_and_titles() -> None:
    html = """
    <html>
      <body>
        <a href="/markets">Markets</a>
        <a href="/news/articles/2026-05-18/bitcoin-rally-extends-on-etf-demand">Bitcoin rally extends on ETF demand</a>
        <a href="/news/articles/2026-05-18/bitcoin-rally-extends-on-etf-demand?srnd=homepage-asia">Duplicate story</a>
        <a href="/opinion/articles/2026-05-18/stablecoins-need-clear-rules">Stablecoins need clear rules</a>
        <a href="/quote/BTCUSD:CUR">BTC</a>
      </body>
    </html>
    """

    pages = discover_bloomberg_pages(html)

    assert [page.detail_url for page in pages] == [
        "https://www.bloomberg.com/news/articles/2026-05-18/bitcoin-rally-extends-on-etf-demand/",
        "https://www.bloomberg.com/opinion/articles/2026-05-18/stablecoins-need-clear-rules/",
    ]
    assert [page.title for page in pages] == [
        "Bitcoin rally extends on ETF demand",
        "Stablecoins need clear rules",
    ]


def test_parse_forbes_article_extracts_body_and_filters_newsletter() -> None:
    payload = {
        "props": {
            "pageProps": {
                "data": {
                    "article": {
                        "title": "Strategy Sale Plan",
                        "date": "2026-05-16T10:10:47.624Z",
                        "description": "Strategy may sell bitcoin to fund repurchases.",
                        "articleId": "abc123",
                        "blogName": "Digital Assets",
                        "authorsList": [{"name": "Billy Bambrough"}],
                        "displayChannel": "Innovation",
                        "displaySection": "Digital Assets",
                    }
                }
            }
        }
    }
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/" />
        <meta property="og:description" content="Fallback description." />
        <script id="__NEXT_DATA__" type="application/json">__PAYLOAD__</script>
      </head>
      <body>
        <article>
          <div class="article-body">
            <p>Bitcoin dropped as Strategy hinted it may sell part of its holdings.</p>
            <p>Sign up now for CryptoCodex - A free crypto newsletter.</p>
            <p>Executives said any sale would support debt repurchases.</p>
          </div>
        </article>
      </body>
    </html>
    """.replace("__PAYLOAD__", json.dumps(payload))

    article = parse_forbes_article(
        html,
        page_url="https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/",
        source_item_id="https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/",
    )

    assert article.canonical_url == "https://www.forbes.com/sites/digital-assets/2026/05/16/strategy-plan/"
    assert article.title == "Strategy Sale Plan"
    assert article.published_at == datetime(2026, 5, 16, 10, 10, 47, 624000, tzinfo=UTC)
    assert article.author_names == ["Billy Bambrough"]
    assert article.categories == ["Innovation", "Digital Assets"]
    assert article.content == (
        "Bitcoin dropped as Strategy hinted it may sell part of its holdings.\n\n"
        "Executives said any sale would support debt repurchases."
    )
    assert article.excerpt == "Strategy may sell bitcoin to fund repurchases."
    assert article.content_format == "forbes_digital_assets"


def test_parse_hk01_article_extracts_text_blocks_and_metadata() -> None:
    payload = {
        "props": {
            "initialProps": {
                "pageProps": {
                    "article": {
                        "articleId": 60347049,
                        "canonicalUrl": "/topic/60347049/sample-story",
                        "publishUrl": "/topic/60347049/sample-story",
                        "title": "HK01 stablecoin explainer",
                        "description": "Hong Kong stablecoin regulation moves closer.",
                        "publishTime": 1778202029,
                        "contentType": "article",
                        "mainCategory": {"name": "Commentary"},
                        "categories": [{"name": "Crypto"}],
                        "authors": [{"name": "Guest Author"}],
                        "tags": [{"name": "Stablecoin"}, {"name": "Hong Kong"}],
                        "blocks": [
                            {"blockType": "summary", "summary": "Ignore summary if html exists"},
                            {
                                "blockType": "article",
                                "htmlTokens": [
                                    [{"type": "h2", "content": "Background"}],
                                    [{"type": "text", "content": "Hong Kong moved its stablecoin framework forward."}],
                                    [{"type": "text", "content": "Issuers will need stronger reserve and compliance controls."}],
                                ],
                            },
                        ],
                    }
                }
            }
        }
    }
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.hk01.com/topic/60347049/sample-story" />
        <script id="__NEXT_DATA__" type="application/json">__PAYLOAD__</script>
      </head>
      <body></body>
    </html>
    """.replace("__PAYLOAD__", json.dumps(payload))

    article = parse_hk01_article(
        html,
        page_url="https://www.hk01.com/topic/60347049/sample-story",
        source_item_id="https://www.hk01.com/topic/60347049/sample-story/",
    )

    assert article.canonical_url == "https://www.hk01.com/topic/60347049/sample-story/"
    assert article.title == "HK01 stablecoin explainer"
    assert article.published_at == datetime(2026, 5, 8, 1, 0, 29, tzinfo=UTC)
    assert article.author_names == ["Guest Author"]
    assert article.tags == ["Stablecoin", "Hong Kong"]
    assert article.categories == ["Commentary", "Crypto"]
    assert article.content == (
        "Background\n\n"
        "Hong Kong moved its stablecoin framework forward.\n\n"
        "Issuers will need stronger reserve and compliance controls."
    )
    assert article.excerpt == "Hong Kong stablecoin regulation moves closer."
    assert article.content_format == "hk01_issue_article"


def test_discover_hankyung_pages_reads_article_links() -> None:
    html = """
    <html>
      <body>
        <section class="premium-list">
          <a href="/article/202606169905i">[단독] 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진</a>
          <a href="/article/202606169905i?utm_source=test">duplicate</a>
          <a href="/premium9/other">ignore</a>
        </section>
      </body>
    </html>
    """

    pages = discover_hankyung_pages(html)

    assert [page.detail_url for page in pages] == ["https://www.hankyung.com/article/202606169905i"]
    assert pages[0].title == "[단독] 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진"


def test_parse_hankyung_article_extracts_body_and_metadata() -> None:
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://www.hankyung.com/article/202606169905i" />
        <meta property="og:title" content="[단독] 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진" />
        <meta property="og:description" content="SK하이닉스가 미국 ADR 상장을 마무리 한 뒤 주주환원책을 추진한다." />
        <script type="application/ld+json">
        {
          "@context": "https://schema.org",
          "@type": "NewsArticle",
          "headline": "[단독] 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진",
          "datePublished": "2026-06-16T17:30:04+09:00",
          "dateModified": "2026-06-16T17:35:00+09:00",
          "author": [
            {"@type": "Person", "name": "김채연"},
            {"@type": "Person", "name": "이선아"}
          ],
          "articleSection": "한경단독",
          "keywords": ["SK하이닉스", "주주환원"]
        }
        </script>
      </head>
      <body>
        <div class="article-body-wrap">
          <div class="article-body" id="articletxt">
            <figure class="article-figure">
              <figcaption>SK하이닉스 본사 모습. 연합뉴스</figcaption>
            </figure>
            SK하이닉스가 미국 주식예탁증서(ADR) 상장을 마무리 한 뒤 올해 4분기 중 최대 100조원 규모의 초대형 주주환원 정책을 추진한다.<br/><br/>
            16일 투자은행(IB) 및 반도체 업계에 따르면 SK하이닉스는 오는 4분기 중 자사주 매입, 현금 배당 등을 포함해 100조원 규모의 주주환원책을 추진 중이다.
          </div>
          <div class="article-license">license text</div>
        </div>
      </body>
    </html>
    """

    article = parse_hankyung_article(
        html,
        page_url="https://www.hankyung.com/article/202606169905i",
        source_item_id="https://www.hankyung.com/article/202606169905i",
    )

    assert article.canonical_url == "https://www.hankyung.com/article/202606169905i"
    assert article.title == "[단독] 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진"
    assert article.author_names == ["김채연", "이선아"]
    assert article.categories == ["한경단독"]
    assert article.tags == ["SK하이닉스", "주주환원"]
    assert "100조원 규모의 초대형 주주환원 정책" in article.content
    assert article.content_format == "hankyung_premium9_article"


def test_parse_hankyung_markdown_article_skips_ui_blocks_and_ai_summary() -> None:
    payload = """
Title: 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진

URL Source: http://www.hankyung.com/article/202606169905i

Published Time: 2026-06-16T17:30:04+09:00

Markdown Content:
# 최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진 | 한국경제

입력 2026.06.16 17:30 수정 2026.06.16 17:35

김채연기자 구독하기

이선아기자 구독하기

**AI 기사요약**

SK하이닉스가 미국 주식예탁증서(ADR) 상장을 마무리한 후 주주가치 제고를 위해 올해 4분기 중 최대 100조 원 규모의 주주환원 정책을 추진한다.

SK하이닉스 본사 모습. 연합뉴스

SK하이닉스가 미국 주식예탁증서(ADR) 상장을 마무리 한 뒤 올해 4분기 중 최대 100조원 규모의 초대형 주주환원 정책을 추진한다.

16일 투자은행(IB) 및 반도체 업계에 따르면 SK하이닉스는 오는 4분기 중 자사주 매입, 현금 배당 등을 포함해 100조원 규모의 주주환원책을 추진 중이다.

한경 프리미엄9의 모든 콘텐츠는 한국경제신문의 저작물로 저작권법의 보호를 받습니다.
"""

    article = parse_hankyung_article(
        payload,
        page_url="https://www.hankyung.com/article/202606169905i",
        source_item_id="https://www.hankyung.com/article/202606169905i",
    )

    assert article.title == "최태원의 승부수…SK하이닉스, 100조 초대형 주주환원 추진"
    assert article.author_names == ["김채연", "이선아"]
    assert "AI 기사요약" not in article.content
    assert "김채연기자 구독하기" not in article.content
    assert article.content.startswith("SK하이닉스가 미국 주식예탁증서(ADR) 상장을 마무리 한 뒤")
    assert "16일 투자은행(IB) 및 반도체 업계에 따르면" in article.content


def test_parse_tether_article_extracts_wp_body_and_terms() -> None:
    payload = [
        {
            "id": 2663,
            "date_gmt": "2026-05-25T07:30:00",
            "link": "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
            "title": {
                "rendered": "Tether and the Government of Georgia to Launch GEL₮, the Official Stablecoin of Georgia"
            },
            "excerpt": {
                "rendered": "<p>25 May, 2026 &#8211; Tether announced plans to launch GEL&#x20AE; in Georgia.</p>"
            },
            "content": {
                "rendered": (
                    "<p><strong>25 May, 2026</strong> &#8211; Tether announced plans to launch GEL&#x20AE; in Georgia."
                    "<br><br>The stablecoin is designed to support faster payments and cross-border transfers.</p>"
                    "<p>Officials said the project will operate under a stablecoin regulatory framework.</p>"
                )
            },
            "_embedded": {
                "wp:term": [
                    [
                        {"taxonomy": "category", "name": "News"},
                        {"taxonomy": "category", "name": "Others"},
                    ],
                    [
                        {"taxonomy": "post_tag", "name": "Stablecoin"},
                        {"taxonomy": "post_tag", "name": "Georgia"},
                    ],
                ]
            },
        }
    ]

    article = parse_tether_article(
        payload,
        page_url="https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
        source_item_id="https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/",
    )

    assert article.canonical_url == (
        "https://tether.io/news/tether-and-the-government-of-georgia-to-launch-gelt-the-official-stablecoin-of-georgia/"
    )
    assert article.title == "Tether and the Government of Georgia to Launch GEL₮, the Official Stablecoin of Georgia"
    assert article.published_at == datetime(2026, 5, 25, 7, 30, tzinfo=UTC)
    assert article.tags == ["Stablecoin", "Georgia"]
    assert article.categories == ["News", "Others"]
    assert article.content == (
        "25 May, 2026 – Tether announced plans to launch GEL₮ in Georgia.\n\n"
        "The stablecoin is designed to support faster payments and cross-border transfers.\n\n"
        "Officials said the project will operate under a stablecoin regulatory framework."
    )
    assert article.excerpt == "25 May, 2026 – Tether announced plans to launch GEL₮ in Georgia."
    assert article.content_format == "tether_news_post"
    assert article.metadata["post_id"] == 2663


def test_clean_body_text_removes_disclaimer_tail() -> None:
    raw = (
        "Intro paragraph.\n\nUseful detail.\n\n"
        "This material is for informational purposes only. It should be removed.\n\n"
        "Related posts"
    )

    assert clean_body_text(raw) == "Intro paragraph.\n\nUseful detail."


def test_worker_seeds_first_run_then_creates_task_for_new_url(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "a16z_crypto_posts": SiteDefinition(
            site_key="a16z_crypto_posts",
            display_name="a16z crypto Posts",
            homepage_url="https://a16zcrypto.com/posts/",
            list_url="https://a16zcrypto.com/posts/",
            capture_method="html_request",
            pipeline_mode="write_flow",
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)

    first_pages = [
        DiscoveredPage(
            source_item_id="https://a16zcrypto.com/posts/article/one/",
            detail_url="https://a16zcrypto.com/posts/article/one/",
        ),
        DiscoveredPage(
            source_item_id="https://a16zcrypto.com/posts/article/two/",
            detail_url="https://a16zcrypto.com/posts/article/two/",
        ),
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://a16zcrypto.com/posts/article/three/",
            detail_url="https://a16zcrypto.com/posts/article/three/",
        )
    ]
    discovered_rounds = [first_pages, second_pages]
    fetched_urls: list[str] = []

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "a16z_crypto_posts"
        return discovered_rounds.pop(0)

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        fetched_urls.append(page.detail_url)
        slug = page.detail_url.rstrip("/").split("/")[-1]
        return ParsedArticle(
            source_item_id=page.detail_url,
            canonical_url=page.detail_url,
            title=f"Title for {slug}",
            content="Cleaned full body text.",
            published_at=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
            author_names=["Alice"],
            tags=["DeFi"],
            categories=["Research"],
            excerpt="Short excerpt",
            content_format="article",
            raw_payload={"page_url": page.detail_url},
            metadata={"structured_type": "Article", "published_at_raw": "2026-05-17T10:00:00Z"},
        )

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    first_stats = worker.run_once()

    assert first_stats[0].seeded_count == 2
    assert first_stats[0].saved_count == 0
    assert repository.tasks == []
    assert repository.sources[1].seeded_at is not None

    second_stats = worker.run_once()

    assert second_stats[0].new_count == 1
    assert second_stats[0].saved_count == 1
    assert fetched_urls == ["https://a16zcrypto.com/posts/article/three/"]
    assert len(repository.tasks) == 1
    task = repository.tasks[0]
    assert task["source"] == "non_mainstream_media"
    assert task["source_item_id"] == "https://a16zcrypto.com/posts/article/three/"
    assert task["source_url"] == "https://a16zcrypto.com/posts/article/three/"
    assert task["content"] == "Cleaned full body text."
    assert task["status"] == "pending"
    assert task["metadata"] == {
        "structured_type": "Article",
        "published_at_raw": "2026-05-17T10:00:00Z",
        "site_key": "a16z_crypto_posts",
        "site_display_name": "a16z crypto Posts",
        "capture_method": "html_request",
        "pipeline_mode": "write_flow",
        "source_group": "external_media",
        "discovery_mode": "direct",
        "source_label": "外媒",
        "content_format": "article",
        "author_names": ["Alice"],
        "tags": ["DeFi"],
        "categories": ["Research"],
        "excerpt": "Short excerpt",
        "canonical_url": "https://a16zcrypto.com/posts/article/three/",
        "source_kind": "non_mainstream_media",
    }
    assert repository.heartbeats[-1]["component"] == "non_mainstream_media"


def test_worker_alert_only_site_saves_title_tasks_without_fetching_details(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "wsj_business": SiteDefinition(
            site_key="wsj_business",
            display_name="WSJ Business",
            homepage_url="https://www.wsj.com/business?mod=nav_top_section",
            list_url="https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",
            capture_method="html_request",
            pipeline_mode="alert_only",
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://www.wsj.com/articles/crypto-one/",
            detail_url="https://www.wsj.com/articles/crypto-one/",
            title="Crypto one",
            excerpt="First seen seed item",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://www.wsj.com/articles/crypto-two/",
            detail_url="https://www.wsj.com/articles/crypto-two/",
            title="Crypto two",
            excerpt="Fresh alert item",
        )
    ]
    discovered_rounds = [first_pages, second_pages]
    fetch_article_calls: list[str] = []

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "wsj_business"
        return discovered_rounds.pop(0)

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        fetch_article_calls.append(page.detail_url)
        raise AssertionError("alert_only flow should not fetch detail pages")

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    first_stats = worker.run_once()

    assert first_stats[0].seeded_count == 1
    assert repository.tasks == []

    second_stats = worker.run_once()

    assert second_stats[0].new_count == 1
    assert second_stats[0].saved_count == 1
    assert fetch_article_calls == []
    assert repository.tasks[0]["source"] == "external_media_alert"
    assert repository.tasks[0]["title"] == "Crypto two"
    assert repository.tasks[0]["content"] == "Fresh alert item"
    assert repository.tasks[0]["metadata"] == {
        "site_key": "wsj_business",
        "site_display_name": "WSJ Business",
        "capture_method": "html_request",
        "pipeline_mode": "alert_only",
        "source_group": "external_media",
        "discovery_mode": "direct",
        "source_label": "外媒",
        "excerpt": "Fresh alert item",
        "published_at_raw": None,
        "source_kind": "external_media_alert",
    }


def test_worker_ai_source_write_flow_saves_ai_source_task(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "thelec_china": SiteDefinition(
            site_key="thelec_china",
            display_name="TheElec CHINA",
            homepage_url="https://www.thelec.net",
            list_url="https://www.thelec.net/news/articleList.html?sc_section_code=S1N2&view_type=sm",
            capture_method="html_request",
            pipeline_mode="write_flow",
            source_group="ai_source",
            interval_seconds=300,
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://www.thelec.net/news/articleView.html?idxno=10961",
            detail_url="https://www.thelec.net/news/articleView.html?idxno=10961",
            title="Seed AI story",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://www.thelec.net/news/articleView.html?idxno=10962",
            detail_url="https://www.thelec.net/news/articleView.html?idxno=10962",
            title="Fresh AI story",
        )
    ]

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "thelec_china"
        return first_pages if repository.sources[1].seeded_at is None else second_pages

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        return ParsedArticle(
            source_item_id=page.source_item_id,
            canonical_url=page.detail_url,
            title=page.title or "Fresh AI story",
            content="AI chip supply chain update.",
            published_at=datetime(2026, 5, 24, 11, 0, tzinfo=UTC),
            author_names=["TheElec"],
            excerpt="AI chip supply chain update.",
            content_format="thelec_article",
            raw_payload={"page_url": page.detail_url},
        )

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    task = repository.tasks[0]
    assert task["source"] == "ai_source"
    assert task["metadata"]["source_group"] == "ai_source"
    assert task["metadata"]["source_label"] == "AI信源"
    assert task["metadata"]["source_kind"] == "ai_source"


def test_worker_ai_source_alert_only_saves_ai_source_alert_task(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "ai_alert_site": SiteDefinition(
            site_key="ai_alert_site",
            display_name="AI Alert Site",
            homepage_url="https://example.com",
            list_url="https://example.com/news",
            capture_method="html_request",
            pipeline_mode="alert_only",
            source_group="ai_source",
            interval_seconds=300,
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://example.com/news/seed",
            detail_url="https://example.com/news/seed",
            title="Seed AI alert",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://example.com/news/fresh",
            detail_url="https://example.com/news/fresh",
            title="Fresh AI alert",
            excerpt="Fresh AI alert excerpt",
        )
    ]

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        return first_pages if repository.sources[1].seeded_at is None else second_pages

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        raise AssertionError("alert_only AI source should not fetch detail pages")

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    task = repository.tasks[0]
    assert task["source"] == "ai_source_alert"
    assert task["metadata"]["source_group"] == "ai_source"
    assert task["metadata"]["source_label"] == "AI信源"
    assert task["metadata"]["source_kind"] == "ai_source_alert"


def test_non_mainstream_media_config_reload_interval_caps_idle_sleep_without_sources(monkeypatch) -> None:
    worker = NonMainstreamMediaWorker(repository=InMemoryNonMainstreamMediaRepository(), site_registry={}, config_reload_interval_seconds=300)
    worker._last_snapshot_loaded_monotonic = 100.0
    monkeypatch.setattr("packages.non_mainstream_media.worker.time.monotonic", lambda: 160.0)

    sleep_seconds = worker._sleep_seconds(worker._snapshot)

    assert sleep_seconds == 60.0


def test_non_mainstream_media_config_reload_interval_due_after_five_minutes() -> None:
    worker = NonMainstreamMediaWorker(repository=InMemoryNonMainstreamMediaRepository(), site_registry={}, config_reload_interval_seconds=300)
    worker._last_snapshot_loaded_monotonic = 100.0

    assert worker._snapshot_reload_due(399.0) is False
    assert worker._snapshot_reload_due(400.0) is True


def test_worker_new_write_flow_site_saves_external_media_article(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "coindesk": SiteDefinition(
            site_key="coindesk",
            display_name="CoinDesk",
            homepage_url="https://www.coindesk.com/",
            list_url="https://www.coindesk.com/arc/outboundfeeds/rss/",
            capture_method="html_request",
            pipeline_mode="write_flow",
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://www.coindesk.com/markets/2026/05/24/seed-story/",
            detail_url="https://www.coindesk.com/markets/2026/05/24/seed-story/",
            title="Seed story",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://www.coindesk.com/markets/2026/05/24/new-story/",
            detail_url="https://www.coindesk.com/markets/2026/05/24/new-story/",
            title="New story",
        )
    ]

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "coindesk"
        return first_pages if repository.sources[1].seeded_at is None else second_pages

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        assert site.site_key == "coindesk"
        return ParsedArticle(
            source_item_id=page.source_item_id,
            canonical_url=page.detail_url,
            title=page.title or "New story",
            content="Spot Bitcoin ETFs extended their inflow streak.",
            published_at=datetime(2026, 5, 24, 10, 0, tzinfo=UTC),
            author_names=["Margaux Nijkerk"],
            tags=["Bitcoin"],
            categories=["Markets"],
            excerpt="Spot Bitcoin ETFs extended their inflow streak.",
            content_format="coindesk_article",
            raw_payload={"page_url": page.detail_url},
            metadata={"structured_type": "NewsArticle", "published_at_raw": "2026-05-24T10:00:00Z"},
        )

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    assert repository.tasks[0]["source"] == "non_mainstream_media"
    assert repository.tasks[0]["metadata"]["site_key"] == "coindesk"
    assert repository.tasks[0]["metadata"]["pipeline_mode"] == "write_flow"


def test_worker_cointelegraph_keeps_discovery_published_at_in_saved_task(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "cointelegraph": SiteDefinition(
            site_key="cointelegraph",
            display_name="Cointelegraph",
            homepage_url="https://cointelegraph.com/",
            list_url="https://cointelegraph.com/sitemap/google-news.xml",
            capture_method="html_request",
            pipeline_mode="write_flow",
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://cointelegraph.com/news/seed-story/",
            detail_url="https://cointelegraph.com/news/seed-story/",
            title="Seed story",
            published_at=datetime(2026, 5, 24, 10, 58, tzinfo=UTC),
            published_at_raw="2026-05-24T10:58:00Z",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://cointelegraph.com/news/new-story/",
            detail_url="https://cointelegraph.com/news/new-story/",
            title="New story",
            published_at=datetime(2026, 5, 24, 11, 0, tzinfo=UTC),
            published_at_raw="2026-05-24T11:00:00Z",
        )
    ]

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "cointelegraph"
        return first_pages if repository.sources[1].seeded_at is None else second_pages

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        assert site.site_key == "cointelegraph"
        return ParsedArticle(
            source_item_id=page.source_item_id,
            canonical_url=page.detail_url,
            title=page.title or "New story",
            content="Prediction markets are ramping up their lobbying push.",
            published_at=page.published_at,
            author_names=["Cointelegraph"],
            excerpt="Prediction markets are ramping up their lobbying push.",
            content_format="cointelegraph_news",
            raw_payload={"page_url": page.detail_url},
            metadata={"published_at_raw": page.published_at_raw},
        )

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    assert repository.tasks[0]["source"] == "non_mainstream_media"
    assert repository.tasks[0]["published_at"] == datetime(2026, 5, 24, 11, 0, tzinfo=UTC)
    assert repository.tasks[0]["metadata"]["published_at_raw"] == "2026-05-24T11:00:00Z"


def test_worker_the_block_alert_only_saves_title_task(monkeypatch) -> None:
    repository = InMemoryNonMainstreamMediaRepository()
    registry = {
        "the_block": SiteDefinition(
            site_key="the_block",
            display_name="The Block",
            homepage_url="https://www.theblock.co/",
            list_url="https://news.google.com/rss/search?q=site:theblock.co&hl=en-US&gl=US&ceid=US:en",
            capture_method="html_request",
            pipeline_mode="alert_only",
        )
    }
    worker = NonMainstreamMediaWorker(repository=repository, site_registry=registry)
    first_pages = [
        DiscoveredPage(
            source_item_id="https://www.theblock.co/post/123/seed-story/",
            detail_url="https://www.theblock.co/post/123/seed-story/",
            title="Seed story",
            excerpt="Seed excerpt",
        )
    ]
    second_pages = first_pages + [
        DiscoveredPage(
            source_item_id="https://www.theblock.co/post/124/new-story/",
            detail_url="https://www.theblock.co/post/124/new-story/",
            title="New story",
            excerpt="Lead paragraph from rss.",
        )
    ]

    def fake_fetch_discovered_pages(site: SiteDefinition, **_: object) -> list[DiscoveredPage]:
        assert site.site_key == "the_block"
        return first_pages if repository.sources[1].seeded_at is None else second_pages

    def fake_fetch_article(site: SiteDefinition, page: DiscoveredPage, **_: object) -> ParsedArticle:
        raise AssertionError("The Block should stay in alert_only mode and never fetch article正文")

    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_discovered_pages", fake_fetch_discovered_pages)
    monkeypatch.setattr("packages.non_mainstream_media.worker.fetch_article", fake_fetch_article)

    worker.run_once()
    stats = worker.run_once()

    assert stats[0].saved_count == 1
    assert repository.tasks[0]["source"] == "external_media_alert"
    assert repository.tasks[0]["metadata"]["site_key"] == "the_block"
    assert repository.tasks[0]["metadata"]["pipeline_mode"] == "alert_only"
