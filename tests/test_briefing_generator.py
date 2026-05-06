from __future__ import annotations

from packages.briefing.generator import build_brief
from packages.market.models import MarketQuote


def quote(symbol: str, regular: float | None = None, premarket: float | None = None) -> MarketQuote:
    return MarketQuote(
        symbol=symbol,
        yahoo_symbol=symbol,
        display_name=symbol,
        regular_market_change_percent=regular,
        pre_market_change_percent=premarket,
    )


def test_close_brief_uses_is_publish_false_and_top5() -> None:
    payload = build_brief(
        kind="close",
        quotes=[
            quote("DJI", regular=0.2),
            quote("SPX", regular=0.3),
            quote("IXIC", regular=0.4),
            quote("MSTR", regular=4.1),
            quote("COIN", regular=3.2),
            quote("HUT", regular=2.4),
            quote("RIOT", regular=1.3),
            quote("MARA", regular=0.8),
            quote("BTBT", regular=0.2),
        ],
        skipped_symbols=[],
    )

    assert payload is not None
    assert payload.isPublish is False
    assert payload.title == "美股收盘加密概念股快讯"
    assert "美股收盘" in payload.content
    assert "Strategy 涨 4.1%" in payload.content
    assert "Bit Digital" not in payload.content


def test_premarket_brief_uses_premarket_values() -> None:
    payload = build_brief(
        kind="premarket",
        quotes=[
            quote("MSTR", regular=-9.0, premarket=2.0),
            quote("COIN", regular=-8.0, premarket=1.0),
        ],
        skipped_symbols=[],
    )

    assert payload is not None
    assert "美股盘前加密概念股普涨" in payload.content
    assert "Strategy 上涨 2%" in payload.content


def test_returns_none_when_no_stock_market_data() -> None:
    assert build_brief(kind="open", quotes=[quote("DJI", regular=0.2)], skipped_symbols=[]) is None
