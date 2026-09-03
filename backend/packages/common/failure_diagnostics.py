from __future__ import annotations

import re
from dataclasses import dataclass


FAILURE_STATUS_STAGE_LABELS = {
    "judge_failed": "判断者",
    "domain_failed": "领域判断",
    "search_failed": "搜索/查重",
    "write_failed": "编写者1",
    "format_failed": "编写者2/格式化",
    "publish_failed": "推送接口",
    "publisher_failed": "发布者",
    "notify_failed": "标题提醒",
}

PROCESSING_STATUS_STAGE_LABELS = {
    "judging": "判断者",
    "deduping": "搜索/查重",
    "writing": "编写者1",
    "formatting": "编写者2/格式化",
    "publishing": "发布者",
}

FAILURE_CATEGORIES = {
    "database_connection": "数据库连接/锁",
    "external_ai_billing": "AI 余额或额度",
    "external_rate_limit": "上游限流",
    "external_auth": "上游鉴权",
    "telegram_delivery": "Telegram 投递",
    "publisher_push": "发布接口",
    "program_output_parse": "AI 输出解析",
    "external_network": "上游网络",
    "program_or_unknown": "程序异常",
    "unknown": "未识别",
}

FAILURE_CATEGORY_ACTIONS = {
    "database_connection": "检查本地 SQLite WAL、锁等待、长事务和相关服务是否释放连接。",
    "external_ai_billing": "检查 AI/embedding 服务余额、额度和账单状态。",
    "external_rate_limit": "检查外部服务限流，必要时降低并发或等待窗口恢复。",
    "external_auth": "检查外部 API token、权限和环境变量配置。",
    "telegram_delivery": "检查 Telegram bot token、chat/topic 配置和 Telegram API 可达性。",
    "publisher_push": "检查发布者规则、Push API 返回和 Odaily 推送接口可达性。",
    "program_output_parse": "检查对应阶段 Prompt、模型输出格式和解析兼容性。",
    "external_network": "检查外部站点/API 网络、超时和重试窗口。",
    "program_or_unknown": "查看对应服务日志和原始错误，定位程序内异常。",
    "unknown": "查看原始错误和对应服务日志补充诊断。",
}

FAILURE_CODE_ACTIONS = {
    "ai_quota_exhausted": "检查对应 AI/embedding 账号余额、额度和账单状态，恢复额度后再观察重试。",
    "ai_request_timeout": "检查 AI 上游端点、模型响应时间和客户端超时配置；确认是否为上游未返回。",
    "embedding_request_timeout": "检查 DashScope embedding 端点和额度，并确认 embedding 请求是否持续超时。",
    "upstream_http_5xx": "检查上游服务状态页、网关日志和对应端点；HTTP 5xx 通常需要等待上游恢复。",
    "upstream_http_4xx": "检查请求参数、接口版本、模型名称和上游返回 body。",
    "upstream_connection_failed": "检查服务器到上游端点的网络、DNS、防火墙和代理。",
    "database_connection_or_lock": "检查本地 SQLite WAL、锁等待、长事务和相关服务是否释放连接。",
    "ai_output_parse_failed": "检查对应阶段 Prompt、模型输出格式、JSON Schema 和解析兼容性。",
}


@dataclass(frozen=True, slots=True)
class FailureClassification:
    code: str
    category: str
    category_label: str
    reason: str
    action_hint: str


def stage_label_for_status(status: str) -> str:
    return FAILURE_STATUS_STAGE_LABELS.get(status) or PROCESSING_STATUS_STAGE_LABELS.get(status) or status


def classify_error(sample_error: str, *, status: str) -> str:
    """Return the broad category used by supervisor alerts and console diagnostics."""
    text = sample_error.lower()
    if not sample_error or sample_error == "-":
        return "unknown"
    if any(
        token in text
        for token in (
            "emaxconnsession",
            "echeckouttimeout",
            "max clients",
            "connection failed",
            "statement timeout",
            "idle in transaction",
            "database is locked",
            "database is busy",
        )
    ):
        return "database_connection"
    if any(token in text for token in ("arrearage", "insufficient_quota", "quota", "billing", "欠费")):
        return "external_ai_billing"
    if any(token in text for token in ("rate limit", "429", "too many requests")):
        return "external_rate_limit"
    if any(token in text for token in ("unauthorized", "forbidden", "invalid api key", "401", "403")):
        return "external_auth"
    if any(token in text for token in ("telegram", "sendmessage")) or status == "notify_failed":
        return "telegram_delivery"
    if any(token in text for token in ("push failed", "push api", "ispublish", "ispush")) or status in {
        "publish_failed",
        "publisher_failed",
    }:
        return "publisher_push"
    if any(token in text for token in ("json", "parse", "schema", "invalid route", "invalid decision")):
        return "program_output_parse"
    if any(token in text for token in ("timeout", "connection reset", "read timed out", "502", "503", "504")):
        return "external_network"
    return "program_or_unknown"


def _is_ai_context(error: str, status: str) -> bool:
    text = error.lower()
    return status in {"judge_failed", "domain_failed", "search_failed", "write_failed"} or any(
        token in text for token in ("openai", "gpt-", "deepseek", "llm", "embedding", "dashscope", "model=")
    )


def _first_status_code(error: str) -> str | None:
    match = re.search(r"(?:status_code|chat_status_code|responses_status_code)=(\d{3})", error, re.IGNORECASE)
    return match.group(1) if match else None


def classify_failure(error: str | None, *, status: str) -> FailureClassification:
    raw = (error or "").strip()
    category = classify_error(raw, status=status)
    ai_context = _is_ai_context(raw, status)
    status_code = _first_status_code(raw)

    if category == "external_ai_billing":
        code = "ai_quota_exhausted"
        reason = "上游 AI 或 embedding 服务返回余额、额度或计费不足。"
    elif category == "external_rate_limit":
        code = "upstream_rate_limited"
        reason = "上游服务触发限流，当前请求没有正常完成。"
    elif category == "external_auth":
        code = "upstream_auth_failed"
        reason = "上游 API 鉴权失败，可能是 Key、权限或环境变量配置问题。"
    elif category == "database_connection":
        code = "database_connection_or_lock"
        reason = "本地数据库连接、锁等待或事务状态异常。"
    elif category == "telegram_delivery":
        code = "telegram_delivery_failed"
        reason = "Telegram 投递没有成功。"
    elif category == "publisher_push":
        code = "publisher_or_push_failed"
        reason = "发布者判断或发布接口没有正常完成。"
    elif category == "program_output_parse":
        code = "ai_output_parse_failed" if ai_context else "program_output_parse_failed"
        reason = "模型或程序输出没有符合当前阶段要求的结构，解析失败。"
    elif category == "external_network":
        if status_code and status_code.startswith("5"):
            code = "upstream_http_5xx"
            reason = f"上游服务返回 HTTP {status_code}，属于上游服务端异常。"
        elif status_code and status_code.startswith("4"):
            code = "upstream_http_4xx"
            reason = f"上游服务返回 HTTP {status_code}，请求被上游拒绝。"
        elif "timeout" in raw.lower() or "timed out" in raw.lower():
            if "embedding" in raw.lower() or "dashscope" in raw.lower():
                code = "embedding_request_timeout"
                reason = "embedding 请求在客户端超时窗口内没有返回。"
            elif ai_context:
                code = "ai_request_timeout"
                reason = "AI 请求在客户端超时窗口内没有返回。"
            else:
                code = "upstream_request_timeout"
                reason = "上游请求在客户端超时窗口内没有返回。"
        elif "connection reset" in raw.lower() or "connection refused" in raw.lower():
            code = "upstream_connection_failed"
            reason = "与上游服务的网络连接被重置或拒绝。"
        else:
            code = "upstream_network_failed"
            reason = "访问上游站点或 API 时发生网络异常。"
    elif category == "program_or_unknown":
        code = "program_or_unknown_error"
        reason = "程序抛出了未被归类的异常。"
    else:
        code = "unknown_error"
        reason = "暂时无法从错误信息中识别根因。"

    return FailureClassification(
        code=code,
        category=category,
        category_label=FAILURE_CATEGORIES.get(category, category),
        reason=reason,
        action_hint=FAILURE_CODE_ACTIONS.get(code, FAILURE_CATEGORY_ACTIONS.get(category, FAILURE_CATEGORY_ACTIONS["unknown"])),
    )


def action_hint_for_category(category: str) -> str:
    return FAILURE_CATEGORY_ACTIONS.get(category, FAILURE_CATEGORY_ACTIONS["unknown"])
