from __future__ import annotations

import re


_LINE_BREAK_PATTERN = re.compile(r"\r\n?|\u2028|\u2029")
_INLINE_SPACE_PATTERN = re.compile(r"[ \t\f\v]+")
_ALL_SPACE_PATTERN = re.compile(r"\s+")


def normalize_inline_text(value: str | None) -> str:
    text = _LINE_BREAK_PATTERN.sub("\n", value or "")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    return _ALL_SPACE_PATTERN.sub(" ", text).strip()


def normalize_multiline_text(value: str | None) -> str:
    text = _LINE_BREAK_PATTERN.sub("\n", value or "")
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    lines: list[str] = []
    for raw_line in text.split("\n"):
        line = _INLINE_SPACE_PATTERN.sub(" ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines).strip()
