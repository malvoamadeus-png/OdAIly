from __future__ import annotations

from .client import normalize_username


def normalize_write_name(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def normalize_lookup_username(value: str | None) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return normalize_username(raw)
    except ValueError:
        return None


def choose_effective_author_name(
    *,
    write_name: str | None,
    author_display_name: str | None,
    author_username: str | None,
) -> str | None:
    for candidate in (normalize_write_name(write_name), _normalize_text(author_display_name), _normalize_text(author_username)):
        if candidate:
            return candidate
    return None


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None
