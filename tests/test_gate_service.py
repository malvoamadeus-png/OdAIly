from __future__ import annotations

from packages.common.config import GateTradfiSettings
from packages.common.paths import AppPaths
from packages.gate.models import GateAssetQuote, GateQuoteBatch
from packages.gate.service import run_gate_once


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


def test_run_gate_once_dry_run_writes_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("packages.gate.service.is_weekend_in_eastern", lambda: False)

    class FakeGateClient:
        def __init__(self, **kwargs):  # noqa: ANN003
            pass

        def fetch_batch(self, **kwargs):  # noqa: ANN003
            return GateQuoteBatch(
                quotes={
                    "XAUUSD": GateAssetQuote(
                        symbol="XAUUSD",
                        display_name="黄金",
                        price=4600,
                        change_percent=1.2,
                        source="test",
                        source_symbol="XAUUSD",
                        raw={"baseline_abs_change_percent": 1},
                    )
                },
                raw_response={},
                errors={},
            )

    monkeypatch.setattr("packages.gate.service.GateClient", FakeGateClient)

    result = run_gate_once(
        kind="morning",
        settings=GateTradfiSettings(dry_run=True),
        paths=make_paths(tmp_path),
        force=False,
    )

    assert result.exit_code == 0
    assert result.status == "success"
    assert result.pushed is False
    assert list((tmp_path / "data" / "raw" / "gate_quotes").glob("*/*.json"))
    assert list((tmp_path / "data" / "processed" / "briefs").glob("*.jsonl"))
