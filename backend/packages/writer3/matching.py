from __future__ import annotations

import re
from datetime import UTC, datetime

from .models import AnalysisResult, OdailyReference, Writer3Candidate


EVENT_PRIOR_TYPES = {
    "financing": ["prior_financing", "mainnet_or_testnet", "tokenomics", "roadmap"],
    "mainnet_launch": ["prior_financing", "testnet", "tokenomics", "airdrop", "roadmap"],
    "testnet_launch": ["prior_financing", "roadmap", "ecosystem_progress"],
    "airdrop": ["prior_financing", "tokenomics", "mainnet_or_testnet", "roadmap"],
    "tokenomics": ["prior_financing", "mainnet_or_testnet", "airdrop", "roadmap"],
    "regulation": ["same_policy_prior_stage", "same_agency_prior_action"],
    "bill": ["same_bill_prior_stage", "same_policy_prior_stage"],
    "lawsuit": ["same_case_prior_stage", "same_party_prior_action"],
    "security": ["same_incident_prior_stage", "attack_loss_tracking", "compensation_or_recovery"],
}


PRIOR_TYPE_KEYWORDS = {
    "prior_financing": ["融资", "完成融资", "投资", "领投", "参投", "种子轮", "战略轮", "funding", "raised"],
    "mainnet_or_testnet": ["主网", "测试网", "激励测试网", "mainnet", "testnet"],
    "testnet": ["测试网", "激励测试网", "testnet"],
    "tokenomics": ["代币经济学", "代币分配", "TGE", "tokenomics"],
    "airdrop": ["空投", "airdrop"],
    "roadmap": ["路线图", "计划", "预计", "roadmap"],
    "ecosystem_progress": ["生态", "集成", "上线主网", "合作", "ecosystem"],
    "same_policy_prior_stage": ["政策", "监管", "规则", "提案", "通过", "审议", "policy", "regulation"],
    "same_agency_prior_action": ["监管机构", "委员会", "SEC", "CFTC", "FCA", "MAS"],
    "same_bill_prior_stage": ["法案", "投票", "通过", "提交", "审议", "bill", "vote"],
    "same_case_prior_stage": ["诉讼", "起诉", "裁决", "和解", "法院", "lawsuit", "court"],
    "same_party_prior_action": ["起诉", "调查", "指控", "和解", "settlement", "charges"],
    "same_incident_prior_stage": ["攻击", "漏洞", "损失", "追踪", "赔付", "恢复", "exploit", "hack"],
    "attack_loss_tracking": ["攻击", "被盗", "损失", "追踪", "黑客", "exploit", "hack", "stolen"],
    "compensation_or_recovery": ["赔付", "补偿", "追回", "归还", "恢复", "赏金", "bounty", "recovery"],
}

MATTER_EVENT_TYPES = {"regulation", "bill", "lawsuit"}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def clean_alias(value: str | None) -> str:
    return clean_text(value).strip("，。、“”\"'（）()[]{}")


def dedupe_aliases(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = clean_alias(value)
        key = alias.lower()
        if not alias or key in seen:
            continue
        seen.add(key)
        result.append(alias)
    return result


def alias_matches(text: str, alias: str) -> bool:
    alias = clean_alias(alias)
    if not alias:
        return False
    if _is_ascii_alias(alias):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])"
        return re.search(pattern, text, flags=re.IGNORECASE) is not None
    return alias.lower() in text.lower()


def _is_ascii_alias(value: str) -> bool:
    return bool(re.search(r"[A-Za-z0-9]", value)) and all(ord(ch) < 128 for ch in value)


def exclusion_reason(title: str | None, content: str, terms: list[str]) -> str | None:
    text = f"{title or ''}\n{content}"
    for term in terms:
        value = clean_alias(term)
        if value and value.lower() in text.lower():
            return value
    return None


def score_candidate(
    *,
    reference: OdailyReference,
    analysis: AnalysisResult,
    current_time: datetime | None,
) -> Writer3Candidate | None:
    aliases = _search_aliases(analysis)
    if not aliases:
        return None

    title = clean_text(reference.title)
    content = clean_text(reference.content)
    full_text = f"{title}\n{content}"
    matched_aliases = [alias for alias in aliases if alias_matches(full_text, alias)]
    if not matched_aliases:
        return None

    prior_types = EVENT_PRIOR_TYPES.get(analysis.current_event_type, [])
    matched_prior_types: list[str] = []
    for prior_type in prior_types:
        keywords = PRIOR_TYPE_KEYWORDS.get(prior_type, [])
        if any(alias_matches(full_text, keyword) for keyword in keywords):
            matched_prior_types.append(prior_type)
    if not matched_prior_types:
        return None

    score = 0.0
    for alias in matched_aliases:
        if alias_matches(title, alias):
            score += 3.0 if analysis.current_event_type in MATTER_EVENT_TYPES else 2.0
        else:
            score += 1.2 if analysis.current_event_type in MATTER_EVENT_TYPES else 1.0
    score += len(matched_prior_types) * 0.8
    score += _recency_bonus(reference.published_at, current_time)
    return Writer3Candidate(
        source_item_id=reference.source_item_id,
        source_url=reference.source_url,
        title=reference.title,
        content=reference.content,
        published_at=reference.published_at,
        score=score,
        matched_aliases=matched_aliases,
        matched_prior_types=matched_prior_types,
    )


def _search_aliases(analysis: AnalysisResult) -> list[str]:
    if analysis.current_event_type in MATTER_EVENT_TYPES:
        return dedupe_aliases([analysis.matter_key, *analysis.matter_aliases])
    return dedupe_aliases([analysis.focus_subject.name, *analysis.focus_subject.aliases])


def _recency_bonus(published_at: datetime | None, current_time: datetime | None) -> float:
    if not published_at or not current_time:
        return 0.0
    left = _ensure_utc(published_at)
    right = _ensure_utc(current_time)
    days = max(0.0, (right - left).total_seconds() / 86400)
    return max(0.0, 0.5 - min(days, 90.0) / 180.0)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
