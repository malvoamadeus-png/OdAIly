from __future__ import annotations

from packages.market.providers import fetch_alpaca_iex_quotes, fetch_finnhub_quotes


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


def test_fetch_alpaca_iex_quotes_normalizes_snapshot(monkeypatch) -> None:
    def fake_get(url, params, headers, timeout):  # noqa: ANN001
        assert params["feed"] == "iex"
        assert headers["APCA-API-KEY-ID"] == "key"
        return Response(
            payload={
                "snapshots": {
                    "MSTR": {
                        "latestTrade": {"p": 105, "t": "2026-05-06T13:31:00Z"},
                        "prevDailyBar": {"c": 100},
                    }
                }
            }
        )

    monkeypatch.setattr("packages.market.providers.requests.get", fake_get)

    batch = fetch_alpaca_iex_quotes(
        symbols=["MSTR"],
        kind="premarket",
        api_key="key",
        api_secret="secret",
        timeout_seconds=1,
        max_attempts=1,
    )

    quote = batch.quotes[0]
    assert quote.symbol == "MSTR"
    assert quote.pre_market_price == 105
    assert quote.pre_market_change_percent == 5
    assert quote.pre_market_time == "2026-05-06T13:31:00Z"
    assert batch.raw_response["provider"] == "alpaca_iex"
