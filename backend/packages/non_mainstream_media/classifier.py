from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from packages.common.config import XProcessingSettings
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient

from .models import MixedClassificationResult


MIXED_CLASSIFICATION_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "mixed_source_route",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target": {
                "type": "string",
                "enum": ["crypto", "ai", "discard"],
            },
            "reason": {
                "type": "string",
            },
        },
        "required": ["target", "reason"],
    },
    "strict": True,
}


@dataclass(slots=True)
class MixedSourceClassifier:
    client: TextGenerationClient
    model: str = "gpt-5.4-mini"
    reasoning_effort: str | None = "low"

    def classify_fulltext(
        self,
        *,
        site_display_name: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> MixedClassificationResult:
        prompt = build_mixed_source_prompt(
            source_kind="混合信源全文",
            site_display_name=site_display_name,
            title=title,
            content=content,
            metadata=metadata or {},
        )
        return self._classify(prompt)

    def classify_headline_excerpt(
        self,
        *,
        site_display_name: str,
        title: str | None,
        excerpt: str | None,
        detail_url: str,
        metadata: dict[str, Any] | None = None,
    ) -> MixedClassificationResult:
        prompt = build_mixed_source_prompt(
            source_kind="混合信源标题提醒",
            site_display_name=site_display_name,
            title=title or "",
            content=excerpt or "",
            metadata={"detail_url": detail_url, **(metadata or {})},
        )
        return self._classify(prompt)

    def _classify(self, prompt: str) -> MixedClassificationResult:
        raw = self.client.generate_text(
            model=self.model,
            prompt=prompt,
            text_format=MIXED_CLASSIFICATION_JSON_SCHEMA,
            reasoning_effort=self.reasoning_effort,
        )
        payload = json.loads(raw)
        target = str(payload.get("target") or "").strip().lower()
        if target not in {"crypto", "ai", "discard"}:
            raise ValueError(f"invalid mixed source target: {target}")
        reason = str(payload.get("reason") or "").strip() or None
        return MixedClassificationResult(target=target, reason=reason)


def build_mixed_source_prompt(
    *,
    source_kind: str,
    site_display_name: str,
    title: str,
    content: str,
    metadata: dict[str, Any],
) -> str:
    metadata_lines = []
    for key in ("detail_url", "categories", "tags", "author_names"):
        value = metadata.get(key)
        if value in (None, "", [], {}):
            continue
        metadata_lines.append(f"{key}: {value}")
    extra_block = "\n".join(metadata_lines) if metadata_lines else "无"
    return (
        "你是 Odaily 混合信源分流器。请把一条综合站点内容分成三类之一："
        "\n1. crypto：与加密货币、区块链、稳定币、交易所、Web3、链上资金、代币、矿业、ETF、加密监管等直接相关"
        "\n2. ai：与 AI、半导体、芯片、算力、模型、数据中心、机器人、先进制造、关键供应链、AI 资本开支等直接相关"
        "\n3. discard：其它泛商业、政治、生活、娱乐、普通科技、宏观但和上面两类没有直接关系的内容"
        "\n\n规则："
        "\n- 只要主体事实明显属于 Crypto，就输出 crypto。"
        "\n- 只要主体事实明显属于 AI / 半导体产业，就输出 ai。"
        "\n- 如果只是普通科技、消费产品、泛金融、政治、娱乐或生活新闻，输出 discard。"
        "\n- 不要因为文中提到 AI 或 crypto 一个词就误判，必须是主要新闻主题。"
        "\n- 返回严格 JSON。"
        f"\n\n内容类型：{source_kind}"
        f"\n站点：{site_display_name}"
        f"\n标题：{title.strip() or '(空)'}"
        f"\n正文/摘要：{content.strip() or '(空)'}"
        f"\n补充信息：{extra_block}"
        '\n\n输出格式：{"target":"crypto|ai|discard","reason":"简短中文原因"}'
    )


def build_mixed_source_classifier(settings: XProcessingSettings) -> MixedSourceClassifier:
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for mixed source classification")
    client = OpenAIResponsesClient(
        api_key=settings.openai_api_key,
        base_url=str(settings.openai_base_url),
        api_style=settings.openai_api_style,
        timeout_seconds=settings.request_timeout_seconds,
        max_attempts=settings.retry.max_attempts,
        backoff_seconds=settings.retry.backoff_seconds,
    )
    return MixedSourceClassifier(client=client, model="gpt-5.4-mini", reasoning_effort="low")
