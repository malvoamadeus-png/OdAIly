from __future__ import annotations

import re
from collections.abc import Callable


SOLANA_CA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])solana:([1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
UNKNOWN_SYMBOLS = {"", "?", "unknown", "n/a", "none"}
TokenSymbolResolver = Callable[[str], str | None]


def resolve_solana_token_symbol_with_gmgn(address: str) -> str | None:
    """Resolve a Solana CA through the existing GMGN CLI-backed adapter."""

    # Keep the import lazy: X capture should still start when the optional
    # GMGN CLI is unavailable, and the scanner module is not needed otherwise.
    from packages.meme_scanner.scanner import fetch_gmgn_token_info

    token = fetch_gmgn_token_info(
        address,
        "solana",
        allow_unknown_platform=True,
        identity_only=True,
    )
    return normalize_token_symbol(token.symbol) if token is not None else None


def normalize_token_symbol(value: str | None) -> str | None:
    symbol = str(value or "").strip().lstrip("#$").strip()
    if not symbol or symbol.casefold() in UNKNOWN_SYMBOLS or any(char.isspace() for char in symbol):
        return None
    return symbol


def replace_solana_ca_tokens(text: str, resolve_symbol: TokenSymbolResolver) -> str:
    """Replace only resolvable ``solana:<CA>`` spans, preserving failures."""

    cache: dict[str, str | None] = {}

    def replacement(match: re.Match[str]) -> str:
        address = match.group(1)
        if address not in cache:
            try:
                cache[address] = normalize_token_symbol(resolve_symbol(address))
            except Exception as exc:
                print(
                    "[odaily] x-capture GMGN token identity lookup failed "
                    f"address={address} error={type(exc).__name__}: {str(exc)[:200]}"
                )
                cache[address] = None
        return cache[address] or match.group(0)

    return SOLANA_CA_PATTERN.sub(replacement, text)
