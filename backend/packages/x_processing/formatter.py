from __future__ import annotations

import re

from packages.common.text import normalize_multiline_text

from .models import DraftBrief


ODAILY_PREFIX = "Odaily星球日报讯 "
_PARAGRAPH_ENDINGS = ("。", "！", "？", "；", ".", "!", "?", ";", "”", "’", "\"", "'")


def _strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def parse_draft_output(value: str) -> DraftBrief:
    text = _strip_code_fence(value)
    lines = text.splitlines()
    title_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            title_index = index
            break
    if title_index is None:
        raise ValueError("writer output is empty")

    title = re.sub(r"^标题[:：]\s*", "", lines[title_index].strip())
    content_lines = lines[title_index + 1 :]
    while content_lines and not content_lines[0].strip():
        content_lines = content_lines[1:]
    content = "\n".join(content_lines).strip()
    content = re.sub(r"^正文[:：]\s*", "", content)
    if not title or not content:
        raise ValueError("writer output must contain title and content")
    return DraftBrief(title=title, content=content)


def _apply_common_replacements(value: str) -> str:
    text = value.replace("美金", "美元")
    while "。。" in text:
        text = text.replace("。。", "。")
    while "，，" in text:
        text = text.replace("，，", "，")
    text = text.replace("Binance", "币安")
    text = re.sub(r"dapp", "DApp", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d(?:[\d,.]*\d)?|\d)\s*USDT\b", r"\1 USDT", text, flags=re.IGNORECASE)
    return text


def _normalize_title_spaces(value: str) -> str:
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9])", "", value)
    text = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def _ensure_prefix(content: str) -> str:
    stripped = normalize_multiline_text(content)
    if stripped.startswith(ODAILY_PREFIX):
        return stripped
    if stripped.startswith("Odaily星球日报讯"):
        suffix = stripped.removeprefix("Odaily星球日报讯").lstrip()
        return f"{ODAILY_PREFIX}{suffix}"
    return f"{ODAILY_PREFIX}{stripped}"


def _ensure_paragraph_punctuation(content: str) -> str:
    paragraphs: list[str] = []
    for line in normalize_multiline_text(content).splitlines():
        stripped = line.strip()
        if not stripped.endswith(_PARAGRAPH_ENDINGS):
            stripped += "。"
        paragraphs.append(stripped)
    return "\n".join(paragraphs).strip()


def format_brief(draft: DraftBrief) -> DraftBrief:
    title = _normalize_title_spaces(_apply_common_replacements(draft.title.strip()))
    content = _apply_common_replacements(draft.content.strip())
    content = _ensure_prefix(content)
    content = _ensure_paragraph_punctuation(content)
    return DraftBrief(title=title, content=content)
