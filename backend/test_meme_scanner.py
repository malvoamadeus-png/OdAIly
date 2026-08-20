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


def token(address: str, market_cap: float, volume_24h: float, chain: str = scanner.CHAIN) -> scanner.Token:
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
        chain=chain,
    )


def args(root: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db=str(root / "scanner.sqlite3"),
        limit=80,
        completed_interval=300,
        token_info_high_interval=300,
        token_info_low_interval=3600,
        tracking_window=604800,
        token_info_min_gap=3,
        audit_dir=str(root / "audit"),
        narrative_command=None,
        narrative_timeout=1,
        push_timeout=1,
        send=False,
    )


class MemeScannerTests(unittest.TestCase):
    def test_volume_ratio_gate_interpolates_by_market_cap(self) -> None:
        cases = (
            (0, 0.5),
            (299_999, 0.5),
            (300_000, 0.5),
            (1_650_000, 0.35),
            (3_000_000, 0.2),
            (5_000_000, 0.2),
        )
        for market_cap, expected in cases:
            with self.subTest(market_cap=market_cap):
                self.assertAlmostEqual(scanner.volume_ratio_gate(market_cap), expected)

    def test_three_million_market_cap_accepts_twenty_percent_volume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            store.upsert_observation(token("0xscaled-volume", 2_900_000, 1_500_000))

            result = scanner.evaluate_market_token(
                store,
                token("0xscaled-volume", 3_000_000, 600_000),
                bootstrap=False,
            )

            self.assertEqual(result, (1, 0))
            job = store.conn.execute(
                "SELECT status, reason FROM jobs WHERE address='0xscaled-volume'"
            ).fetchone()
            self.assertEqual((job["status"], job["reason"]), ("queued", None))
            store.close()

    def test_completed_request_has_no_market_cap_filters(self) -> None:
        payload = {"data": {"completed": []}}
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(scanner, "ensure_cli_ready", return_value=True), patch.object(
            scanner.subprocess, "run", return_value=completed
        ) as run:
            scanner.fetch_completed_tokens(80)
        command = run.call_args.args[0]
        self.assertNotIn("--min-marketcap", command)
        self.assertNotIn("--max-marketcap", command)
        self.assertNotIn("--launchpad-platform", command)
        self.assertNotIn("--sort-by", command)

    def test_completed_request_keeps_unknown_launchpad_platforms(self) -> None:
        payload = {
            "completed": [
                {
                    "address": "0x1111111111111111111111111111111111111111",
                    "launchpad_platform": "flap_stocks",
                    "symbol": "STOCKS",
                    "name": "Stocks",
                    "usd_market_cap": 20_000,
                    "volume_24h": 15_000,
                }
            ]
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.object(scanner, "ensure_cli_ready", return_value=True), patch.object(
            scanner.subprocess, "run", return_value=completed
        ):
            tokens = scanner.fetch_completed_tokens(80)
        self.assertEqual([(token.platform, token.symbol) for token in tokens], [("flap_stocks", "STOCKS")])

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

    def test_telegram_token_info_accepts_unknown_launchpad_platform(self) -> None:
        address = "0x1111111111111111111111111111111111111111"
        robinhood_payload = {
            "address": address,
            "name": "Robinhood Token",
            "symbol": "RHO",
            "launchpad_platform": "pons_v2",
            "circulating_supply": "1000000000",
            "price": {"address": address, "price": "0.0012", "volume_24h": "900000"},
        }
        bsc_payload = {
            "address": address,
            "circulating_supply": "0",
            "price": {"address": "", "price": "0"},
        }

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            chain = command[command.index("--chain") + 1]
            payload = robinhood_payload if chain == "robinhood" else bsc_payload
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")

        with patch.object(scanner, "ensure_cli_ready", return_value=True), patch.object(
            scanner.subprocess, "run", side_effect=run
        ):
            current = scanner.fetch_tg_token_info(address, "evm")
        self.assertIsNotNone(current)
        self.assertEqual((current.platform, current.chain, current.market_cap), ("pons_v2", "robinhood", 1_200_000.0))

    def test_token_payload_accepts_unknown_launchpad_platform(self) -> None:
        current = scanner.token_from_row(
            {
                "address": "4xmegmrmd2tfqexxv39vtmp1r5ffvua7vcasmahlpump",
                "launchpad_platform": "pumpfun",
                "symbol": "GUNICORN",
                "name": "Gunicorn",
                "chain": "solana",
            }
        )
        self.assertIsNotNone(current)
        self.assertEqual((current.platform, current.chain), ("pumpfun", "solana"))

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
            self.assertEqual([row["trigger_key"] for row in rows], ["market_cap:0xlevels:500000"])
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
        self.assertTrue(content.startswith("据Odaily Meme速递监测，BSC上KIDS社群热议中，当前市值36万美元。"))
        self.assertNotIn("发射", title + content)
        self.assertNotIn("社区短时多次出现", title + content)

    def test_jump_across_multiple_levels_only_queues_highest_crossed_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            store.upsert_observation(token("0xjump", 200_000, 150_000))
            jumped = token("0xjump", 1_200_000, 800_000)
            self.assertEqual(scanner.evaluate_market_token(store, jumped, bootstrap=False), (1, 0))
            row = store.conn.execute("SELECT trigger_key, trigger_level FROM jobs").fetchone()
            self.assertEqual((row["trigger_key"], row["trigger_level"]), ("market_cap:0xjump:1000000", 1_000_000.0))
            store.close()

    def test_tg_burst_does_not_consume_market_cap_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            store.mark_initialized()
            store.set_meta("milestone_initialized")
            address = "0xburst-milestone"
            first = token(address, 400_000, 300_000)
            burst = token(address, 1_100_000, 700_000)
            current = token(address, 1_200_000, 800_000)
            self.assertEqual(scanner.evaluate_market_token(store, first, bootstrap=False), (0, 0))
            stamp = scanner.now_iso()
            store.conn.execute(
                """INSERT INTO tg_candidates(
                  address, detected_at, window_start, mention_count, chat_count,
                  sender_count, evidence_json, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (address, stamp, stamp, 5, 2, 3, "{}", stamp),
            )
            store.conn.commit()
            with patch.object(scanner, "fetch_token_info", return_value=burst):
                self.assertEqual(scanner.process_tg_candidate(store), (1, 0))
            observation = store.conn.execute(
                "SELECT last_market_cap, highest_market_cap, tracking_status FROM observations WHERE address=?",
                (address,),
            ).fetchone()
            self.assertEqual((observation["last_market_cap"], observation["highest_market_cap"]), (1_100_000.0, 400_000.0))
            self.assertEqual(scanner.evaluate_market_token(store, current, bootstrap=False), (1, 0))
            row = store.conn.execute(
                "SELECT trigger_kind, trigger_key, trigger_level FROM jobs WHERE trigger_kind='market_cap_milestone'"
            ).fetchone()
            self.assertEqual(
                (row["trigger_kind"], row["trigger_key"], row["trigger_level"]),
                ("market_cap_milestone", "market_cap:0xburst-milestone:1000000", 1_000_000.0),
            )
            self.assertEqual(observation["tracking_status"], "legacy_untracked")
            store.close()

    def test_completed_reactivates_tg_observation_and_records_snapshot_milestone(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xreactivate", 520_000, 400_000)
            stamp = scanner.now_iso()
            store.upsert_observation(current, advance_market_cap_high_watermark=False, tracking_source="tg")
            self.assertEqual(store.observation(current.address)["tracking_status"], "legacy_untracked")
            previous_high, _, status = store.start_or_observe_completed(
                current, observed_at=stamp, tracking_window_seconds=604800, args=args(root)
            )
            self.assertEqual((previous_high, status), (0.0, "active"))
            self.assertEqual(scanner.evaluate_market_token(store, current, bootstrap=False), (1, 0))
            snapshot = store.conn.execute(
                "SELECT address, chain, platform, market_cap, source FROM token_snapshots WHERE address=?",
                (current.address,),
            ).fetchone()
            self.assertEqual(tuple(snapshot), (current.address, "bsc", "fourmeme", 520000.0, "completed"))
            milestone = store.conn.execute(
                "SELECT level, snapshot_id, status FROM market_cap_milestones WHERE address=?",
                (current.address,),
            ).fetchone()
            self.assertEqual((milestone["level"], milestone["snapshot_id"] > 0, milestone["status"]), (500000.0, True, "detected"))
            store.close()

    def test_legacy_observations_are_not_backfilled_into_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scanner.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE observations (
                  address TEXT PRIMARY KEY, platform TEXT NOT NULL, symbol TEXT NOT NULL,
                  last_market_cap REAL NOT NULL, last_seen_at TEXT NOT NULL,
                  triggered_at TEXT, published_at TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO observations(address, platform, symbol, last_market_cap, last_seen_at)
                VALUES ('0xold', 'fourmeme', 'OLD', 400000, '2026-08-01T00:00:00+00:00')"""
            )
            conn.commit()
            conn.close()
            store = scanner.Store(path)
            row = store.observation("0xold")
            self.assertEqual(row["tracking_status"], "legacy_untracked")
            self.assertEqual(row["chain"], "bsc")
            self.assertIsNone(row["tracking_started_at"])
            store.close()

    def test_legacy_observation_chain_is_backfilled_from_latest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scanner.sqlite3"
            conn = sqlite3.connect(path)
            conn.execute(
                """CREATE TABLE observations (
                  address TEXT PRIMARY KEY, platform TEXT NOT NULL, symbol TEXT NOT NULL,
                  last_market_cap REAL NOT NULL, last_seen_at TEXT NOT NULL,
                  triggered_at TEXT, published_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE token_snapshots (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  address TEXT NOT NULL, chain TEXT NOT NULL, platform TEXT NOT NULL,
                  symbol TEXT NOT NULL, name TEXT NOT NULL, market_cap REAL NOT NULL,
                  volume_24h REAL NOT NULL, observed_at TEXT NOT NULL, source TEXT NOT NULL,
                  scan_id TEXT, payload_json TEXT NOT NULL
                )"""
            )
            conn.execute(
                """INSERT INTO observations(address, platform, symbol, last_market_cap, last_seen_at)
                VALUES ('sol-token', 'pumpfun', 'SOL', 400000, '2026-08-01T00:00:00+00:00')"""
            )
            conn.execute(
                """INSERT INTO token_snapshots(
                  id, address, chain, platform, symbol, name, market_cap, volume_24h,
                  observed_at, source, payload_json
                ) VALUES (1, 'sol-token', 'solana', 'pumpfun', 'SOL', 'Sol', 400000, 200000,
                  '2026-08-02T00:00:00+00:00', 'legacy', '{}')"""
            )
            conn.commit()
            conn.close()

            store = scanner.Store(path)
            self.assertEqual(store.observation("sol-token")["chain"], "solana")
            store.close()

    def test_token_info_uses_observation_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("sol-token", 800_000, 500_000, chain="solana")
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute(
                "UPDATE observations SET next_token_info_at=?, last_completed_seen_at=? WHERE address=?",
                ("2000-01-01T00:00:00+00:00", "old-scan", current.address),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args = args(root)
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", return_value=current) as token_info:
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "observed")
            token_info.assert_called_once_with(current.address, "solana")
            self.assertEqual(store.observation(current.address)["chain"], "solana")
            store.close()

    def test_first_discovery_at_three_million_queues_only_three_million(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xthree", 3_200_000, 2_000_000)
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]):
                scanner.discover_once(store, args(root))
            rows = store.conn.execute("SELECT trigger_key FROM jobs WHERE address=?", (current.address,)).fetchall()
            self.assertEqual([row["trigger_key"] for row in rows], ["market_cap:0xthree:3000000"])
            store.close()

    def test_completed_token_does_not_call_token_info_while_in_latest_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xin-list", 600_000, 400_000)
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(
                scanner, "fetch_token_info"
            ) as token_info:
                scanner.discover_once(store, args(root))
                result = scanner.process_due_token_info(store, args(root))
            self.assertEqual(result["status"], "idle")
            token_info.assert_not_called()
            store.close()

    def test_out_of_list_low_cap_uses_hour_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xlow", 400_000, 300_000)
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute(
                "UPDATE observations SET next_token_info_at=?, last_completed_seen_at=? WHERE address=?",
                ("2000-01-01T00:00:00+00:00", "old-scan", current.address),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args = args(root)
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", return_value=token("0xlow", 400_000, 300_000)) as token_info:
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "observed")
            token_info.assert_called_once_with(current.address)
            self.assertEqual(store.observation(current.address)["tracking_interval_seconds"], 3600)
            store.close()

    def test_out_of_list_high_cap_uses_five_minute_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xhigh", 800_000, 500_000)
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute(
                "UPDATE observations SET next_token_info_at=?, last_completed_seen_at=? WHERE address=?",
                ("2000-01-01T00:00:00+00:00", "old-scan", current.address),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args = args(root)
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", return_value=token("0xhigh", 800_000, 500_000)):
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "observed")
            self.assertEqual(store.observation(current.address)["tracking_interval_seconds"], 300)
            store.close()

    def test_crossing_five_hundred_thousand_switches_to_high_interval(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xcross", 400_000, 300_000)
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute(
                "UPDATE observations SET next_token_info_at=?, last_completed_seen_at=? WHERE address=?",
                ("2000-01-01T00:00:00+00:00", "old-scan", current.address),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args = args(root)
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", return_value=token("0xcross", 600_000, 400_000)):
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "observed")
            self.assertEqual(store.observation(current.address)["tracking_interval_seconds"], 300)
            store.close()

    def test_completed_reappearance_suppresses_due_token_info(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xback", 800_000, 500_000)
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute("UPDATE observations SET next_token_info_at='2000-01-01T00:00:00+00:00' WHERE address=?", (current.address,))
            store.conn.commit()
            run_args = args(root)
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(
                scanner, "fetch_token_info"
            ) as token_info:
                scanner.discover_once(store, run_args)
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "idle")
            token_info.assert_not_called()
            store.close()

    def test_expired_tracking_does_not_poll_or_reactivate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xexpire", 800_000, 500_000)
            store.start_or_observe_completed(current, observed_at=scanner.now_iso(), tracking_window_seconds=1, args=args(root))
            store.conn.execute(
                "UPDATE observations SET tracking_expires_at='2000-01-01T00:00:00+00:00', next_token_info_at='2000-01-01T00:00:00+00:00' WHERE address=?",
                (current.address,),
            )
            store.conn.commit()
            with patch.object(scanner, "fetch_token_info") as token_info:
                result = scanner.process_due_token_info(store, args(root))
                _, _, status = store.start_or_observe_completed(
                    current, observed_at=scanner.now_iso(), tracking_window_seconds=604800, args=args(root)
                )
            self.assertEqual(result["status"], "idle")
            self.assertEqual(status, "expired")
            self.assertEqual(store.observation(current.address)["tracking_status"], "expired")
            token_info.assert_not_called()
            store.close()

    def test_token_info_failure_preserves_high_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xfail", 1_200_000, 800_000)
            old_time = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            store.start_or_observe_completed(current, observed_at=old_time, tracking_window_seconds=604800, args=args(root))
            store.conn.execute(
                "UPDATE observations SET next_token_info_at='2000-01-01T00:00:00+00:00', last_completed_seen_at='old-scan' WHERE address=?",
                (current.address,),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args = args(root)
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", side_effect=RuntimeError("429 ~12s remaining")):
                result = scanner.process_due_token_info(store, run_args)
            row = store.observation(current.address)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(row["highest_market_cap"], 1_200_000.0)
            self.assertEqual(row["last_market_cap"], 1_200_000.0)
            self.assertEqual(row["token_info_failures"], 1)
            self.assertIn("429", row["last_token_info_error"])
            store.close()

    def test_jitter_is_stable_and_minimum_gap_is_enforced(self) -> None:
        first = scanner.next_tracking_time("0xphase-a", scanner.datetime.now(scanner.UTC), 300)
        second = scanner.next_tracking_time("0xphase-b", scanner.datetime.now(scanner.UTC), 300)
        self.assertNotEqual(scanner.tracking_phase_seconds("0xphase-a", 300), scanner.tracking_phase_seconds("0xphase-b", 300))
        self.assertNotEqual(first, second)
        with tempfile.TemporaryDirectory() as temp:
            store = scanner.Store(Path(temp) / "scanner.sqlite3")
            moment = scanner.datetime.now(scanner.UTC)
            store.mark_token_info_request(moment)
            allowed, remaining = store.token_info_request_allowed(moment + scanner.timedelta(seconds=2), 3)
            self.assertFalse(allowed)
            self.assertGreater(remaining, 0)
            allowed, _ = store.token_info_request_allowed(moment + scanner.timedelta(seconds=3), 3)
            self.assertTrue(allowed)
            store.close()

    def test_thirty_first_token_remains_observable_after_completed_rollover(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            run_args = args(root)
            tokens = [token(f"0xroll-{index}", 800_000, 500_000) for index in range(31)]
            observed_at = (scanner.datetime.now(scanner.UTC) - scanner.timedelta(hours=2)).isoformat()
            for current in tokens:
                store.start_or_observe_completed(current, observed_at=observed_at, tracking_window_seconds=604800, args=run_args)
            oldest = tokens[-1]
            store.conn.execute(
                "UPDATE observations SET next_token_info_at='2000-01-01T00:00:00+00:00', last_completed_seen_at='old-scan' WHERE address=?",
                (oldest.address,),
            )
            store.conn.commit()
            store.set_meta("completed_scan_at", "latest-scan")
            run_args.token_info_min_gap = 0
            with patch.object(scanner, "fetch_token_info", return_value=oldest) as token_info:
                result = scanner.process_due_token_info(store, run_args)
            self.assertEqual(result["status"], "observed")
            token_info.assert_called_once_with(oldest.address)
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

    def test_first_poll_starts_seven_day_tracking_and_queues_highest_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = scanner.Store(root / "scanner.sqlite3")
            current = token("0xaaa", 600_000, 400_000)
            narrative = {"returncode": 0, "reader_text": "A usable source narrative."}
            with patch.object(scanner, "fetch_completed_tokens", return_value=[current]), patch.object(
                scanner, "collect_narrative", return_value=narrative
            ):
                scanner.scan_once(store, args(root))
            job = store.conn.execute("SELECT trigger_key FROM jobs WHERE address=?", (current.address,)).fetchone()
            observation = store.observation(current.address)
            self.assertEqual(job["trigger_key"], "market_cap:0xaaa:500000")
            self.assertEqual(observation["tracking_status"], "active")
            self.assertEqual(observation["tracking_source"], "completed")
            self.assertTrue(observation["tracking_started_at"])
            self.assertTrue(observation["tracking_expires_at"])
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
        self.assertEqual(post.call_args.kwargs["json"]["content"], "<p>content</p>")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Idempotency-Key"],
            "community-monitor:bsc:0xabc",
        )

    def test_push_converts_meme_content_to_paragraph_html_and_escapes_markup(self) -> None:
        with patch.object(scanner.requests, "post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.status_code = 200
            post.return_value.text = "ok"
            result = scanner.push_pending(
                "title",
                "第一段。\n\n第二段包含 A&B < C。",
                endpoint="http://example.test/push",
                timeout=1,
                send=True,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(
            post.call_args.kwargs["json"]["content"],
            "<p>第一段。</p><p>第二段包含 A&amp;B &lt; C。</p>",
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
