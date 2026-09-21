"""One shared advisory lock for short-lived Meme browser jobs.

Both the public OKX page and the authenticated FOMO page use Chromium.  The
production host is intentionally small, so callers take this lock around the
entire browser lifetime rather than allowing their renderer processes to
overlap.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from packages.common.paths import get_paths


class BrowserLockTimeout(RuntimeError):
    """Raised when another local Meme browser owns the shared lock."""


_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}


def browser_lock_path() -> Path:
    configured = str(os.getenv("MEME_BROWSER_LOCK_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (get_paths().processed_dir / "meme_browser.lock").resolve()


def _process_lock(path: Path) -> threading.Lock:
    with _LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(path, threading.Lock())


def _acquire_file_lock(handle: object, *, deadline: float) -> None:
    """Use flock on production Linux; the process lock covers Windows tests."""
    try:
        import fcntl
    except ImportError:
        return
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise BrowserLockTimeout("Meme browser is busy")
            time.sleep(0.1)


def _release_file_lock(handle: object) -> None:
    try:
        import fcntl
    except ImportError:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
    except OSError:
        pass


@contextmanager
def exclusive_browser(*, timeout_seconds: float = 60, path: Path | None = None) -> Iterator[Path]:
    """Acquire the shared local browser lock for a bounded period.

    The caller owns lifecycle cleanup for Chromium itself.  This helper only
    serializes independent processes and threads that use the same lock path.
    """
    resolved = (path or browser_lock_path()).expanduser().resolve()
    timeout = max(float(timeout_seconds), 0.0)
    deadline = time.monotonic() + timeout
    process_lock = _process_lock(resolved)
    if not process_lock.acquire(timeout=timeout):
        raise BrowserLockTimeout("Meme browser is busy")

    handle = None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        handle = resolved.open("a+", encoding="utf-8")
        _acquire_file_lock(handle, deadline=deadline)
        yield resolved
    finally:
        if handle is not None:
            _release_file_lock(handle)
            handle.close()
        process_lock.release()
