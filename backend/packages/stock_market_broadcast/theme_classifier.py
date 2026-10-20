from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from packages.common.config import load_x_processing_settings
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient


THEME_CLASSIFICATION_SCHEMA = {
    "type": "json_schema",
    "name": "stock_market_theme_classification",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assignments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "symbol": {"type": "string"},
                        "company_name": {"type": ["string", "null"]},
                        "theme": {"type": ["string", "null"]},
                        "confidence": {"type": "number"},
                    },
                    "required": ["symbol", "company_name", "theme", "confidence"],
                },
            },
        },
        "required": ["assignments"],
    },
    "strict": True,
}

_THEME_LABEL_PATTERN = re.compile(
    r"^[A-Za-z\u4e00-\u9fff]{2,18}(?:[与和及/][A-Za-z\u4e00-\u9fff]{1,18})?$"
)
_GENERIC_THEMES = {"其他", "股票", "美股", "科技", "公司", "市场", "不确定"}
_MIN_CONFIDENCE = 0.8
_MIN_THEME_SHARE = 0.7


@dataclass(frozen=True, slots=True)
class ThemeClassification:
    label: str
    symbols: frozenset[str]


class StockThemeClassifier:
    def __init__(
        self,
        *,
        client: TextGenerationClient | None,
        model: str | None,
    ) -> None:
        self.client = client
        self.model = model

    @classmethod
    def from_environment(cls) -> StockThemeClassifier:
        try:
            settings = load_x_processing_settings()
            if not settings.openai_api_key:
                return cls(client=None, model=None)
            return cls(
                client=OpenAIResponsesClient(
                    api_key=settings.openai_api_key,
                    base_url=str(settings.openai_base_url),
                    api_style=settings.openai_api_style,
                    timeout_seconds=min(8.0, settings.writer_request_timeout_seconds),
                    max_attempts=1,
                    backoff_seconds=0,
                ),
                model=settings.writer_model,
            )
        except Exception as exc:
            print(f"[stock-market] theme classifier unavailable error={type(exc).__name__}")
            return cls(client=None, model=None)

    def classify(self, symbols: tuple[str, ...]) -> ThemeClassification | None:
        requested = tuple(sorted(set(symbols)))
        if self.client is None or self.model is None or len(requested) < 2:
            return None

        prompt = (
            "你是美股标的识别与产业主题分类器。请用你已有的知识识别输入代码对应的公司或产品，"
            "并判断它们是否有一个明确的共同投资主题。ETF按其实际跟踪标的判断，不要把无关ETF硬归入主题。"
            "不确定公司或主题时返回 null。只输出JSON，不写解释或推理。\n"
            "每个代码必须恰好返回一次。company_name写简洁的公司或产品名称；theme只写简短主题名，"
            "例如AI产业链、半导体、云计算；不写涨跌、市场范围或结论。confidence为0到1之间的识别置信度。\n"
            f"代码：{json.dumps(requested, ensure_ascii=False)}"
        )
        try:
            raw = self.client.generate_text(
                model=self.model,
                prompt=prompt,
                text_format=THEME_CLASSIFICATION_SCHEMA,
            )
            return self._parse(raw, requested)
        except Exception as exc:
            print(f"[stock-market] theme classification fallback error={type(exc).__name__}")
            return None

    @staticmethod
    def _parse(raw: str, requested: tuple[str, ...]) -> ThemeClassification | None:
        try:
            payload: Any = json.loads(raw)
        except (TypeError, ValueError):
            return None
        assignments = payload.get("assignments") if isinstance(payload, dict) else None
        if not isinstance(assignments, list) or len(assignments) != len(requested):
            return None

        requested_set = set(requested)
        seen: set[str] = set()
        by_theme: dict[str, tuple[str, set[str]]] = {}
        for item in assignments:
            if not isinstance(item, dict):
                return None
            symbol = item.get("symbol")
            if not isinstance(symbol, str) or symbol not in requested_set or symbol in seen:
                return None
            seen.add(symbol)

            company = item.get("company_name")
            confidence = item.get("confidence")
            theme = item.get("theme")
            if (
                not isinstance(company, str) or not company.strip()
                or isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not _MIN_CONFIDENCE <= confidence <= 1
                or not isinstance(theme, str)
            ):
                continue
            label = theme.strip()
            if (
                len(label) > 36
                or not _THEME_LABEL_PATTERN.fullmatch(label)
                or label in _GENERIC_THEMES
            ):
                continue
            key = label.casefold()
            if key not in by_theme:
                by_theme[key] = (label, set())
            by_theme[key][1].add(symbol)

        if seen != requested_set or not by_theme:
            return None
        ranked = sorted(by_theme.values(), key=lambda group: (-len(group[1]), group[0]))
        if len(ranked) > 1 and len(ranked[0][1]) == len(ranked[1][1]):
            return None
        label, members = ranked[0]
        required = max(2, math.ceil(len(requested) * _MIN_THEME_SHARE))
        if len(members) < required:
            return None
        return ThemeClassification(label=label, symbols=frozenset(members))
