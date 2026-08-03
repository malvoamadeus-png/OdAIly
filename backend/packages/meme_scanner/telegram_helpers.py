from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import socks
from dotenv import load_dotenv
from telethon.tl.types import User


EVM_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
PROXY_TYPES = {
    "socks5": socks.SOCKS5,
    "socks5h": socks.SOCKS5,
    "socks4": socks.SOCKS4,
    "http": socks.HTTP,
    "https": socks.HTTP,
}


def load_name_list(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip().casefold()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def extract_ca_references(text: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for match in EVM_ADDRESS_RE.finditer(text):
        address = match.group(0).lower()
        if address not in seen:
            seen.add(address)
            result.append(("evm", address))
    return result


def parse_config(path: Path) -> tuple[int, str]:
    load_dotenv()
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    api_id_match = re.search(r"App\s+api_id\s*:\s*(\d+)", text, re.IGNORECASE)
    api_hash_match = re.search(r"App\s+api_hash\s*:\s*([0-9a-fA-F]{16,64})", text, re.IGNORECASE)
    api_id = api_id_match.group(1) if api_id_match else os.environ.get("MEME_TELEGRAM_API_ID") or os.environ.get("TELEGRAM_API_ID")
    api_hash = api_hash_match.group(1) if api_hash_match else os.environ.get("MEME_TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        raise ValueError("Set MEME_TELEGRAM_API_ID and MEME_TELEGRAM_API_HASH, or provide --config")
    return int(api_id), api_hash


def _windows_system_proxy() -> str | None:
    if not sys.platform.startswith("win"):
        return None
    script = (
        "Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
        "| Select-Object -ExpandProperty ProxyEnable; "
        "Get-ItemProperty -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' "
        "| Select-Object -ExpandProperty ProxyServer"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or len(lines) < 2 or lines[0].lower() not in {"1", "true"}:
        return None
    value = lines[1]
    if "://" not in value:
        value = f"socks5://{value}"
    return value


def resolve_proxy(value: str | None) -> tuple[int, str, int, bool, str | None, str | None] | None:
    if not value or value == "none":
        return None
    proxy_url = _windows_system_proxy() if value == "auto" else value
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url if "://" in proxy_url else f"socks5://{proxy_url}")
    if parsed.scheme.lower() not in PROXY_TYPES or not parsed.hostname or not parsed.port:
        raise ValueError(f"Invalid Telegram proxy: {proxy_url}")
    return (
        PROXY_TYPES[parsed.scheme.lower()],
        parsed.hostname,
        parsed.port,
        True,
        parsed.username,
        parsed.password,
    )


def display_name(entity: Any) -> str:
    if entity is None:
        return "unknown"
    if isinstance(entity, User):
        name = " ".join(part for part in (entity.first_name, entity.last_name) if part).strip()
        return name or entity.username or str(entity.id)
    return getattr(entity, "title", None) or getattr(entity, "username", None) or str(getattr(entity, "id", "unknown"))


def is_automated_sender(sender: Any) -> bool:
    username = str(getattr(sender, "username", "") or "").casefold()
    return bool(getattr(sender, "bot", False)) or username.endswith("_bot") or username.endswith("bot")
