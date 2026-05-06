from __future__ import annotations

from packages.gate.generator import build_gate_brief
from packages.gate.models import GateAssetQuote


def quote(symbol: str, price: float, change: float, baseline: float = 1) -> GateAssetQuote:
    return GateAssetQuote(
        symbol=symbol,
        display_name=symbol,
        price=price,
        change_percent=change,
        source="test",
        source_symbol=symbol,
        raw={"baseline_abs_change_percent": baseline},
    )


def test_gate_title_uses_normalized_volatility_score() -> None:
    payload = build_gate_brief(
        quotes={
            "XAUUSD": quote("XAUUSD", 4600, 1.0, 1.0),
            "XAGUSD": quote("XAGUSD", 54, 1.5, 2.0),
            "USDCNH": quote("USDCNH", 7.1, 0.1, 0.2),
            "USDJPY": quote("USDJPY", 150, 0.5, 0.2),
            "XTIUSD": quote("XTIUSD", 80, 2.0, 10.0),
        }
    )

    assert payload is not None
    assert payload.title == "美元兑日元上涨0.5%，报150"


def test_gate_title_can_select_gold_and_content_contains_futures() -> None:
    payload = build_gate_brief(
        quotes={
            "XAUUSD": quote("XAUUSD", 4664.34, 2.3, 1.0),
            "XAGUSD": quote("XAGUSD", 60, 1.0, 1.0),
            "USDCNH": quote("USDCNH", 6.8, -0.1, 0.2),
            "USDJPY": quote("USDJPY", 150, 0.1, 0.2),
            "XTIUSD": quote("XTIUSD", 102, -1.0, 2.0),
            "BVIXUSDT": quote("BVIXUSDT", 41.08, -0.87, 1.0),
            "EVIXUSDT": quote("EVIXUSDT", 55.05, -2.57, 1.0),
        }
    )

    assert payload is not None
    assert payload.title == "黄金上涨2.3%，报4664.34美元/盎司"
    assert "BVIX（BTC 波动率指数）最新报价 41.08，日内跌幅为 0.87%" in payload.content
    assert payload.isPublish is False


def test_gate_missing_non_title_assets_are_placeholders() -> None:
    payload = build_gate_brief(quotes={"XTIUSD": quote("XTIUSD", 80, -1, 1)})

    assert payload is not None
    assert payload.title == "WTI原油下跌1%，报80美元/桶"
    assert "BVIX（BTC 波动率指数）最新报价 暂未获取到数据" in payload.content


def test_gate_returns_none_when_all_title_candidates_missing() -> None:
    payload = build_gate_brief(quotes={"BVIXUSDT": quote("BVIXUSDT", 41, 1, 1)})
    assert payload is None
