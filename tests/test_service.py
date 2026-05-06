from __future__ import annotations

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
        settings=MarketBriefSettings(watchlist=["MSTR"], dry_run=True),
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
        settings=MarketBriefSettings(watchlist=["MSTR"], dry_run=True),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 0
    assert result.status == "success"
    assert result.pushed is False
