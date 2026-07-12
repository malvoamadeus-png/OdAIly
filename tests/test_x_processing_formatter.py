from __future__ import annotations

from packages.x_processing.formatter import format_brief
from packages.x_processing.models import DraftBrief


def test_format_brief_normalizes_han_ascii_spacing_in_content() -> None:
    draft = DraftBrief(
        title="标题",
        content="Altman表示OpenAI在2026年投入1000美元支持dapp生态",
    )

    formatted = format_brief(draft)

    assert formatted.content == "Odaily星球日报讯 Altman 表示 OpenAI 在 2026 年投入 1000 美元支持 DApp 生态。"


def test_format_brief_preserves_existing_odaily_prefix_when_normalizing_content() -> None:
    draft = DraftBrief(
        title="标题",
        content="Odaily星球日报讯Altman表示将投入1000美元支持OpenAI生态",
    )

    formatted = format_brief(draft)

    assert formatted.content == "Odaily星球日报讯 Altman 表示将投入 1000 美元支持 OpenAI 生态。"
