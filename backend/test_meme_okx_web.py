from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from packages.meme_scanner.okx_web import OKXMemeWebClient, OKXMemeWebError, parse_meme_ranking_response


class OKXMemeWebAdapterTests(unittest.TestCase):
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

    def test_click_chain_uses_collapsed_chain_selector(self) -> None:
        client = OKXMemeWebClient()
        selector = MagicMock()
        option = MagicMock()
        selector.count.return_value = 1
        option.count.return_value = 1
        client._page = MagicMock()
        client._page.locator.side_effect = [selector, option]

        client._click_chain("bsc")

        self.assertEqual(
            client._page.locator.call_args_list[0].args,
            ('[data-testid="okd-select-text"]:visible',),
        )
        self.assertIn('data-testid="okd-select-popup"', client._page.locator.call_args_list[1].args[0])
        self.assertIn('img[alt="BNB Chain"]', client._page.locator.call_args_list[1].args[0])
        selector.evaluate.assert_called_once_with("element => element.click()")
        option.click.assert_called_once_with(force=True, timeout=3_000)

    def test_click_chain_reports_missing_popup_option(self) -> None:
        client = OKXMemeWebClient()
        selector = MagicMock()
        option = MagicMock()
        selector.count.return_value = 1
        option.count.return_value = 0
        client._page = MagicMock()
        client._page.locator.side_effect = [selector, option]

        with self.assertRaisesRegex(OKXMemeWebError, "has no BNB Chain option"):
            client._click_chain("bsc")


if __name__ == "__main__":
    unittest.main()
