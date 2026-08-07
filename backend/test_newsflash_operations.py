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
