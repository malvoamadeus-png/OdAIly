from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.editor_plugin_feed_writer import LocalEditorPluginFeedWriter
from packages.editor_plugin_local_store import LocalEditorPluginStore


def test_gate_and_meme_feed_items_have_distinct_identity(tmp_path: Path) -> None:
    store = LocalEditorPluginStore(tmp_path / "feed.sqlite")
    writer = LocalEditorPluginFeedWriter(store)
    occurred_at = datetime.now(UTC)

    writer.upsert_gate_market(
        event_id=12,
        symbol="XAUUSD",
        display_name="黄金",
        title="黄金上涨突破4400美元/盎司",
        content="据 Gate 数据，黄金现报4401美元/盎司。",
        mode="live",
        trigger_level="4400",
        direction="up",
        occurred_at=occurred_at,
    )
    writer.upsert_meme_digest(
        job_id=34,
        address="0xabc",
        platform="fourmeme",
        symbol="TEST",
        title="Meme速递：BSC上TEST市值突破50万美元",
        content="TEST 市值达到50万美元。",
        trigger_kind="first_seen_above_gate",
        market_cap=500_000,
        occurred_at=occurred_at,
    )

    rows = {row["feed_kind"]: row for row in store.list_feed_items(limit=10, max_age_hours=2)}
    assert rows["gate_market"]["status_label"] == "行情直发"
    assert rows["gate_market"]["status_tone"] == "market"
    assert rows["gate_market"]["lane"] == "high"
    assert rows["meme_digest"]["status_label"] == "Meme挂后台"
    assert rows["meme_digest"]["status_tone"] == "meme"
    assert rows["meme_digest"]["meta_json"]["address"] == "0xabc"


def test_auto_newsflash_event_feed_item_is_idempotent_and_links_to_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EDITOR_PLUGIN_CONSOLE_URL", "https://console.example.test/#tasks")
    store = LocalEditorPluginStore(tmp_path / "feed.sqlite")
    writer = LocalEditorPluginFeedWriter(store)
    occurred_at = datetime.now(UTC)

    writer.upsert_auto_newsflash_event(
        event_id="tracking-event:giwa",
        title="GIWA 假桥事件",
        occurred_at=occurred_at,
    )
    writer.upsert_auto_newsflash_event(
        event_id="tracking-event:giwa",
        title="GIWA 假桥事件更新标题",
        occurred_at=occurred_at,
    )

    rows = store.list_feed_items(limit=10, max_age_hours=2)
    assert len(rows) == 1
    assert rows[0]["feed_item_id"] == "tracking-event:giwa"
    assert rows[0]["feed_kind"] == "auto_newsflash_event"
    assert rows[0]["lane"] == "high"
    assert rows[0]["title"] == "热点自动快讯新增：GIWA 假桥事件更新标题"
    assert rows[0]["action_schema"]["url"] == (
        "https://console.example.test/?view=x_agent&x_agent_tab=auto_newsflash&event_id=tracking-event%3Agiwa"
    )


def test_business_news_plugin_notice_contains_only_the_customer_sentence(tmp_path: Path) -> None:
    store = LocalEditorPluginStore(tmp_path / "feed.sqlite")
    writer = LocalEditorPluginFeedWriter(store)
    occurred_at = datetime.now(UTC)

    writer.upsert_business_news_notice(inbox_id=91, customer_name="Soso", occurred_at=occurred_at)

    rows = store.list_feed_items(limit=10, max_age_hours=2)
    assert len(rows) == 1
    assert rows[0]["feed_kind"] == "business_news"
    assert rows[0]["title"] == "Soso商务有新消息"
    assert rows[0]["summary"] == ""
    assert rows[0]["status_label"] is None
    assert rows[0]["badges"] == []


def test_manual_rejection_outbox_is_written_with_feedback_and_survives_feedback_pruning(tmp_path: Path) -> None:
    store = LocalEditorPluginStore(tmp_path / "feedback.sqlite")
    result = store.record_feedback(
        feed_item_id="auditor:41", feed_kind="auditor_alert", feedback="reject",
        actor_user_id="editor", actor_email="editor@example.com", actor_display_name="Editor",
        session_id=None, extra_json={},
    )
    pending = store.pending_manual_rejections()
    assert [(item.id, item.auditor_check_id, item.actor_email, item.status) for item in pending] == [
        (result["feedback_id"], 41, "editor@example.com", "pending")
    ]
    with store._connect() as conn:
        conn.execute(
            "UPDATE editor_plugin_local_feedbacks SET sync_status='synced',created_at=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(days=120)).isoformat(), result["feedback_id"]),
        )
    store.prune_old(synced_feedback_retention_days=90)
    with store._connect() as conn:
        row = conn.execute("SELECT manual_rejection_status FROM editor_plugin_local_feedbacks WHERE id=?", (result["feedback_id"],)).fetchone()
        assert row["manual_rejection_status"] == "pending"
    store.mark_manual_rejection_queued(result["feedback_id"])
    assert store.pending_manual_rejections()[0].status == "queued"
    store.mark_manual_rejection_complete(result["feedback_id"])
    assert store.pending_manual_rejections() == []


def test_hyperliquid_completed_draft_uses_existing_plugin_alert_and_console_link(tmp_path, monkeypatch):
    monkeypatch.setenv('EDITOR_PLUGIN_CONSOLE_URL','https://console.example.test/#tasks')
    store=LocalEditorPluginStore(tmp_path/'feed.sqlite');writer=LocalEditorPluginFeedWriter(store)
    for _ in range(2):
        writer.upsert_newsflash(task_id=123,source='hyperliquid_news',source_item_id='event123',
            source_url='https://hyperbot.network/trader/0x123',title='总盈利地址加仓',content='正文',status='ready_review')
    rows=store.list_feed_items(limit=10,max_age_hours=2)
    assert len(rows)==1
    row=rows[0]
    assert row['feed_kind']=='newsflash' and row['status_label']=='挂后台'
    assert row['detail_url']=='https://console.example.test/?view=hyperliquid_news&event_key=event123'
    assert any(b['value']=='Hyperliquid监测' for b in row['badges'])
