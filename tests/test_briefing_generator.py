from __future__ import annotations

from packages.briefing.generator import FOOTER_TEXT, build_brief
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
            quote("NVDA", regular=4.1),
            quote("MSFT", regular=3.2),
            quote("META", regular=2.4),
            quote("AMD", regular=1.3),
            quote("PLTR", regular=0.8),
            quote("GOOGL", regular=0.2),
        ],
        skipped_symbols=[],
    )

    assert payload is not None
    assert payload.isPublish is False
    assert payload.title == "美股收盘AI概念股普涨，NVIDIA涨超4.1%"
    assert "美股收盘" in payload.content
    assert "NVIDIA 涨 4.1%" in payload.content
    assert "Alphabet" not in payload.content


def test_premarket_brief_uses_premarket_values() -> None:
    payload = build_brief(
        kind="premarket",
        quotes=[
            quote("NVDA", regular=-9.0, premarket=2.0),
            quote("MSFT", regular=-8.0, premarket=1.0),
        ],
        skipped_symbols=[],
    )

    assert payload is not None
    assert payload.title == "美股盘前AI概念股普涨，NVIDIA涨超2%"
    assert "美股盘前AI概念股普涨" in payload.content
    assert "NVIDIA 上涨 2%" in payload.content


def test_returns_none_when_no_stock_market_data() -> None:
    assert build_brief(kind="open", quotes=[quote("DJI", regular=0.2)], skipped_symbols=[]) is None


def test_trend_and_title_use_fixed_ai_stock_pool() -> None:
    payload = build_brief(
        kind="open",
        quotes=[
            quote("AAPL", regular=50.0),
            quote("NVDA", regular=-3.0),
            quote("MSFT", regular=-1.0),
        ],
        skipped_symbols=[],
    )

    assert payload is not None
    assert payload.title == "美股开盘AI概念股普跌，NVIDIA跌超3%"
    assert "Apple" not in payload.content
    assert "Microsoft 下跌 1%" in payload.content


def test_msx_footer_uses_latest_copy() -> None:
    assert (
        FOOTER_TEXT
        == "据悉，MSX是一家头部RWA交易平台，累计已上线数百种 RWA 代币，"
        "涵盖 NVDA、GOOGL、MSFT、AMZN、META、TSM、AMD 等热门美股及 ETF 代币标的。"
    )
