from __future__ import annotations

from packages.writer3.backfill import parse_odaily_item


def test_writer3_backfill_preserves_paragraph_breaks_in_odaily_content() -> None:
    item = parse_odaily_item(
        {
            "id": 494733,
            "title": "MGBX将上线 REUSDT、ARXUSDT、ALABUSDT 永续合约交易对",
            "content": (
                "<p>Odaily星球日报讯 据官方消息，MGBX 将于 2026 年 6 月 26 日 18:00（SGT） "
                "上线 REUSDT、ARXUSDT、ALABUSDT 永续合约交易对</p>"
                "<p>交易开放时间：2026 年 6 月 26 日 18:00（SGT）</p>"
                "<p>杠杆倍数： RE 最大支持 50 倍。</p>"
            ),
            "publishDate": "2026-06-26T07:22:39+00:00",
        }
    )

    assert item is not None
    assert item.content == (
        "据官方消息，MGBX 将于 2026 年 6 月 26 日 18:00（SGT） 上线 REUSDT、ARXUSDT、ALABUSDT 永续合约交易对\n"
        "交易开放时间：2026 年 6 月 26 日 18:00（SGT）\n"
        "杠杆倍数： RE 最大支持 50 倍。"
    )
