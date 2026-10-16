from __future__ import annotations

from datetime import date
from decimal import Decimal

from packages import okx_stock_brief_service as service
from packages.common.paths import AppPaths
from packages.okx_stock_brief import BriefResult
from packages.publisher import PushResult


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        root_dir=tmp_path,
        backend_dir=tmp_path / "backend",
        frontend_dir=tmp_path / "frontend",
        data_dir=tmp_path / "data",
        raw_dir=tmp_path / "data" / "raw",
        processed_dir=tmp_path / "data" / "processed",
        exports_dir=tmp_path / "data" / "exports",
        config_dir=tmp_path / "data" / "config",
        market_brief_config_path=tmp_path / "data" / "config" / "market_brief.json",
        gate_tradfi_config_path=tmp_path / "data" / "config" / "gate_tradfi.json",
    )


class FakePushClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def push(self, **kwargs) -> PushResult:
        self.calls.append(kwargs)
        return PushResult(ok=True, status_code=200, response_text="published")


def test_scheduled_run_publishes_with_fixed_title_and_idempotency(monkeypatch, tmp_path) -> None:
    brief = BriefResult(
        as_of_date=date(2026, 10, 15),
        window_start=date(2026, 9, 16),
        total_volume=Decimal("50260000000"),
        perpetual_volume=Decimal("48875000000"),
        spot_volume=Decimal("1385000000"),
        top_stocks=(("SNDK", Decimal("20000000000")), ("SOXL", Decimal("5000000000")), ("SPCX", Decimal("3000000000"))),
        top3_share_pct=Decimal("55"),
        storage_is_leading=True,
        candidates=(),
        product_count=287,
        failed_products=(),
    )
    push_client = FakePushClient()
    monkeypatch.setattr(service, "generate_brief", lambda _client, as_of_date: brief)
    monkeypatch.setattr(service, "_push_client", lambda: push_client)

    result = service.run_okx_stock_brief_once(
        kind="weekly",
        config_path=None,
        paths=_paths(tmp_path),
    )

    assert result.status == "success"
    assert result.pushed is True
    assert push_client.calls[0]["title"] == "OKX美股产品过去30日成交额超502亿美元"
    assert push_client.calls[0]["dry_run"] is False
    assert push_client.calls[0]["is_publish"] is True
    assert push_client.calls[0]["is_push"] is False
    assert push_client.calls[0]["idempotency_key"] == "okx-stock-brief:2026-10-15"

    audit_files = list((tmp_path / "data" / "processed" / "briefs").glob("*.jsonl"))
    assert len(audit_files) == 1
    assert "OKX美股产品过去30日成交额超502亿美元" in audit_files[0].read_text(encoding="utf-8")
