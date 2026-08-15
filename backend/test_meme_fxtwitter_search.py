from __future__ import annotations

from packages.meme_scanner import fxtwitter_search
import requests


ADDRESS = "0x1111111111111111111111111111111111111111"


class Response:
    status_code = 200

    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")

    def json(self) -> dict[str, object]:
        return self.payload


def tweet(tweet_id: str, text: str) -> dict[str, object]:
    return {
        "id": tweet_id,
        "text": text,
        "created_at": "Fri Aug 14 03:00:00 +0000 2026",
        "url": f"https://x.com/user/status/{tweet_id}",
        "author": {"screen_name": "user"},
    }


def test_search_ca_fetches_two_pages_per_feed_deduplicates_and_only_removes_gmgn(
    monkeypatch,
) -> None:
    responses = iter(
        [
            Response(
                {
                    "results": [
                        tweet("1", f"human claim {ADDRESS}"),
                        tweet("2", f"gmgn https://gmgn.ai/bsc/token/{ADDRESS}"),
                        tweet("3", "unrelated $PACMAN post"),
                    ],
                    "cursor": {"bottom": "top-2"},
                }
            ),
            Response(
                {
                    "results": [
                        tweet("1", f"human claim {ADDRESS}"),
                        tweet("4", f"another claim {ADDRESS}"),
                    ],
                    "cursor": {},
                }
            ),
            Response(
                {
                    "results": [
                        tweet("1", f"human claim {ADDRESS}"),
                        tweet("5", f"latest claim {ADDRESS}"),
                    ],
                    "cursor": {"bottom": "latest-2"},
                }
            ),
            Response(
                {
                    "results": [
                        tweet("6", f"another gmgn https://www.gmgn.ai/bsc/token/{ADDRESS}"),
                    ],
                    "cursor": {},
                }
            ),
        ]
    )
    monkeypatch.setattr(fxtwitter_search.requests, "get", lambda *args, **kwargs: next(responses))

    result = fxtwitter_search.search_ca(ADDRESS, timeout=1)

    assert [post["tweet_id"] for post in result["posts"]] == ["1", "4", "5"]
    assert {post["excluded_reason"] for post in result["excluded_posts"]} == {"gmgn_link", "no_exact_ca"}
    assert len(result["raw"]["pages"]) == 4
    assert len(result["posts"][0]["search_feeds"]) == 3


def test_search_ca_treats_404_as_an_empty_page(monkeypatch) -> None:
    response = Response({}, status_code=404)
    monkeypatch.setattr(fxtwitter_search.requests, "get", lambda *args, **kwargs: response)

    result = fxtwitter_search.search_ca(ADDRESS, pages_per_feed=1, timeout=1)

    assert result["posts"] == []
    assert result["excluded_posts"] == []
    assert result["raw"]["unique_results"] == 0
    assert result["raw"]["pages"] == [
        {"feed": "top", "page": 1, "result_count": 0, "has_next_cursor": False, "http_status": 404},
        {"feed": "latest", "page": 1, "result_count": 0, "has_next_cursor": False, "http_status": 404},
    ]
