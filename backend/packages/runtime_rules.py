from __future__ import annotations

import json
from typing import Any

from packages.common.source_exclusions import SOURCE_EXCLUSION_SCOPES
from packages.x_processing.formatter import (
    _COMMON_ACCOUNT_NAMES,
    _COMMON_CHINESE_COMPANY_NAMES,
    _FORBIDDEN_META_PHRASES,
)
from packages.x_processing.known_title_subjects_config import load_known_title_subject_names
from packages.x_processing.publisher_config import default_publisher_rule_config
from packages.x_processing.repository import DEFAULT_FEATURE_MODE_TEXT, PUBLISHER_CHANNEL_DEFAULTS
from packages.x_processing.title_trace import (
    TITLE_RULES,
    TITLE_STRATEGIES,
    WRITER_JSON_SCHEMA,
    normalize_known_title_subject_names,
)
from packages.x_processing.worker import (
    AI_JUDGE_JSON_SCHEMA,
    AI_JUDGE_PROMPT_TEMPLATE,
    COMPETITOR_JUDGE_JSON_SCHEMA,
    COMPETITOR_JUDGE_PROMPT_TEMPLATE,
    JUDGE_RULE_VERSIONS,
    JIN10_JUDGE_JSON_SCHEMA,
    NON_MAINSTREAM_JUDGE_JSON_SCHEMA,
    NON_MAINSTREAM_JUDGE_PROMPT_TEMPLATE,
    PUBLISHER_CHANNEL_BY_SOURCE,
    X_JUDGE_JSON_SCHEMA,
    X_JUDGE_PROMPT_TEMPLATE,
)


PUBLISHER_RUNTIME_POLICY = {
    "source_to_channel": dict(PUBLISHER_CHANNEL_BY_SOURCE),
    "profile_mapping": {
        "ai_source": "ai_source",
        "x with metadata.x_account_is_ai_source=true": "ai_source",
        "all other eligible sources": "regular",
    },
    "fallbacks": {
        "source_not_eligible": "manual review without a publisher model call",
        "publisher_profile_disabled": "manual review without a publisher model call",
        "publisher_no_enabled_allow_rules": "manual review without a publisher model call",
        "model_failed": "publisher_failed and retry through the existing pipeline mechanism",
        "push_failed": "publisher_failed and retry through the existing pipeline mechanism",
        "rule_rejected": "submit as non-published and leave ready for manual review",
    },
}


def build_runtime_rules_payload(*, known_title_subject_names: list[str] | None = None) -> dict[str, Any]:
    publisher_defaults = default_publisher_rule_config().model_dump(mode="json")
    subject_names = normalize_known_title_subject_names(
        load_known_title_subject_names() if known_title_subject_names is None else known_title_subject_names
    )
    return {
        "schema_version": 1,
        "sections": [
            {
                "key": "judge",
                "label": "判断者",
                "entries": [
                    _prompt_entry("judge-x", "X 判断者", ["x"], X_JUDGE_PROMPT_TEMPLATE, X_JUDGE_JSON_SCHEMA, "x"),
                    _prompt_entry(
                        "judge-competitor",
                        "竞品判断者",
                        ["competitor"],
                        COMPETITOR_JUDGE_PROMPT_TEMPLATE,
                        COMPETITOR_JUDGE_JSON_SCHEMA,
                        "competitor",
                    ),
                    _prompt_entry(
                        "judge-crypto-source",
                        "Crypto 信源判断者",
                        ["crypto_source"],
                        NON_MAINSTREAM_JUDGE_PROMPT_TEMPLATE,
                        NON_MAINSTREAM_JUDGE_JSON_SCHEMA,
                        "crypto_source",
                    ),
                    _prompt_entry(
                        "judge-ai-source",
                        "AI 信源判断者",
                        ["ai_source"],
                        AI_JUDGE_PROMPT_TEMPLATE,
                        AI_JUDGE_JSON_SCHEMA,
                        "ai_source",
                    ),
                    {
                        "key": "judge-jin10",
                        "title": "金十判断者",
                        "kind": "schema",
                        "scopes": ["jin10"],
                        "summary": "自然语言主题白名单在 Prompt 页面编辑；代码固定输出 Schema 与失败语义。",
                        "content": _pretty(JIN10_JUDGE_JSON_SCHEMA),
                        "source_location": "backend/packages/x_processing/worker.py:JIN10_JUDGE_JSON_SCHEMA",
                        "editable": True,
                        "editable_at": "Prompt / 判断者-金十",
                    },
                ],
            },
            {
                "key": "exclusions",
                "label": "源头排除",
                "entries": [
                    {
                        "key": "source-exclusion-semantics",
                        "title": "路径化排除词匹配语义",
                        "kind": "policy",
                        "scopes": list(SOURCE_EXCLUSION_SCOPES),
                        "summary": "Unicode NFKC 规范化、大小写不敏感、任一词子串命中；按规则组适用范围检查标题或标题+摘要+正文。",
                        "content": (
                            "match_target=title 只检查标题类文本；match_target=all 检查标题、摘要和正文，不检查 URL。"
                            "命中规则组后在 tasks 入库前停止处理。X-AI 同时属于 x 与 ai_source；"
                            "混合信源分类前属于 mixed_source，分类后再叠加 crypto_source 或 ai_source。"
                        ),
                        "source_location": "backend/packages/common/source_exclusions.py",
                        "editable": True,
                        "editable_at": "排除词",
                    }
                ],
            },
            {
                "key": "writer",
                "label": "编写者",
                "entries": [
                    {
                        "key": "writer-title-trace-schema",
                        "title": "标题溯源输出 Schema",
                        "kind": "schema",
                        "scopes": ["write"],
                        "summary": "生产编写者一次返回发布文本和标题策略元数据。",
                        "content": _pretty(WRITER_JSON_SCHEMA),
                        "source_location": "backend/packages/x_processing/title_trace.py:WRITER_JSON_SCHEMA",
                        "editable": False,
                    },
                    {
                        "key": "writer-title-strategies",
                        "title": "标题策略与规则标识",
                        "kind": "mapping",
                        "scopes": ["write"],
                        "summary": "标题策略和 matched_title_rules 的稳定枚举。",
                        "content": _pretty({"title_strategies": TITLE_STRATEGIES, "title_rules": TITLE_RULES}),
                        "source_location": "backend/packages/x_processing/title_trace.py",
                        "editable": False,
                    },
                    {
                        "key": "writer-known-subjects",
                        "title": "知名主体表",
                        "kind": "knowledge",
                        "scopes": ["write"],
                        "summary": "仅把当前材料实际命中的知名人物姓名注入 Prompt。",
                        "content": "、".join(subject_names),
                        "source_location": "data/config/known_title_subjects.json",
                        "editable": True,
                        "editable_at": "Prompt及规则管理 / 知名人物",
                    },
                    {
                        "key": "writer-feature-mode",
                        "title": "特色模式内置说明",
                        "kind": "prompt",
                        "scopes": ["write"],
                        "summary": "模板开关启用后拼接在具体写作 Prompt 之前。",
                        "content": DEFAULT_FEATURE_MODE_TEXT,
                        "source_location": "backend/packages/x_processing/repository.py:DEFAULT_FEATURE_MODE_TEXT",
                        "editable": True,
                        "editable_at": "Prompt",
                    },
                ],
            },
            {
                "key": "formatter",
                "label": "编写者2",
                "entries": [
                    {
                        "key": "formatter-replacements",
                        "title": "确定性替换",
                        "kind": "replacement",
                        "scopes": ["format_publish"],
                        "summary": "标题和正文共同执行的账号、公司名及固定写法替换。",
                        "content": _pretty(
                            {
                                "companies": _pattern_mappings(_COMMON_CHINESE_COMPANY_NAMES),
                                "accounts": _pattern_mappings(_COMMON_ACCOUNT_NAMES),
                                "fixed": {"Binance": "币安", "dapp": "DApp", "美金": "美元"},
                            }
                        ),
                        "source_location": "backend/packages/x_processing/formatter.py",
                        "editable": False,
                    },
                    {
                        "key": "formatter-output-guards",
                        "title": "输出污染拦截",
                        "kind": "policy",
                        "scopes": ["format_publish"],
                        "summary": "拒绝链接、解释性前言和来源提示污染最终稿。",
                        "content": _pretty({"forbidden_phrases": _FORBIDDEN_META_PHRASES}),
                        "source_location": "backend/packages/x_processing/formatter.py",
                        "editable": False,
                    },
                ],
            },
            {
                "key": "publisher",
                "label": "发布者",
                "entries": [
                    {
                        "key": "publisher-default-rules",
                        "title": "发布者默认规则",
                        "kind": "policy",
                        "scopes": ["publish"],
                        "summary": "代码默认值；运行时可被控制台保存的规则配置覆盖。",
                        "content": _pretty(publisher_defaults),
                        "source_location": "backend/packages/x_processing/publisher_config.py",
                        "editable": True,
                        "editable_at": "发布者",
                    },
                    {
                        "key": "publisher-channel-defaults",
                        "title": "来源渠道默认开关",
                        "kind": "mapping",
                        "scopes": ["publish"],
                        "summary": "发布者各来源渠道的默认启用状态。",
                        "content": _pretty(PUBLISHER_CHANNEL_DEFAULTS),
                        "source_location": "backend/packages/x_processing/repository.py:PUBLISHER_CHANNEL_DEFAULTS",
                        "editable": True,
                        "editable_at": "发布者",
                    },
                    {
                        "key": "publisher-source-fallbacks",
                        "title": "发布者来源映射与 fallback",
                        "kind": "mapping",
                        "scopes": ["publish"],
                        "summary": "来源到规则块的映射，以及未配置、模型失败和推送失败时的确定性处理。",
                        "content": _pretty(PUBLISHER_RUNTIME_POLICY),
                        "source_location": "backend/packages/x_processing/worker.py:_run_publish",
                        "editable": False,
                    },
                ],
            },
        ],
    }


def _prompt_entry(
    key: str,
    title: str,
    scopes: list[str],
    prompt: str,
    schema: dict[str, Any],
    rule_set: str,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "kind": "prompt",
        "scopes": scopes,
        "summary": f"规则版本 {JUDGE_RULE_VERSIONS[rule_set]}；Prompt 与严格 JSON Schema 组合使用。",
        "content": f"{prompt}\n\n【JSON Schema】\n{_pretty(schema)}",
        "source_location": "backend/packages/x_processing/worker.py",
        "editable": False,
    }


def _pattern_mappings(values: tuple[tuple[Any, str], ...]) -> list[dict[str, str]]:
    return [{"pattern": pattern.pattern, "replacement": replacement} for pattern, replacement in values]


def _pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=list)
