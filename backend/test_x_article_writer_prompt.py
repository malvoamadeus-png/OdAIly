from __future__ import annotations

from datetime import UTC, datetime

from packages.x_processing.models import PromptTemplateVersion, TaskRecord
from packages.x_processing.worker import build_writer_prompt


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
