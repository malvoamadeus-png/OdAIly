from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from packages.meme_scanner.okx_web import (
    OKXMemeWebClient,
    OKXMemeWebError,
    parse_meme_ranking_response,
    solana_launchpad,
)


class OKXMemeWebAdapterTests(unittest.TestCase):
    def test_solana_launchpad_uses_okx_identity_and_pumpfun_only_ca_fallback(self) -> None:
        self.assertEqual(solana_launchpad({"protocolId": "120596", "ca": "solana-address"}), "pumpfun")
        self.assertEqual(solana_launchpad({"protocolId": "141620", "ca": "solana-address"}), "stonkfun")
        self.assertEqual(solana_launchpad({"protoId": "141620", "ca": "solana-address"}), "stonkfun")
        self.assertEqual(solana_launchpad({"protocolName": "Stonkfun", "ca": "solana-address"}), "stonkfun")
        self.assertEqual(solana_launchpad({"ca": "solana-address-pump"}), "pumpfun")
        self.assertIsNone(solana_launchpad({"protocolId": "other", "ca": "solana-address-pump"}))
        self.assertIsNone(solana_launchpad({"ca": "solana-address-PUMP"}))
        self.assertIsNone(solana_launchpad({"protocolId": "other", "ca": "solana-address"}))

    def test_solana_discovery_filters_rows_to_supported_launchpads_and_stamps_platform(self) -> None:
        client = OKXMemeWebClient()
        client._latest["solana"] = [
            {"protocolId": "other", "ca": "ordinary-token"},
            {"protocolId": "120596", "ca": "pumpfun-token"},
            {"protocolId": "141620", "ca": "stonk-token"},
            {"ca": "suffix-token-pump"},
        ]
        client._start = lambda: None
        client._refresh_current_chain = lambda chain: None

        rows = client.list_migrated("solana")

        self.assertEqual([row["ca"] for row in rows], ["pumpfun-token", "stonk-token", "suffix-token-pump"])
        self.assertTrue(all(row["_okx_web_chain_id"] == "501" for row in rows))
        self.assertEqual([row["_okx_platform_filter"] for row in rows], ["pumpfun", "stonkfun", "pumpfun"])

    def test_parse_meme_ranking_response_accepts_business_success(self) -> None:
        rows = parse_meme_ranking_response(
            {
                "code": 0,
                "msg": "",
                "data": [{"ca": "0xabc", "chain": "56", "smbl": "ABC"}],
            }
        )
        self.assertEqual(rows[0]["ca"], "0xabc")

    def test_parse_meme_ranking_response_rejects_business_error(self) -> None:
        with self.assertRaisesRegex(OKXMemeWebError, "incorrect request sign"):
            parse_meme_ranking_response({"code": 50113, "msg": "incorrect request sign parameters", "data": []})

    def test_parse_meme_ranking_response_rejects_missing_list(self) -> None:
        with self.assertRaisesRegex(OKXMemeWebError, "no token list"):
            parse_meme_ranking_response({"code": 0, "data": {}})

    def test_refresh_fetches_explicit_chain_rank_from_page_context(self) -> None:
        client = OKXMemeWebClient()
        client._page = MagicMock()
        client._page.evaluate.return_value = [[{"ca": "0xabc"}]]

        client._refresh_current_chain("bsc")

        self.assertEqual(client._latest["bsc"], [{"ca": "0xabc"}])
        script, args = client._page.evaluate.call_args.args
        self.assertIn("fetch(", script)
        self.assertIn("['120596', '141620']", script)
        self.assertEqual(args, {"chainId": "56", "rankingPath": "/priapi/v1/dx/market/v2/memefun/meme-ranking/content"})

    def test_ranking_response_reads_chain_and_rank_type_from_post_body(self) -> None:
        client = OKXMemeWebClient()
        response = MagicMock()
        response.url = "https://web3.okx.com/priapi/v1/dx/market/v2/memefun/meme-ranking/content/batch?t=1"
        response.request.post_data_json = {
            "rankType": "4",
            "chainQueryList": [{"rankType": "4", "chainId": "56"}],
        }
        response.json.return_value = {"code": 0, "data": [{"ca": "0xabc"}]}

        client._on_response(response)

        self.assertEqual(client._latest["bsc"], [{"ca": "0xabc"}])

    def test_refresh_reuses_initial_page_response_for_requested_chain(self) -> None:
        client = OKXMemeWebClient()
        client._latest["bsc"] = [{"ca": "0xabc"}]
        client._page = MagicMock()

        client._refresh_current_chain("bsc")

        client._page.evaluate.assert_not_called()
        client._page.reload.assert_not_called()

    def test_refresh_fetches_platform_scoped_solana_rows_even_if_initial_rows_exist(self) -> None:
        client = OKXMemeWebClient()
        client._latest["solana"] = [{"ca": "initial-page-row"}]
        client._page = MagicMock()
        client._page.evaluate.return_value = [[{"ca": "pump", "protoId": "120596"}], [{"ca": "stonk", "protoId": "141620"}]]

        client._refresh_current_chain("solana")

        client._page.evaluate.assert_called_once()
        self.assertEqual([row["ca"] for row in client._latest["solana"]], ["pump", "stonk"])

    def test_list_migrated_closes_browser_on_web_error(self) -> None:
        client = OKXMemeWebClient()
        context = MagicMock()
        browser = MagicMock()
        playwright = MagicMock()
        client._context = context
        client._browser = browser
        client._playwright = playwright
        client._page = MagicMock()
        client._start = MagicMock()
        client._refresh_current_chain = MagicMock(side_effect=OKXMemeWebError("selector failed"))

        with self.assertRaisesRegex(OKXMemeWebError, "selector failed"):
            client.list_migrated("bsc")

        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(client._page)
        self.assertIsNone(client._context)
        self.assertIsNone(client._browser)
        self.assertIsNone(client._playwright)


if __name__ == "__main__":
    unittest.main()
