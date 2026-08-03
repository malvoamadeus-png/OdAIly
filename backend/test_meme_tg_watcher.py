from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from packages.meme_scanner import scanner, tg_watcher


ADDRESS = "0x1111111111111111111111111111111111111111"


class TelegramBurstTests(unittest.TestCase):
    def record(self, store: tg_watcher.MentionStore, index: int, chat: int, sender: int) -> bool:
        return store.record(
            address=ADDRESS,
            chat_id=chat,
            message_id=index,
            sender_key=str(sender),
            sent_at=datetime.now(UTC),
            text=f"call {ADDRESS} {index}",
            dedupe_key=f"message:{chat}:{index}:{ADDRESS}",
            window_minutes=20,
            cooldown_hours=6,
        )

    def test_community_buzz_requires_five_mentions_and_three_senders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = tg_watcher.MentionStore(Path(temp) / "scanner.sqlite3")
            self.assertFalse(self.record(store, 1, 100, 1))
            self.assertFalse(self.record(store, 2, 200, 2))
            self.assertFalse(self.record(store, 3, 200, 3))
            self.assertFalse(self.record(store, 4, 100, 1))
            self.assertTrue(self.record(store, 5, 200, 2))
            candidate = store.conn.execute("SELECT * FROM tg_candidates").fetchone()
            self.assertEqual((candidate["mention_count"], candidate["chat_count"], candidate["sender_count"]), (5, 2, 3))
            store.close()

    def test_five_mentions_still_require_three_distinct_senders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = tg_watcher.MentionStore(Path(temp) / "scanner.sqlite3")
            self.assertFalse(self.record(store, 1, 100, 1))
            self.assertFalse(self.record(store, 2, 100, 2))
            self.assertFalse(self.record(store, 3, 100, 1))
            self.assertFalse(self.record(store, 4, 100, 2))
            self.assertFalse(self.record(store, 5, 100, 1))
            self.assertTrue(self.record(store, 6, 100, 3))
            store.close()

    def test_qualifying_candidate_enters_normal_job_flow_after_market_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scanner.sqlite3"
            mentions = tg_watcher.MentionStore(path)
            self.record(mentions, 1, 100, 1)
            self.record(mentions, 2, 200, 2)
            self.record(mentions, 3, 200, 3)
            self.record(mentions, 4, 100, 1)
            self.record(mentions, 5, 200, 2)
            mentions.close()

            store = scanner.Store(path)
            market_token = scanner.Token(
                address=ADDRESS,
                platform="telegram",
                name="Burst",
                symbol="BURST",
                market_cap=360_000,
                volume_24h=200_000,
                created_timestamp=None,
                raw={
                    "address": ADDRESS,
                    "launchpad_platform": "telegram",
                    "symbol": "BURST",
                    "market_cap": 360_000,
                    "volume_24h": 200_000,
                },
            )
            with patch.object(scanner, "fetch_token_info", return_value=market_token):
                self.assertEqual(scanner.process_tg_candidate(store), (1, 0))
            job = store.conn.execute("SELECT trigger_kind, status FROM jobs").fetchone()
            self.assertEqual((job["trigger_kind"], job["status"]), ("tg_burst", "queued"))
            store.close()

    def test_community_buzz_below_300k_market_cap_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scanner.sqlite3"
            mentions = tg_watcher.MentionStore(path)
            for index, sender in enumerate((1, 2, 3, 1, 2), start=1):
                self.record(mentions, index, 100, sender)
            mentions.close()

            store = scanner.Store(path)
            market_token = scanner.Token(
                address=ADDRESS,
                platform="telegram",
                name="Burst",
                symbol="BURST",
                market_cap=299_999,
                volume_24h=200_000,
                created_timestamp=None,
                raw={
                    "address": ADDRESS,
                    "launchpad_platform": "telegram",
                    "symbol": "BURST",
                    "market_cap": 299_999,
                    "volume_24h": 200_000,
                },
            )
            with patch.object(scanner, "fetch_token_info", return_value=market_token):
                self.assertEqual(scanner.process_tg_candidate(store), (0, 1))
            candidate = store.conn.execute("SELECT status, reason FROM tg_candidates").fetchone()
            self.assertEqual((candidate["status"], candidate["reason"]), ("discarded", "tg_market_cap_gate_failed"))
            store.close()


if __name__ == "__main__":
    unittest.main()
