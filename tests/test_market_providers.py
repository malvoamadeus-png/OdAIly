from __future__ import annotations

from packages.market.providers import fetch_finnhub_quotes


class Response:
    def __init__(self, *, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("request failed")

    def json(self) -> dict:
        return self.payload


def test_fetch_finnhub_quotes_normalizes_quote(monkeypatch) -> None:
    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert params["symbol"] == "MSTR"
        assert params["token"] == "key"
        return Response(payload={"c": 105, "pc": 100, "dp": 5, "t": 1778061600})

    monkeypatch.setattr("packages.market.providers.requests.get", fake_get)

    batch = fetch_finnhub_quotes(
        symbols=["MSTR"],
        kind="open",
        api_key="key",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.symbol == "MSTR"
    assert quote.regular_market_price == 105
    assert quote.regular_market_change_percent == 5
    assert quote.regular_market_time is not None
    assert batch.raw_response["provider"] == "finnhub"
