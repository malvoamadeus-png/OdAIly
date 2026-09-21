from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from packages.meme_scanner import scanner


def pair(address: str, market_cap: float, volume_24h: float, liquidity: float, *, name: str = "", symbol: str = "") -> dict:
    return {
        "chainId": "bsc",
        "baseToken": {"address": address, "name": name, "symbol": symbol},
        "marketCap": market_cap,
        "volume": {"h24": volume_24h},
        "liquidity": {"usd": liquidity},
    }


class MemeMarketAdapterTests(unittest.TestCase):
    def test_migrated_discovery_releases_web_client_after_complete_pass(self) -> None:
        web_client = Mock()
        web_client.list_migrated.side_effect = [[], []]
        with patch.object(scanner, "get_okx_meme_web_client", return_value=web_client), patch.object(
            scanner, "close_okx_meme_web_client"
        ) as close_client:
            scanner.fetch_okx_migrated_tokens()

        self.assertEqual(web_client.list_migrated.call_count, 2)
        close_client.assert_called_once()

    def test_migrated_discovery_releases_web_client_after_failure(self) -> None:
        web_client = Mock()
        web_client.list_migrated.side_effect = RuntimeError("page failed")
        with patch.object(scanner, "get_okx_meme_web_client", return_value=web_client), patch.object(
            scanner, "close_okx_meme_web_client"
        ) as close_client:
            with self.assertRaisesRegex(RuntimeError, "page failed"):
                scanner.fetch_okx_migrated_tokens()

        close_client.assert_called_once()

    def test_migrated_discovery_enriches_both_chains_with_dexscreener_metrics(self) -> None:
        web_client = Mock()
        web_client.list_migrated.side_effect = [
            [{"tokenAddress": "0xBSC", "symbol": "BSC", "name": "BSC Token"}],
            [{"tokenAddress": "0xRH", "symbol": "RH", "name": "RH Token"}],
        ]
        market_client = Mock()
        market_client.price_info.side_effect = [
            {"0xbsc": pair("0xbsc", 600_000, 300_000, 100_000)},
            {"0xrh": {**pair("0xrh", 1_200_000, 800_000, 200_000), "chainId": "robinhood"}},
        ]
        with patch.object(scanner, "get_okx_meme_web_client", return_value=web_client), patch.object(
            scanner, "get_dexscreener_client", return_value=market_client
        ):
            tokens = scanner.fetch_okx_migrated_tokens()

        self.assertEqual([(token.chain, token.symbol) for token in tokens], [("bsc", "BSC"), ("robinhood", "RH")])
        self.assertTrue(all(token.metrics_complete for token in tokens))
        self.assertEqual([token.volume_24h for token in tokens], [300_000.0, 800_000.0])
        self.assertTrue(all(token.raw["market_source"] == "dexscreener" for token in tokens))

    def test_web_meme_row_preserves_discovery_context_and_uses_dexscreener_metrics(self) -> None:
        web_client = Mock()
        web_client.list_migrated.side_effect = [
            [{"ca": "0xBSC", "smbl": "WEB", "name": "Web Token", "mcap": "700000", "vol1h": "120000", "_okx_discovery_source": "web_meme_ranking"}],
            [],
        ]
        market_client = Mock()
        market_client.price_info.return_value = {"0xbsc": pair("0xbsc", 710_000, 320_000, 100_000)}
        with patch.object(scanner, "get_okx_meme_web_client", return_value=web_client), patch.object(
            scanner, "get_dexscreener_client", return_value=market_client
        ):
            tokens = scanner.fetch_okx_migrated_tokens()

        current = tokens[0]
        self.assertEqual((current.chain, current.symbol, current.name), ("bsc", "WEB", "Web Token"))
        self.assertEqual(current.raw["okx_discovery_source"], "web_meme_ranking")
        self.assertEqual(current.raw["volume_1h"], "120000")
        self.assertEqual((current.market_cap, current.volume_24h), (710_000.0, 320_000.0))

    def test_tracking_uses_dexscreener_without_official_or_gmgn_clients(self) -> None:
        address = "0x23f1ad82bdb58f7524b6e76bdf5406267ef24413"
        market_client = Mock()
        market_client.price_info.return_value = {
            address: {**pair(address, 1_300_000, 900_000, 200_000, name="Dolores", symbol="DOLORES"), "chainId": "robinhood"}
        }
        with patch.object(scanner, "get_dexscreener_client", return_value=market_client), patch.object(
            scanner, "fetch_gmgn_token_info"
        ) as gmgn_client:
            current = scanner.fetch_okx_token_info(address, "robinhood", resolve_identity=True)

        self.assertIsNotNone(current)
        self.assertEqual((current.name, current.symbol, current.market_cap, current.volume_24h), ("Dolores", "DOLORES", 1_300_000.0, 900_000.0))
        self.assertEqual(current.raw["market_source"], "dexscreener")
        gmgn_client.assert_not_called()

    def test_robinhood_uses_one_million_first_market_cap_level(self) -> None:
        self.assertEqual(scanner.market_cap_gate("bsc"), 500_000.0)
        self.assertEqual(scanner.market_cap_gate("robinhood"), 1_000_000.0)
        self.assertEqual(scanner.milestone_level(900_000, 1_100_000, "robinhood"), 1_000_000.0)
        self.assertIsNone(scanner.milestone_level(400_000, 900_000, "robinhood"))


if __name__ == "__main__":
    unittest.main()
