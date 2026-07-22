from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.common.paths import ensure_runtime_dirs, get_paths
from packages.editor_plugin_local_store import LocalEditorPluginStore


def _now() -> datetime:
    return datetime.now(UTC)


def _source_label(source: str) -> str:
    return {
        "x": "X",
        "non_mainstream_media": "Crypto信源",
        "ai_source": "AI信源",
        "external_media_alert": "Crypto信源",
        "ai_source_alert": "AI信源",
        "blockbeats": "BlockBeats",
        "panews": "PANews",
        "jinse": "金色财经",
        "jin10": "金十",
    }.get(source, source)


def _news_type_label(news_type: str | None) -> str:
    return {
        "onchain": "链上",
        "funding": "融资",
        "non_mainstream_media": "Crypto信源",
        "ai_source": "AI信源",
        "mainstream_media": "Crypto信源",
    }.get(news_type or "", "常规")


def _odaily_newsflash_url(source_item_id: str | None, fallback: str | None) -> str | None:
    if source_item_id and str(source_item_id).isdigit():
        return f"https://www.odaily.news/zh-CN/newsflash/{source_item_id}"
    return fallback


class LocalEditorPluginFeedWriter:
    def __init__(self, store: LocalEditorPluginStore | None = None) -> None:
        if store is None:
            paths = get_paths()
            ensure_runtime_dirs(paths)
            store = LocalEditorPluginStore(paths.runtime_dir / "editor_plugin_local.sqlite")
        self.store = store

    def upsert_newsflash(
        self,
        *,
        task_id: int,
        source: str,
        source_url: str | None,
        title: str | None,
        content: str,
        status: str,
        source_item_id: str | None = None,
        news_type: str | None = None,
        publisher_decision: str | None = None,
        publisher_reason_code: str | None = None,
        publisher_category: str | None = None,
        x_account_is_ai_source: bool = False,
        feature_mode_enabled: bool = False,
        occurred_at: datetime | None = None,
    ) -> None:
        lane = "ai" if source == "ai_source" or x_account_is_ai_source else "high"
        badges: list[dict[str, Any] | None] = [
            {"label": "来源", "value": _source_label(source), "tone": "neutral"},
            {"label": "领域", "value": _news_type_label(news_type), "tone": "neutral"} if news_type else None,
            {"label": "模式", "value": "特色", "tone": "accent"} if feature_mode_enabled else None,
        ]
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": f"newsflash:{task_id}",
                    "feed_kind": "newsflash",
                    "lane": lane,
                    "priority": 92 if status == "ready_review" else 88,
                    "title": title or "未命名快讯",
                    "summary": content,
                    "badges": [item for item in badges if item],
                    "status_label": "挂后台" if status == "ready_review" else "已直发",
                    "status_tone": "manual" if status == "ready_review" else "success",
                    "occurred_at": occurred_at or _now(),
                    "source_url": source_url,
                    "detail_url": source_url,
                    "action_schema": {"type": "read"},
                    "meta_json": {
                        "task_id": task_id,
                        "source": source,
                        "task_status": status,
                        "publisher_decision": publisher_decision,
                        "publisher_reason_code": publisher_reason_code,
                        "publisher_category": publisher_category,
                        "news_type": news_type,
                        "x_account_is_ai_source": x_account_is_ai_source,
                        "feature_mode_enabled": feature_mode_enabled,
                        "source_item_id": source_item_id,
                    },
                }
            ]
        )

    def upsert_external_media_alert(
        self,
        *,
        task_id: int,
        source: str,
        source_url: str | None,
        title: str | None,
        content: str,
        metadata: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> None:
        site = str(metadata.get("site_display_name") or "").strip() or _source_label(source)
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": f"external_media_alert:{task_id}",
                    "feed_kind": "external_media_alert",
                    "lane": "high",
                    "priority": 86,
                    "title": title or content or "Crypto信源标题提醒",
                    "summary": content or title or "Crypto信源标题提醒暂无摘要",
                    "badges": [
                        {"label": "来源", "value": site, "tone": "neutral"},
                        {"label": "类型", "value": "标题提醒", "tone": "accent"},
                    ],
                    "status_label": "标题提醒",
                    "status_tone": "warning",
                    "occurred_at": occurred_at or _now(),
                    "source_url": source_url,
                    "detail_url": source_url,
                    "action_schema": {"type": "read"},
                    "meta_json": {
                        "task_id": task_id,
                        "source": source,
                        "task_status": "notified",
                        "site_key": metadata.get("site_key"),
                        "pipeline_mode": metadata.get("pipeline_mode"),
                        "discovery_mode": metadata.get("discovery_mode"),
                    },
                }
            ]
        )

    def upsert_auditor_alert(
        self,
        *,
        check_id: int,
        source_item_id: str,
        source_url: str | None,
        title: str | None,
        telegram_text: str,
        audit_result: dict[str, Any],
        occurred_at: datetime | None = None,
    ) -> None:
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": f"auditor:{check_id}",
                    "feed_kind": "auditor_alert",
                    "lane": "high",
                    "priority": 100,
                    "title": title or "审核者提醒",
                    "summary": telegram_text or str(audit_result.get("summary") or "审核者发现疑似文本问题"),
                    "badges": [],
                    "status_label": "审核者",
                    "status_tone": "warning",
                    "occurred_at": occurred_at or _now(),
                    "source_url": source_url,
                    "detail_url": _odaily_newsflash_url(source_item_id, source_url),
                    "action_schema": {"type": "feedback", "actions": ["accept", "reject"]},
                    "meta_json": {
                        "source_item_id": source_item_id,
                        "severity": audit_result.get("severity"),
                        "issues": audit_result.get("issues") or [],
                        "audit_summary": audit_result.get("summary"),
                    },
                }
            ]
        )

    def upsert_writer3_context(
        self,
        *,
        context_id: int,
        current_source: str,
        current_source_item_id: str,
        current_source_url: str | None,
        current_title: str | None,
        current_content: str,
        context_text: str,
        evidence_source_item_ids: list[str],
        occurred_at: datetime | None = None,
    ) -> None:
        if current_content and context_text:
            summary = f"原文：{current_content}\n\n此前消息：{context_text}"
        elif current_content:
            summary = f"原文：{current_content}"
        elif context_text:
            summary = f"此前消息：{context_text}"
        else:
            summary = "此前消息暂无摘要"
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": f"writer3:{context_id}",
                    "feed_kind": "writer3_context",
                    "lane": "low",
                    "priority": 64,
                    "title": current_title or "此前消息",
                    "summary": summary,
                    "badges": [],
                    "status_label": "此前消息",
                    "status_tone": "info",
                    "occurred_at": occurred_at or _now(),
                    "source_url": current_source_url,
                    "detail_url": _odaily_newsflash_url(current_source_item_id, current_source_url),
                    "action_schema": {"type": "feedback", "actions": ["accept", "reject"]},
                    "meta_json": {
                        "context_id": context_id,
                        "current_source": current_source,
                        "current_source_item_id": current_source_item_id,
                        "evidence_source_item_ids": evidence_source_item_ids,
                    },
                }
            ]
        )

    def upsert_whale_onchain(
        self,
        *,
        feed_item_id: str,
        address: str,
        address_label: str,
        chain_key: str,
        activity_type: str,
        direction: str | None,
        summary: str,
        tx_url: str | None,
        occurred_at: datetime | None = None,
    ) -> None:
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": feed_item_id,
                    "feed_kind": "whale_onchain",
                    "lane": "high",
                    "priority": 52,
                    "title": address_label or f"{address[:6]}...{address[-4:]}",
                    "summary": summary or "链上巨鲸信号",
                    "badges": [],
                    "status_label": "新信号",
                    "status_tone": "info",
                    "occurred_at": occurred_at or _now(),
                    "source_url": tx_url,
                    "detail_url": tx_url,
                    "action_schema": {"type": "read"},
                    "meta_json": {
                        "address": address,
                        "address_label": address_label,
                        "chain_key": chain_key,
                        "activity_type": activity_type,
                        "direction": direction,
                    },
                }
            ]
        )

    def upsert_whale_hyperliquid(
        self,
        *,
        feed_item_id: str,
        address: str,
        address_label: str,
        coin: str,
        direction: str,
        notional_usd: str,
        alert_kind: str,
        summary: str,
        detail_url: str | None,
        occurred_at: datetime | None = None,
    ) -> None:
        self.store.upsert_feed_items(
            [
                {
                    "feed_item_id": feed_item_id,
                    "feed_kind": "whale_hyperliquid",
                    "lane": "high",
                    "priority": 48,
                    "title": address_label or f"{address[:6]}...{address[-4:]}",
                    "summary": summary or "Hyperliquid 巨鲸信号",
                    "badges": [],
                    "status_label": "新信号",
                    "status_tone": "info",
                    "occurred_at": occurred_at or _now(),
                    "source_url": detail_url,
                    "detail_url": detail_url,
                    "action_schema": {"type": "read"},
                    "meta_json": {
                        "address": address,
                        "address_label": address_label,
                        "coin": coin,
                        "direction": direction,
                        "notional_usd": notional_usd,
                        "alert_kind": alert_kind,
                    },
                }
            ]
        )
