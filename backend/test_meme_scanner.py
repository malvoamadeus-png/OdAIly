from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from packages.meme_scanner import scanner


def token(address: str, market_cap: float, volume_24h: float) -> scanner.Token:
    return scanner.Token(
        address=address,
        platform="fourmeme",
        name="Test Token",
        symbol="TEST",
        market_cap=market_cap,
        volume_24h=volume_24h,
        created_timestamp=None,
        raw={
            "address": address,
            "launchpad_platform": "fourmeme",
            "name": "Test Token",
            "symbol": "TEST",
            "usd_market_cap": market_cap,
            "volume_24h": volume_24h,
        },
    )


def args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(root / "scanner.sqlite3"),
        limit=80,
        milestone_interval=300,
        audit_dir=str(root / "audit"),
        narrative_command=None,
        narrative_timeout=1,
        push_timeout=1,
        send=False,
    )


class MemeScannerTests(unittest.TestCase):
    def test_token_info_accepts_nested_market_payload_without_repeated_address(self) -> None:
        payload = {
            "data": {
                "token": {"symbol": "NEST", "name": "Nested"},
                "metrics": {"market_cap": "160000", "volume_24h": "90000"},
            }
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(scanner, "ensure_cli_ready", return_value=True), patch.object(
            scanner.subprocess, "run", return_value=completed
        ):
            current = scanner.fetch_token_info("0x1111111111111111111111111111111111111111")
        self.assertIsNotNone(current)
        self.assertEqual((current.symbol, current.market_cap, current.volume_24h), ("NEST", 160_000.0, 90_000.0))

    def test_token_info_derives_market_cap_from_price_and_circulating_supply(self) -> None:
        address = "0x1111111111111111111111111111111111111111"
        payload = {
            "address": address,
            "symbol": "SUPPLY",
            "circulating_supply": "1000000",
            "launchpad_platform": "fourmeme",
            "price": {"address": address, "price": "0.16", "volume_24h": "90000"},
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(scanner, "ensure_cli_ready", return_value=True), patch.object(
            scanner.subprocess, "run", return_value=completed
        ):
            current = scanner.fetch_token_info(address)
        self.assertIsNotNone(current)
        self.assertEqual((current.market_cap, current.volume_24h), (160_000.0, 90_000.0))

    def test_store_migrates_legacy_unique_address_jobs_to_event_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scanner.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE jobs (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, address TEXT NOT NULL UNIQUE,
                  payload_json TEXT NOT NULL, trigger_kind TEXT NOT NULL, queued_at TEXT NOT NULL,
                  status TEXT NOT NULL, reason TEXT, narrative_json TEXT, title TEXT, content TEXT,
                  publish_json TEXT, updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO jobs(address, payload_json, trigger_kind, queued_at, status, updated_at)
                VALUES ('0xlegacy', '{}', 'startup_seen', '2026-01-01T00:00:00+00:00', 'discarded', '2026-01-01T00:00:00+00:00')"""
            )
            conn.commit()
            conn.close()
            store = scanner.Store(path)
            row = store.conn.execute("SELECT address, trigger_key, attempts FROM jobs").fetchone()
            self.assertEqual((row["address"], row["trigger_key"], row["attempts"]), ("0xlegacy", "legacy:0xlegacy", 0))
            store.close()

    def test_market_cap_milestones_trigger_once_per_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            first = token("0xlevels", 310_000, 200_000)
            second = token("0xlevels", 520_000, 300_000)
            self.assertEqual(scanner.evaluate_market_token(store, first, bootstrap=False), (0, 0))
            self.assertEqual(scanner.evaluate_market_token(store, second, bootstrap=False), (1, 0))
            self.assertEqual(scanner.evaluate_market_token(store, token("0xlevels", 490_000, 300_000), bootstrap=False), (0, 0))
            self.assertEqual(scanner.evaluate_market_token(store, second, bootstrap=False), (0, 0))
            rows = store.conn.execute(
                "SELECT trigger_key FROM jobs WHERE address=? ORDER BY id", (first.address,)
            ).fetchall()
            self.assertEqual([row["trigger_key"] for row in rows], ["market_cap:500000"])
            store.close()

    def test_text_shell_omits_launch_age_and_uses_news_wording(self) -> None:
        current = scanner.Token(
            address="0xkids",
            platform="telegram",
            name="Kids",
            symbol="KIDS",
            market_cap=360_000,
            volume_24h=220_000,
            created_timestamp=1,
            raw={},
        )
        title, content = scanner.format_text(
            current,
            "社群正在讨论其名称来源。",
            scanner.datetime.now(scanner.UTC),
            trigger_kind="tg_burst",
        )
        self.assertEqual(title, "Meme速递：BSC上KIDS社群热议中，市值36万美元")
        self.assertTrue(content.startswith("BSC上KIDS社群热议中，当前市值36万美元。"))
        self.assertNotIn("发射", title + content)
        self.assertNotIn("社区短时多次出现", title + content)

    def test_jump_across_multiple_levels_only_queues_highest_crossed_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            store.upsert_observation(token("0xjump", 200_000, 150_000))
            jumped = token("0xjump", 1_200_000, 800_000)
            self.assertEqual(scanner.evaluate_market_token(store, jumped, bootstrap=False), (1, 0))
            row = store.conn.execute("SELECT trigger_key, trigger_level FROM jobs").fetchone()
            self.assertEqual((row["trigger_key"], row["trigger_level"]), ("market_cap:1000000", 1_000_000.0))
            store.close()

    def test_collect_narrative_uses_odaily_internal_generator(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            current = token("0xv2", 400_000, 250_000)
            generated = {"reader_text": "A usable source narrative.", "telegram_messages": []}
            with patch.object(scanner, "generate_reader_text", return_value=generated) as generate:
                result = scanner.collect_narrative(
                    current,
                    command_template=None,
                    audit_dir=root,
                    timeout=1,
                    database_path=root / "scanner.sqlite3",
                )
            self.assertEqual(result["reader_text"], "A usable source narrative.")
            self.assertIsNone(result["command"])
            self.assertTrue(Path(result["output_path"]).exists())
            generate.assert_called_once()

    def test_first_poll_marks_existing_token_without_queuing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xaaa", 600_000, 400_000)
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(scanner, "fetch_milestone_tokens", return_value=([current], [])):
                scanner.scan_once(store, args(root))
            job = store.conn.execute("SELECT status, reason FROM jobs WHERE address=?", (current.address,)).fetchone()
            self.assertEqual((job["status"], job["reason"]), ("discarded", "startup_seen"))
            self.assertIsNotNone(store.observation(current.address))
            store.close()

    def test_new_token_below_volume_ratio_is_archived_without_narrative(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            store.mark_initialized()
            store.set_meta("milestone_initialized")
            store.set_meta("milestone_scan_at")
            current = token("0xbbb", 600_000, 100_000)
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]):
                scanner.scan_once(store, args(root))
            job = store.conn.execute("SELECT status, reason FROM jobs WHERE address=?", (current.address,)).fetchone()
            self.assertEqual((job["status"], job["reason"]), ("discarded", "volume_gate_failed"))
            store.close()

    def test_new_token_with_volume_generates_pending_draft_in_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            store.mark_initialized()
            store.set_meta("milestone_initialized")
            store.set_meta("milestone_scan_at")
            current = token("0xccc", 600_000, 350_000)
            narrative = {"returncode": 0, "reader_text": "社区账号表示，这个梗出自一条公开发言。"}
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(scanner, "collect_narrative", return_value=narrative):
                scanner.scan_once(store, args(root))
            job = store.conn.execute("SELECT status, title, content FROM jobs WHERE address=?", (current.address,)).fetchone()
            self.assertEqual(job["status"], "publisher_pending")
            self.assertIn("TEST", job["title"])
            self.assertIn(narrative["reader_text"], job["content"])
            self.assertNotIn("谨慎参与", job["content"])
            store.close()

    def test_force_contract_replays_a_startup_archived_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xforce", 600_000, 350_000)
            store.add_job(current, "startup_seen", "discarded", "startup_seen")
            store.upsert_observation(current)
            store.mark_initialized()
            store.set_meta("milestone_initialized")
            store.set_meta("milestone_scan_at")
            run_args = args(root)
            run_args.force_contract = current.address
            narrative = {"returncode": 0, "reader_text": "A usable source narrative."}
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(scanner, "collect_narrative", return_value=narrative):
                scanner.scan_once(store, run_args)
            job = store.conn.execute("SELECT trigger_kind, status, reason FROM jobs WHERE address=? ORDER BY id DESC", (current.address,)).fetchone()
            self.assertEqual((job["trigger_kind"], job["status"], job["reason"]), ("manual_replay", "publisher_pending", None))
            store.close()

    def test_push_uses_odaily_default_endpoint_when_not_overridden(self) -> None:
        with patch.object(scanner.requests, "post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = scanner.push_pending("title", "content", endpoint=scanner.DEFAULT_ODAILY_PUSH_ENDPOINT, timeout=1, send=True)
        self.assertTrue(result["ok"])
        self.assertEqual(post.call_args.args[0], "http://47.113.217.70:8501/push/data")

    def test_push_preserves_draft_flags_and_sends_idempotency_key(self) -> None:
        with patch.object(scanner.requests, "post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = scanner.push_pending(
                "title",
                "content",
                endpoint="http://example.test/push",
                timeout=1,
                send=True,
                idempotency_key="community-monitor:bsc:0xabc",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(post.call_args.kwargs["json"]["isPublish"], False)
        self.assertEqual(post.call_args.kwargs["json"]["isPush"], False)
        self.assertEqual(
            post.call_args.kwargs["headers"]["Idempotency-Key"],
            "community-monitor:bsc:0xabc",
        )

    def test_transient_narrative_failure_moves_job_to_retry_wait(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xretry", 400_000, 250_000)
            store.add_job(current, "first_seen_above_gate", "queued")
            narrative = {
                "returncode": 1,
                "reader_text": "",
                "transient_error": "narrative_command_failed",
            }
            with patch.object(scanner, "collect_narrative", return_value=narrative):
                result = scanner.process_one(store, args(root))
            row = store.conn.execute(
                "SELECT status, reason, attempts, next_attempt_at FROM jobs WHERE address=?",
                (current.address,),
            ).fetchone()
            self.assertEqual(result, "retry_wait")
            self.assertEqual((row["status"], row["reason"], row["attempts"]), ("retry_wait", "narrative_command_failed", 1))
            self.assertTrue(row["next_attempt_at"])
            store.close()

    def test_recover_inflight_jobs_returns_them_to_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            one = token("0xprocessing", 400_000, 250_000)
            two = token("0xpublishing", 400_000, 250_000)
            store.add_job(one, "first_seen_above_gate", "processing")
            store.add_job(two, "first_seen_above_gate", "publishing")
            self.assertEqual(store.recover_inflight(), 2)
            rows = store.conn.execute("SELECT status, reason FROM jobs ORDER BY id").fetchall()
            self.assertEqual(
                [(row["status"], row["reason"]) for row in rows],
                [("retry_wait", "service_restarted"), ("retry_wait", "service_restarted")],
            )
            store.close()

    def test_reader_text_with_internal_review_language_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            store.mark_initialized()
            store.set_meta("milestone_initialized")
            store.set_meta("milestone_scan_at")
            current = token("0xddd", 600_000, 350_000)
            narrative = {"returncode": 0, "reader_text": "这里不能写成官方币。"}
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(scanner, "collect_narrative", return_value=narrative):
                scanner.scan_once(store, args(root))
            job = store.conn.execute("SELECT status, reason FROM jobs WHERE address=?", (current.address,)).fetchone()
            self.assertEqual(job["status"], "discarded")
            self.assertEqual(job["reason"], "forbidden_reader_text_phrase:这里不能写成")
            store.close()

    def test_reader_text_with_project_page_wording_is_discarded(self) -> None:
        self.assertEqual(
            scanner.validate_reader_text("项目页面将 Brodie 描述为办公室狗。"),
            "forbidden_reader_text_phrase:项目页面将",
        )

    def test_reader_text_with_monitoring_summary_is_discarded(self) -> None:
        self.assertEqual(
            scanner.validate_reader_text(
                "TSHIRT 今日在 Telegram 社群出现集中提及，相关消息来自两个群组的多名用户。"
            ),
            "not_final_angle_phrase:今日在 Telegram 社群出现集中提及",
        )
        self.assertEqual(
            scanner.validate_reader_text("Telegram 中多条消息重复提及 bStonkBroker。"),
            "not_final_angle_phrase:Telegram 中多条消息重复提及",
        )
        self.assertEqual(
            scanner.validate_reader_text(
                "群聊 A 表示它是“知了”，也说“就是大金啊”；还有人感叹“卧槽真能飞啊”。"
            ),
            "not_final_angle_phrase:还有人感叹“卧槽真能飞",
        )
