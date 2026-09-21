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

    def test_click_chain_prefers_visible_chain_shortcut(self) -> None:
        client = OKXMemeWebClient()
        shortcut = MagicMock()
        shortcut.count.return_value = 1
        client._page = MagicMock()
        client._page.locator.return_value = shortcut

        client._click_chain("bsc")

        self.assertEqual(
            client._page.locator.call_args_list[0].args,
            ('button:has(img[alt="BNB Chain"]):visible',),
        )
        shortcut.first.click.assert_called_once_with(force=True, timeout=3_000)

    def test_click_chain_uses_collapsed_selector_when_shortcut_is_hidden(self) -> None:
        client = OKXMemeWebClient()
        shortcut = MagicMock()
        selector = MagicMock()
        option = MagicMock()
        shortcut.count.return_value = 0
        selector.count.return_value = 1
        option.count.return_value = 1
        client._page = MagicMock()
        client._page.locator.side_effect = [shortcut, selector, option]

        client._click_chain("bsc")

        self.assertEqual(
            client._page.locator.call_args_list[1].args,
            ('[data-testid="okd-select-reference-value-box"]:visible',),
        )
        self.assertEqual(
            client._page.locator.call_args_list[2].args,
            ('[role="option"]:has(img[alt="BNB Chain"]):visible',),
        )
        selector.first.click.assert_called_once_with(force=True, timeout=3_000)
        option.first.click.assert_called_once_with(force=True, timeout=3_000)

    def test_click_chain_retries_collapsed_selector_with_dom_click(self) -> None:
        client = OKXMemeWebClient()
        shortcut = MagicMock()
        selector = MagicMock()
        option = MagicMock()
        shortcut.count.return_value = 0
        selector.count.return_value = 1
        option.count.return_value = 1
        option.wait_for.side_effect = [TimeoutError(), None]
        client._page = MagicMock()
        client._page.locator.side_effect = [shortcut, selector, option]

        client._click_chain("bsc")

        selector.first.evaluate.assert_called_once_with("element => element.click()")
        self.assertEqual(option.wait_for.call_count, 2)
        option.first.click.assert_called_once_with(force=True, timeout=3_000)

    def test_click_chain_reports_missing_popup_option(self) -> None:
        client = OKXMemeWebClient()
        shortcut = MagicMock()
        selector = MagicMock()
        option = MagicMock()
        shortcut.count.return_value = 0
        selector.count.return_value = 1
        option.count.return_value = 0
        client._page = MagicMock()
        client._page.locator.side_effect = [shortcut, selector, option]

        with self.assertRaisesRegex(OKXMemeWebError, "has no BNB Chain option"):
            client._click_chain("bsc")

    def test_refresh_reuses_initial_page_response_for_requested_chain(self) -> None:
        client = OKXMemeWebClient()
        client._latest["bsc"] = [{"ca": "0xabc"}]
        client._page = MagicMock()
        client._selected_chain = MagicMock()

        client._refresh_current_chain("bsc")

        client._selected_chain.assert_not_called()
        client._page.reload.assert_not_called()

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
