"""Local, bounded evidence collection for Meme narrative generation.

The evidence bundle deliberately keeps the former versioned interface so the
writer and console audit surface do not need to know where collection runs.
No browser cookie, access token, or raw FOMO API response is persisted.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from packages.common.paths import get_paths

from . import narrative_v2
from .browser_lock import BrowserLockTimeout, exclusive_browser
from .fast_narrative import INTERFACE_VERSION


FOMO_ENTRY_URL = "https://fomo.family/profile/unipcs"
FOMO_NETWORK_IDS = {
    "ethereum": 1,
    "eth": 1,
    "bsc": 56,
    "monad": 143,
    "robinhood": 4663,
    "base": 8453,
    "solana": 1399811149,
}
HEAVY_RESOURCE_TYPES = {"image", "media", "font"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name) or default)
    except ValueError:
        value = default
    value = max(value, minimum)
    return min(value, maximum) if maximum is not None else value


def _float_env(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(float(os.getenv(name) or default), minimum)
    except ValueError:
        return default


def _paths() -> Any:
    return get_paths()


def _telegram_args(contract: str) -> argparse.Namespace:
    paths = _paths()
    return argparse.Namespace(
        contract=contract,
        telegram_config=os.getenv("MEME_NARRATIVE_TELEGRAM_CONFIG") or str(paths.config_dir / "meme_telegram.txt"),
        telegram_session=os.getenv("MEME_NARRATIVE_TELEGRAM_SESSION") or str(
            paths.processed_dir / "meme_telegram_narrative"
        ),
        allowed_chats=os.getenv("MEME_NARRATIVE_TELEGRAM_ALLOWED_CHATS") or str(
            paths.config_dir / "meme_whitelist.txt"
        ),
        dialogs_limit=_int_env("MEME_NARRATIVE_TELEGRAM_DIALOGS_LIMIT", 250, minimum=1),
        proxy=os.getenv("MEME_NARRATIVE_TELEGRAM_PROXY") or "auto",
        telegram_timeout=_int_env("MEME_NARRATIVE_TELEGRAM_TIMEOUT_SECONDS", 20, minimum=1),
        connection_retries=_int_env("MEME_NARRATIVE_TELEGRAM_CONNECTION_RETRIES", 3, minimum=1),
    )


def _run_coroutine(coroutine: Any) -> Any:
    """Run collection safely even when a caller already owns an event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[Any] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - loop-hosted callers are rare.
            errors.append(exc)

    thread = threading.Thread(target=run, name="meme-telegram-evidence", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0]


def _contract_in_text(text: str, contract: str) -> bool:
    if contract.lower().startswith("0x"):
        return contract.casefold() in text.casefold()
    return contract in text


def _telegram_url(context: dict[str, Any], message_id: int) -> str:
    username = str(context.get("chat_username") or "").strip().lstrip("@")
    return f"https://t.me/{username}/{message_id}" if username and message_id > 0 else ""


def _collect_telegram(contract: str) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="meme-telegram-") as directory:
        output = Path(directory) / "contexts.json"
        result = _run_coroutine(narrative_v2.collect_telegram_contexts(_telegram_args(contract), output))
    contexts = result.get("contexts") if isinstance(result, dict) and isinstance(result.get("contexts"), list) else []
    evidence: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for context in contexts:
        if not isinstance(context, dict):
            continue
        chat_title = str(context.get("chat_title") or "")
        for message in context.get("context") or []:
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            message_id = int(message.get("message_id") or 0)
            key = (chat_title, message_id)
            if not text or message_id <= 0 or key in seen or not _contract_in_text(text, contract):
                continue
            seen.add(key)
            evidence.append(
                {
                    "id": f"telegram:{len(evidence) + 1}",
                    "source": "telegram",
                    "statement": text,
                    "observedAt": str(message.get("sent_at") or context.get("sent_at") or ""),
                    "url": _telegram_url(context, message_id),
                    "metadata": {"messageId": message_id},
                }
            )
    return {
        "evidence": evidence,
        "contexts": contexts,
        "diagnostic": {
            "stage": "telegram_collection",
            "status": "success" if evidence else "empty",
            "contextCount": len(contexts),
            "itemCount": len(evidence),
            "durationMs": round((time.perf_counter() - started) * 1000),
        },
    }


def _collect_x(contract: str) -> dict[str, Any]:
    started = time.perf_counter()
    result = narrative_v2.collect_x_posts_resilient(
        contract,
        timeout=_int_env("MEME_NARRATIVE_X_TIMEOUT_SECONDS", 5, minimum=1),
    )
    posts = result.get("posts") if isinstance(result, dict) and isinstance(result.get("posts"), list) else []
    evidence: list[dict[str, Any]] = []
    for post in posts:
        if not isinstance(post, dict):
            continue
        statement = str(post.get("text") or "").strip()
        item_id = str(post.get("id") or "").strip()
        if not statement or not item_id:
            continue
        evidence.append(
            {
                "id": item_id,
                "source": "x",
                "statement": statement,
                "observedAt": str(post.get("timestamp") or ""),
                "url": str(post.get("url") or ""),
                "metadata": {
                    "author": str(post.get("author") or ""),
                    "likes": post.get("likes"),
                    "reposts": post.get("reposts"),
                },
            }
        )
    upstream = result.get("diagnostic") if isinstance(result, dict) and isinstance(result.get("diagnostic"), dict) else {}
    return {
        "evidence": evidence,
        "contexts": [],
        "diagnostic": {
            "stage": "x_ca_collection",
            "status": "partial" if upstream.get("degraded") else ("success" if evidence else "empty"),
            "code": "upstream_degraded" if upstream.get("degraded") else "",
            "itemCount": len(evidence),
            "durationMs": round((time.perf_counter() - started) * 1000),
        },
    }


def _fomo_profile_dir() -> Path:
    configured = str(os.getenv("MEME_FOMO_PROFILE_DIR") or "").strip()
    return Path(configured).expanduser() if configured else _paths().processed_dir / "meme_fomo_profile"


def _ensure_profile_dir(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        profile_dir.chmod(0o700)


def _browser_lock_timeout_seconds() -> float:
    return _float_env("MEME_BROWSER_LOCK_TIMEOUT_SECONDS", 90, minimum=1.0)


def _remaining_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _bounded_timeout_ms(configured_ms: int, deadline: float | None) -> int:
    """Keep one FOMO browser action inside the evidence bundle deadline."""
    remaining = _remaining_seconds(deadline)
    if remaining is None:
        return max(int(configured_ms), 1)
    if remaining <= 0:
        return 0
    return max(1, min(int(configured_ms), int(remaining * 1000)))


def _looks_like_login_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = parsed.path.casefold()
    return any(part in path for part in ("login", "signin", "sign-in", "auth"))


def _fomo_page_fetch(page: Any, *, contract: str, network_id: int, last_id: str | None, timeout_ms: int) -> dict[str, Any]:
    return page.evaluate(
        """async input => {
          let moduleUrl = window.__ODAILY_FOMO_FETCH_MODULE_URL__ || performance.getEntriesByType('resource')
            .map(entry => entry.name)
            .find(name => /\\/assets\\/fomoFetch-v2-[A-Za-z0-9_-]+\\.js(?:\\?|$)/.test(name));
          if (!moduleUrl) {
            moduleUrl = [...document.querySelectorAll('link[rel="modulepreload"]')]
              .map(link => link.href)
              .find(href => /\\/assets\\/fomoFetch-v2-[A-Za-z0-9_-]+\\.js(?:\\?|$)/.test(href));
          }
          if (!moduleUrl) {
            const manifestUrl = [...document.querySelectorAll('link[rel="modulepreload"]')]
              .map(link => link.href)
              .find(href => /\\/assets\\/manifest-[A-Za-z0-9_-]+\\.js(?:\\?|$)/.test(href));
            if (manifestUrl) {
              try {
                const manifest = await fetch(manifestUrl, {credentials: 'same-origin'}).then(response => response.text());
                const match = manifest.match(/\\/assets\\/fomoFetch-v2-[A-Za-z0-9_-]+\\.js/);
                if (match) moduleUrl = new URL(match[0], location.origin).href;
              } catch (_) {}
            }
          }
          if (!moduleUrl) return {nativeAvailable: false, code: 'module_unavailable'};
          window.__ODAILY_FOMO_FETCH_MODULE_URL__ = moduleUrl;
          try {
            const api = await import(moduleUrl);
            if (typeof api.f !== 'function') return {nativeAvailable: false, code: 'module_unavailable'};
            const query = new URLSearchParams({
              tokenAddress: input.contract,
              networkId: String(input.networkId),
              limit: String(input.limit),
              threshold: '1000',
            });
            if (input.lastId) query.set('lastId', input.lastId);
            const body = await api.f(`/feed/token/thesis?${query}`, {timeoutMs: input.timeoutMs});
            const status = Number(body?.statusCode) || (body?.success ? 200 : 502);
            return {nativeAvailable: true, ok: body?.success === true, status, body};
          } catch (error) {
            return {
              nativeAvailable: true,
              ok: false,
              status: null,
              code: /unauthori[sz]ed|not.?authenticated|login|session.*expired/i.test(String(error?.message || error))
                ? 'login_required'
                : 'request_failed',
            };
          }
        }""",
        {
            "contract": contract,
            "networkId": network_id,
            "lastId": last_id,
            "limit": _int_env("MEME_FOMO_PAGE_SIZE", 25, minimum=1, maximum=50),
            "timeoutMs": timeout_ms,
        },
    )


def _fomo_items(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict):
        return []
    payload = body.get("responseObject") or body.get("data") or body
    if not isinstance(payload, dict):
        return []
    return [item for item in payload.get("items") or [] if isinstance(item, dict)]


def _fomo_has_next(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    payload = body.get("responseObject") or body.get("data") or body
    return bool(payload.get("hasNextPage")) if isinstance(payload, dict) else False


def _fomo_login_status(response: dict[str, Any]) -> bool:
    status = response.get("status")
    return response.get("code") == "login_required" or status in {401, 403, 430, 431}


def _block_heavy_resources(route: Any) -> None:
    if route.request.resource_type in HEAVY_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _collect_fomo(chain: str, contract: str, *, deadline: float | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    if not _env_bool("MEME_FOMO_ENABLED", True):
        return {
            "evidence": [],
            "contexts": [],
            "diagnostic": {"stage": "fomo_thesis_collection", "status": "disabled", "code": "disabled", "itemCount": 0, "durationMs": 0},
        }
    network_id = FOMO_NETWORK_IDS.get(chain.strip().lower())
    if network_id is None:
        return {
            "evidence": [],
            "contexts": [],
            "diagnostic": {"stage": "fomo_thesis_collection", "status": "unsupported", "code": "unsupported_chain", "itemCount": 0, "durationMs": 0},
        }
    profile_dir = _fomo_profile_dir()
    if not profile_dir.exists():
        return {
            "evidence": [],
            "contexts": [],
            "diagnostic": {
                "stage": "fomo_thesis_collection",
                "status": "login_required",
                "code": "login_required",
                "itemCount": 0,
                "durationMs": round((time.perf_counter() - started) * 1000),
            },
        }

    remaining = _remaining_seconds(deadline)
    if remaining is not None and remaining <= 0:
        return _fomo_error("collection_timeout", started)

    context: Any = None
    try:
        lock_timeout = _browser_lock_timeout_seconds()
        if remaining is not None:
            lock_timeout = min(lock_timeout, remaining)
        with exclusive_browser(timeout_seconds=lock_timeout):
            remaining = _remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                return _fomo_error("collection_timeout", started)
            from playwright.sync_api import sync_playwright

            _ensure_profile_dir(profile_dir)
            with sync_playwright() as playwright:
                # Playwright startup can take enough time to consume the
                # remaining bundle budget. Do not launch Chromium after the
                # narrative collector has already timed out.
                remaining = _remaining_seconds(deadline)
                if remaining is not None and remaining <= 0:
                    return _fomo_error("collection_timeout", started)
                launch_kwargs: dict[str, Any] = {
                    # FOMO renders only its shell in headless Chromium, which
                    # leaves the Privy runtime unavailable to its own fetch module.
                    # The scanner systemd unit already provides an isolated Xvfb display.
                    "headless": _env_bool("MEME_FOMO_HEADLESS", False),
                    "viewport": {"width": 1440, "height": 900},
                    "locale": "en-US",
                    "timezone_id": "Asia/Shanghai",
                }
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    launch_kwargs["args"] = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
                context = playwright.chromium.launch_persistent_context(str(profile_dir), **launch_kwargs)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.route("**/*", _block_heavy_resources)
                    navigation_timeout_ms = _bounded_timeout_ms(
                        _int_env("MEME_FOMO_NAVIGATION_TIMEOUT_SECONDS", 12, minimum=1) * 1000,
                        deadline,
                    )
                    if navigation_timeout_ms <= 0:
                        return _fomo_error("collection_timeout", started)
                    page.set_default_timeout(navigation_timeout_ms)
                    page.goto(
                        FOMO_ENTRY_URL,
                        wait_until="domcontentloaded",
                        timeout=navigation_timeout_ms,
                    )
                    settle_timeout_ms = _bounded_timeout_ms(500, deadline)
                    if settle_timeout_ms <= 0:
                        return _fomo_error("collection_timeout", started)
                    page.wait_for_timeout(settle_timeout_ms)
                    if _looks_like_login_url(page.url):
                        return {
                            "evidence": [],
                            "contexts": [],
                            "diagnostic": {
                                "stage": "fomo_thesis_collection",
                                "status": "login_required",
                                "code": "login_required",
                                "itemCount": 0,
                                "durationMs": round((time.perf_counter() - started) * 1000),
                            },
                        }
                    max_pages = _int_env("MEME_FOMO_MAX_PAGES", 1, minimum=1, maximum=5)
                    items: list[dict[str, Any]] = []
                    last_id: str | None = None
                    for _ in range(max_pages):
                        page_timeout_ms = _bounded_timeout_ms(
                            _int_env("MEME_FOMO_REQUEST_TIMEOUT_SECONDS", 12, minimum=1) * 1000,
                            deadline,
                        )
                        if page_timeout_ms <= 0:
                            return _fomo_error("collection_timeout", started)
                        page.set_default_timeout(page_timeout_ms)
                        response = _fomo_page_fetch(
                            page,
                            contract=contract,
                            network_id=network_id,
                            last_id=last_id,
                            timeout_ms=page_timeout_ms,
                        )
                        if not isinstance(response, dict):
                            return _fomo_error("invalid_response", started)
                        if _fomo_login_status(response):
                            return _fomo_login_required_result(started)
                        if not response.get("nativeAvailable"):
                            return _fomo_error("module_unavailable", started)
                        if not response.get("ok"):
                            return _fomo_error(str(response.get("code") or "request_failed"), started)
                        page_items = _fomo_items(response.get("body"))
                        items.extend(page_items)
                        if not _fomo_has_next(response.get("body")) or not page_items:
                            break
                        next_id = str(page_items[-1].get("id") or "").strip()
                        if not next_id or next_id == last_id:
                            break
                        last_id = next_id
                finally:
                    if context is not None:
                        context.close()
                        context = None
    except BrowserLockTimeout:
        return _fomo_error("browser_busy", started)
    except Exception:
        return _fomo_error("browser_failed", started)

    evidence: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        statement = _fomo_statement(item)
        source_id = str(item.get("id") or "").strip()
        item_id = f"fomo_thesis:{source_id}" if source_id else f"fomo_thesis:{len(evidence) + 1}"
        if not statement or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        evidence.append(
            {
                "id": item_id,
                "source": "fomo_thesis",
                "statement": statement,
                "observedAt": str(item.get("createdAt") or item.get("created_at") or ""),
                "url": "",
                "metadata": {"thesisId": source_id} if source_id else {},
            }
        )
    return {
        "evidence": evidence,
        "contexts": [],
        "diagnostic": {
            "stage": "fomo_thesis_collection",
            "status": "success" if evidence else "empty",
            "itemCount": len(evidence),
            "durationMs": round((time.perf_counter() - started) * 1000),
        },
    }


def _fomo_statement(item: dict[str, Any]) -> str:
    """Extract public thesis text without serializing nested response objects."""
    for key in ("comment", "content", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for nested_key in ("comment", "content", "text"):
                nested = value.get(nested_key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return ""


def _fomo_login_required_result(started: float) -> dict[str, Any]:
    return {
        "evidence": [],
        "contexts": [],
        "diagnostic": {
            "stage": "fomo_thesis_collection",
            "status": "login_required",
            "code": "login_required",
            "itemCount": 0,
            "durationMs": round((time.perf_counter() - started) * 1000),
        },
    }


def _fomo_error(code: str, started: float) -> dict[str, Any]:
    return {
        "evidence": [],
        "contexts": [],
        "diagnostic": {
            "stage": "fomo_thesis_collection",
            "status": "error",
            "code": re.sub(r"[^a-z0-9_]+", "_", code.casefold())[:64] or "request_failed",
            "itemCount": 0,
            "durationMs": round((time.perf_counter() - started) * 1000),
        },
    }


class LocalFastEvidenceAdapter:
    """Collect the three public/owned Meme evidence sources in bounded parallelism."""

    def __init__(self, *, timeout_seconds: int = 45) -> None:
        self.timeout_seconds = max(1, int(timeout_seconds))

    def collect(self, *, chain: str, contract: str, symbol: str, request_id: str) -> dict[str, Any]:
        del symbol, request_id
        started = time.perf_counter()
        deadline = time.monotonic() + self.timeout_seconds
        workers = {
            "telegram": lambda: _collect_telegram(contract),
            "x": lambda: _collect_x(contract),
        }
        results: dict[str, dict[str, Any]] = {}
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="meme-fast-evidence")
        try:
            futures = {executor.submit(worker): source for source, worker in workers.items()}
            try:
                results["fomo_thesis"] = _collect_fomo(chain, contract, deadline=deadline)
            except Exception:
                results["fomo_thesis"] = _source_error("fomo_thesis", "collection_failed")
            completed, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
            for future in completed:
                source = futures[future]
                try:
                    result = future.result()
                    results[source] = result if isinstance(result, dict) else _source_error(source, "invalid_result")
                except Exception:
                    results[source] = _source_error(source, "collection_failed")
            for future in pending:
                results[futures[future]] = _source_error(futures[future], "collection_timeout")
                future.cancel()
        finally:
            # A source has its own network/browser timeout. Do not block the
            # narrative worker beyond the configured bundle deadline if one
            # upstream ignores that timeout.
            executor.shutdown(wait=False, cancel_futures=True)

        evidence: list[dict[str, Any]] = []
        contexts: list[dict[str, Any]] = []
        diagnostics: dict[str, Any] = {}
        for source in ("telegram", "x", "fomo_thesis"):
            result = results.get(source) or _source_error(source, "collection_failed")
            evidence.extend(item for item in result.get("evidence") or [] if isinstance(item, dict))
            if source == "telegram":
                contexts.extend(item for item in result.get("contexts") or [] if isinstance(item, dict))
            diagnostic = result.get("diagnostic") if isinstance(result.get("diagnostic"), dict) else _source_error(source, "collection_failed")["diagnostic"]
            diagnostics[source] = diagnostic

        statuses = {str(item.get("status") or "error") for item in diagnostics.values()}
        technical_errors = {"error"}
        if evidence:
            status = "success" if not (statuses & technical_errors or "login_required" in statuses or "partial" in statuses) else "partial"
        elif statuses == {"empty"} or statuses.issubset({"empty", "disabled", "unsupported"}):
            status = "empty"
        elif "login_required" in statuses:
            status = "partial"
        elif statuses & technical_errors:
            status = "error"
        else:
            status = "partial"
        errors = {
            source: diagnostic
            for source, diagnostic in diagnostics.items()
            if str(diagnostic.get("status") or "") in {"error", "login_required", "partial"}
        }
        return {
            "version": INTERFACE_VERSION,
            "status": status,
            "evidence": evidence,
            "telegramContexts": contexts,
            "sourceDiagnostics": diagnostics,
            "diagnostics": {
                "errors": errors,
                "performance": {
                    "parallelWallDurationMs": round((time.perf_counter() - started) * 1000),
                    "sourceCount": len(diagnostics),
                },
            },
            "decisionReason": "local Meme evidence collection completed",
        }


def _source_error(source: str, code: str) -> dict[str, Any]:
    return {
        "evidence": [],
        "contexts": [],
        "diagnostic": {
            "stage": f"{source}_collection",
            "status": "error",
            "code": code,
            "itemCount": 0,
            "durationMs": 0,
        },
    }
