from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import MagicMock, patch

from packages.meme_scanner import fast_evidence


ADDRESS = "0x" + "a" * 40


def source_result(source: str, *, status: str, evidence=None, contexts=None, code: str = ""):
    return {
        "evidence": evidence or [],
        "contexts": contexts or [],
        "diagnostic": {
            "stage": f"{source}_collection",
            "status": status,
            "code": code,
            "itemCount": len(evidence or []),
            "durationMs": 1,
        },
    }


def test_local_adapter_preserves_versioned_bundle_and_partial_fomo_login():
    telegram = source_result(
        "telegram",
        status="success",
        evidence=[
            {
                "id": "telegram:1",
                "source": "telegram",
                "statement": f"CA {ADDRESS} has a concrete meme angle",
                "observedAt": "2026-09-01T00:00:00Z",
                "url": "https://t.me/test/1",
                "metadata": {"messageId": 1},
            }
        ],
        contexts=[{"chat_title": "test", "context": []}],
    )
    x_source = source_result("x", status="empty")
    fomo = source_result("fomo_thesis", status="login_required", code="login_required")
    adapter = fast_evidence.LocalFastEvidenceAdapter()

    with patch.object(fast_evidence, "_collect_telegram", return_value=telegram), patch.object(
        fast_evidence, "_collect_x", return_value=x_source
    ), patch.object(fast_evidence, "_collect_fomo", return_value=fomo):
        bundle = adapter.collect(chain="bsc", contract=ADDRESS, symbol="TEST", request_id="test")

    assert bundle["version"] == "2026-09-01"
    assert bundle["status"] == "partial"
    assert bundle["evidence"][0]["source"] == "telegram"
    assert bundle["telegramContexts"] == telegram["contexts"]
    assert bundle["sourceDiagnostics"]["fomo_thesis"]["code"] == "login_required"
    assert bundle["diagnostics"]["errors"]["fomo_thesis"]["status"] == "login_required"


def test_missing_fomo_profile_requests_login_without_starting_browser(tmp_path):
    missing_profile = tmp_path / "missing-profile"
    with patch.object(fast_evidence, "_fomo_profile_dir", return_value=missing_profile):
        result = fast_evidence._collect_fomo("bsc", ADDRESS)

    assert result["evidence"] == []
    assert result["diagnostic"]["status"] == "login_required"
    assert result["diagnostic"]["code"] == "login_required"
    assert not missing_profile.exists()


def test_expired_fomo_collection_deadline_never_starts_a_browser(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    with patch.object(fast_evidence, "_fomo_profile_dir", return_value=profile), patch.object(
        fast_evidence, "exclusive_browser"
    ) as browser_lock:
        result = fast_evidence._collect_fomo("bsc", ADDRESS, deadline=fast_evidence.time.monotonic() - 1)

    assert result["diagnostic"]["code"] == "collection_timeout"
    browser_lock.assert_not_called()


def test_fomo_does_not_launch_chromium_when_playwright_startup_uses_the_remaining_budget(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    playwright = MagicMock()
    with patch.object(fast_evidence, "_fomo_profile_dir", return_value=profile), patch.object(
        fast_evidence, "_remaining_seconds", side_effect=(1.0, 1.0, 0.0)
    ), patch.object(fast_evidence, "exclusive_browser", return_value=nullcontext()), patch(
        "playwright.sync_api.sync_playwright", return_value=nullcontext(playwright)
    ):
        result = fast_evidence._collect_fomo("bsc", ADDRESS, deadline=1.0)

    assert result["diagnostic"]["code"] == "collection_timeout"
    playwright.chromium.launch_persistent_context.assert_not_called()


def test_fomo_uses_headful_chromium_by_default_for_the_privy_runtime(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    page = MagicMock()
    page.url = fast_evidence.FOMO_ENTRY_URL
    browser_context = MagicMock()
    browser_context.pages = [page]
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context.return_value = browser_context
    monkeypatch.delenv("MEME_FOMO_HEADLESS", raising=False)

    with patch.object(fast_evidence, "_fomo_profile_dir", return_value=profile), patch.object(
        fast_evidence, "exclusive_browser", return_value=nullcontext()
    ), patch("playwright.sync_api.sync_playwright", return_value=nullcontext(playwright)), patch.object(
        fast_evidence,
        "_fomo_page_fetch",
        return_value={"nativeAvailable": True, "ok": False, "status": 401},
    ):
        result = fast_evidence._collect_fomo("bsc", ADDRESS)

    assert result["diagnostic"]["status"] == "login_required"
    assert playwright.chromium.launch_persistent_context.call_args.kwargs["headless"] is False
    browser_context.close.assert_called_once()


def test_fomo_nested_comment_uses_only_comment_text():
    assert fast_evidence._fomo_statement({"comment": {"comment": "Concrete thesis text"}}) == "Concrete thesis text"
    assert fast_evidence._fomo_statement({"comment": {"author": "private"}}) == ""


def test_local_adapter_never_exposes_source_exception_text():
    adapter = fast_evidence.LocalFastEvidenceAdapter()
    with patch.object(fast_evidence, "_collect_telegram", side_effect=RuntimeError("private-token-value")), patch.object(
        fast_evidence, "_collect_x", return_value=source_result("x", status="empty")
    ), patch.object(fast_evidence, "_collect_fomo", return_value=source_result("fomo_thesis", status="empty")):
        bundle = adapter.collect(chain="bsc", contract=ADDRESS, symbol="TEST", request_id="test")

    assert bundle["status"] == "error"
    assert bundle["sourceDiagnostics"]["telegram"]["code"] == "collection_failed"
    assert "private-token-value" not in repr(bundle)
