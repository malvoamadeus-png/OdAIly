from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from packages.common.config import load_x_processing_settings
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient

from .models import AlertMember, Direction


THEME_CLASSIFICATION_SCHEMA = {
    "type": "json_schema",
    "name": "stock_market_headline",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "concepts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "label": {"type": "string"},
                        "members": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "symbol": {"type": "string"},
                                    "confidence": {"type": "number"},
                                },
                                "required": ["symbol", "confidence"],
                            },
                        },
                    },
                    "required": ["label", "members"],
                },
            },
            "unclassified_symbols": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "concepts", "unclassified_symbols"],
    },
    "strict": True,
}

_CONCEPT_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff]{2,10}$")
_FORBIDDEN_TITLE_PATTERN = re.compile(
    r"合约|相关|1\s*小时|24\s*小时|1\s*h\b|24\s*h\b",
    re.IGNORECASE,
)
_MIN_CONFIDENCE = 0.8
_MIN_CLASSIFIED_SHARE = 0.7


@dataclass(frozen=True, slots=True)
class InstrumentKnowledge:
    company_name: str | None
    business_tags: tuple[str, ...]
    verified: bool


@dataclass(frozen=True, slots=True)
class HeadlineConcept:
    label: str
    members: tuple[tuple[str, float], ...]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(symbol for symbol, _ in self.members)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "members": [
                {"symbol": symbol, "confidence": confidence}
                for symbol, confidence in self.members
            ],
        }


@dataclass(frozen=True, slots=True)
class HeadlineGeneration:
    title: str
    concepts: tuple[HeadlineConcept, ...]
    unclassified_symbols: tuple[str, ...]
    fallback_reason: str | None = None

    def diagnostics(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "generated" if self.fallback_reason is None else "fallback",
            "concepts": [concept.as_dict() for concept in self.concepts],
            "unclassified_symbols": list(self.unclassified_symbols),
        }
        if self.fallback_reason is not None:
            result["fallback_reason"] = self.fallback_reason
        return result


@lru_cache(maxsize=1)
def load_instrument_knowledge() -> dict[str, InstrumentKnowledge]:
    path = Path(__file__).with_name("instrument_knowledge.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("stock market instrument knowledge must be an object")

    knowledge: dict[str, InstrumentKnowledge] = {}
    for symbol, raw in payload.items():
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.]{1,16}", symbol):
            raise ValueError("stock market knowledge contains an invalid symbol")
        if not isinstance(raw, dict):
            raise ValueError(f"stock market knowledge entry is invalid for {symbol}")
        company_name = raw.get("company_name")
        tags = raw.get("business_tags")
        verified = raw.get("verified")
        if company_name is not None and (not isinstance(company_name, str) or not company_name.strip()):
            raise ValueError(f"stock market company name is invalid for {symbol}")
        if (
            not isinstance(tags, list)
            or any(not isinstance(tag, str) or not tag.strip() for tag in tags)
            or not isinstance(verified, bool)
        ):
            raise ValueError(f"stock market business tags are invalid for {symbol}")
        if verified and (company_name is None or not tags):
            raise ValueError(f"verified stock market knowledge is incomplete for {symbol}")
        knowledge[symbol] = InstrumentKnowledge(
            company_name=company_name.strip() if isinstance(company_name, str) else None,
            business_tags=tuple(tag.strip() for tag in tags),
            verified=verified,
        )
    return knowledge


class StockThemeClassifier:
    def __init__(
        self,
        *,
        client: TextGenerationClient | None,
        model: str | None,
        knowledge: dict[str, InstrumentKnowledge] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.knowledge = knowledge if knowledge is not None else load_instrument_knowledge()

    @classmethod
    def from_environment(cls) -> StockThemeClassifier:
        try:
            knowledge = load_instrument_knowledge()
        except Exception as exc:
            print(f"[stock-market] instrument knowledge unavailable error={type(exc).__name__}")
            knowledge = {}
        try:
            settings = load_x_processing_settings()
            if not settings.openai_api_key:
                return cls(client=None, model=None, knowledge=knowledge)
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
                knowledge=knowledge,
            )
        except Exception as exc:
            print(f"[stock-market] theme classifier unavailable error={type(exc).__name__}")
            return cls(client=None, model=None, knowledge=knowledge)

    def generate_title(
        self,
        *,
        members: tuple[AlertMember, ...],
        direction: Direction,
        broad: bool,
        fallback_title: str,
    ) -> HeadlineGeneration:
        symbols = tuple(member.instrument.asset_name for member in members)
        if len(symbols) < 2:
            return self._fallback(fallback_title, symbols, "not_aggregated")
        if self.client is None or self.model is None:
            return self._fallback(fallback_title, symbols, "classifier_unavailable")

        inputs = []
        for member in members:
            symbol = member.instrument.asset_name
            profile = self.knowledge.get(symbol)
            known = profile is not None and profile.verified and bool(profile.business_tags)
            inputs.append({
                "symbol": symbol,
                "company_name": profile.company_name if known else None,
                "business_tags": list(profile.business_tags) if known else [],
                "knowledge_verified": known,
                "change_percent": str(member.change_pct),
            })
        if not any(item["knowledge_verified"] for item in inputs):
            return self._fallback(fallback_title, symbols, "knowledge_missing")
        verified_symbols = {
            str(item["symbol"]) for item in inputs if item["knowledge_verified"]
        }

        prompt = self._prompt(inputs, direction=direction, broad=broad)
        try:
            raw = self.client.generate_text(
                model=self.model,
                prompt=prompt,
                text_format=THEME_CLASSIFICATION_SCHEMA,
            )
        except Exception as exc:
            print(f"[stock-market] headline generation fallback error={type(exc).__name__}")
            return self._fallback(fallback_title, symbols, "model_error")

        moves = {member.instrument.asset_name: abs(member.change_pct) for member in members}
        generated, reason = self._parse(
            raw,
            requested=symbols,
            verified_symbols=verified_symbols,
            moves=moves,
            direction=direction,
            broad=broad,
        )
        if generated is None:
            return self._fallback(fallback_title, symbols, reason or "invalid_response")
        return generated

    @staticmethod
    def _prompt(inputs: list[dict[str, Any]], *, direction: Direction, broad: bool) -> str:
        movement = "上涨" if direction == "up" else "下跌"
        broad_word = "普涨" if direction == "up" else "普跌"
        narrow_word = "上涨" if direction == "up" else "下跌"
        breadth = "达到普涨/普跌广度标准" if broad else "未达到普涨/普跌广度标准"
        return (
            "你是美股行情标题生成器。根据每个标的已核实的公司名和极简业务标签，"
            "归纳本次同向行情涉及的投资概念，并直接生成完整中文标题。标签是背景知识，不是代码到标题的固定映射；"
            "可综合、合并或区分标签所表达的业务概念。\n"
            "只使用 knowledge_verified=true 的公司名和业务标签。其他标的不得凭代码、常识猜公司或行业，"
            "放入 unclassified_symbols。概念标签用简短名词，不含“概念”二字；每个标的最多归入一个概念。"
            "为每个标的分别输出0到1的confidence；置信度低于0.8的标的不得支持标题分类，放入未归类列表。"
            "最多输出3类。\n"
            f"本组方向：{movement}。市场广度：{breadth}。"
            "只有广度达标才能在标题写“普涨”或“普跌”；未达标必须写“上涨”或“下跌”。"
            "不允许使用“合约”“相关”，不写1小时或24小时。标题只能严格使用以下形式："
            f"单类且广度达标：{{概念}}概念美股{broad_word}；"
            f"单类且广度未达标：{{概念}}概念美股多只{narrow_word}；"
            f"多类且广度达标：美股多类股票{broad_word}，包括{{概念1}}概念、{{概念2}}概念；"
            f"多类且广度未达标：美股多类股票{narrow_word}，包括{{概念1}}概念、{{概念2}}概念。"
            "多类标题最多列3类，按组内标的数量从多到少，再按组内平均绝对涨幅从大到小。"
            "concepts必须给出标题所列概念，并逐一列出组内代码及该代码的confidence；"
            "unclassified_symbols列出所有未核实或置信度低于0.8的代码。"
            "标题必须与concepts的数量、排序、方向和广度状态完全一致。只返回约定JSON，不要解释。\n"
            f"标的资料：{json.dumps(inputs, ensure_ascii=False, separators=(',', ':'))}"
        )

    @staticmethod
    def _fallback(
        title: str,
        symbols: tuple[str, ...],
        reason: str,
    ) -> HeadlineGeneration:
        return HeadlineGeneration(
            title=title,
            concepts=(),
            unclassified_symbols=tuple(sorted(symbols)),
            fallback_reason=reason,
        )

    @staticmethod
    def _parse(
        raw: str,
        *,
        requested: tuple[str, ...],
        verified_symbols: set[str],
        moves: dict[str, Decimal],
        direction: Direction,
        broad: bool,
    ) -> tuple[HeadlineGeneration | None, str | None]:
        try:
            payload: Any = json.loads(raw)
        except (TypeError, ValueError):
            return None, "invalid_response"
        if not isinstance(payload, dict) or set(payload) != {"title", "concepts", "unclassified_symbols"}:
            return None, "invalid_response"

        title = payload.get("title")
        raw_concepts = payload.get("concepts")
        raw_unclassified = payload.get("unclassified_symbols")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(raw_concepts, list)
            or not 1 <= len(raw_concepts) <= 3
            or not isinstance(raw_unclassified, list)
        ):
            return None, "invalid_response"

        requested_set = set(requested)
        classified: set[str] = set()
        low_confidence: set[str] = set()
        unclassified: set[str] = set()
        concepts: list[HeadlineConcept] = []
        seen_labels: set[str] = set()
        for raw_concept in raw_concepts:
            if not isinstance(raw_concept, dict) or set(raw_concept) != {"label", "members"}:
                return None, "invalid_response"
            label = raw_concept.get("label")
            raw_members = raw_concept.get("members")
            if (
                not isinstance(label, str)
                or not _CONCEPT_LABEL_PATTERN.fullmatch(label)
                or "概念" in label
                or label.casefold() in seen_labels
                or not isinstance(raw_members, list)
                or not raw_members
            ):
                return None, "invalid_response"
            seen_labels.add(label.casefold())
            accepted_members: list[tuple[str, float]] = []
            for raw_member in raw_members:
                if not isinstance(raw_member, dict) or set(raw_member) != {"symbol", "confidence"}:
                    return None, "invalid_response"
                symbol = raw_member.get("symbol")
                confidence = raw_member.get("confidence")
                if (
                    not isinstance(symbol, str)
                    or symbol not in requested_set
                    or symbol not in verified_symbols
                    or symbol in classified
                    or symbol in low_confidence
                    or isinstance(confidence, bool)
                    or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence)
                    or not 0 <= confidence <= 1
                ):
                    return None, "invalid_response"
                if confidence < _MIN_CONFIDENCE:
                    low_confidence.add(symbol)
                else:
                    classified.add(symbol)
                    accepted_members.append((symbol, float(confidence)))
            if accepted_members:
                concepts.append(HeadlineConcept(label, tuple(accepted_members)))

        for symbol in raw_unclassified:
            if not isinstance(symbol, str) or symbol not in requested_set or symbol in unclassified:
                return None, "invalid_response"
            unclassified.add(symbol)
        if classified & unclassified:
            return None, "invalid_response"
        if classified | low_confidence | unclassified != requested_set:
            return None, "invalid_response"

        covered = len(classified) / len(requested_set) if requested_set else 0
        if covered < _MIN_CLASSIFIED_SHARE or not concepts:
            return None, "classification_below_threshold"

        concepts.sort(key=lambda concept: (
            -len(concept.symbols),
            -sum(moves[symbol] for symbol in concept.symbols) / len(concept.symbols),
            concept.label.casefold(),
        ))
        if len(concepts) == 1:
            trend = ("普涨" if direction == "up" else "普跌") if broad else (
                "多只上涨" if direction == "up" else "多只下跌"
            )
            expected_title = f"{concepts[0].label}概念美股{trend}"
        else:
            trend = ("普涨" if direction == "up" else "普跌") if broad else (
                "上涨" if direction == "up" else "下跌"
            )
            labels = "、".join(f"{concept.label}概念" for concept in concepts)
            expected_title = f"美股多类股票{trend}，包括{labels}"

        if (
            title.strip() != title
            or title != expected_title
            or len(title) > 60
            or _FORBIDDEN_TITLE_PATTERN.search(title)
        ):
            return None, "title_validation_failed"

        remaining = tuple(sorted(low_confidence | unclassified))
        return HeadlineGeneration(title, tuple(concepts), remaining), None
