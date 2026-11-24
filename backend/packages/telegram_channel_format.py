from __future__ import annotations

import re


_APEWIRE_SIGNATURE = re.compile(
    r"^(?:@apewirenews|https?://t\.me/apewirenews(?:/\d+)?|t\.me/apewirenews(?:/\d+)?)/?$",
    re.IGNORECASE,
)


def format_aggregated_message(channel: str, text: str) -> str:
    """Remove blank lines and the ApeWire footer without changing message content."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if channel.casefold().lstrip("@") == "apewirenewscn":
        while lines and _APEWIRE_SIGNATURE.fullmatch(lines[-1]):
            lines.pop()
    return "\n".join(lines)
