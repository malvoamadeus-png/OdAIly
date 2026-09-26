from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def load_screen_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "x_agent_local_screen.py"
    spec = importlib.util.spec_from_file_location("x_agent_local_screen_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_screening_uses_author_text_and_skips_quote_only_rows(tmp_path: Path) -> None:
    screen = load_screen_module()
    now = datetime(2026, 9, 26, tzinfo=UTC)
    path = tmp_path / "items.json"
    path.write_text(
        json.dumps(
            [
                {
                    "tweet_id": "quote-only",
                    "account_screen_name": "alice",
                    "created_at_iso": now.isoformat(),
                    "text": "",
                    "expanded_text": "Quoted @someone: BTC is extremely bullish",
                },
                {
                    "tweet_id": "author-text",
                    "account_screen_name": "alice",
                    "created_at_iso": now.isoformat(),
                    "text": "I am bullish on BTC after the ETF flows.",
                    "expanded_text": "I am bullish on BTC after the ETF flows.",
                },
            ]
        ),
        encoding="utf-8",
    )

    posts = screen.load_posts([path], cutoff=now - timedelta(days=1), as_of=now)

    assert [post["tweet_id"] for post in posts["alice"]] == ["author-text"]
    assert posts["alice"][0]["text"] == "I am bullish on BTC after the ETF flows."


def test_include_requires_three_unique_signal_bearing_posts_per_direction() -> None:
    screen = load_screen_module()
    row = {
        "representative_posts": [
            {"tweet_id": "m1", "text": "BTC is bullish and I am long."},
            {"tweet_id": "m2", "text": "I am accumulating ETH after the ETF flows."},
            {"tweet_id": "m3", "text": "I am bearish on NASDAQ and cutting exposure."},
            {"tweet_id": "p1", "text": "This protocol mainnet launch has a clear product thesis."},
            {"tweet_id": "p2", "text": "The token TGE and points program are the project catalyst."},
            {"tweet_id": "p3", "text": "The 0x12345678 DeFi contract design is why I am accumulating this project."},
        ]
    }
    raw = {
        "market_sentiment": {
            "decision": "include",
            "reason": "claimed market evidence",
            "evidence_post_ids": ["m1", "m1", "p1"],
        },
        "project_promotion": {
            "decision": "include",
            "reason": "three separate project examples",
            "evidence_post_ids": ["p1", "p2", "p3"],
        },
    }

    result = screen.validate(raw, row)

    assert result["market_sentiment"]["decision"] == "review"
    assert result["market_sentiment"]["evidence_post_ids"] == ["m1", "p1"]
    assert result["project_promotion"]["decision"] == "include"


def test_bare_market_or_project_mentions_cannot_fill_include_evidence() -> None:
    screen = load_screen_module()

    assert screen.has_market_signal("BTC price moved after a news headline.")
    assert not screen.has_market_include_evidence("BTC price moved after a news headline.")
    assert screen.has_project_signal("The protocol announced a launch.")
    assert not screen.has_project_include_evidence("The protocol announced a launch.")


def test_validator_downgrades_bare_price_news_and_generic_project_vocabulary() -> None:
    screen = load_screen_module()
    row = {
        "representative_posts": [
            {"tweet_id": "m1", "text": "BTC price changed after a news headline."},
            {"tweet_id": "m2", "text": "ETH price changed after a news headline."},
            {"tweet_id": "m3", "text": "NASDAQ price changed after a news headline."},
            {"tweet_id": "p1", "text": "The protocol announced a launch."},
            {"tweet_id": "p2", "text": "The token product has new points."},
            {"tweet_id": "p3", "text": "The contract team announced more liquidity."},
        ]
    }
    raw = {
        "market_sentiment": {"decision": "include", "reason": "market", "evidence_post_ids": ["m1", "m2", "m3"]},
        "project_promotion": {"decision": "include", "reason": "project", "evidence_post_ids": ["p1", "p2", "p3"]},
    }

    result = screen.validate(raw, row)

    assert result["market_sentiment"]["decision"] == "review"
    assert result["project_promotion"]["decision"] == "review"


def test_local_litellm_key_is_used_for_loopback_route(monkeypatch) -> None:
    screen = load_screen_module()
    monkeypatch.setenv("X_AGENT_OPENAI_BASE_URL", "http://127.0.0.1:4000/v1")
    monkeypatch.delenv("X_AGENT_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ODAILY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("LITELLM_MASTER_KEY", "local-master")

    base_url, api_key = screen.api_settings()

    assert base_url == "http://127.0.0.1:4000/v1"
    assert api_key == "local-master"
