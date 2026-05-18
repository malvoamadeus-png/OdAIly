from __future__ import annotations

import json
from datetime import UTC, datetime

from packages.non_mainstream_media.fetcher import (
    clean_body_text,
    discover_a16z_pages,
    discover_forbes_pages,
    discover_hk01_pages,
    parse_a16z_article,
    parse_forbes_article,
    parse_hk01_article,
)
from packages.non_mainstream_media.models import DiscoveredPage, ParsedArticle, SiteDefinition
from packages.non_mainstream_media.repository import InMemoryNonMainstreamMediaRepository
from packages.non_mainstream_media.worker import NonMainstreamMediaWorker


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
                                        }
                                    },
                                    {
                                        "data": {
                                            "publishUrl": "/topic/60347049/sample-story?utm_source=test",
                                            "canonicalUrl": "/topic/60347049/sample-story",
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
        "content_format": "article",
        "author_names": ["Alice"],
        "tags": ["DeFi"],
        "categories": ["Research"],
        "excerpt": "Short excerpt",
        "canonical_url": "https://a16zcrypto.com/posts/article/three/",
        "source_kind": "non_mainstream_media",
    }
    assert repository.heartbeats[-1]["component"] == "non_mainstream_media"
