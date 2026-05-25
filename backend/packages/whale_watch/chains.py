from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ChainDefinition:
    key: str
    display_name: str
    blockscout_base_url: str
    explorer_base_url: str
    native_symbol: str
    native_decimals: int = 18

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer_base_url.rstrip('/')}/tx/{tx_hash}"


CHAIN_REGISTRY: dict[str, ChainDefinition] = {
    "ethereum": ChainDefinition(
        key="ethereum",
        display_name="Ethereum",
        blockscout_base_url="https://eth.blockscout.com",
        explorer_base_url="https://eth.blockscout.com",
        native_symbol="ETH",
    ),
    "base": ChainDefinition(
        key="base",
        display_name="Base",
        blockscout_base_url="https://base.blockscout.com",
        explorer_base_url="https://base.blockscout.com",
        native_symbol="ETH",
    ),
}


def resolve_chains(chain_keys: tuple[str, ...] | list[str]) -> list[ChainDefinition]:
    chains: list[ChainDefinition] = []
    unknown: list[str] = []
    for key in chain_keys:
        normalized = key.strip().lower()
        if not normalized:
            continue
        chain = CHAIN_REGISTRY.get(normalized)
        if chain is None:
            unknown.append(key)
            continue
        chains.append(chain)
    if unknown:
        raise ValueError(f"Unknown whale watch chain keys: {', '.join(unknown)}")
    return chains
