from __future__ import annotations

from datetime import timedelta

from packages.editor_plugin_local_store import LocalEditorPluginStore, utc_now


def test_local_feed_high_lane_uses_timeline_order_and_normalizes_whales(tmp_path) -> None:
    store = LocalEditorPluginStore(tmp_path / "feed.sqlite")
    now = utc_now()

    store.upsert_feed_items(
        [
            {
                "feed_item_id": "auditor:1",
                "feed_kind": "auditor_alert",
                "lane": "high",
                "priority": 100,
                "title": "auditor",
                "summary": "older auditor",
                "occurred_at": now - timedelta(minutes=10),
            },
            {
                "feed_item_id": "whale_onchain:1",
                "feed_kind": "whale_onchain",
                "lane": "low",
                "priority": 52,
                "title": "whale",
                "summary": "newer whale",
                "occurred_at": now - timedelta(minutes=5),
            },
            {
                "feed_item_id": "newsflash:1",
                "feed_kind": "newsflash",
                "lane": "high",
                "priority": 1,
                "title": "news",
                "summary": "newest news",
                "occurred_at": now,
            },
        ]
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE editor_plugin_local_feed_items SET lane = 'low' WHERE feed_kind = 'whale_onchain'"
        )

    rows = store.list_feed_items(limit=10, max_age_hours=24)

    assert [row["feed_item_id"] for row in rows] == [
        "newsflash:1",
        "whale_onchain:1",
        "auditor:1",
    ]
    assert rows[1]["lane"] == "high"
