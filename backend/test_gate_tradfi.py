from __future__ import annotations

from packages.common.config import GateTradfiSettings
from packages.gate.generator import build_gate_brief
from packages.gate.models import GateAssetQuote


def _tradfi_quote(symbol: str, display_name: str, price: str, change: str) -> GateAssetQuote:
    return GateAssetQuote(
        symbol=symbol,
        display_name=display_name,
        price=float(price),
        change_percent=float(change),
        source="gate-tradfi",
        source_symbol=symbol,
    )


def test_legacy_futures_config_is_ignored() -> None:
    settings = GateTradfiSettings.model_validate(
        {
            "futures_symbols": {
                "BVIXUSDT": {"contract": "BVIX_USDT", "display_name": "BVIX"},
                "EVIXUSDT": {"contract": "EVIX_USDT", "display_name": "EVIX"},
            }
        }
    )

    assert not hasattr(settings, "futures_symbols")


def test_gate_brief_no_longer_contains_removed_volatility_indices() -> None:
    brief = build_gate_brief(
        quotes={
            "XAUUSD": _tradfi_quote("XAUUSD", "黄金", "4355.32", "2.62"),
            "XAGUSD": _tradfi_quote("XAGUSD", "白银", "64.079", "4.31"),
        }
    )

    assert brief is not None
    assert "BVIX" not in brief.content
    assert "EVIX" not in brief.content
    assert "波动率指数" not in brief.content
