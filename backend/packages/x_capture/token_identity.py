from __future__ import annotations

import re
from collections.abc import Callable


SOLANA_CA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])solana:(?P<address>[1-9A-HJ-NP-Za-km-z]{32,44})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
EVM_CA_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:ethereum|eth|bsc|base|robinhood|hood):"
    r"(?P<address>0x[0-9a-f]{40})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
EVM_CHAIN_PRIORITY = ("robinhood", "bsc", "base", "eth")
UNKNOWN_SYMBOLS = {"", "?", "unknown", "n/a", "none"}
TokenSymbolResolver = Callable[[tuple[str, ...], str], str | None]


def resolve_token_symbol_with_gmgn(chains: tuple[str, ...], address: str) -> str | None:
    """Try GMGN identity lookup in order and return the first valid symbol."""

    # Keep the import lazy: X capture should still start when the optional
    # GMGN CLI is unavailable, and the scanner module is not needed otherwise.
    from packages.meme_scanner.scanner import fetch_gmgn_token_info

    for chain in chains:
        try:
            token = fetch_gmgn_token_info(
                address,
                chain,
                allow_unknown_platform=True,
                identity_only=True,
            )
        except Exception:
            continue
        symbol = normalize_token_symbol(token.symbol) if token is not None else None
        if symbol:
            return symbol
    return None


def resolve_solana_token_symbol_with_gmgn(address: str) -> str | None:
    """Resolve a Solana CA through the existing GMGN CLI-backed adapter."""

    return resolve_token_symbol_with_gmgn(("solana",), address)


def normalize_token_symbol(value: str | None) -> str | None:
    symbol = str(value or "").strip().lstrip("#$").strip()
    if not symbol or symbol.casefold() in UNKNOWN_SYMBOLS or any(char.isspace() for char in symbol):
        return None
    return symbol


def replace_token_ca_tokens(text: str, resolve_symbol: TokenSymbolResolver) -> str:
    """Replace resolvable Solana/EVM token aliases while preserving failures."""

    cache: dict[tuple[tuple[str, ...], str], str | None] = {}

    def replacement(match: re.Match[str]) -> str:
        address = match.group("address")
        chains = ("solana",) if match.re is SOLANA_CA_PATTERN else EVM_CHAIN_PRIORITY
        cache_key = (chains, address.casefold())
        if cache_key not in cache:
            try:
                cache[cache_key] = normalize_token_symbol(resolve_symbol(chains, address))
            except Exception as exc:
                print(
                    "[odaily] x-capture GMGN token identity lookup failed "
                    f"address={address} error={type(exc).__name__}: {str(exc)[:200]}"
                )
                cache[cache_key] = None
        return cache[cache_key] or match.group(0)

    return SOLANA_CA_PATTERN.sub(replacement, EVM_CA_PATTERN.sub(replacement, text))
