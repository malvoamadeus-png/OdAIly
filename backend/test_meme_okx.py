from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from packages.meme_scanner import okx, scanner


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class OKXAdapterTests(unittest.TestCase):
    def test_list_request_is_signed_and_has_no_market_cap_filter(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse({"code": "0", "msg": "", "data": []})
        client = okx.OKXClient(
            api_key="key",
            secret_key="secret",
            passphrase="pass",
            session=session,
            max_attempts=1,
        )

        client.list_migrated("bsc")

        request = session.request.call_args.kwargs
        self.assertEqual(request["params"], {"chainIndex": "56", "stage": "MIGRATED", "limit": 30})
        self.assertNotIn("minMarketCapUsd", request["params"])
        self.assertIn("OK-ACCESS-SIGN", request["headers"])

    def test_price_info_is_batched_and_normalized_by_address(self) -> None:
        session = Mock()
        session.request.return_value = FakeResponse(
            {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "tokenContractAddress": "0xABC",
                        "marketCap": "600000",
                        "volume24H": "300000",
                    }
                ],
            }
        )
        client = okx.OKXClient(
            api_key="key",
            secret_key="secret",
            passphrase="pass",
            session=session,
            max_attempts=1,
        )

        result = client.price_info("robinhood", ["0xABC"])

        self.assertEqual(result["0xabc"]["volume24H"], "300000")
        body = session.request.call_args.kwargs["data"].decode("utf-8")
        self.assertIn('"chainIndex":"4663"', body)
        self.assertIn('"tokenContractAddress":"0xABC"', body)

    def test_migrated_discovery_enriches_both_chains_with_24h_metrics(self) -> None:
        client = Mock()
        client.list_migrated.side_effect = [
            [
                {
                    "tokenAddress": "0xBSC",
                    "protocolId": "129826",
                    "symbol": "BSC",
                    "name": "BSC Token",
                    "market": {"marketCapUsd": "600000"},
                    "tags": {"top10HoldingsPercent": "12"},
                    "social": {},
                }
            ],
            [
                {
                    "tokenAddress": "0xRH",
                    "protocolId": "141144",
                    "symbol": "RH",
                    "name": "RH Token",
                    "market": {"marketCapUsd": "1200000"},
                    "tags": {},
                    "social": {},
                }
            ],
        ]
        client.price_info.side_effect = [
            {"0xbsc": {"marketCap": "600000", "volume24H": "300000", "liquidity": "100000"}},
            {"0xrh": {"marketCap": "1200000", "volume24H": "800000", "liquidity": "200000"}},
        ]
        with patch.object(scanner, "get_okx_client", return_value=client):
            tokens = scanner.fetch_okx_migrated_tokens()

        self.assertEqual([(token.chain, token.symbol) for token in tokens], [("bsc", "BSC"), ("robinhood", "RH")])
        self.assertTrue(all(token.metrics_complete for token in tokens))
        self.assertEqual([token.volume_24h for token in tokens], [300000.0, 800000.0])

    def test_robinhood_uses_one_million_first_market_cap_level(self) -> None:
        self.assertEqual(scanner.market_cap_gate("bsc"), 500_000.0)
        self.assertEqual(scanner.market_cap_gate("robinhood"), 1_000_000.0)
        self.assertEqual(scanner.milestone_level(900_000, 1_100_000, "robinhood"), 1_000_000.0)
        self.assertIsNone(scanner.milestone_level(400_000, 900_000, "robinhood"))

    def test_price_info_keeps_tracking_when_token_details_is_unavailable(self) -> None:
        client = Mock()
        client.token_details.side_effect = okx.OKXError("OKX tokenDetails returned no token object")
        client.price_info.return_value = {
            "0xold": {"marketCap": "800000", "volume24H": "500000", "liquidity": "100000"}
        }
        with patch.object(scanner, "get_okx_client", return_value=client):
            current = scanner.fetch_okx_token_info("0xold", "bsc")

        self.assertIsNotNone(current)
        self.assertTrue(current.metrics_complete)
        self.assertEqual(current.market_cap, 800000.0)
        self.assertIn("okx_details_error", current.raw)

    def test_telegram_okx_fallback_recovers_identity_without_replacing_market_metrics(self) -> None:
        address = "0x23f1ad82bdb58f7524b6e76bdf5406267ef24413"
        client = Mock()
        client.token_details.side_effect = okx.OKXError("OKX tokenDetails returned no token object")
        client.price_info.return_value = {
            address: {"marketCap": "1300000", "volume24H": "900000", "liquidity": "200000"}
        }
        gmgn_identity = scanner.Token(
            address=address,
            platform="telegram",
            name="Dolores",
            symbol="DOLORES",
            market_cap=1,
            volume_24h=1,
            created_timestamp=None,
            raw={},
            chain="robinhood",
        )
        with patch.object(scanner, "get_okx_client", return_value=client), patch.object(
            scanner, "fetch_gmgn_token_info", return_value=gmgn_identity
        ):
            current = scanner.fetch_okx_token_info(address, "robinhood", resolve_identity=True)

        self.assertIsNotNone(current)
        self.assertEqual((current.name, current.symbol, current.market_cap, current.volume_24h),
                         ("Dolores", "DOLORES", 1_300_000.0, 900_000.0))
        self.assertEqual(current.raw["identity_source"], "gmgn")

    def test_okx_risk_flags_are_recorded_without_being_a_volume_gate(self) -> None:
        token = scanner.Token(
            address="0xrisk",
            platform="okx:1",
            name="Risk",
            symbol="RISK",
            market_cap=600_000,
            volume_24h=400_000,
            created_timestamp=None,
            raw={
                "market_source": "okx",
                "tags": {"top10HoldingsPercent": "84", "suspectedPhishingWalletPercent": "8"},
                "social": {},
                "liquidity": "100000",
            },
        )
        self.assertEqual(
            scanner.okx_risk_flags(token),
            ["suspected_phishing_wallets_high", "top10_concentration_high", "no_social_links"],
        )


if __name__ == "__main__":
    unittest.main()
