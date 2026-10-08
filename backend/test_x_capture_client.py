import json
from types import SimpleNamespace
from unittest.mock import patch

from packages.x_capture.client import FXTwitterClient
from packages.x_capture.models import TweetCandidate, XCaptureAccount
from packages.x_capture.sqlite_repository import SQLiteXCaptureRepository
from packages.x_capture.worker import XCaptureWorker
from packages.common.storage import connect_sqlite
from packages.x_capture.token_identity import (
    resolve_solana_token_symbol_with_gmgn,
    resolve_token_symbol_with_gmgn,
)


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


def test_build_record_keeps_quoted_article_out_of_current_content() -> None:
    record = FXTwitterClient().build_record(
        "tradexyz",
        _candidate("Outer post with quoted article"),
        detail={
            "text": "Outer post with quoted article",
            "quote": {
                "id": "122",
                "text": "https://x.com/i/article/1",
                "article": _article(article_id="article-2"),
            },
        },
        context_chain=[{"id": "122", "text": "【X文章】\n标题：Article title\n正文：First paragraph."}],
    )

    assert record.text == "Outer post with quoted article"
    assert record.metadata["context_chain"][0]["id"] == "122"
    assert "Article title" in record.metadata["context_chain"][0]["text"]
    assert "content_format" not in record.metadata


def test_collect_context_chain_fetches_each_predecessor_until_the_earliest() -> None:
    client = FXTwitterClient()
    calls = []
    posts = {
        "2": {"id": "2", "text": "麻吉再次减仓 1 万枚 ETH", "author": {"screen_name": "ai_9684xtpa", "name": "Ai 姨"}, "quote": {"id": "1", "author": {"screen_name": "ai_9684xtpa"}}},
        "1": {"id": "1", "text": "麻吉持有 ETH 多单", "author": {"screen_name": "ai_9684xtpa", "name": "Ai 姨"}},
    }

    def fetch(username: str, tweet_id: str) -> dict:
        calls.append((username, tweet_id))
        return posts[tweet_id]

    client.fetch_detail = fetch
    chain, status, error = client.collect_context_chain(
        {"id": "3", "text": "更新：只剩 1 万枚，实质性亏损 262.8 万美元", "quote": {"id": "2", "author": {"screen_name": "ai_9684xtpa"}}},
        root_id="3",
    )

    assert [item["id"] for item in chain] == ["2", "1"]
    assert chain[0]["text"] == "麻吉再次减仓 1 万枚 ETH"
    assert calls == [("ai_9684xtpa", "2"), ("ai_9684xtpa", "1")]
    assert (status, error) == ("complete", None)


def test_collect_context_chain_without_reference_keeps_single_post_path() -> None:
    client = FXTwitterClient()
    client.fetch_detail = lambda *_args: (_ for _ in ()).throw(AssertionError("unexpected fetch"))

    chain, status, error = client.collect_context_chain({"id": "3", "text": "独立快讯"}, root_id="3")

    assert (chain, status, error) == ([], "complete", None)


def test_collect_context_chain_keeps_embedded_post_on_fetch_failure() -> None:
    client = FXTwitterClient()
    client.fetch_detail = lambda _username, _id: (_ for _ in ()).throw(TimeoutError("upstream timeout"))

    chain, status, error = client.collect_context_chain(
        {"id": "3", "quote": {"id": "2", "text": "麻吉减仓 ETH", "author": {"screen_name": "ai_9684xtpa"}}},
        root_id="3",
    )

    assert [item["text"] for item in chain] == ["麻吉减仓 ETH"]
    assert status == "partial"
    assert "upstream timeout" in error


def test_collect_context_chain_continues_through_embedded_post_after_fetch_failure() -> None:
    client = FXTwitterClient()
    raw_layers = []
    client.fetch_detail = lambda _username, _id: (_ for _ in ()).throw(TimeoutError("upstream timeout"))
    chain, status, error = client.collect_context_chain(
        {"id": "3", "quote": {
            "id": "2", "text": "麻吉减仓 ETH", "author": {"screen_name": "ai_9684xtpa"},
            "quote": {"id": "1", "text": "麻吉持有 ETH 多单", "author": {"screen_name": "ai_9684xtpa"}},
        }},
        root_id="3", raw_layers=raw_layers,
    )

    assert [item["id"] for item in chain] == ["2", "1"]
    assert [item["id"] for item in raw_layers] == ["2", "1"]
    assert status == "partial"
    assert "upstream timeout" in error


def test_collect_context_chain_stops_at_cycle() -> None:
    client = FXTwitterClient()
    client.fetch_detail = lambda _username, _id: {
        "id": "2", "text": "previous", "author": {"screen_name": "ai_9684xtpa"},
        "quote": {"id": "3", "author": {"screen_name": "ai_9684xtpa"}},
    }

    chain, status, error = client.collect_context_chain(
        {"id": "3", "quote": {"id": "2", "author": {"screen_name": "ai_9684xtpa"}}},
        root_id="3",
    )

    assert [item["id"] for item in chain] == ["2"]
    assert status == "partial"
    assert "cycle" in error


def test_collect_context_chain_follows_id_only_reference() -> None:
    client = FXTwitterClient()
    calls = []

    def fetch(username: str, tweet_id: str) -> dict:
        calls.append((username, tweet_id))
        return {"id": tweet_id, "text": "麻吉 ETH 多单", "author": {"screen_name": "ai_9684xtpa"}}

    client.fetch_detail = fetch
    chain, status, error = client.collect_context_chain(
        {"id": "3", "author": {"screen_name": "ai_9684xtpa"}, "quote_id": "2"},
        root_id="3",
    )

    assert [item["id"] for item in chain] == ["2"]
    assert calls == [("ai_9684xtpa", "2")]
    assert (status, error) == ("complete", None)


def test_collect_context_chain_follows_production_reply_shape() -> None:
    client = FXTwitterClient()
    fetched = []

    def fetch(username: str, tweet_id: str) -> dict:
        fetched.append((username, tweet_id))
        return {
            "id": tweet_id,
            "text": "麻吉再次减仓 1 万枚 ETH",
            "author": {"screen_name": "ai_9684xtpa"},
            "quote": {"id": "2108193057229455562", "author": {"screen_name": "ai_9684xtpa"}},
        } if tweet_id == "2108218549844250691" else {
            "id": tweet_id, "text": "麻吉又濒临清算", "author": {"screen_name": "ai_9684xtpa"},
        }

    client.fetch_detail = fetch
    chain, status, error = client.collect_context_chain(
        {
            "id": "2108221765877133461",
            "author": {"screen_name": "ai_9684xtpa"},
            "replying_to": "ai_9684xtpa",
            "replying_to_status": "2108218549844250691",
        },
        root_id="2108221765877133461",
    )

    assert [item["id"] for item in chain] == ["2108218549844250691", "2108193057229455562"]
    assert [item["relation"] for item in chain] == ["reply", "quote"]
    assert fetched == [("ai_9684xtpa", "2108218549844250691"), ("ai_9684xtpa", "2108193057229455562")]
    assert (status, error) == ("complete", None)


def test_collect_context_chain_follows_timeline_reply_object() -> None:
    client = FXTwitterClient()
    client.fetch_detail = lambda _username, tweet_id: {
        "id": tweet_id, "text": "麻吉减仓 ETH", "author": {"screen_name": "ai_9684xtpa"},
    }
    chain, status, error = client.collect_context_chain(
        {"id": "3", "replying_to": {"screen_name": "ai_9684xtpa", "status": "2", "url": "https://x.com/ai_9684xtpa/status/2"}},
        root_id="3",
    )

    assert chain[0]["id"] == "2"
    assert chain[0]["relation"] == "reply"
    assert (status, error) == ("complete", None)


def test_capture_uses_timeline_quote_when_detail_omits_it_and_persists_context(tmp_path) -> None:
    client = FXTwitterClient()
    client.fetch_detail = lambda _username, tweet_id: (
        {"id": "3", "text": "更新：仅剩 1 万枚 ETH", "author": {"screen_name": "ai_9684xtpa"}}
        if tweet_id == "3" else
        {"id": "2", "text": "麻吉再次减仓 ETH 多单", "author": {"screen_name": "ai_9684xtpa"}}
    )
    worker = object.__new__(XCaptureWorker)
    worker.client = client
    account = XCaptureAccount(id=1, username="ai_9684xtpa", username_lower="ai_9684xtpa")
    candidate = TweetCandidate(
        tweet_id="3", author_username="ai_9684xtpa", author_display_name="Ai 姨",
        text="更新：仅剩 1 万枚 ETH", raw_payload={"quote_id": "2"},
    )

    record = worker._record_from_candidate(account, candidate, {})
    repository = SQLiteXCaptureRepository(tmp_path / "odaily.sqlite")
    task_id = repository.save_task(account, record)
    with connect_sqlite(tmp_path / "odaily.sqlite") as conn:
        task = conn.execute("SELECT content,metadata,raw_payload FROM tasks WHERE id=?", (task_id,)).fetchone()
    metadata = json.loads(task["metadata"])

    assert task["content"] == "更新：仅剩 1 万枚 ETH"
    assert metadata["context_chain"][0]["text"] == "麻吉再次减仓 ETH 多单"
    assert metadata["context_chain_status"] == "complete"
    assert json.loads(task["raw_payload"])["raw_payload"]["context_layers"][0]["detail"]["id"] == "2"


def test_build_record_keeps_plain_post_content_unchanged() -> None:
    record = FXTwitterClient().build_record("tradexyz", _candidate("Plain post"), detail={"text": "Plain post"})

    assert record.text == "Plain post"
    assert "content_format" not in record.metadata


def test_build_record_resolves_solana_ca_with_gmgn_symbol_resolver() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    calls: list[tuple[tuple[str, ...], str]] = []

    def resolve_symbol(chains: tuple[str, ...], value: str) -> str | None:
        calls.append((chains, value))
        return "TRUMP"

    client = FXTwitterClient(token_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={
            "text": f"The Official Trump Meme Team transferred out another 11.01M solana:{address} ($26.65M).",
        },
    )

    assert record.text == "The Official Trump Meme Team transferred out another 11.01M TRUMP ($26.65M)."
    assert calls == [(("solana",), address)]


def test_build_record_resolves_solana_ca_in_merged_article() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    client = FXTwitterClient(token_symbol_resolver=lambda _chains, _address: "TRUMP")
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
    calls: list[tuple[tuple[str, ...], str]] = []

    def resolve_symbol(chains: tuple[str, ...], value: str) -> str | None:
        calls.append((chains, value))
        return None

    client = FXTwitterClient(token_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={"text": f"solana:{address} then solana:{address}"},
    )

    assert record.text == f"solana:{address} then solana:{address}"
    assert calls == [(("solana",), address)]


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


def test_build_record_resolves_ethereum_ca_in_priority_chain_order() -> None:
    address = "0x07f5b6823751c2e2cd4560f28af75ff887102241"
    calls: list[tuple[tuple[str, ...], str]] = []

    def resolve_symbol(chains: tuple[str, ...], value: str) -> str | None:
        calls.append((chains, value))
        return "PONS"

    client = FXTwitterClient(token_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={"text": f"Unipcs holds ethereum:{address}."},
    )

    assert record.text == "Unipcs holds PONS."
    assert calls == [(("robinhood", "bsc", "base", "eth"), address)]


def test_build_record_resolves_solana_ca_without_evm_fallback() -> None:
    address = "6p6xgHyF7AeE6TZkSmFsko444wqoP15icUSqi2jfGiPN"
    calls: list[tuple[tuple[str, ...], str]] = []

    def resolve_symbol(chains: tuple[str, ...], value: str) -> str | None:
        calls.append((chains, value))
        return "TRUMP"

    client = FXTwitterClient(token_symbol_resolver=resolve_symbol)
    record = client.build_record(
        "lookonchain",
        _candidate(),
        detail={"text": f"The token is solana:{address}."},
    )

    assert record.text == "The token is TRUMP."
    assert calls == [(("solana",), address)]


def test_gmgn_symbol_resolver_falls_back_across_evm_chains() -> None:
    address = "0x07f5b6823751c2e2cd4560f28af75ff887102241"
    responses = iter([None, None, None, SimpleNamespace(symbol="$PONS")])
    with patch(
        "packages.meme_scanner.scanner.fetch_gmgn_token_info",
        side_effect=lambda value, chain, **kwargs: next(responses),
    ) as lookup:
        assert resolve_token_symbol_with_gmgn(("robinhood", "bsc", "base", "eth"), address) == "PONS"

    assert [call.args[:2] for call in lookup.call_args_list] == [
        (address, "robinhood"),
        (address, "bsc"),
        (address, "base"),
        (address, "eth"),
    ]
