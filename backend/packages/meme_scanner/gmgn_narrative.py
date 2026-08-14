from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any


BASE_URL = "https://gmgn.ai/api/v1/token_ai_narrative"
DEFAULT_CLIENT_ID = "gmgn_web_20260814-3315-6889702"
DEFAULT_APP_VERSION = "20260814-3315-6889702"
DEFAULT_BROWSER_SETTLE_MS = 3000


class GmgnHTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, *, page_status: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.page_status = page_status


def _public_params() -> dict[str, str]:
    device_id = os.getenv("MEME_GMGN_DEVICE_ID") or str(uuid.uuid4())
    fp_did = os.getenv("MEME_GMGN_FP_DID") or str(uuid.uuid4())
    if fp_did == device_id:
        fp_did = str(uuid.uuid4())
    return {
        "device_id": device_id,
        "fp_did": fp_did,
        "client_id": os.getenv("MEME_GMGN_CLIENT_ID") or DEFAULT_CLIENT_ID,
        "from_app": "gmgn",
        "app_ver": os.getenv("MEME_GMGN_APP_VER") or DEFAULT_APP_VERSION,
        "tz_name": os.getenv("MEME_GMGN_TZ_NAME") or "Asia/Shanghai",
        "tz_offset": os.getenv("MEME_GMGN_TZ_OFFSET") or "28800",
        "app_lang": os.getenv("MEME_GMGN_APP_LANG") or "en-US",
        "os": "web",
        "worker": "0",
    }


def _browser_fetch(page_url: str, api_url: str, params: dict[str, str], *, timeout: int, settle_ms: int) -> dict[str, Any]:
    # Import lazily so the rest of the Meme pipeline can still run when the optional
    # browser dependency is unavailable and GMGN is treated as a supplement only.
    from playwright.sync_api import sync_playwright

    proxy = os.getenv("MEME_GMGN_HTTPS_PROXY") or os.getenv("GMGN_HTTPS_PROXY")
    launch_kwargs: dict[str, Any] = {"headless": False}
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(
                locale="en-US",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page_response = page.goto(
                page_url,
                wait_until="domcontentloaded",
                timeout=max(1, timeout) * 1000,
            )
            page.wait_for_timeout(max(0, settle_ms))
            api_result = page.evaluate(
                """
                async ({apiUrl, params, timeoutMs}) => {
                  const started = performance.now();
                  const query = new URLSearchParams(params);
                  const controller = new AbortController();
                  const timer = setTimeout(() => controller.abort(), timeoutMs);
                  try {
                    const response = await fetch(`${apiUrl}?${query.toString()}`, {
                      credentials: "omit",
                      headers: {accept: "application/json, text/plain, */*"},
                      signal: controller.signal
                    });
                    const text = await response.text();
                    let body = null;
                    try { body = JSON.parse(text); } catch {}
                    return {
                      elapsedMs: Math.round(performance.now() - started),
                      status: response.status,
                      body,
                      bodyPreview: body ? null : text.slice(0, 200)
                    };
                  } finally {
                    clearTimeout(timer);
                  }
                }
                """,
                {"apiUrl": api_url, "params": params, "timeoutMs": max(1, timeout) * 1000},
            )
            return {
                "page_status": page_response.status if page_response else None,
                **dict(api_result or {}),
            }
        finally:
            browser.close()


def collect(chain: str, token_address: str, *, timeout: int = 20) -> dict[str, Any]:
    chain = chain.strip().lower()
    token_address = token_address.strip()
    if not chain or not token_address:
        raise ValueError("chain and token_address are required")

    params = _public_params()
    page_url = f"https://gmgn.ai/{chain}/token/{token_address}"
    api_url = f"{BASE_URL}/{chain}/{token_address}"
    started_at = datetime.now().astimezone().isoformat()
    settle_ms = int(os.getenv("MEME_GMGN_BROWSER_SETTLE_MS") or DEFAULT_BROWSER_SETTLE_MS)
    result = _browser_fetch(page_url, api_url, params, timeout=timeout, settle_ms=settle_ms)
    status = int(result.get("status") or 0)
    page_status = result.get("page_status")
    if status >= 400:
        raise GmgnHTTPError(status, f"GMGN narrative returned HTTP {status}", page_status=page_status)
    payload = result.get("body")
    if not isinstance(payload, dict):
        raise RuntimeError("GMGN narrative response is not an object")
    if payload.get("code") not in (0, "0", None):
        raise RuntimeError(f"GMGN narrative returned code {payload.get('code')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("GMGN narrative response has no data object")
    narrative = str(data.get("zh_cn") or "").strip()
    return {
        "narrative": narrative,
        "raw": payload,
        "diagnostic": {
            "stage": "gmgn_narrative",
            "source": "gmgn",
            "browser_mode": "playwright_headed",
            "http_status": status,
            "page_http_status": page_status,
            "api_duration_ms": result.get("elapsedMs"),
            "started_at": started_at,
            "has_zh_cn": bool(narrative),
        },
    }
