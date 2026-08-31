from __future__ import annotations

import unittest

from packages.meme_scanner.okx_web import OKXMemeWebError, parse_meme_ranking_response


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


if __name__ == "__main__":
    unittest.main()
