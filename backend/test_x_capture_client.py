from packages.x_capture.client import FXTwitterClient
from packages.x_capture.models import TweetCandidate


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
