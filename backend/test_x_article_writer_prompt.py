from __future__ import annotations

from datetime import UTC, datetime

from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.worker import build_structured_writer_prompt, build_writer_prompt


def _prompt() -> PromptTemplateVersion:
    return PromptTemplateVersion(
        id=33,
        template_key="x_regular_writer",
        version_number=9,
        content="基础写作规则：只写事实。",
    )


def _task(*, content_format: str | None, content: str) -> TaskRecord:
    metadata = {"effective_author_name": "Uniswap创始人Hayden"}
    if content_format is not None:
        metadata["content_format"] = content_format
    return TaskRecord(
        id=625790,
        source="x",
        source_item_id="2095564227989700913",
        source_url="https://x.com/haydenzadams/status/2095564227989700913",
        title="相关性交易对将推动AMM进入全球金融市场",
        content=content,
        published_at=datetime(2026, 9, 3, 17, 27, tzinfo=UTC),
        metadata=metadata,
        status="searched",
    )


def test_x_article_material_uses_one_source_compact_editing_context() -> None:
    prompt = build_writer_prompt(
        task=_task(
            content_format="x_post_with_article",
            content=(
                "【普通帖子】\n相关性交易对将推动AMM进入全球金融市场\n"
                "【X文章】\n标题：Correlated Pairs\n正文：我已在 DeFi 前沿工作 9 年。"
            ),
        ),
        prompt=_prompt(),
        structured_output=True,
    )

    assert "【X Article写作上下文】" in prompt
    assert "外层帖子与 Article 合并为同一条 X 来源" in prompt
    assert "按单一来源编辑" in prompt
    assert "只保留核心观点、关键事实和数字" in prompt
    assert "2–4 句、1–2 段" in prompt
    assert "禁止逐段翻译或完整复述 Article" in prompt
    assert "不得保留输入区块标题" in prompt
    assert "发言人在 X 平台发文表示”最多出现一次" in prompt
    assert "【普通帖子】" in prompt
    assert "【X文章】" in prompt


def test_x_article_format_without_outer_post_uses_same_context() -> None:
    prompt = build_writer_prompt(
        task=_task(content_format="x_article", content="【X文章】\n正文：文章全文"),
        prompt=_prompt(),
    )

    assert "【X Article写作上下文】" in prompt
    assert "2–4 句、1–2 段" in prompt


def test_plain_x_post_keeps_existing_input_path() -> None:
    prompt = build_writer_prompt(
        task=_task(content_format=None, content="普通 X 帖子正文"),
        prompt=_prompt(),
    )

    assert "【待处理原文】" in prompt
    assert "【X Article写作上下文】" not in prompt
    assert "发布人：Uniswap创始人Hayden" in prompt


def test_structured_writer_prompt_does_not_require_title_deduplication() -> None:
    prompt = build_structured_writer_prompt(
        task=_task(content_format=None, content="CoinMarketCap宣布完成对Coinglass的收购，交易金额未披露。"),
        prompt=_prompt(),
        known_subjects=[],
    )

    assert "不要复述 title" not in prompt


def test_context_chain_keeps_latest_post_primary_and_labels_old_facts() -> None:
    task = TaskRecord(
        id=1,
        source="x",
        source_item_id="2108221765877133461",
        source_url="https://x.com/ai_9684xtpa/status/2108221765877133461",
        title=None,
        content="更新：只剩 1 万枚，实质性亏损扩大到 262.8 万美元，清算价 2418.4 美元",
        metadata={
            "effective_author_name": "链上分析师Ai姨",
            "context_chain": [
                {"id": "2108218549844250691", "author_username": "ai_9684xtpa", "text": "麻吉再次减仓 1 万枚 ETH，剩余 14500 ETH 多单，清算价 2442.16 美元"},
                {"id": "older", "author_username": "ai_9684xtpa", "text": "麻吉此前持有 ETH 多单"},
            ],
        },
    )

    prompt = build_structured_writer_prompt(task=task, prompt=_prompt(), known_subjects=[])

    assert "【本次播报（最新帖）】" in prompt
    assert "【引用/回复前序链背景（由近到远）】" in prompt
    assert prompt.index("262.8 万美元") < prompt.index("14500 ETH") < prompt.index("此前持有 ETH")
    assert "旧数值" in prompt and "此前" in prompt
    assert "不得把前序帖" in prompt


def test_unrelated_context_cannot_be_promoted_to_current_event() -> None:
    task = TaskRecord(
        id=2, source="x", source_item_id="3", source_url="https://x.com/a/status/3",
        title=None, content="本次更新：ETH 多单剩余 1 万枚",
        metadata={"context_chain": [{"id": "2", "text": "另一个事件：BTC ETF 资金流入"}]},
    )

    prompt = build_structured_writer_prompt(task=task, prompt=_prompt(), known_subjects=[])

    assert "只有同一主体同一事件的相关旧事实才可简述" in prompt
    assert "不得把前序帖的其他事件、作者观点或旧状态提升为标题和核心播报" in prompt
    assert prompt.index("本次更新：ETH") < prompt.index("BTC ETF")
