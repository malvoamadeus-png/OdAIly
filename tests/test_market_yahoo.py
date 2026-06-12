from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from packages.market.yahoo import fetch_chart_quotes, fetch_quotes, yahoo_symbol_for


def _et_timestamp(hour: int, minute: int) -> int:
    return int(datetime(2026, 5, 6, hour, minute, tzinfo=ZoneInfo("America/New_York")).timestamp())


def test_yahoo_symbol_for_normalizes_indices_and_tickers() -> None:
    assert yahoo_symbol_for("spx") == "^GSPC"
    assert yahoo_symbol_for("vix") == "^VIX"
    assert yahoo_symbol_for("brk.b") == "BRK-B"


def test_yahoo_symbol_for_allows_overrides() -> None:
    assert yahoo_symbol_for("hodl", {"HODL": "HODL.CN"}) == "HODL.CN"


def test_fetch_quotes_records_quote_error_without_chart_fallback(monkeypatch) -> None:
    class Response:
        def __init__(self, *, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("forbidden")

        def json(self) -> dict:
            return self.payload

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert "v7/finance/quote" in url
        return Response(status_code=403)

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_quotes(symbols=["MSTR"], timeout_seconds=1, max_attempts=1)

    assert batch.quotes == []
    assert batch.raw_response["quote_error"] == "forbidden"


def test_fetch_chart_quotes_close_uses_intraday_meta_regular_price(monkeypatch) -> None:
    class Response:
        def __init__(self, *, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("forbidden")

        def json(self) -> dict:
            return self.payload

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert "v8/finance/chart" in url
        assert params["range"] == "1d"
        assert params["interval"] == "1m"
        assert params["includePrePost"] == "false"
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 126,
                                "previousClose": 120,
                                "regularMarketTime": 1778025600,
                                "currency": "USD",
                            },
                            "timestamp": [1778022000, 1778025600],
                            "indicators": {"quote": [{"close": [125, 127]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_chart_quotes(
        symbols=["MSTR"],
        kind="close",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.symbol == "MSTR"
    assert quote.regular_market_price == 126
    assert quote.regular_market_change_percent == 5
    assert batch.raw_response["provider"] == "yahoo_chart"


def test_fetch_chart_quotes_close_rejects_suspicious_previous_close(monkeypatch) -> None:
    class Response:
        def __init__(self, *, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("forbidden")

        def json(self) -> dict:
            return self.payload

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert "v8/finance/chart" in url
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 2411.64,
                                "previousClose": 213.11,
                                "regularMarketTime": 1781208001,
                                "currency": "USD",
                            },
                            "timestamp": [1781208001],
                            "indicators": {"quote": [{"close": [2411.64]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_chart_quotes(
        symbols=["KLAC"],
        kind="close",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.source_error == "Yahoo chart previous close appears inconsistent with latest price."
    assert "KLAC" in batch.missing_symbols


def test_fetch_chart_quotes_open_uses_latest_minute_vs_previous_close(monkeypatch) -> None:
    class Response:
        def __init__(self, *, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("unauthorized")

        def json(self) -> dict:
            return self.payload

    calls: list[dict] = []

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        calls.append({"url": url, "params": params})
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "previousClose": 100,
                                "currency": "USD",
                            },
                            "timestamp": [
                                _et_timestamp(4, 0),
                                _et_timestamp(9, 31),
                            ],
                            "indicators": {"quote": [{"close": [102, 106]}]},
                        }
                    ]
                }
            },
    )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_chart_quotes(
        symbols=["MSTR"],
        kind="open",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.regular_market_price == 106
    assert quote.regular_market_change_percent == 6
    assert any(call["params"].get("includePrePost") == "true" for call in calls)


def test_fetch_chart_quotes_premarket_filters_to_premarket_window(monkeypatch) -> None:
    class Response:
        def __init__(self, *, status_code: int, payload: dict | None = None) -> None:
            self.status_code = status_code
            self.payload = payload or {}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError("unauthorized")

        def json(self) -> dict:
            return self.payload

    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert params["range"] == "1d"
        assert params["interval"] == "1m"
        assert params["includePrePost"] == "true"
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "previousClose": 100,
                                "currency": "USD",
                            },
                            "timestamp": [
                                _et_timestamp(4, 5),
                                _et_timestamp(9, 31),
                            ],
                            "indicators": {"quote": [{"close": [103, 110]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_chart_quotes(
        symbols=["MSTR"],
        kind="premarket",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.pre_market_price == 103
    assert quote.pre_market_change_percent == 3
    assert quote.pre_market_time is not None
