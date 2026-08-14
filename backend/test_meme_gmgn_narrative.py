from __future__ import annotations

from packages.meme_scanner import gmgn_narrative


def test_collect_reads_simplified_chinese_narrative(monkeypatch) -> None:
    captured = {}

    def fake_browser_fetch(page_url, api_url, params, *, timeout, settle_ms):
        captured.update(
            {
                "page_url": page_url,
                "api_url": api_url,
                "params": params,
                "timeout": timeout,
                "settle_ms": settle_ms,
            }
        )
        return {
            "page_status": 200,
            "status": 200,
            "elapsedMs": 317,
            "body": {
                "code": 0,
                "message": "success",
                "data": {"zh_cn": "这是简体中文叙事", "en": "English narrative"},
            },
        }

    monkeypatch.setattr(gmgn_narrative, "_browser_fetch", fake_browser_fetch)

    result = gmgn_narrative.collect("bsc", "0xabc", timeout=1)

    assert result["narrative"] == "这是简体中文叙事"
    assert captured["page_url"].endswith("/bsc/token/0xabc")
    assert captured["api_url"].endswith("/bsc/0xabc")
    assert captured["params"]["from_app"] == "gmgn"
    assert captured["params"]["os"] == "web"
    assert captured["params"]["worker"] == "0"
    assert captured["params"]["app_ver"] == "20260814-3315-6889702"
    assert captured["params"]["device_id"] != captured["params"]["fp_did"]
    assert result["diagnostic"]["browser_mode"] == "playwright_headed"
    assert result["diagnostic"]["api_duration_ms"] == 317
