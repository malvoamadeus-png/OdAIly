"""One-shot, localhost-only FOMO profile login helper.

This command deliberately has no daemon or systemd unit. It opens Chromium
only while an operator is actively refreshing the private persistent profile,
and binds DevTools solely to the server loopback interface for an SSH tunnel.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv

from .browser_lock import exclusive_browser
from .fast_evidence import FOMO_ENTRY_URL, _browser_lock_timeout_seconds, _ensure_profile_dir, _fomo_profile_dir


DEFAULT_LOGIN_TIMEOUT_SECONDS = 15 * 60
DEFAULT_CDP_PORT = 9224


def chromium_executable() -> str:
    """Resolve the managed Playwright Chromium without starting a browser."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            executable = Path(playwright.chromium.executable_path)
    except Exception as exc:
        raise RuntimeError("Playwright Chromium is unavailable for FOMO login") from exc
    if not executable.exists():
        raise RuntimeError("Playwright Chromium executable is missing for FOMO login")
    return str(executable)


def chromium_command(*, executable: str, profile_dir: Path, cdp_port: int) -> list[str]:
    command = [
        executable,
        "--remote-debugging-address=127.0.0.1",
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        FOMO_ENTRY_URL,
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")
    return command


def _wait_for_cdp(process: subprocess.Popen[Any], cdp_port: int, deadline: float) -> bool:
    endpoint = f"http://127.0.0.1:{cdp_port}/json/version"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urlopen(endpoint, timeout=1) as response:
                return 200 <= int(getattr(response, "status", 200)) < 300
        except (OSError, URLError):
            time.sleep(0.25)
    return False


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # pragma: no cover - production login runs on Linux.
            process.terminate()
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:  # pragma: no cover - production login runs on Linux.
                process.kill()
        except OSError:
            pass


def _validated_port(value: int) -> int:
    if not 1024 <= int(value) <= 65535:
        raise ValueError("--cdp-port must be between 1024 and 65535")
    return int(value)


def _assert_loopback_port_available(cdp_port: int) -> None:
    """Fail closed rather than mistaking another local CDP endpoint for ours."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", cdp_port))
        except OSError as exc:
            raise RuntimeError(f"FOMO DevTools port 127.0.0.1:{cdp_port} is already in use") from exc


def run(args: Any) -> int:
    load_dotenv()
    if os.name == "posix" and not os.getenv("DISPLAY"):
        raise RuntimeError("FOMO login needs a local display; run this command under xvfb-run -a")
    timeout_seconds = max(60, int(getattr(args, "timeout", DEFAULT_LOGIN_TIMEOUT_SECONDS)))
    cdp_port = _validated_port(int(getattr(args, "cdp_port", DEFAULT_CDP_PORT)))
    profile_dir = _fomo_profile_dir()
    _ensure_profile_dir(profile_dir)
    command = chromium_command(
        executable=chromium_executable(),
        profile_dir=profile_dir,
        cdp_port=cdp_port,
    )
    process: subprocess.Popen[Any] | None = None
    with exclusive_browser(timeout_seconds=_browser_lock_timeout_seconds()):
        try:
            _assert_loopback_port_available(cdp_port)
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            ready_deadline = min(time.monotonic() + 10, time.monotonic() + timeout_seconds)
            if not _wait_for_cdp(process, cdp_port, ready_deadline):
                raise RuntimeError("FOMO Chromium did not expose its local DevTools endpoint")
            print(f"[meme-fomo-login] DevTools is local-only at 127.0.0.1:{cdp_port}")
            print("[meme-fomo-login] Use an SSH local-port tunnel, finish login, then stop this one-shot command.")
            deadline = time.monotonic() + timeout_seconds
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            if process.poll() is None:
                print("[meme-fomo-login] login window timed out; closing Chromium")
            elif process.returncode not in (0, None):
                raise RuntimeError("FOMO Chromium closed before the login window completed")
            return 0
        except KeyboardInterrupt:
            print("[meme-fomo-login] login window stopped by operator")
            return 0
        finally:
            if process is not None:
                _stop_process(process)
