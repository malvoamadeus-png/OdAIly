from __future__ import annotations

import re

from packages.common.text import normalize_multiline_text

from .models import DraftBrief


ODAILY_PREFIX = "Odaily星球日报讯 "
_PARAGRAPH_ENDINGS = ("。", "！", "？", "；", ".", "!", "?", ";", "”", "’", "\"", "'")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]\n]{1,300}\]\(https?://[^)\s]+(?:\s+\"[^\"]*\")?\)", re.IGNORECASE)
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_MODEL_META_LINE_PATTERN = re.compile(
    r"^(?:"
    r"好的|以下是|下面是|已根据|根据要求|按要求|我会先|我将先|我先|我已经|我已|"
    r"先读取|先提取|将原文|这里是|这是"
    r")"
)
_FORBIDDEN_META_PHRASES = ("原文链接", "来源链接", "按指定格式输出", "可核验的信息")
_TITLE_REPEAT_BOUNDARIES = set("。！？；，、：,.!?;:)]）】》」”’\"' \t")
_FIELD_LABEL_LINE_PATTERN = re.compile(
    r"^(?:\*\*)?\s*(?:"
    r"\u6807\u9898(?:\u7b56\u7565)?(?:\u4e3a|\u662f)?|\u6b63\u6587(?:\u4e3a|\u662f)?|"
    r"title(?:_strategy)?|content|matched_title_rules|feature_mode(?:_applied|_reason)?"
    r")\s*[:\uff1a]",
    re.IGNORECASE,
)
_COMMON_CHINESE_COMPANY_NAMES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![A-Za-z0-9])Samsung\s+Electronics(?![A-Za-z0-9])", re.IGNORECASE), "三星电子"),
    (re.compile(r"(?<![A-Za-z0-9])Samsung(?![A-Za-z0-9])", re.IGNORECASE), "三星"),
    (re.compile(r"(?<![A-Za-z0-9])NVIDIA(?![A-Za-z0-9])", re.IGNORECASE), "英伟达"),
    (re.compile(r"(?<![A-Za-z0-9])NVDA(?![A-Za-z0-9])", re.IGNORECASE), "英伟达"),
    (re.compile(r"(?<![A-Za-z0-9])Apple(?![A-Za-z0-9])", re.IGNORECASE), "苹果"),
    (re.compile(r"(?<![A-Za-z0-9])AAPL(?![A-Za-z0-9])", re.IGNORECASE), "苹果"),
)
_COMMON_ACCOUNT_NAMES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?<![A-Za-z0-9_])@?Jason60704294(?![A-Za-z0-9_])", re.IGNORECASE),
        "“先定10个大目标”",
    ),
)


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
    _reject_contaminated_output(title=title, content=content)
    if not title or not content:
        raise ValueError("writer output must contain title and content")
    return DraftBrief(title=title, content=content)


def _reject_contaminated_output(*, title: str, content: str) -> None:
    title_text = title.strip()
    content_text = content.strip()
    if not title_text or not content_text:
        return

    fields = (("title", title_text), ("content", content_text))
    for field_name, text in fields:
        if _MARKDOWN_LINK_PATTERN.search(text) or _URL_PATTERN.search(text):
            raise ValueError(f"writer output contains forbidden link in {field_name}")
        if any(phrase in text for phrase in _FORBIDDEN_META_PHRASES):
            raise ValueError(f"writer output contains meta text in {field_name}")

    first_content_line = content_text.splitlines()[0].strip()
    if first_content_line.startswith(ODAILY_PREFIX):
        first_content_line = first_content_line.removeprefix(ODAILY_PREFIX).lstrip()
    elif first_content_line.startswith("Odaily星球日报讯"):
        first_content_line = first_content_line.removeprefix("Odaily星球日报讯").lstrip()
    for field_name, text in (("title", title_text), ("content", first_content_line)):
        normalized = text.strip("「」《》【】[]()（）\"'“”‘’*# 　")
        if field_name == "content" and _FIELD_LABEL_LINE_PATTERN.match(normalized):
            raise ValueError("writer output contains structured field label in content")
        if field_name == "content" and _line_repeats_title(title_text, normalized):
            raise ValueError("writer output repeats title in content")
        if _MODEL_META_LINE_PATTERN.match(normalized):
            raise ValueError(f"writer output contains explanatory text in {field_name}")


def _line_repeats_title(title: str, line: str) -> bool:
    compact_title = _compact_title_like(title)
    compact_line = _compact_title_like(line)
    if not compact_title or not compact_line:
        return False
    if compact_line == compact_title:
        return True
    if not compact_line.startswith(compact_title):
        return False
    suffix = compact_line[len(compact_title) :]
    return bool(suffix and suffix[0] in _TITLE_REPEAT_BOUNDARIES)


def _compact_title_like(value: str) -> str:
    stripped = value.strip("「」《》【】[]()（）\"'“”‘’*# 　。！；，？?!.")
    return re.sub(r"\s+", "", stripped)


def _apply_common_replacements(value: str) -> str:
    text = value.replace("美金", "美元")
    while "。。" in text:
        text = text.replace("。。", "。")
    while "，，" in text:
        text = text.replace("，，", "，")
    for pattern, replacement in _COMMON_CHINESE_COMPANY_NAMES:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _COMMON_ACCOUNT_NAMES:
        text = pattern.sub(replacement, text)
    text = text.replace("Binance", "币安")
    text = re.sub(r"dapp", "DApp", text, flags=re.IGNORECASE)
    text = re.sub(r"(\d(?:[\d,.]*\d)?|\d)\s*USDT\b", r"\1 USDT", text, flags=re.IGNORECASE)
    return text


def _restore_fixed_account_names(value: str) -> str:
    return re.sub(r"“先定\s*10\s*个大目标”", "“先定10个大目标”", value)


def _normalize_title_spaces(value: str) -> str:
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[A-Za-z0-9])", "", value)
    text = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return text


def _split_existing_prefix(content: str) -> tuple[str, str]:
    stripped = content.strip()
    if stripped.startswith(ODAILY_PREFIX):
        return ODAILY_PREFIX, stripped.removeprefix(ODAILY_PREFIX)
    if stripped.startswith("Odaily星球日报讯"):
        return ODAILY_PREFIX, stripped.removeprefix("Odaily星球日报讯").lstrip()
    return "", stripped


def _normalize_content_spaces(value: str) -> str:
    text = re.sub(r"(?<=[A-Za-z])(?=[\u4e00-\u9fff])", " ", value)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?=[A-Za-z])", " ", text)
    text = re.sub(r"(?<=\d)(?=[\u4e00-\u9fff])", " ", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])(?=\d)", " ", text)
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
    title = _restore_fixed_account_names(_normalize_title_spaces(_apply_common_replacements(draft.title.strip())))
    prefix, content_body = _split_existing_prefix(draft.content)
    content = _apply_common_replacements(content_body)
    content = _restore_fixed_account_names(_normalize_content_spaces(content))
    if prefix:
        content = f"{prefix}{content}"
    content = _ensure_prefix(content)
    content = _ensure_paragraph_punctuation(content)
    _reject_contaminated_output(title=title, content=content)
    return DraftBrief(title=title, content=content)
