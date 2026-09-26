from __future__ import annotations

from packages.x_agent.analysis import XAgentAnalyzer, is_relevant


def test_project_relevance_gate_rejects_generic_ai_and_product_language() -> None:
    assert not is_relevant(
        "project_promotion",
        "Our AI product launch adds a token budget, a protocol integration, points, and a new contract.",
    )
    assert not is_relevant(
        "project_promotion",
        "公司宣布项目发布，新的 liquidity 方案会在下周上线。",
    )


def test_project_relevance_gate_accepts_crypto_project_evidence() -> None:
    assert is_relevant(
        "project_promotion",
        "The $EXAMPLE token TGE is next week; its Base chain contract is 0xabcdef0123456789.",
    )
    assert is_relevant("project_promotion", "这个链上项目有空投和代币经济催化。")


def test_quote_text_is_not_sent_to_x_agent_extractor() -> None:
    prompt = XAgentAnalyzer._prompt(
        "project_promotion",
        {
            "tweet_id": "quoted",
            "account_screen_name": "alice",
            "activity_type": "quote",
            "text": "I will keep watching this.",
            "expanded_text": "I will keep watching this. 引用 @other: $EXAMPLE token has airdrop upside.",
            "quote_text": "$EXAMPLE token has airdrop upside.",
        },
    )

    assert "I will keep watching this." in prompt
    assert "$EXAMPLE token has airdrop upside." not in prompt
    assert "quoted_context" not in prompt
