from __future__ import annotations

from packages.market.yahoo import yahoo_symbol_for
from packages.market.yahoo import fetch_quotes


def test_yahoo_symbol_for_normalizes_indices_and_tickers() -> None:
    assert yahoo_symbol_for("spx") == "^GSPC"
    assert yahoo_symbol_for("vix") == "^VIX"
    assert yahoo_symbol_for("brk.b") == "BRK-B"


def test_yahoo_symbol_for_allows_overrides() -> None:
    assert yahoo_symbol_for("hodl", {"HODL": "HODL.CN"}) == "HODL.CN"


def test_fetch_quotes_falls_back_to_chart(monkeypatch) -> None:
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
        if "v7/finance/quote" in url:
            return Response(status_code=403)
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "regularMarketPrice": 110,
                                "chartPreviousClose": 100,
                                "currency": "USD",
                            },
                            "indicators": {"quote": [{"close": [100, 110]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_quotes(symbols=["MSTR"], timeout_seconds=1, max_attempts=1)

    assert batch.quotes[0].symbol == "MSTR"
    assert batch.quotes[0].regular_market_change_percent == 10
    assert batch.raw_response["quote_error"] == "forbidden"


def test_fetch_quotes_enriches_premarket_from_intraday_chart(monkeypatch) -> None:
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
        if "v7/finance/quote" in url:
            return Response(
                status_code=200,
                payload={
                    "quoteResponse": {
                        "result": [
                            {
                                "symbol": "MSTR",
                                "shortName": "MSTR",
                                "regularMarketPrice": 100,
                                "regularMarketChangePercent": 1,
                            }
                        ]
                    }
                },
            )
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "previousClose": 100,
                                "regularMarketPrice": 100,
                                "currency": "USD",
                            },
                            "timestamp": [
                                1778061600,  # 2026-05-06 05:00 ET
                            ],
                            "indicators": {"quote": [{"close": [105]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_quotes(
        symbols=["MSTR"],
        timeout_seconds=1,
        max_attempts=1,
        include_premarket=True,
    )

    quote = batch.quotes[0]
    assert quote.symbol == "MSTR"
    assert quote.pre_market_price == 105
    assert quote.pre_market_change_percent == 5
    assert quote.pre_market_time is not None


def test_fetch_quotes_fallback_enriches_premarket_after_quote_error(monkeypatch) -> None:
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
        if "v7/finance/quote" in url:
            return Response(status_code=401)
        if params["interval"] == "1d":
            return Response(
                status_code=200,
                payload={
                    "chart": {
                        "result": [
                            {
                                "meta": {
                                    "regularMarketPrice": 100,
                                    "chartPreviousClose": 100,
                                    "currency": "USD",
                                },
                                "indicators": {"quote": [{"close": [100]}]},
                            }
                        ]
                    }
                },
            )
        return Response(
            status_code=200,
            payload={
                "chart": {
                    "result": [
                        {
                            "meta": {
                                "previousClose": 100,
                                "regularMarketPrice": 100,
                                "currency": "USD",
                            },
                            "timestamp": [
                                1778061600,  # 2026-05-06 05:00 ET
                            ],
                            "indicators": {"quote": [{"close": [103]}]},
                        }
                    ]
                }
            },
        )

    monkeypatch.setattr("packages.market.yahoo.requests.get", fake_get)
    batch = fetch_quotes(
        symbols=["MSTR"],
        timeout_seconds=1,
        max_attempts=1,
        include_premarket=True,
    )

    quote = batch.quotes[0]
    assert quote.pre_market_price == 103
    assert quote.pre_market_change_percent == 3
    assert batch.raw_response["quote_error"] == "unauthorized"
    assert any(call["params"].get("includePrePost") == "true" for call in calls)
