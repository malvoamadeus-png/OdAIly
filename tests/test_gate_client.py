from __future__ import annotations

from packages.gate.client import GateClient, baseline_abs_change_percent


def test_baseline_abs_change_percent_uses_median_with_floor() -> None:
    rows = [
        {"o": "100", "c": "101"},
        {"o": "100", "c": "102"},
        {"o": "100", "c": "103"},
    ]
    assert baseline_abs_change_percent(rows) == 2
    assert baseline_abs_change_percent([{"o": "100", "c": "100.01"}]) == 0.2


def test_fetch_tradfi_quote_parses_latest_kline(monkeypatch) -> None:
    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": {
                    "list": [
                        {"o": "100", "c": "101", "t": 1},
                        {"o": "100", "c": "105", "t": 2},
                    ]
                }
            }

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        return Response()

    monkeypatch.setattr("packages.gate.client.requests.get", fake_get)
    quote = GateClient(timeout_seconds=1, max_attempts=1, backoff_seconds=0).fetch_tradfi_quote(
        symbol="XAUUSD",
        display_name="黄金",
    )

    assert quote.price == 105
    assert quote.change_percent == 5
    assert quote.raw["baseline_abs_change_percent"] == 3


def test_fetch_futures_quote_uses_change_percentage() -> None:
    quote = GateClient(timeout_seconds=1, max_attempts=1, backoff_seconds=0).fetch_futures_quote(
        symbol="BVIXUSDT",
        contract="BVIX_USDT",
        display_name="BVIX",
        ticker_rows={"BVIX_USDT": {"last": "41.08", "change_percentage": "-0.87"}},
    )

    assert quote.price == 41.08
    assert quote.change_percent == -0.87
