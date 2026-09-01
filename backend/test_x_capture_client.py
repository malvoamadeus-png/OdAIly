from types import SimpleNamespace
from unittest.mock import patch

from packages.x_capture.client import FXTwitterClient
from packages.x_capture.models import TweetCandidate
from packages.x_capture.token_identity import resolve_solana_token_symbol_with_gmgn


def _candidate(text: str = "Outer post") -> TweetCandidate:
    return TweetCandidate(
        tweet_id="123",
        author_username="tradexyz",
        author_display_name="trade.xyz",
        text=text,
    )


def _article(*, article_id: str = "article-1") -> dict:
    return {
        "id": article_id,
        "title": "Article title",
        "content": {
            "blocks": [
                {"type": "header-two", "text": "First heading"},
                {"type": "unstyled", "text": "First paragraph."},
                {"type": "unordered-list-item", "text": "A point"},
            ]
        },
    }


def test_build_record_merges_top_level_article_into_post_text() -> None:
    record = FXTwitterClient().build_record(
        "tradexyz",
        _candidate(),
        detail={"text": "Outer post", "article": _article()},
    )

    assert record.text == (
        "【普通帖子】\n"
        "Outer post\n"
        "【X文章】\n"
        "标题：Article title\n"
        "正文：## First heading\n"
        "First paragraph.\n"
        "- A point"
    )
    assert record.metadata["content_format"] == "x_post_with_article"
    assert record.metadata["article_count"] == 1
    assert record.metadata["article_titles"] == ["Article title"]


def test_build_record_merges_article_nested_in_quote() -> None:
    record = FXTwitterClient().build_record(
        "tradexyz",
        _candidate("Outer post with quoted article"),
        detail={
            "text": "Outer post with quoted article",
            "quote": {
                "text": "https://x.com/i/article/1",
                "article": _article(article_id="article-2"),
            },
        },
    )

    assert "Outer post with quoted article" in record.text
    assert "Article title" in record.text
    assert "First paragraph." in record.text
    assert record.metadata["content_format"] == "x_post_with_article"


def test_build_record_keeps_plain_post_content_unchanged() -> None:
    record = FXTwitterClient().build_record("tradexyz", _candidate("Plain post"), detail={"text": "Plain post"})

    assert record.text == "Plain post"
    assert "content_format" not in record.metadata


def test_build_record_resolves_solana_ca_with_gmgn_symbol_resolver() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    calls: list[str] = []

    def resolve_symbol(value: str) -> str | None:
        calls.append(value)
        return "TRUMP"

    client = FXTwitterClient(solana_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={
            "text": f"The Official Trump Meme Team transferred out another 11.01M solana:{address} ($26.65M).",
        },
    )

    assert record.text == "The Official Trump Meme Team transferred out another 11.01M TRUMP ($26.65M)."
    assert calls == [address]


def test_build_record_resolves_solana_ca_in_merged_article() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    client = FXTwitterClient(solana_symbol_resolver=lambda _: "TRUMP")
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={
            "text": "Outer post",
            "article": {
                "title": "Token transfer",
                "content": {"blocks": [{"type": "unstyled", "text": f"solana:{address}"}]},
            },
        },
    )

    assert f"solana:{address}" not in record.text
    assert "TRUMP" in record.text


def test_build_record_caches_duplicate_solana_ca_and_keeps_unresolved_ca() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    calls: list[str] = []

    def resolve_symbol(value: str) -> str | None:
        calls.append(value)
        return None

    client = FXTwitterClient(solana_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={"text": f"solana:{address} then solana:{address}"},
    )

    assert record.text == f"solana:{address} then solana:{address}"
    assert calls == [address]


def test_gmgn_symbol_resolver_uses_solana_identity_lookup() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    with patch(
        "packages.meme_scanner.scanner.fetch_gmgn_token_info",
        return_value=SimpleNamespace(symbol="$TRUMP"),
    ) as lookup:
        assert resolve_solana_token_symbol_with_gmgn(address) == "TRUMP"

    lookup.assert_called_once_with(
        address,
        "solana",
        allow_unknown_platform=True,
        identity_only=True,
    )
