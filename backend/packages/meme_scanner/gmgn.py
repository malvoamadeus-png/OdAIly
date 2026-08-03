from __future__ import annotations

import email.utils
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


GMGN = shutil.which("gmgn-cli.cmd") or shutil.which("gmgn-cli") or "gmgn-cli"


def gmgn_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    proxy = os.environ.get("GMGN_HTTPS_PROXY")
    if proxy:
        env["HTTPS_PROXY"] = proxy
        env["HTTP_PROXY"] = proxy
    return env


def _sync_time_offset() -> None:
    if os.environ.get("GMGN_TIME_OFFSET_MS"):
        return
    try:
        request = Request("https://openapi.gmgn.ai/v1/user/info", method="GET")
        try:
            with urlopen(request, timeout=10) as response:
                date_header = response.headers.get("Date")
        except HTTPError as exc:
            date_header = exc.headers.get("Date")
    except (URLError, OSError):
        return
    if not date_header:
        return
    server_time = email.utils.parsedate_to_datetime(date_header).timestamp()
    offset_ms = int(round((server_time - datetime.now().timestamp()) * 1000))
    if abs(offset_ms) <= 4000:
        return
    preload = Path(__file__).with_name("gmgn_time_offset.cjs")
    if not preload.exists():
        return
    os.environ["GMGN_TIME_OFFSET_MS"] = str(offset_ms)
    require_arg = f"--require={preload.resolve().as_posix()}"
    existing = os.environ.get("NODE_OPTIONS", "")
    if require_arg not in existing:
        os.environ["NODE_OPTIONS"] = f"{existing} {require_arg}".strip()


def ensure_cli_ready() -> bool:
    load_dotenv()
    _sync_time_offset()
    if GMGN == "gmgn-cli" and not shutil.which("gmgn-cli"):
        print("gmgn-cli is missing. Install it with: npm install -g gmgn-cli", file=sys.stderr)
        return False
    if os.environ.get("GMGN_API_KEY"):
        return True
    result = subprocess.run(
        [GMGN, "config", "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=gmgn_subprocess_env(),
        check=False,
    )
    if result.returncode == 0:
        return True
    print(result.stderr.strip() or result.stdout.strip() or "GMGN API key is not configured", file=sys.stderr)
    return False
