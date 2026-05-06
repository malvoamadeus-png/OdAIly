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
