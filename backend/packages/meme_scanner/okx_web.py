from __future__ import annotations

import os
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse


OKX_MEME_PAGE_URL = "https://web3.okx.com/zh-hans/meme-pump"
OKX_MEME_RANKING_PATH = "/priapi/v1/dx/market/v2/memefun/meme-ranking/content"
OKX_MEME_CHAIN_IDS = {"bsc": "56", "robinhood": "4663"}
OKX_MEME_CHAIN_LABELS = {"bsc": "BNB Chain", "robinhood": "Robinhood"}
DEFAULT_PAGE_SETTLE_MS = 5_000
DEFAULT_ACTION_TIMEOUT_SECONDS = 45


class OKXMemeWebError(RuntimeError):
    """A browser or business error from the public OKX MemePump page."""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def parse_meme_ranking_response(payload: Any) -> list[dict[str, Any]]:
    """Validate one web response and return its token rows."""
    if not isinstance(payload, dict):
        raise OKXMemeWebError("OKX MemePump web response is not an object")
    if str(payload.get("code")) != "0":
        raise OKXMemeWebError(
            f"OKX MemePump web code {payload.get('code')}: "
            f"{payload.get('msg') or payload.get('error_message') or 'unknown error'}"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise OKXMemeWebError("OKX MemePump web response has no token list")
    return [item for item in data if isinstance(item, dict)]


class OKXMemeWebClient:
    """Playwright client for OKX's public MemePump web data request.

    The page, rather than this client, produces the dynamic ``ok-verify-*``
    headers. No API key, login, cookie, or header value is copied into Python.
    """

    def __init__(
        self,
        *,
        page_url: str | None = None,
        timeout: int | None = None,
        settle_ms: int | None = None,
    ) -> None:
        self.page_url = page_url or os.getenv("MEME_OKX_WEB_PAGE_URL") or OKX_MEME_PAGE_URL
        timeout_value = timeout if timeout is not None else os.getenv("MEME_OKX_WEB_TIMEOUT_SECONDS")
        settle_value = settle_ms if settle_ms is not None else os.getenv("MEME_OKX_WEB_SETTLE_MS")
        self.timeout = max(5, int(timeout_value or DEFAULT_ACTION_TIMEOUT_SECONDS))
        self.settle_ms = max(0, int(settle_value or DEFAULT_PAGE_SETTLE_MS))
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._latest: dict[str, list[dict[str, Any]]] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.RLock()

    def _start(self) -> None:
        if self._page is not None and not self._page.is_closed():
            return
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": _env_bool("MEME_OKX_WEB_HEADLESS", False),
        }
        proxy = os.getenv("MEME_OKX_WEB_PROXY")
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage"]
        try:
            self._browser = self._playwright.chromium.launch(**launch_kwargs)
            self._context = self._browser.new_context(
                locale=os.getenv("MEME_OKX_WEB_LOCALE") or "zh-CN",
                timezone_id=os.getenv("MEME_OKX_WEB_TIMEZONE") or "Asia/Shanghai",
                viewport={"width": 1440, "height": 900},
            )
            self._page = self._context.new_page()
            self._page.on("response", self._on_response)
            self._page.goto(self.page_url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            self._page.wait_for_timeout(self.settle_ms)
        except Exception:
            self.close()
            raise

    def _on_response(self, response: Any) -> None:
        if OKX_MEME_RANKING_PATH not in response.url:
            return
        query = parse_qs(urlparse(response.url).query)
        if query.get("rankType", [""])[-1] != "4":
            return
        chain_id = query.get("chainId", [""])[-1]
        chain = next((name for name, value in OKX_MEME_CHAIN_IDS.items() if value == chain_id), None)
        if chain is None:
            return
        try:
            rows = parse_meme_ranking_response(response.json())
        except Exception as exc:
            self._errors[chain] = str(exc)
            return
        self._latest[chain] = rows
        self._errors.pop(chain, None)

    def _selected_chain(self) -> str | None:
        if self._page is None or self._page.is_closed():
            return None
        try:
            chain_id = self._page.evaluate(
                """() => {
                    try {
                        const raw = localStorage.getItem('ok_universe_swap');
                        return raw ? JSON.parse(raw).MEME_PUMP_SELECTED_CHAIN_STORAGE : null;
                    } catch (e) { return null; }
                }"""
            )
        except Exception:
            return None
        return next((name for name, value in OKX_MEME_CHAIN_IDS.items() if str(chain_id) == value), None)

    def _click_chain(self, chain: str) -> None:
        label = OKX_MEME_CHAIN_LABELS[chain]
        image = self._page.locator(f'img[alt="{label}"]:visible').first
        if image.count() == 0:
            raise OKXMemeWebError(f"OKX MemePump page has no visible {label} chain control")
        try:
            image.locator("xpath=ancestor::button[1]").click(force=True, timeout=3_000)
        except Exception:
            # The page sometimes leaves a tooltip/virtual-list layer over the
            # shortcut. The page-owned handler below is the same action without
            # depending on the overlay hit-testing result.
            return

    def _invoke_page_chain_handler(self, chain: str) -> None:
        """Retry the same page-owned handler when a virtualized control misses a click."""
        label = OKX_MEME_CHAIN_LABELS[chain]
        self._page.evaluate(
            """label => {
                const image = [...document.querySelectorAll('img[alt]')]
                    .find(node => node.alt === label && node.getBoundingClientRect().width > 0);
                const button = image && image.closest('button');
                const key = button && Object.keys(button).find(name => name.startsWith('__reactProps'));
                const handler = key && button[key] && button[key].onClick;
                if (typeof handler !== 'function') throw new Error(`no handler for ${label}`);
                handler();
            }""",
            label,
        )

    def _wait_for_chain(self, chain: str, timeout: float) -> None:
        deadline = time.monotonic() + max(timeout, 0.1)
        while chain not in self._latest and time.monotonic() < deadline:
            self._page.wait_for_timeout(250)
        if chain in self._latest:
            return
        detail = self._errors.get(chain) or "no rankType=4 response"
        raise OKXMemeWebError(f"OKX MemePump web discovery timed out for {chain}: {detail}")

    def _refresh_current_chain(self, chain: str) -> None:
        self._latest.pop(chain, None)
        self._errors.pop(chain, None)
        current = self._selected_chain()
        if current == chain:
            self._page.reload(wait_until="domcontentloaded", timeout=self.timeout * 1000)
            self._page.wait_for_timeout(min(self.settle_ms, 2_000))
        else:
            self._click_chain(chain)
        if chain in self._latest:
            return
        # On some OKX page builds the first React click only persists the chain
        # in localStorage; invoking the page's own handler once more starts the
        # rank requests. This still uses the browser-generated request headers.
        try:
            self._invoke_page_chain_handler(chain)
        except Exception as exc:
            if chain not in self._latest:
                raise OKXMemeWebError(f"OKX MemePump chain control failed for {chain}: {exc}") from exc
        self._wait_for_chain(chain, self.timeout)

    def list_migrated(self, chain: str, *, limit: int = 30) -> list[dict[str, Any]]:
        chain = str(chain).strip().lower()
        if chain not in OKX_MEME_CHAIN_IDS:
            raise OKXMemeWebError(f"OKX MemePump web discovery does not support chain {chain}")
        with self._lock:
            try:
                self._start()
                self._refresh_current_chain(chain)
                requested = min(max(int(limit), 1), 30)
                rows: list[dict[str, Any]] = []
                for item in self._latest[chain][:requested]:
                    row = dict(item)
                    row["_okx_discovery_source"] = "web_meme_ranking"
                    row["_okx_web_chain_id"] = OKX_MEME_CHAIN_IDS[chain]
                    rows.append(row)
                return rows
            except OKXMemeWebError:
                raise
            except Exception as exc:
                self.close()
                raise OKXMemeWebError(f"OKX MemePump web discovery failed for {chain}: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            for resource in (self._context, self._browser, self._playwright):
                if resource is None:
                    continue
                try:
                    resource.close() if resource is not self._playwright else resource.stop()
                except Exception:
                    pass
            self._page = self._context = self._browser = self._playwright = None
            self._latest.clear()
            self._errors.clear()
