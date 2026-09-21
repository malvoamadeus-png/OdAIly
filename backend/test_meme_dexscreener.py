from __future__ import annotations

import unittest
from unittest.mock import Mock

from packages.meme_scanner.dexscreener import DexScreenerClient


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class DexScreenerAdapterTests(unittest.TestCase):
    def test_chooses_liquid_complete_pair_for_exact_base_token(self) -> None:
        session = Mock()
        session.get.return_value = FakeResponse(
            [
                {"chainId": "bsc", "baseToken": {"address": "0xabc"}, "marketCap": 900_000, "volume": {"h24": 400_000}, "liquidity": {"usd": 10_000}},
                {"chainId": "bsc", "baseToken": {"address": "0xabc"}, "marketCap": 800_000, "volume": {"h24": 300_000}, "liquidity": {"usd": 50_000}},
                {"chainId": "bsc", "baseToken": {"address": "0xabc"}, "marketCap": None, "volume": {"h24": 999_999}, "liquidity": {"usd": 999_999}},
                {"chainId": "bsc", "baseToken": {"address": "0xother"}, "marketCap": 1, "volume": {"h24": 1}},
                {"chainId": "solana", "baseToken": {"address": "0xabc"}, "marketCap": 1, "volume": {"h24": 1}},
            ]
        )

        result = DexScreenerClient(session=session).price_info("bsc", ["0xAbC"])

        self.assertEqual(result["0xabc"]["marketCap"], 800_000)
        self.assertEqual(result["0xabc"]["liquidity"]["usd"], 50_000)
        self.assertIn("/tokens/v1/bsc/0xabc", session.get.call_args.args[0])

    def test_falls_back_to_available_pair_when_no_complete_pair_exists(self) -> None:
        session = Mock()
        session.get.return_value = FakeResponse(
            [
                {"chainId": "robinhood", "baseToken": {"address": "0xabc"}, "marketCap": None, "volume": {}, "liquidity": {"usd": 100}},
                {"chainId": "robinhood", "baseToken": {"address": "0xabc"}, "marketCap": 50, "volume": {}, "liquidity": {"usd": 20}},
            ]
        )

        result = DexScreenerClient(session=session).price_info("robinhood", ["0xabc"])

        self.assertEqual(result["0xabc"]["liquidity"]["usd"], 100)


if __name__ == "__main__":
    unittest.main()
