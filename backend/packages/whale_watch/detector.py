from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .chains import ChainDefinition
from .models import Activity, AssetAmount, WhaleAddress


DEX_KEYWORDS = (
    "uniswap",
    "universalrouter",
    "poolmanager",
    "pool manager",
    "sushiswap",
    "curve",
    "balancer",
    "pancake",
    "aerodrome",
    "velodrome",
    "1inch",
    "zeroex",
    "0x",
    "okx",
    "paraswap",
    "cow",
    "swap",
)

SWAP_METHODS = ("swap", "execute", "exactinput", "exactoutput", "multicall")


def normalize_evm_address(value: str) -> str:
    address = value.strip()
    if not address.startswith("0x"):
        raise ValueError("EVM address must start with 0x")
    if len(address) != 42:
        raise ValueError("EVM address must be 42 characters")
    try:
        int(address[2:], 16)
    except ValueError as exc:
        raise ValueError("EVM address contains non-hex characters") from exc
    return address


def detect_activity(
    *,
    chain: ChainDefinition,
    whale: WhaleAddress,
    tx: dict[str, Any],
    token_transfers: list[dict[str, Any]],
) -> Activity | None:
    address_lower = whale.address_lower
    tx_hash = str(tx.get("hash") or "")
    block_number = _int(tx.get("block_number") or tx.get("blockNumber"))
    if not tx_hash or block_number is None:
        return None

    timestamp = _parse_timestamp(tx.get("timestamp"))
    native_in, native_out, native_counterparty = _native_delta(chain, address_lower, tx)
    token_ins, token_outs, token_counterparty = _token_deltas(address_lower, token_transfers)

    asset_in = _largest_asset([*token_ins, *([native_in] if native_in else [])])
    asset_out = _largest_asset([*token_outs, *([native_out] if native_out else [])])
    tx_url = chain.tx_url(tx_hash)

    if asset_in and asset_out and _looks_like_swap(tx, token_transfers):
        summary = f"{_fmt_asset(asset_out)} -> {_fmt_asset(asset_in)}"
        text = f"「{whale.label}」在 {chain.display_name} 用 {_fmt_asset(asset_out)} 换入 {_fmt_asset(asset_in)}\n{tx_url}"
        return Activity(
            kind="swap",
            tx_hash=tx_hash,
            block_number=block_number,
            timestamp=timestamp,
            fingerprint=_fingerprint("swap", tx_hash, asset_in, asset_out),
            summary=summary,
            telegram_text=text,
            tx_url=tx_url,
            asset_in=asset_in,
            asset_out=asset_out,
            raw_payload={"tx": tx, "token_transfers": token_transfers},
        )

    if asset_out and not asset_in:
        counterparty = token_counterparty or native_counterparty
        summary = f"out {_fmt_asset(asset_out)}"
        text = f"「{whale.label}」在 {chain.display_name} 转出 {_fmt_asset(asset_out)}"
        if counterparty:
            text += f" 至 {_short_address(counterparty)}"
        text += f"\n{tx_url}"
        return Activity(
            kind="transfer",
            tx_hash=tx_hash,
            block_number=block_number,
            timestamp=timestamp,
            fingerprint=_fingerprint("out", tx_hash, asset_out),
            summary=summary,
            telegram_text=text,
            tx_url=tx_url,
            direction="out",
            counterparty=counterparty,
            asset_out=asset_out,
            raw_payload={"tx": tx, "token_transfers": token_transfers},
        )

    if asset_in and not asset_out:
        counterparty = token_counterparty or native_counterparty
        summary = f"in {_fmt_asset(asset_in)}"
        text = f"「{whale.label}」在 {chain.display_name} 收到 {_fmt_asset(asset_in)}"
        if counterparty:
            text += f" 来自 {_short_address(counterparty)}"
        text += f"\n{tx_url}"
        return Activity(
            kind="transfer",
            tx_hash=tx_hash,
            block_number=block_number,
            timestamp=timestamp,
            fingerprint=_fingerprint("in", tx_hash, asset_in),
            summary=summary,
            telegram_text=text,
            tx_url=tx_url,
            direction="in",
            counterparty=counterparty,
            asset_in=asset_in,
            raw_payload={"tx": tx, "token_transfers": token_transfers},
        )

    return None


def _native_delta(
    chain: ChainDefinition,
    address_lower: str,
    tx: dict[str, Any],
) -> tuple[AssetAmount | None, AssetAmount | None, str | None]:
    value = _decimal(tx.get("value"))
    if value is None or value <= 0:
        return None, None, None
    amount = value / (Decimal(10) ** chain.native_decimals)
    asset = AssetAmount(symbol=chain.native_symbol, amount=amount, decimals=chain.native_decimals, is_native=True)
    from_hash = _address_hash(tx.get("from"))
    to_hash = _address_hash(tx.get("to"))
    if from_hash == address_lower:
        return None, asset, to_hash
    if to_hash == address_lower:
        return asset, None, from_hash
    return None, None, None


def _token_deltas(address_lower: str, transfers: list[dict[str, Any]]) -> tuple[list[AssetAmount], list[AssetAmount], str | None]:
    ins: list[AssetAmount] = []
    outs: list[AssetAmount] = []
    counterparty: str | None = None
    for transfer in transfers:
        from_hash = _address_hash(transfer.get("from"))
        to_hash = _address_hash(transfer.get("to"))
        if from_hash != address_lower and to_hash != address_lower:
            continue
        token = transfer.get("token") if isinstance(transfer.get("token"), dict) else {}
        total = transfer.get("total") if isinstance(transfer.get("total"), dict) else {}
        raw_value = _decimal(total.get("value"))
        if raw_value is None or raw_value <= 0:
            continue
        decimals = _int(total.get("decimals") or token.get("decimals")) or 0
        amount = raw_value / (Decimal(10) ** decimals)
        symbol = str(token.get("symbol") or "TOKEN")
        asset = AssetAmount(
            symbol=symbol,
            amount=amount,
            token_address=str(token.get("address_hash") or "") or None,
            decimals=decimals,
            is_native=False,
        )
        if from_hash == address_lower:
            outs.append(asset)
            counterparty = to_hash or counterparty
        else:
            ins.append(asset)
            counterparty = from_hash or counterparty
    return ins, outs, counterparty


def _looks_like_swap(tx: dict[str, Any], transfers: list[dict[str, Any]]) -> bool:
    haystack_parts = [
        str(tx.get("method") or ""),
        str(tx.get("raw_input") or "")[:10],
        _address_name(tx.get("to")),
    ]
    for transfer in transfers:
        haystack_parts.append(_address_name(transfer.get("from")))
        haystack_parts.append(_address_name(transfer.get("to")))
    haystack = " ".join(haystack_parts).lower()
    method = str(tx.get("method") or "").lower()
    return any(item in method for item in SWAP_METHODS) or any(item in haystack for item in DEX_KEYWORDS)


def _largest_asset(assets: list[AssetAmount]) -> AssetAmount | None:
    if not assets:
        return None
    return max(assets, key=lambda item: abs(item.amount))


def _fmt_asset(asset: AssetAmount) -> str:
    value = asset.amount.normalize()
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {asset.symbol}"


def _fingerprint(prefix: str, tx_hash: str, *assets: AssetAmount) -> str:
    raw = "|".join(
        [
            prefix,
            tx_hash.lower(),
            *[f"{asset.token_address or 'native'}:{asset.symbol}:{asset.amount}" for asset in assets],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


def _address_hash(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("hash")
    if not value:
        return None
    return str(value).lower()


def _address_name(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    tags = metadata.get("tags") if isinstance(metadata.get("tags"), list) else []
    tag_names = [str(item.get("name") or "") for item in tags if isinstance(item, dict)]
    return " ".join([str(value.get("name") or ""), *tag_names])


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _short_address(value: str) -> str:
    return f"{value[:6]}...{value[-4:]}" if len(value) > 12 else value
