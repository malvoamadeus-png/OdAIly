from __future__ import annotations

import json
from typing import Any


PRIORITY_LEVELS = {"high", "medium", "low"}
NEWSFLASH_PRIORITY_SCHEMA = {
    "type": "json_schema",
    "name": "newsflash_review_priority",
    "schema": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "priority": {"type": "string", "enum": ["high", "medium", "low"]},
            "priority_reason": {"type": "string"},
        },
        "required": ["priority", "priority_reason"],
    },
    "strict": False,
}

PRIORITY_RUBRIC = "\n".join(
    [
        "只判断快讯标题和正文明确表达的事件重要性，给编辑建议阅读顺序，不判断发布资格、准确性或风险。",
        "高：事件直接影响读者交易决策，或是值得广泛传播的重大进展，例如头部交易平台上线重要产品、加密政策/法案取得实质进展、重要人物就加密行业作出明确且有实际影响的讲话、重大安全或基础设施事件。",
        "中：与加密行业相关的具体进展，有一定决策或传播价值，但影响范围、主体重要性或实际变化有限。",
        "低：正文表达的是常规、局部或影响有限的动态，缺少明显的近期决策影响或广泛传播价值。",
        "只使用标题和正文中明示的信息，不凭外部知识推测主体重要性。不得用事实/文本疑点、阅读状态、写作质量、模型信心或直发/挂后台决定来赋值。",
        "行情监控、Meme速递、定时简报和人工发布由系统标为不适用，不在此任务中分类。低仅表示事件重要性低，不能表示未评估或评估失败。",
        "priority 必须是 high、medium、low 之一。priority_reason 用一句简短中文说明等级依据，不超过 50 个汉字。",
    ]
)


def build_newsflash_priority_prompt(*, title: str, content: str) -> str:
    return (
        "你是 OdAIly 编辑值班助手，请评定快讯审阅优先级。只输出 JSON 对象，不输出解释。\n\n"
        f"{PRIORITY_RUBRIC}\n\n"
        f"标题：{title}\n正文：{content}"
    )


def parse_newsflash_priority(payload: Any) -> tuple[str, str]:
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("priority output must be a JSON object")
    level = str(payload.get("priority") or "").strip().lower()
    reason = " ".join(str(payload.get("priority_reason") or "").split())
    if level not in PRIORITY_LEVELS:
        raise ValueError("priority must be high, medium or low")
    if not reason:
        raise ValueError("priority_reason is required")
    return level, reason[:200]
