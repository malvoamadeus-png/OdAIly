from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
