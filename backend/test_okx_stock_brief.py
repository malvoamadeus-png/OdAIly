from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from packages.okx_stock_brief import generate_brief


def _timestamp_for_bjt_day(day: date) -> int:
    instant = datetime.combine(day - timedelta(days=1), datetime.min.time(), UTC).replace(hour=16)
    return int(instant.timestamp() * 1000)


class FakeOKXClient:
    def __init__(self, as_of: date) -> None:
        self.as_of = as_of
        self.items = {
            "SNDK-USDT-SWAP": ("SNDK", "p", Decimal("1000000000")),
            "SPCX-USDT-SWAP": ("SPCX", "p", Decimal("500000000")),
            "XSKHYNIX-USDT": ("SKHYNIX", "s", Decimal("200000000")),
        }

    def instruments(self, inst_type: str) -> list[dict[str, str]]:
        if inst_type == "SWAP":
            return [
                {
                    "state": "live",
                    "instCategory": "3",
                    "settleCcy": "USDT",
                    "instId": "SNDK-USDT-SWAP",
                    "instFamily": "SNDK-USDT",
                    "listTime": "1",
                },
                {
                    "state": "live",
                    "instCategory": "3",
                    "settleCcy": "USDT",
                    "instId": "SPCX-USDT-SWAP",
                    "instFamily": "SPCX-USDT",
                    "listTime": "1",
                },
                {
                    "state": "live",
                    "instCategory": "1",
                    "settleCcy": "USDT",
                    "instId": "BTC-USDT-SWAP",
                    "instFamily": "BTC-USDT",
                    "listTime": "1",
                },
            ]
        return [
            {
                "state": "live",
                "instCategory": "3",
                "quoteCcy": "USDT",
                "baseCcy": "XSKHYNIX",
                "instId": "XSKHYNIX-USDT",
                "listTime": "1",
            }
        ]

    def history_candles(self, inst_id: str, *, limit: int = 100) -> list[list[str]]:
        underlying, _, volume = self.items[inst_id]
        rows = []
        for days_ago in range(0, 61):
            day = self.as_of - timedelta(days=days_ago)
            # The as-of date is complete for this fixture. It also proves that
            # the collector does not silently include an unconfirmed row.
            rows.append(
                [
                    str(_timestamp_for_bjt_day(day)),
                    "1",
                    "1",
                    "1",
                    "1",
                    "1",
                    "1",
                    str(volume),
                    "1",
                ]
            )
        rows.append([str(_timestamp_for_bjt_day(self.as_of + timedelta(days=1))), "1", "1", "1", "1", "1", "1", "999", "0"])
        return rows


def test_generate_brief_uses_all_category_three_products_and_renders_template() -> None:
    as_of = date(2026, 9, 9)
    result = generate_brief(FakeOKXClient(as_of), as_of_date=as_of)

    assert result.product_count == 3
    assert result.total_volume == Decimal("51000000000")
    assert result.perpetual_volume == Decimal("45000000000")
    assert result.spot_volume == Decimal("6000000000")
    assert [name for name, _ in result.top_stocks] == ["SNDK", "SPCX", "SKHYNIX"]
    assert result.storage_is_leading is True
    assert "截至 2026 年 9 月 9 日" in result.render()
    assert result.window_start == date(2026, 8, 10)
    assert "过去 30 天 OKX 美股相关产品成交额达 510.00 亿 USDT" in result.render()
    assert "其中，股票永续成交额为 450.00 亿 USDT，现货成交额为 60.00 亿 USDT" in result.render()
    assert "近 30 日最热门股票为 SNDK、SPCX 和 SKHYNIX" in result.render()


def test_growth_is_diagnostic_only_and_incomplete_candle_is_ignored() -> None:
    as_of = date(2026, 9, 9)
    result = generate_brief(FakeOKXClient(as_of), as_of_date=as_of)

    assert all(candidate.quality == "complete" for candidate in result.candidates[:2])
    assert "环比增长" not in result.render()
