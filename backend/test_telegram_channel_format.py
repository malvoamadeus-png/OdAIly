from packages.telegram_channel_format import format_aggregated_message


def test_apewire_removes_footer_and_blank_lines() -> None:
    text = "*英伟达董事会恢复黄仁勋总裁兼CEO职务: BBG\n\n$NVDA\n\n@apewirenews\n"
    assert format_aggregated_message("apewirenewsCN", text) == (
        "*英伟达董事会恢复黄仁勋总裁兼CEO职务: BBG\n$NVDA"
    )


def test_apewire_preserves_mentions_in_body_and_other_links() -> None:
    text = "@apewirenews 提及消息\n\nhttps://example.com/news\n\nhttps://t.me/apewirenews"
    assert format_aggregated_message("apewirenewsCN", text) == (
        "@apewirenews 提及消息\nhttps://example.com/news"
    )


def test_other_channel_does_not_strip_apewire_footer() -> None:
    assert format_aggregated_message("trad_fin_cn", "正文\n\n@apewirenews") == "正文\n@apewirenews"
