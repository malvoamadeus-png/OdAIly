from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from packages.common.storage import connect_sqlite
from packages.competitor_monitor.events import EventAssignment
from packages.competitor_monitor.fetchers import NewsflashItem
from packages.competitor_monitor.sqlite_repository import SQLiteCompetitorMonitorRepository
from packages.competitor_monitor.worker import CompetitorMonitorWorker
from packages.newsflash_operations import NewsflashOperationsRepository
from packages.x_processing.sqlite_repository import SQLITE_SCHEMA_SQL

from openpyxl import Workbook


SHANGHAI = ZoneInfo("Asia/Shanghai")


class NewsflashOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "odaily.sqlite"
        with connect_sqlite(self.path) as conn:
            conn.executescript(SQLITE_SCHEMA_SQL)
            conn.commit()
        self.repository = NewsflashOperationsRepository(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_reference(self, source_item_id: str, title: str, published_at: str) -> None:
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "INSERT INTO odaily_reference_items(source_item_id,title,content,published_at) VALUES (?,?,?,?)",
                (source_item_id, title, title, published_at),
            )
            conn.commit()

    def test_source_facts_map_aliases_and_reconcile_ai(self) -> None:
        self.add_reference("1", "Human", "2026-07-20T09:00:00+08:00")
        self.add_reference("2", "Automated", "2026-07-20T10:00:00+08:00")
        with connect_sqlite(self.path) as conn:
            conn.execute("INSERT INTO tasks(source,source_item_id,content) VALUES ('x','x-1','source')")
            task_id = conn.execute("SELECT id FROM tasks WHERE source_item_id='x-1'").fetchone()[0]
            conn.execute(
                "INSERT INTO x_task_pipeline(task_id,final_title,publish_completed_at) VALUES (?,?,?)",
                (task_id, "Automated", "2026-07-20T09:58:00+08:00"),
            )
            conn.commit()

        result = self.repository.upsert_source_facts([
            {"source_item_id": "1", "operator_raw": "蔡聪", "view_count": 100, "is_pushed": 1},
            {"source_item_id": "2", "operator_raw": None, "view_count": 200, "is_pushed": 0},
        ])

        self.assertEqual(result["matched"], 2)
        with connect_sqlite(self.path) as conn:
            human = conn.execute("SELECT * FROM newsflash_operation_facts WHERE source_item_id='1'").fetchone()
            automated = conn.execute("SELECT * FROM newsflash_operation_facts WHERE source_item_id='2'").fetchone()
        self.assertEqual(human["publisher_person_key"], "harbour")
        self.assertEqual(human["publisher_kind"], "human")
        self.assertEqual(automated["publisher_kind"], "odaily_ai")

    def test_contribution_is_required_and_excluded_from_shift_summary(self) -> None:
        self.add_reference("10", "Counted", "2026-07-20T09:00:00+08:00")
        self.add_reference("11", "Contribution", "2026-07-20T10:00:00+08:00")
        self.repository.upsert_source_facts([
            {"source_item_id": "10", "operator_raw": "Z", "view_count": 100, "is_pushed": 1},
            {"source_item_id": "11", "operator_raw": "Zoey", "view_count": 500, "is_pushed": 1},
        ])
        self.repository.save_day_mode({"date": "2026-07-20", "mode": "three"}, actor_email="test@example.com")
        self.repository.save_assignment(
            {"date": "2026-07-20", "shift_key": "morning", "person_key": "zoey"},
            actor_email="test@example.com",
        )
        self.repository.update_newsflash(
            {
                "source_item_id": "11",
                "patch": {"is_contribution": True, "contributor_person_key": "asher", "contribution_type": "night"},
            },
            actor_email="test@example.com",
        )

        summary = self.repository.get_summary({"week_start": "2026-07-20"})
        morning = next(row for row in summary["rows"] if row["date"] == "2026-07-20" and row["shift_key"] == "morning")
        self.assertEqual(morning["published_count"], 1)
        self.assertEqual(morning["average_views"], 100)
        contributions = self.repository.list_contributions({"week_start": "2026-07-20"})
        asher = next(group for group in contributions["groups"] if group["person_key"] == "asher")
        self.assertEqual(asher["count"], 1)

    def test_adjacent_shifts_use_real_boundary_for_same_person(self) -> None:
        self.add_reference("20", "Before", "2026-07-20T13:20:00+08:00")
        self.add_reference("21", "After", "2026-07-20T13:40:00+08:00")
        self.repository.upsert_source_facts([
            {"source_item_id": "20", "operator_raw": "Z", "view_count": 10, "is_pushed": 0},
            {"source_item_id": "21", "operator_raw": "Z", "view_count": 20, "is_pushed": 0},
        ])
        self.repository.save_day_mode({"date": "2026-07-20", "mode": "three"}, actor_email="test@example.com")
        for shift in ("morning", "middle"):
            self.repository.save_assignment(
                {"date": "2026-07-20", "shift_key": shift, "person_key": "zoey"},
                actor_email="test@example.com",
            )

        summary = self.repository.get_summary({"week_start": "2026-07-20"})
        rows = {row["shift_key"]: row for row in summary["rows"] if row["date"] == "2026-07-20" and row["person_key"] == "zoey"}
        self.assertEqual(rows["morning"]["published_count"], 1)
        self.assertEqual(rows["middle"]["published_count"], 1)

    def test_month_summary_includes_full_natural_weeks_and_manual_cross_month_week(self) -> None:
        self.repository.save_week_month(
            {"week_start": "2026-06-29", "report_month": "2026-07"},
            actor_email="test@example.com",
        )
        with connect_sqlite(self.path) as conn:
            dates, label, weeks = self.repository._period_dates(conn, {"report_month": "2026-07"})
        self.assertEqual(label, "2026-07")
        self.assertEqual(weeks, ["2026-06-29", "2026-07-06", "2026-07-13", "2026-07-20"])
        self.assertEqual(len(dates), 28)

    def test_xlsx_import_updates_existing_rows_and_skips_missing_ids(self) -> None:
        self.add_reference("30", "Old title", "2026-07-20T08:00:00+08:00")
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["ID", "标题", "操作人", "链接", "发布时间", "阅读量", "是否推送", "推送时间"])
        sheet.append([30, "New title", "南枳", "https://example.com/30", datetime(2026, 7, 20, 9, 0), 321, "是", datetime(2026, 7, 20, 9, 1)])
        sheet.append([31, "Missing", "Z", "https://example.com/31", datetime(2026, 7, 20, 10, 0), 100, "否", None])
        path = Path(self.temp_dir.name) / "input.xlsx"
        workbook.save(path)

        result = self.repository.import_xlsx(path, start_date=date(2026, 7, 20), end_date=date(2026, 7, 27))

        self.assertEqual(result["read"], 2)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["skipped"], 1)
        with connect_sqlite(self.path) as conn:
            reference = conn.execute("SELECT title FROM odaily_reference_items WHERE source_item_id='30'").fetchone()
            fact = conn.execute("SELECT * FROM newsflash_operation_facts WHERE source_item_id='30'").fetchone()
        self.assertEqual(reference["title"], "New title")
        self.assertEqual(fact["publisher_person_key"], "malvo")
        self.assertEqual(fact["view_count"], 321)
        self.assertEqual(fact["is_pushed"], 1)

    def test_unmapped_human_is_counted_as_unassigned(self) -> None:
        self.add_reference("40", "Unknown", "2026-07-20T09:00:00+08:00")
        self.repository.upsert_source_facts([
            {"source_item_id": "40", "operator_raw": "Unknown Editor", "view_count": 50, "is_pushed": 0},
        ])
        summary = self.repository.get_summary({"week_start": "2026-07-20"})
        self.assertEqual(summary["unassigned_count"], 1)

    def assign_morning(self, duty_date: str, person_key: str = "zoey") -> None:
        self.repository.save_day_mode({"date": duty_date, "mode": "three"}, actor_email="test@example.com")
        self.repository.save_assignment(
            {"date": duty_date, "shift_key": "morning", "person_key": person_key},
            actor_email="test@example.com",
        )

    def test_quality_requires_known_pushed_views(self) -> None:
        self.add_reference("q0", "No view", "2026-08-03T09:00:00+08:00")
        self.repository.upsert_source_facts([
            {"source_item_id": "q0", "operator_raw": "Z", "view_count": None, "is_pushed": 1},
        ])

        result = self.repository.get_quality({"week_start": "2026-08-03"})

        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["pushed_count"], 1)
        self.assertEqual(result["pushed_view_count"], 0)
        self.assertEqual(result["qualified_count"], 0)

    def test_quality_uses_strict_week_threshold_and_has_no_kpi_cap(self) -> None:
        references = [
            ("base-1", "Baseline one", "2026-08-10T08:00:00+08:00"),
            ("base-2", "Baseline two", "2026-08-10T08:10:00+08:00"),
            ("equal", "Exactly threshold", "2026-08-10T09:00:00+08:00"),
            ("unassigned", "No shift", "2026-08-11T10:00:00+08:00"),
        ] + [(f"winner-{index}", f"Above threshold {index}", "2026-08-10T10:00:00+08:00") for index in range(51)]
        for values in references:
            self.add_reference(*values)
        self.repository.upsert_source_facts([
            {"source_item_id": "base-1", "operator_raw": None, "view_count": 100, "is_pushed": 1},
            {"source_item_id": "base-2", "operator_raw": None, "view_count": 300, "is_pushed": 1},
            {"source_item_id": "equal", "operator_raw": "Z", "view_count": 300, "is_pushed": 0},
            {"source_item_id": "unassigned", "operator_raw": "Z", "view_count": 999, "is_pushed": 0},
        ] + [
            {"source_item_id": f"winner-{index}", "operator_raw": "Z", "view_count": 301, "is_pushed": 0}
            for index in range(51)
        ])
        self.assign_morning("2026-08-10")

        result = self.repository.get_quality({"week_start": "2026-08-10"})

        self.assertEqual(result["average_views"], 200)
        self.assertEqual(result["threshold_views"], 300)
        self.assertEqual(result["qualified_count"], 51)
        self.assertEqual(result["total_kpi"], 10.2)
        self.assertEqual(result["unassigned_count"], 1)
        zoey = next(group for group in result["groups"] if group["person_key"] == "zoey")
        self.assertEqual(len(zoey["qualified"]), 51)

    def test_quality_records_multiple_exclusions_and_competitor_tie_passes(self) -> None:
        for source_item_id, title, published_at in (
            ("base", "Baseline", "2026-08-17T08:00:00+08:00"),
            ("excluded", "Excluded", "2026-08-17T09:00:00+08:00"),
            ("tie", "Tie", "2026-08-17T10:00:00+08:00"),
            ("media", "Media covered", "2026-08-17T11:00:00+08:00"),
        ):
            self.add_reference(source_item_id, title, published_at)
        self.repository.upsert_source_facts([
            {"source_item_id": "base", "operator_raw": None, "view_count": 100, "is_pushed": 1},
            {"source_item_id": "excluded", "operator_raw": "Z", "view_count": 200, "is_pushed": 0},
            {"source_item_id": "tie", "operator_raw": "Z", "view_count": 201, "is_pushed": 0},
            {"source_item_id": "media", "operator_raw": "Z", "view_count": 202, "is_pushed": 0},
        ])
        self.assign_morning("2026-08-17")
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "UPDATE odaily_reference_items SET raw_payload=?,content=? WHERE source_item_id='excluded'",
                (json.dumps({"sourceUrl": "https://twitter.com/LinChen91162689/status/1"}), "来自金十的消息"),
            )
            conn.execute("UPDATE odaily_reference_items SET source_url='https://markets.coindesk.com/story' WHERE source_item_id='media'")
            conn.execute("INSERT INTO x_capture_accounts(username,username_lower,enabled) VALUES ('LinChen91162689','linchen91162689',1)")
            conn.execute("UPDATE newsflash_operation_facts SET is_contribution=1,contributor_person_key='asher' WHERE source_item_id='excluded'")
            conn.commit()
        events = SQLiteCompetitorMonitorRepository(self.path)
        records = events.upsert_newsflash_items([
            NewsflashItem(source="blockbeats", source_item_id="b-ex", title="Excluded", content="x", published_at="2026-08-17T08:59:00+08:00"),
            NewsflashItem(source="odaily", source_item_id="excluded", title="Excluded", content="x", published_at="2026-08-17T09:00:00+08:00"),
        ])
        event_id = events.create_event_with_source(records[0])
        events.assign_item_to_event(EventAssignment(
            item_id=records[1].id, event_id=event_id, role="supporting", match_method="test",
            similarity=1.0, matched_item_id=records[0].id, ai_result={}, needs_review=False,
        ))
        tie_records = events.upsert_newsflash_items([
            NewsflashItem(source="blockbeats", source_item_id="b-tie", title="Tie", content="x", published_at="2026-08-17T10:00:00.900000+08:00"),
            NewsflashItem(source="odaily", source_item_id="tie", title="Tie", content="x", published_at="2026-08-17T10:00:00.100000+08:00"),
        ])
        tie_event_id = events.create_event_with_source(tie_records[0])
        events.assign_item_to_event(EventAssignment(
            item_id=tie_records[1].id, event_id=tie_event_id, role="supporting", match_method="test",
            similarity=1.0, matched_item_id=tie_records[0].id, ai_result={}, needs_review=False,
        ))

        result = self.repository.get_quality({"week_start": "2026-08-17"})

        zoey = next(group for group in result["groups"] if group["person_key"] == "zoey")
        excluded = next(item for item in zoey["excluded"] if item["source_item_id"] == "excluded")
        self.assertEqual(set(excluded["exclusion_reasons"]), {
            "contribution", "competitor_first", "regular_source", "automated_coverage", "jin10_content",
        })
        self.assertEqual([item["source_item_id"] for item in zoey["qualified"]], ["tie"])
        self.assertFalse(zoey["qualified"][0]["has_original_url"])
        media = next(item for item in zoey["excluded"] if item["source_item_id"] == "media")
        self.assertEqual(media["exclusion_reasons"], ["automated_coverage"])

    def test_quality_rule_snapshot_does_not_follow_later_monitor_changes(self) -> None:
        self.add_reference("snapshot-base", "Baseline", "2026-08-24T08:00:00+08:00")
        self.repository.upsert_source_facts([
            {"source_item_id": "snapshot-base", "operator_raw": None, "view_count": 100, "is_pushed": 1},
        ])
        with connect_sqlite(self.path) as conn:
            conn.execute("INSERT INTO x_capture_accounts(username,username_lower,enabled) VALUES ('Before','before',1)")
            conn.commit()
        before = self.repository.get_quality({"week_start": "2026-08-24"})
        with connect_sqlite(self.path) as conn:
            conn.execute("UPDATE x_capture_accounts SET enabled=0 WHERE username_lower='before'")
            conn.execute("INSERT INTO x_capture_accounts(username,username_lower,enabled) VALUES ('After','after',1)")
            conn.commit()
        after = self.repository.get_quality({"week_start": "2026-08-24"})

        self.assertEqual(before["rules"], after["rules"])
        self.assertIn("Before", after["rules"]["automated_x_accounts"])
        self.assertNotIn("After", after["rules"]["automated_x_accounts"])

    def test_quality_keyword_groups_and_manual_overrides(self) -> None:
        references = [
            ("override-base", "Baseline", "2026-08-31T08:00:00+08:00"),
            ("override-normal", "BTC 行情", "2026-08-31T09:00:00+08:00"),
            ("override-keyword", "BTC 发生爆仓", "2026-08-31T10:00:00+08:00"),
            ("override-low", "低浏览人工入选", "2026-08-31T11:00:00+08:00"),
            ("override-exclude", "人工排除", "2026-08-31T12:00:00+08:00"),
        ]
        for values in references:
            self.add_reference(*values)
        self.repository.upsert_source_facts([
            {"source_item_id": "override-base", "operator_raw": None, "view_count": 100, "is_pushed": 1},
            {"source_item_id": "override-normal", "operator_raw": "Z", "view_count": 200, "is_pushed": 0},
            {"source_item_id": "override-keyword", "operator_raw": "Z", "view_count": 200, "is_pushed": 0},
            {"source_item_id": "override-low", "operator_raw": "Z", "view_count": 50, "is_pushed": 0},
            {"source_item_id": "override-exclude", "operator_raw": "Z", "view_count": 201, "is_pushed": 0},
        ])
        self.assign_morning("2026-08-31")
        with connect_sqlite(self.path) as conn:
            conn.execute(
                "UPDATE odaily_reference_items SET content=? WHERE source_item_id='override-keyword'",
                ("BTC 市场出现爆仓",),
            )
            conn.commit()
        self.repository.update_newsflash(
            {"source_item_id": "override-low", "patch": {"quality_override": "include"}},
            actor_email="test@example.com",
        )
        self.repository.update_newsflash(
            {"source_item_id": "override-exclude", "patch": {"quality_override": "exclude"}},
            actor_email="test@example.com",
        )

        result = self.repository.get_quality({"week_start": "2026-08-31"})

        self.assertEqual(
            [(group["key"], group["terms"]) for group in result["rules"]["keyword_groups"]],
            [
                ("btc_liquidation", ["BTC", "爆仓"]),
                ("eth_liquidation", ["ETH", "爆仓"]),
                ("sol_liquidation", ["SOL", "爆仓"]),
            ],
        )
        zoey = next(group for group in result["groups"] if group["person_key"] == "zoey")
        self.assertEqual([item["source_item_id"] for item in zoey["qualified"]], ["override-low", "override-normal"])
        self.assertEqual([item["source_item_id"] for item in zoey["excluded"]], ["override-exclude", "override-keyword"])
        keyword = next(item for item in zoey["excluded"] if item["source_item_id"] == "override-keyword")
        self.assertEqual(keyword["exclusion_reasons"], ["keyword_btc_liquidation"])
        self.assertEqual(keyword["exclusion_reason_labels"], ["排除词：BTC + 爆仓"])
        manual = next(item for item in zoey["excluded"] if item["source_item_id"] == "override-exclude")
        self.assertEqual(manual["exclusion_reasons"], ["manual_exclude"])
        self.assertEqual(manual["exclusion_reason_labels"], ["人工排除"])
        low = next(item for item in zoey["qualified"] if item["source_item_id"] == "override-low")
        self.assertEqual(low["quality_override"], "include")
        self.assertEqual(result["total_kpi"], 0.4)
        listed = self.repository.list_newsflashes({"search": "低浏览人工入选"})
        self.assertEqual(listed["items"][0]["quality_override"], "include")

        with self.assertRaises(ValueError):
            self.repository.update_newsflash(
                {"source_item_id": "override-low", "patch": {"quality_override": "maybe"}},
                actor_email="test@example.com",
            )


class CompetitorEventSummaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "odaily.sqlite"
        self.repository = SQLiteCompetitorMonitorRepository(self.path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_missing_publish_time_does_not_win_and_exact_second_ties(self) -> None:
        items = [
            NewsflashItem(source="odaily", source_item_id="o1", title="Odaily", content="a", published_at="2026-07-20T10:00:00.100000+08:00", source_url=None, raw_payload={}, metadata={}),
            NewsflashItem(source="blockbeats", source_item_id="b1", title="Block", content="b", published_at="2026-07-20T10:00:00.900000+08:00", source_url=None, raw_payload={}, metadata={}),
            NewsflashItem(source="panews", source_item_id="p1", title="No time", content="c", published_at=None, source_url=None, raw_payload={}, metadata={}),
        ]
        records = self.repository.upsert_newsflash_items(items)
        event_id = self.repository.create_event_with_source(records[0])
        for record in records[1:]:
            self.repository.assign_item_to_event(EventAssignment(
                item_id=record.id,
                event_id=event_id,
                role="supporting",
                match_method="test",
                similarity=1.0,
                matched_item_id=records[0].id,
                needs_review=False,
                ai_result={},
            ))
        with connect_sqlite(self.path) as conn:
            event = conn.execute("SELECT * FROM newsflash_events WHERE event_id=?", (event_id,)).fetchone()
        self.assertEqual(json.loads(event["first_sources"]), ["blockbeats", "odaily"])
        self.assertEqual(event["representative_title"], "Odaily")
        self.assertTrue(event["first_published_at"].startswith("2026-07-20T02:00:00"))
        self.assertTrue(event["first_published_at"].endswith("+00:00"))

    def test_event_search_matches_source_item_titles(self) -> None:
        items = [
            NewsflashItem(
                source="jinse",
                source_item_id="j1",
                title="英伟达周三将发布业绩 分析师料营收增速继续领先大多数同行",
                content="英伟达将发布业绩。",
                published_at="2026-08-26T02:40:00+08:00",
            ),
            NewsflashItem(
                source="odaily",
                source_item_id="o1",
                title="美股盘前要闻一览：英伟达Q2财报今夜来袭",
                content="英伟达Q2财报今夜来袭。",
                published_at="2026-08-26T20:24:00+08:00",
            ),
        ]
        records = self.repository.upsert_newsflash_items(items)
        event_id = self.repository.create_event_with_source(records[0])
        self.repository.assign_item_to_event(EventAssignment(
            item_id=records[1].id,
            event_id=event_id,
            role="supporting",
            match_method="test",
            similarity=1.0,
            matched_item_id=records[0].id,
            needs_review=False,
            ai_result={},
        ))

        result = NewsflashOperationsRepository(self.path).list_events({"search": "美股盘前要闻一览"})

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["event_id"], event_id)

    def test_odaily_exclusion_is_removed_from_events_but_retained_for_pipeline(self) -> None:
        class Matcher:
            def is_excluded(self, **kwargs):
                return True

        worker = CompetitorMonitorWorker.__new__(CompetitorMonitorWorker)
        worker.exclusion_matcher = Matcher()
        item = NewsflashItem(source="odaily", source_item_id="excluded", title="Excluded", content="body")
        included, excluded = worker._split_event_items([item])
        self.assertEqual(included, [])
        self.assertEqual(excluded, [item])
        self.assertEqual(worker._exclude_pipeline_items([item]), [item])


if __name__ == "__main__":
    unittest.main()
