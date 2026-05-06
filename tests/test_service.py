from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from packages.briefing.service import run_brief_once
from packages.common.config import MarketBriefSettings
from packages.common.paths import AppPaths
from packages.market.models import MarketQuote, QuoteBatch


def make_paths(root) -> AppPaths:  # noqa: ANN001
    data = root / "data"
    return AppPaths(
        root_dir=root,
        backend_dir=root / "backend",
        frontend_dir=root / "frontend",
        data_dir=data,
        raw_dir=data / "raw",
        processed_dir=data / "processed",
        exports_dir=data / "exports",
        config_dir=data / "config",
        market_brief_config_path=data / "config" / "market_brief.json",
        gate_tradfi_config_path=data / "config" / "gate_tradfi.json",
    )


def test_run_once_skips_without_valid_data(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("packages.briefing.service.is_weekend_in_eastern", lambda: False)
    monkeypatch.setattr(
        "packages.briefing.service.fetch_quotes",
        lambda **_: QuoteBatch(quotes=[], missing_symbols=["MSTR"], raw_response={}),
    )

    result = run_brief_once(
        kind="close",
        settings=MarketBriefSettings(
            watchlist=["MSTR"],
            dry_run=True,
            min_valid_crypto_stocks=1,
            min_valid_indices=0,
        ),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 0
    assert result.status == "skipped"
    assert list((tmp_path / "data" / "processed" / "briefs").glob("*.jsonl"))


def test_run_once_dry_run_writes_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("packages.briefing.service.is_weekend_in_eastern", lambda: False)
    monkeypatch.setattr(
        "packages.briefing.service.fetch_quotes",
        lambda **_: QuoteBatch(
            quotes=[
                MarketQuote(
                    symbol="MSTR",
                    yahoo_symbol="MSTR",
                    display_name="MSTR",
                    regular_market_change_percent=2.3,
                )
            ],
            missing_symbols=[],
            raw_response={"ok": True},
        ),
    )

    result = run_brief_once(
        kind="close",
        settings=MarketBriefSettings(
            watchlist=["MSTR"],
            dry_run=True,
            min_valid_crypto_stocks=1,
            min_valid_indices=0,
        ),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 0
    assert result.status == "success"
    assert result.pushed is False


def test_run_once_records_error_without_push_when_yahoo_quote_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("packages.briefing.service.is_weekend_in_eastern", lambda: False)
    monkeypatch.setattr(
        "packages.briefing.service.fetch_quotes",
        lambda **_: QuoteBatch(
            quotes=[
                MarketQuote(
                    symbol="MSTR",
                    yahoo_symbol="MSTR",
                    display_name="MSTR",
                    regular_market_change_percent=2.3,
                )
            ],
            missing_symbols=[],
            raw_response={"quote_error": "401 Client Error: Unauthorized", "quote_attempts": 3},
        ),
    )

    class FailingPushClient:
        def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise AssertionError("Push client should not be created when Yahoo quote fails")

    monkeypatch.setattr("packages.briefing.service.PushClient", FailingPushClient)

    result = run_brief_once(
        kind="open",
        settings=MarketBriefSettings(
            watchlist=["MSTR"],
            dry_run=False,
            min_valid_crypto_stocks=1,
            min_valid_indices=0,
        ),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 1
    assert result.status == "error"
    records = list((tmp_path / "data" / "processed" / "briefs").glob("*.jsonl"))
    assert records
    assert "all market data sources failed or were skipped" in records[0].read_text(encoding="utf-8")


def test_run_once_uses_finnhub_fallback_when_yahoo_quote_fails(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("packages.briefing.service.is_weekend_in_eastern", lambda: False)
    monkeypatch.setattr(
        "packages.briefing.service.fetch_quotes",
        lambda **_: QuoteBatch(
            quotes=[],
            missing_symbols=["MSTR"],
            raw_response={"quote_error": "401 Client Error: Unauthorized", "quote_attempts": 3},
        ),
    )
    monkeypatch.setattr(
        "packages.briefing.service.fetch_finnhub_quotes",
        lambda **_: QuoteBatch(
            quotes=[
                MarketQuote(
                    symbol="MSTR",
                    yahoo_symbol="MSTR",
                    display_name="MSTR",
                    regular_market_change_percent=2.3,
                    regular_market_time="2026-05-06T13:31:00+00:00",
                )
            ],
            missing_symbols=[],
            raw_response={"provider": "finnhub"},
        ),
    )
    monkeypatch.setattr(
        "packages.briefing.service.now_shanghai",
        lambda: datetime(2026, 5, 6, 21, 32, tzinfo=ZoneInfo("Asia/Shanghai")),
    )

    result = run_brief_once(
        kind="open",
        settings=MarketBriefSettings(
            watchlist=["MSTR"],
            dry_run=True,
            finnhub_api_key="test-key",
            market_data_sources=["yahoo_quote", "finnhub"],
            min_valid_crypto_stocks=1,
            min_valid_indices=0,
        ),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 0
    assert result.status == "success"
