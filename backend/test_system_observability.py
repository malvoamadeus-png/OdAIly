from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from packages.system_observability import MainTaskProjector, SystemInformationStore
from packages.system_observability.projector import SourceAuditInstaller, TABLE_SPECS, TableSpec
from packages.system_observability.reviews import SystemReviewRepository, SystemReviewService, SystemReviewWorker
from packages.system_observability.runtime import system_observability_directory
from packages.system_observability.store import timestamp_ms
from packages.x_processing.models import PromptTemplateVersion
from packages.x_processing.sqlite_repository import SQLiteXProcessingRepository


def _source_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT,
                title TEXT, content TEXT, status TEXT, created_at TEXT, updated_at TEXT, metadata TEXT
            );
            CREATE TABLE x_task_pipeline (
                task_id INTEGER PRIMARY KEY, final_title TEXT, final_content TEXT, writer_output TEXT,
                publisher_output TEXT, publisher_reason_code TEXT, last_error TEXT, push_result TEXT,
                judge_completed_at TEXT, search_completed_at TEXT, write_completed_at TEXT,
                format_completed_at TEXT, publisher_decided_at TEXT, publish_completed_at TEXT
            );
            INSERT INTO tasks VALUES
                (1,'x','post-1','https://x.example/1','初始标题','区块链交易所发布重要公告','write_failed','2024-03-01 10:00:00',NULL,'{}'),
                (2,'jin10','item-2','https://jin10.example/2','市场消息','美股市场收盘','publisher_pending','2024-03-01T11:00:00+08:00',NULL,'{}'),
                (3,'unknown_new','item-3','https://source.example/3','未知来源','当前状态可查','discarded','2024-03-01 12:00:00',NULL,'{}');
            INSERT INTO x_task_pipeline(task_id,final_title,final_content,writer_output,publisher_output,last_error,write_completed_at)
            VALUES (1,'定稿标题','区块链交易所发布公告。','{"reason":"材料完整"}','{}','解析错误', '2024-03-01T02:00:00Z');
            """
        )


def test_projector_is_idempotent_searches_cjk_and_preserves_observed_changes() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        _source_db(source)
        store = SystemInformationStore(root / "runtime" / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, batch_size=100)

        first = projector.run_once()
        assert first == {"read": 3, "changed": 3}
        assert projector.run_once() == {"read": 3, "changed": 0}

        found = store.query({"q": "区块链交易"})
        assert found["total"] == 1
        assert found["items"][0]["task_id"] == 1
        assert found["items"][0]["business_key"] == "x"
        assert store.query({"q": "市场消息"})["items"][0]["source"] == "jin10"
        assert store.query({"source": "unknown_new"})["items"][0]["business_key"] == "unmapped_source"
        with pytest.raises(ValueError, match="两个字符"):
            store.query({"q": "币"})

        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE tasks SET status='auto_published',updated_at='2024-03-01T03:00:00Z' WHERE id=1")
            conn.execute("UPDATE x_task_pipeline SET push_result=? WHERE task_id=1", (json.dumps({"ok": True}),))
        assert projector.run_once()["changed"] == 1

        detail = store.detail("task:x:post-1")
        assert detail is not None
        assert detail["result"] == "direct_publish"
        assert len(detail["timeline"]) == 2
        assert detail["timeline"][0]["event_type"] == "legacy_snapshot"
        assert detail["timeline"][1]["event_type"] == "observed_change"
        assert detail["timeline"][1]["evidence"]["pipeline"]["push_result"] == '{"ok": true}'
        assert detail["legacy_history"] == 1


def test_observability_sqlite_connections_close_when_context_exits() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SystemInformationStore(root / "runtime" / "system_information.sqlite")
        store.initialize()
        repository = SystemReviewRepository(root / "runtime" / "system_information.sqlite")
        connections = [store.connect(), repository.connect()]
        for connection in connections:
            with connection as active:
                assert active.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                connection.execute("SELECT 1")

        source = root / "source.sqlite"
        _source_db(source)
        for connection in (
            MainTaskProjector(source, store)._read(),
            SystemReviewService._connect_path(source),
        ):
            with connection as active:
                assert active.execute("SELECT 1").fetchone()[0] == 1
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                connection.execute("SELECT 1")


def test_observability_store_uses_wal_and_extended_busy_timeout() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "system_information.sqlite"
        store = SystemInformationStore(database)
        store.initialize()
        repository = SystemReviewRepository(database)

        with store.connect() as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000
        with repository.connect() as conn:
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 60_000


def test_coverage_groups_business_sources_with_covering_index() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        for item_id, business, source in (
            ("x:1", "x", "x"),
            ("x:2", "x", "x"),
            ("media:1", "crypto_media", "blockbeats"),
        ):
            store.upsert_snapshot(
                {
                    "item_id": item_id, "business_key": business, "source": source,
                    "stage": "采集", "status": "pending", "result": "processing",
                    "source_database": "fixture", "source_table": "items", "source_row_id": item_id,
                },
                None,
            )

        assert store.coverage()["sources"] == [
            {"business_key": "crypto_media", "source": "blockbeats", "items": 1},
            {"business_key": "x", "source": "x", "items": 2},
        ]
        with store.connect() as conn:
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(system_information_items)")}
        assert "idx_system_information_business_source" in indexes


def test_search_cursor_and_evidence_redaction() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SystemInformationStore(root / "index.sqlite")
        store.initialize()
        for index in range(3):
            store.upsert_snapshot(
                {
                    "item_id": f"sample:{index}", "business_key": "x", "source": "x", "source_id": str(index),
                    "title": f"交易报告 {index}", "summary": "区块链市场变化", "search_extra": "Token ABC",
                    "stage": "写作", "status": "written", "result": "processing", "created_at_ms": 1000 + index,
                    "updated_at_ms": 1000 + index, "source_database": "fixture", "source_table": "items",
                    "source_row_id": str(index), "legacy_history": False,
                },
                {"content": "示例正文", "api_key": "hidden", "nested": {"Authorization": "hidden"}},
            )
        first = store.query({"q": "交易", "limit": 2})
        assert [item["item_id"] for item in first["items"]] == ["sample:2", "sample:1"]
        second = store.query({"q": "交易", "limit": 2, "cursor": first["next_cursor"]})
        assert [item["item_id"] for item in second["items"]] == ["sample:0"]
        evidence = store.detail("sample:0")["evidence"]
        assert "api_key" not in evidence
        assert "Authorization" not in evidence["nested"]


def test_mixed_timestamp_formats_normalize_as_utc_instants() -> None:
    assert timestamp_ms("2024-03-01 10:00:00") == timestamp_ms("2024-03-01T10:00:00+00:00")
    assert timestamp_ms("2024-03-01T18:00:00+08:00") == timestamp_ms("2024-03-01T10:00:00Z")


def test_observability_storage_directory_can_use_dedicated_data_volume() -> None:
    default = Path("/opt/OdAIly/data/runtime")
    assert system_observability_directory(default, "") == default
    assert system_observability_directory(default, "/data/OdAIly/system-observability") == Path("/data/OdAIly/system-observability")


def test_search_requires_contiguous_phrase_order() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "index.sqlite")
        store.initialize()
        base = {
            "business_key": "x", "source": "x", "stage": "写作", "status": "written", "result": "processing",
            "created_at_ms": 1, "updated_at_ms": 1, "source_database": "fixture", "source_table": "items",
            "source_row_id": "1", "legacy_history": False,
        }
        store.upsert_snapshot({**base, "item_id": "ordered", "title": "甲乙丙"}, None)
        store.upsert_snapshot({**base, "item_id": "shuffled", "title": "甲乙丁乙丙"}, None)

        assert [item["item_id"] for item in store.query({"q": "甲乙丙"})["items"]] == ["ordered"]


def test_compact_search_migration_is_resumable_and_indexes_long_evidence_once() -> None:
    with TemporaryDirectory() as directory:
        database = Path(directory) / "index.sqlite"
        store = SystemInformationStore(database)
        store.initialize()
        base = {
            "business_key": "x", "source": "x", "stage": "写作", "status": "written", "result": "processing",
            "created_at_ms": 1, "updated_at_ms": 1, "source_database": "fixture", "source_table": "items",
            "legacy_history": False,
        }
        store.upsert_snapshot(
            {**base, "item_id": "item:1", "source_row_id": "1", "title": "正文样本"},
            {"content": "开头" + "甲乙丙" * 5000 + "结尾"},
        )
        store.upsert_snapshot(
            {**base, "item_id": "item:2", "source_row_id": "2", "title": "正文样本"},
            {"content": "其他正文"},
        )

        with sqlite3.connect(database) as conn:
            conn.execute(
                "CREATE TABLE system_information_search_positions "
                "(term TEXT NOT NULL,item_id TEXT NOT NULL,position INTEGER NOT NULL,PRIMARY KEY(term,item_id,position))"
            )
            conn.execute("DELETE FROM system_information_state WHERE state_key LIKE 'search_bigrams_rebuild_%'")
        store.initialize()
        assert store.query({"q": "甲乙丙"})["search_index_ready"] is False

        assert store.rebuild_search_index(limit=1) == 1
        assert store.get_state("search_bigrams_rebuild_done") == "0"
        assert store.rebuild_search_index(limit=1) == 1
        assert store.rebuild_search_index(limit=1) == 0
        assert store.get_state("search_bigrams_rebuild_done") == "1"

        with sqlite3.connect(database) as conn:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_information_search_positions'"
            ).fetchone() is None
            distinct_terms = conn.execute(
                "SELECT COUNT(*) FROM system_information_search WHERE item_id='item:1'"
            ).fetchone()[0]
        assert distinct_terms < 100
        result = store.query({"q": "甲乙丙"})
        assert result["search_index_ready"] is True
        assert result["total"] == 1
        assert result["items"][0]["item_id"] == "item:1"


def test_sparse_task_ids_report_unprojected_row_count() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        _source_db(source)
        with sqlite3.connect(source) as conn:
            conn.executemany(
                "INSERT INTO tasks(id,source,source_item_id,title,content,status,created_at,metadata) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    (100_000 * index, "x", f"sparse-{index}", "标题", "正文", "pending", "2026-11-03T00:00:00Z", "{}")
                    for index in range(1, 28)
                ],
            )
        store = SystemInformationStore(root / "runtime" / "system_information.sqlite")
        store.initialize()
        MainTaskProjector(source, store, batch_size=25).run_once()

        assert int(store.get_state("main_tasks_lag")) == 5


def test_pinned_attempt_evidence_survives_orphan_pruning_until_projection() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        reference = store.save_evidence({"raw_model_response": "可重放的模型返回"})
        store.pin_evidence(reference, "x-attempt-response:1:1")

        assert store.prune_orphan_evidence(dry_run=False)["files"] == 0
        assert store.load_evidence(reference)["raw_model_response"] == "可重放的模型返回"

        store.unpin_evidence("x-attempt-response:1:1")
        assert store.prune_orphan_evidence(dry_run=False)["files"] == 1
        assert store.load_evidence(reference) is None


def test_evidence_retention_compacts_only_closed_records_and_prunes_with_dry_run() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        store = SystemInformationStore(root / "runtime" / "system_information.sqlite")
        store.initialize()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        closed = {
            "item_id": "closed:1", "business_key": "x", "source": "x", "source_id": "post-1",
            "title": "已完成稿件", "summary": "已发布", "stage": "publish", "status": "published",
            "result": "direct_publish", "created_at_ms": now_ms - 100 * 86_400_000, "updated_at_ms": now_ms,
            "source_database": "fixture", "source_table": "items", "source_row_id": "1", "legacy_history": False,
        }
        store.upsert_snapshot(closed, {"task": {"content": "原文" * 3000}, "pipeline": {"final_content": "长期保留的定稿"}, "raw_model_response": "模型返回" * 2000})
        pending = {**closed, "item_id": "pending:1", "status": "processing", "result": "processing", "source_row_id": "2"}
        store.upsert_snapshot(pending, {"task": {"content": "未结束原文" * 3000}})
        old_cutoff = now_ms - 100 * 86_400_000
        with store.connect() as conn:
            conn.execute("UPDATE system_information_evidence SET created_at_ms=?", (old_cutoff,))

        before_files = list(store.evidence_dir.glob("[0-9a-f][0-9a-f]/*.json.gz"))
        dry_run = store.cleanup_expired_evidence(now_ms=now_ms, dry_run=True)
        assert dry_run["compacted"] == 1
        assert dry_run["protected"] == 1
        assert list(store.evidence_dir.glob("[0-9a-f][0-9a-f]/*.json.gz")) == before_files

        applied = store.cleanup_expired_evidence(now_ms=now_ms, dry_run=False)
        assert applied["compacted"] == 1
        closed_detail = store.detail("closed:1")
        assert closed_detail["evidence"]["task"]["content_excerpt"].startswith("原文")
        assert "content" not in closed_detail["evidence"]["task"]
        assert closed_detail["evidence"]["pipeline"]["final_content"] == "长期保留的定稿"
        pending_detail = store.detail("pending:1")
        assert "content" in pending_detail["evidence"]["task"]

        orphan_ref = store.save_evidence({"unused": "orphan"})
        orphan_path = store.evidence_dir / orphan_ref[:2] / f"{orphan_ref}.json.gz"
        orphan_preview = store.prune_orphan_evidence(dry_run=True)
        assert orphan_preview["files"] == 1
        assert orphan_path.exists()
        assert store.prune_orphan_evidence(dry_run=False)["files"] == 1
        assert not orphan_path.exists()


def test_storage_health_reports_margin_and_growth_sample_count() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        store.storage_health(sample=True, now_ms=now_ms - 86_400_000)
        result = store.storage_health(sample=True, now_ms=now_ms)
        assert result["filesystem_total_bytes"] > 0
        assert result["filesystem_free_bytes"] > 0
        assert result["required_free_bytes"] >= 10 * 1024**3
        assert result["samples"] == 2
        assert result["estimated_growth_bytes_per_day"] is not None
        assert result["estimated_growth_next_30d_bytes"] >= 0


def test_storage_health_does_not_report_zero_growth_with_one_sample() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        store.storage_health(sample=True, now_ms=now_ms - 86_400_000)

        result = store.storage_health(now_ms=now_ms)

        assert result["samples"] == 1
        assert result["estimated_growth_bytes_per_day"] is None
        assert result["estimated_growth_next_30d_bytes"] is None


def test_channel_checks_follow_enabled_config_and_detect_missing_heartbeat() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        competitor = root / "competitor.sqlite"
        queue = root / "queue.sqlite"
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE x_capture_settings(singleton_key TEXT,global_interval_seconds INTEGER);
                INSERT INTO x_capture_settings VALUES('global',30);
                CREATE TABLE x_capture_accounts(id INTEGER PRIMARY KEY,enabled INTEGER);
                INSERT INTO x_capture_accounts VALUES(1,1);
                CREATE TABLE jin10_settings(singleton_key TEXT,enabled INTEGER,interval_seconds INTEGER);
                INSERT INTO jin10_settings VALUES('global',0,60);
                CREATE TABLE whale_watch_addresses(id INTEGER PRIMARY KEY,enabled INTEGER);
                CREATE TABLE whale_watch_hyperliquid_addresses(id INTEGER PRIMARY KEY,enabled INTEGER);
            """)
        with sqlite3.connect(competitor) as conn:
            conn.executescript("""
                CREATE TABLE non_mainstream_media_settings(singleton_key TEXT,global_interval_seconds INTEGER);
                INSERT INTO non_mainstream_media_settings VALUES('global',60);
                CREATE TABLE non_mainstream_media_sources(id INTEGER PRIMARY KEY,enabled INTEGER);
                INSERT INTO non_mainstream_media_sources VALUES(1,0);
            """)
        with sqlite3.connect(queue) as conn:
            conn.execute("CREATE TABLE local_pipeline_jobs(id INTEGER PRIMARY KEY)")
        repository = SystemReviewRepository(root / "review.sqlite")
        service = SystemReviewService(main, repository, competitor_database_path=competitor, local_pipeline_database_path=queue)

        expected = service._expected_components()
        assert expected["x_capture"]["enabled"] is True
        assert expected["non_mainstream_media"]["enabled"] is False
        assert expected["jin10_monitor"]["enabled"] is False
        assert expected["local_pipeline"]["enabled"] is True

        issues = service._channel_outages()
        missing = {(item["scope"]["component"], item["scope"]["worker_id"]) for item in issues}
        assert ("x_capture", "x_capture") in missing
        assert ("local_pipeline", "local_pipeline") in missing
        assert all(component not in {"non_mainstream_media", "jin10_monitor"} for component, _worker in missing)


def test_channel_outages_ignore_unconfigured_historical_components_without_n_plus_one_config_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(main) as conn:
            conn.execute("""CREATE TABLE pipeline_worker_heartbeats(
                component TEXT NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
                last_seen_at TEXT NOT NULL, last_success_at TEXT, last_error TEXT, metadata TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(component,worker_id))""")
            conn.executemany(
                "INSERT INTO pipeline_worker_heartbeats VALUES(?,?,?,?,?,?,?)",
                [
                    ("non_mainstream_media_telegram", f"telegram-{pid}", "ok", now, now, None, "{}")
                    for pid in range(64)
                ],
            )
        service = SystemReviewService(main, SystemReviewRepository(root / "review.sqlite"))
        original = service._expected_components
        calls = 0

        def counted_expected_components():
            nonlocal calls
            calls += 1
            return original()

        monkeypatch.setattr(service, "_expected_components", counted_expected_components)

        assert service._channel_outages() == []
        assert calls == 1


def test_channel_outages_use_latest_pid_heartbeat_for_singleton_components() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        stale = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE x_capture_settings(singleton_key TEXT,global_interval_seconds INTEGER);
                INSERT INTO x_capture_settings VALUES('global',30);
                CREATE TABLE x_capture_accounts(id INTEGER PRIMARY KEY,enabled INTEGER);
                INSERT INTO x_capture_accounts VALUES(1,1);
                CREATE TABLE pipeline_worker_heartbeats(
                    component TEXT NOT NULL, worker_id TEXT NOT NULL, status TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, last_success_at TEXT, last_error TEXT, metadata TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(component,worker_id));
            """)
            conn.executemany(
                "INSERT INTO pipeline_worker_heartbeats VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        "x_capture", f"x_capture-{pid}", "ok",
                        (stale + timedelta(seconds=pid)).isoformat(),
                        (stale + timedelta(seconds=pid)).isoformat(), None, "{}",
                    )
                    for pid in range(40)
                ],
            )
        service = SystemReviewService(main, SystemReviewRepository(root / "review.sqlite"))

        issues = service._channel_outages()

        assert len(issues) == 1
        assert issues[0]["scope"]["worker_id"] == "x_capture-39"


def test_system_review_worker_marks_interrupted_runs_after_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = SystemReviewRepository(root / "review.sqlite")
        repository.start_run("channel_outage:stuck", "channel_outage")
        service = SystemReviewService(root / "main.sqlite", repository)
        worker = SystemReviewWorker(service)
        monkeypatch.setattr(worker, "_run", lambda: None)

        worker.start()
        worker.stop()

        run = repository.recent_runs()[0]
        assert run["status"] == "failed"
        assert "重启" in run["error"]
        assert run["completed_at"] is not None


def test_system_review_worker_survives_lock_while_persisting_error() -> None:
    class LockedRepository:
        calls = 0

        def set_state(self, _key: str, _value: str) -> None:
            self.calls += 1
            raise sqlite3.OperationalError("database is locked")

    class Service:
        repository = LockedRepository()

    worker = SystemReviewWorker(Service())
    worker.interval_seconds = 0
    run_calls = 0

    def run_once() -> None:
        nonlocal run_calls
        run_calls += 1
        if run_calls == 1:
            raise sqlite3.OperationalError("database is locked")
        worker._stop.set()

    worker.run_once = run_once
    worker._run()

    assert run_calls == 2
    assert worker.service.repository.calls == 1


def test_competitor_events_are_projected_from_the_active_main_database() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "main.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE tasks(id INTEGER PRIMARY KEY);
                CREATE TABLE newsflash_events(
                    event_id TEXT PRIMARY KEY, representative_title TEXT, first_source TEXT,
                    first_published_at TEXT, source_count INTEGER, competitor_source_count INTEGER,
                    has_odaily INTEGER, needs_review INTEGER, metadata TEXT, updated_at TEXT
                );
                INSERT INTO newsflash_events VALUES(
                    'event-1','竞品首发事件','blockbeats','2026-11-03T10:00:00Z',1,1,0,0,'{}','2026-11-03T10:00:00Z'
                );
            """)
        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        specs = tuple(spec for spec in TABLE_SPECS if spec.table == "newsflash_events")
        projector = MainTaskProjector(source, store, additional_databases={"main": source}, table_specs=specs)

        projector.run_once()

        detail = store.detail("main:newsflash_events:event-1")
        assert detail is not None
        assert detail["business_key"] == "crypto_media"
        assert detail["source"] == "competitor_event"
        assert store.get_state("adapter_main_newsflash_events_status") == "ready"
        assert not any(spec.database_key == "competitor" for spec in TABLE_SPECS)


def test_coverage_review_joins_source_links_decisions_config_changes_and_pipeline_receipts() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE newsflash_events(
                    event_id TEXT PRIMARY KEY, representative_title TEXT, first_source TEXT,
                    first_published_at TEXT, source_count INTEGER, competitor_source_count INTEGER,
                    has_odaily INTEGER, needs_review INTEGER, metadata TEXT, updated_at TEXT
                );
                CREATE TABLE newsflash_items(
                    id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT,
                    title TEXT, created_at TEXT
                );
                CREATE TABLE newsflash_event_sources(
                    event_id TEXT, item_id INTEGER, source TEXT, source_item_id TEXT,
                    role TEXT, match_method TEXT, created_at TEXT
                );
                CREATE TABLE tasks(
                    id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, status TEXT, updated_at TEXT
                );
                CREATE TABLE x_task_pipeline(
                    task_id INTEGER PRIMARY KEY, publisher_reason_code TEXT, publisher_output TEXT,
                    push_result TEXT, last_error TEXT, final_title TEXT, final_content TEXT
                );
            """)
            conn.execute("INSERT INTO newsflash_events VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "event-1", "竞品报道事件", "blockbeats", now, 2, 1, 1, 1, "{}", now,
            ))
            conn.execute("INSERT INTO newsflash_items VALUES(1,'blockbeats','item-1','https://source.test/1','原始标题',?)", (now,))
            conn.execute("INSERT INTO newsflash_items VALUES(2,'odaily','flash-77','https://odaily.test/flash-77','已发布快讯',?)", (now,))
            conn.execute("INSERT INTO newsflash_event_sources VALUES('event-1',1,'blockbeats','item-1','primary','new_event',?)", (now,))
            conn.execute("INSERT INTO newsflash_event_sources VALUES('event-1',2,'odaily','flash-77','covered','official_id',?)", (now,))
            conn.execute("INSERT INTO tasks VALUES(7,'blockbeats','item-1','publisher_failed',?)", (now,))
            conn.execute("INSERT INTO x_task_pipeline VALUES(7,'push_failed','{}','{}','HTTP timeout','提交标题','提交正文')")
        service = SystemReviewService(main, SystemReviewRepository(root / "review.sqlite"), competitor_database_path=main)
        information_store = service.repository.information_store
        information_store.upsert_snapshot({
            "item_id": "main:non_mainstream_media_sources:blockbeats", "business_key": "crypto_media",
            "source": "non_mainstream_media", "source_id": "blockbeats", "title": "BlockBeats",
            "stage": "覆盖配置", "status": "enabled", "result": "configuration_change",
            "created_at_ms": timestamp_ms(now), "updated_at_ms": timestamp_ms(now),
            "source_database": str(main), "source_table": "non_mainstream_media_sources",
            "source_row_id": "1", "legacy_history": False,
        }, None)
        information_store.append_event(
            item_id="main:non_mainstream_media_sources:blockbeats", event_id="config:blockbeats:1",
            event_type="source_state_change", stage="覆盖配置", status="enabled", result="configuration_change",
            happened_at_ms=timestamp_ms(now), summary="订阅配置已变更",
            evidence={"payload": {"site_key": "blockbeats", "enabled": 1}},
            details={"operation": "insert", "source_table": "non_mainstream_media_sources"},
        )
        with information_store.connect() as conn:
            conn.execute(
                "INSERT INTO system_information_decision_samples(sample_key,sample_date,sampled_at_ms,raw_expires_at_ms,item_id,business_key,source,source_id,stage,status,result,reason_summary,rule_version,evidence_ref,evidence_bytes) "
                "VALUES('decision-1','2026-11-03',?,?,?,'crypto_media','blockbeats','item-1','判断','discarded','stopped','重复事件','rules-v3',NULL,0)",
                (timestamp_ms(now), timestamp_ms(now) + 14 * 86_400_000, "task:blockbeats:item-1"),
            )

        issues = service._coverage_gaps()

        assert len(issues) == 1
        linked = issues[0]["evidence"][0]
        assert linked["event_sources"][0]["source_url"] == "https://source.test/1"
        assert linked["official_publications"][0]["source_item_id"] == "flash-77"
        assert linked["source_tasks"][0]["task"]["status"] == "publisher_failed"
        assert linked["source_tasks"][0]["task"]["push_result"] == "{}"
        source = linked["source_tasks"][0]
        assert source["configuration_history_status"] == "observed_changes"
        assert source["configuration_history"][0]["evidence"]["payload"]["enabled"] == 1
        assert source["decision_samples"][0]["sample_key"] == "decision-1"


def test_coverage_review_marks_missing_configuration_history_unknown() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE newsflash_events(
                    event_id TEXT PRIMARY KEY, representative_title TEXT, first_source TEXT,
                    first_published_at TEXT, source_count INTEGER, competitor_source_count INTEGER,
                    has_odaily INTEGER, needs_review INTEGER, metadata TEXT, updated_at TEXT
                );
                CREATE TABLE newsflash_items(id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT, title TEXT, created_at TEXT);
                CREATE TABLE newsflash_event_sources(event_id TEXT, item_id INTEGER, source TEXT, source_item_id TEXT, role TEXT, match_method TEXT, created_at TEXT);
                CREATE TABLE tasks(id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, status TEXT, updated_at TEXT);
                CREATE TABLE x_task_pipeline(task_id INTEGER PRIMARY KEY, publisher_reason_code TEXT, publisher_output TEXT, push_result TEXT, last_error TEXT, final_title TEXT, final_content TEXT);
            """)
            conn.execute("INSERT INTO newsflash_events VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "event-unknown", "待核对事件", "blockbeats", now, 1, 1, 0, 0, "{}", now,
            ))
            conn.execute("INSERT INTO newsflash_items VALUES(1,'blockbeats','item-unknown','https://source.test/unknown','原始标题',?)", (now,))
            conn.execute("INSERT INTO newsflash_event_sources VALUES('event-unknown',1,'blockbeats','item-unknown','primary','new_event',?)", (now,))
        service = SystemReviewService(main, SystemReviewRepository(root / "review.sqlite"), competitor_database_path=main)

        issue = service._coverage_gaps()[0]

        source = issue["evidence"][0]["source_tasks"][0]
        assert source["configuration_history_status"] == "unknown"
        assert source["configuration_history"] is None
        assert source["decision_samples"] == []


def test_no_candidate_samples_are_stratified_capped_and_expire_raw_evidence() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        now = int(datetime.now(UTC).timestamp() * 1000)
        base = {
            "stage": "判断", "status": "discarded", "result": "stopped", "created_at_ms": now,
            "updated_at_ms": now, "source_database": "fixture", "source_table": "tasks",
            "source_row_id": "0", "legacy_history": False,
        }
        for index in range(80):
            business = "x" if index < 60 else "crypto_media"
            store.upsert_snapshot(
                {**base, "item_id": f"sample:{index}", "business_key": business, "source": f"source:{index % 3}",
                 "source_id": str(index), "title": f"无候选 {index}", "summary": "材料不足"},
                {"task": {"content": "原文" * 80, "prompt_version_id": 3}},
            )

        result = store.record_decision_samples(now_ms=now)
        samples = store.decision_samples(sample_date=datetime.fromtimestamp(now / 1000, UTC).date().isoformat())
        assert result["sampled"] == 50
        assert result["evidence_bytes"] <= 20 * 1024 * 1024
        assert {item["business_key"] for item in samples} == {"x", "crypto_media"}
        assert all(item["rule_version"] == "3" for item in samples)
        assert store.record_decision_samples(now_ms=now)["already_sampled"] == 1

        expired = store.expire_decision_samples(now_ms=now + 15 * 86_400_000)
        assert expired["raw_expired"] == 50
        assert all(item["evidence_ref"] is None for item in store.decision_samples(sample_date=samples[0]["sample_date"]))
        assert store.decision_sample_details([samples[0]["sample_key"]])[0]["evidence_available"] is False
        removed = store.expire_decision_samples(now_ms=now + 31 * 86_400_000)
        assert removed["index_removed"] == 50


def test_no_candidate_samples_feed_review_card_detail_and_ai_input(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        with sqlite3.connect(main):
            pass
        repository = SystemReviewRepository(root / "observability.sqlite")
        service = SystemReviewService(main, repository)
        now = int(datetime.now(UTC).timestamp() * 1000)
        repository.information_store.upsert_snapshot(
            {
                "item_id": "decision:sample-1", "business_key": "x_agent", "source": "investment_leads",
                "source_id": "post-1", "title": "待核对线索", "summary": "未发现投资候选",
                "stage": "筛选", "status": "no_candidate", "result": "stopped",
                "created_at_ms": now, "updated_at_ms": now, "source_database": "fixture",
                "source_table": "investment_lead_tasks", "source_row_id": "1", "legacy_history": False,
            },
            {"decision": {"reason": "缺少明确传导"}, "source": {"content": "完整待复核原文"}, "prompt_version_id": "leads-v4"},
        )
        repository.information_store.upsert_snapshot(
            {
                "item_id": "output:sample-1", "business_key": "x_agent", "source": "hot_topic",
                "source_id": "event-1", "title": "热点研究摘要", "summary": "事件主体确认",
                "stage": "内部研究完成", "status": "succeeded", "result": "internal_complete",
                "created_at_ms": now, "updated_at_ms": now, "source_database": "fixture",
                "source_table": "hottopic_events", "source_row_id": "1", "legacy_history": False,
            },
            {"event": {"source_claim": "原帖称项目完成升级", "analysis": "仅代表公开主张"}, "prompt_version_id": "topic-v3"},
        )

        issues = service._audit_findings()
        sample_issue = next(issue for issue in issues if issue["category"] == "no_candidate_sample_review")
        key = repository.upsert_issue({**sample_issue, "task_kind": "output_audit"})
        detail = repository.detail(key)
        output_issue = next(issue for issue in issues if issue["category"] == "successful_output_sample_review")
        output_key = repository.upsert_issue({**output_issue, "task_kind": "output_audit"})
        output_detail = repository.detail(output_key)

        assert detail["evidence"][0]["evidence_available"] is True
        assert "完整待复核原文" in detail["evidence"][0]["evidence_excerpt"]
        assert output_detail["evidence"][0]["evidence_available"] is True
        assert "原帖称项目完成升级" in output_detail["evidence"][0]["evidence_excerpt"]
        captured: list[dict[str, object]] = []

        def inspect_input(item: dict[str, object], *, attempt_id: int, on_usage) -> dict[str, object]:
            captured.extend(json.loads(str(item["evidence_json"])))
            on_usage(80)
            return {"summary": "证据已检查", "severity": "unknown", "certainty": "low", "suggestion": "保持人工判断", "validation": "核对原文", "input_tokens": 80, "output_tokens": 10}

        monkeypatch.setattr(service, "_analyze", inspect_input)
        assert service.analyze_pending()["analyzed"] == 2
        assert any("完整待复核原文" in str(item.get("evidence_excerpt")) for item in captured)
        assert any("原帖称项目完成升级" in str(item.get("evidence_excerpt")) for item in captured)


def test_binance_square_channel_checks_settings_worker_and_enabled_accounts() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        now = datetime.now(UTC).isoformat()
        old_success = (datetime.now(UTC).timestamp() - 3600)
        old_success_at = datetime.fromtimestamp(old_success, UTC).isoformat()
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE binance_square_settings(
                    singleton_key TEXT PRIMARY KEY, enabled INTEGER, interval_seconds INTEGER,
                    worker_status TEXT, worker_last_seen_at TEXT
                );
                CREATE TABLE binance_square_accounts(
                    id INTEGER PRIMARY KEY, slug_lower TEXT, enabled INTEGER,
                    last_polled_at TEXT, last_success_at TEXT, last_error TEXT
                );
            """)
            conn.execute(
                "INSERT INTO binance_square_settings VALUES('global',1,180,'running',?)", (now,),
            )
            conn.executemany(
                "INSERT INTO binance_square_accounts VALUES(?,?,?,?,?,?)",
                [
                    (1, "healthy", 1, now, now, None),
                    (2, "failing", 1, now, old_success_at, "429 rate limited"),
                    (3, "disabled", 0, now, old_success_at, "old failure"),
                ],
            )

        repository = SystemReviewRepository(root / "review.sqlite")
        service = SystemReviewService(main, repository)
        issues = service._channel_outages()
        square_issues = [item for item in issues if item["scope"]["component"] == "binance_square"]
        assert [(item["scope"]["worker_id"], item["consecutive_check_required"]) for item in square_issues] == [
            ("account:failing", True),
        ]

        with sqlite3.connect(main) as conn:
            conn.execute("UPDATE binance_square_settings SET enabled=0 WHERE singleton_key='global'")
        assert not [item for item in service._channel_outages() if item["scope"]["component"] == "binance_square"]


def test_channel_failure_requires_three_consecutive_observations() -> None:
    with TemporaryDirectory() as directory:
        repository = SystemReviewRepository(Path(directory) / "review.sqlite")
        bucket = "2026-11-03T00:00:00+00:00"
        assert repository.record_channel_observation("x_capture", "worker-1", failed=True, bucket=bucket) == 1
        assert repository.record_channel_observation("x_capture", "worker-1", failed=True, bucket=bucket) == 1
        assert repository.record_channel_observation("x_capture", "worker-1", failed=True, bucket="2026-11-03T00:05:00+00:00") == 2
        assert repository.record_channel_observation("x_capture", "worker-1", failed=True, bucket="2026-11-03T00:10:00+00:00") == 3
        assert repository.record_channel_observation("x_capture", "worker-1", failed=False, bucket="2026-11-03T00:15:00+00:00") == 0


def test_system_review_parse_failure_keeps_raw_response_and_settles_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        repository = SystemReviewRepository(root / "system_information.sqlite")
        key = repository.upsert_issue({"task_kind": "output_audit", "category": "sample", "title": "需核对稿件版本", "identity": {"sample": 1}})
        service = SystemReviewService(root / "main.sqlite", repository, model_settings=object())

        def malformed_response(item: dict[str, object], *, attempt_id: int, on_usage) -> dict[str, object]:
            repository.record_analysis_response(attempt_id, '{"output_text":"not JSON","usage":{"input_tokens":23}}', "req-review-1", 23)
            on_usage(23)
            raise ValueError("AI巡检未返回JSON")

        monkeypatch.setattr(service, "_analyze", malformed_response)
        assert service.analyze_pending()["failed"] == 1
        detail = repository.detail(key)
        assert detail["analysis_status"] == "failed"
        assert detail["analysis_history"][0]["request_id"] == "req-review-1"
        assert detail["analysis_history"][0]["input_tokens"] == 23
        assert "not JSON" in detail["analysis_history"][0]["raw_response"]
        with repository.connect() as conn:
            usage = conn.execute("SELECT calls,input_tokens,reserved_tokens FROM system_review_usage").fetchone()
            conn.execute("UPDATE system_information_evidence SET created_at_ms=0")
        assert tuple(usage) == (1, 23, 0)

        compacted = repository.information_store.cleanup_expired_evidence(
            now_ms=int(datetime.now(UTC).timestamp() * 1000), dry_run=False,
        )
        assert compacted["compacted"] == 1
        assert repository.information_store.prune_orphan_evidence(dry_run=True)["files"] == 0
        with repository.connect() as conn:
            response_ref = conn.execute(
                "SELECT raw_response_ref FROM system_review_analysis_attempts WHERE item_key=?", (key,),
            ).fetchone()[0]
        compact_response = repository.information_store.load_evidence(response_ref)
        assert "raw_model_response" not in compact_response
        assert compact_response["raw_model_response_excerpt"].startswith('{"output_text"')


def test_raw_writer_response_and_failed_attempt_survive_successful_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        monkeypatch.setenv("ODAILY_SYSTEM_OBSERVABILITY_DIR", str(root / "observability"))
        database = root / "main.sqlite"
        repository = SQLiteXProcessingRepository(database)
        with repository._connect() as conn:
            conn.execute(
                "INSERT INTO tasks(id,source,source_item_id,title,content,status,created_at,updated_at) VALUES(1,'x','post-1','标题','正文','pending',?,?)",
                ("2026-11-03T00:00:00Z", "2026-11-03T00:00:00Z"),
            )
            conn.execute("INSERT INTO x_task_pipeline(task_id) VALUES(1)")
            conn.commit()

        first = repository.start_stage_attempt(1, stage="write", executor="test-model", input_evidence={"source": "x"}, rule_evidence={"version": 1})
        repository.record_stage_attempt_response(first, "not valid JSON")
        repository.fail_task(1, stage="write", error="ValueError: bad JSON", error_code="ValueError")
        second = repository.start_stage_attempt(1, stage="write", executor="test-model", input_evidence={"source": "x"}, rule_evidence={"version": 1})
        repository.record_stage_attempt_response(second, '{"title":"稿件","content":"正文"}')
        repository.complete_stage_attempt(second, result={"status": "written"})
        with repository._connect() as conn:
            conn.execute("UPDATE tasks SET status='written',updated_at=? WHERE id=1", ("2026-11-03T00:02:00Z",))
            conn.commit()

        store = SystemInformationStore(root / "observability" / "system_information.sqlite")
        store.initialize()
        with repository._connect() as conn:
            source_responses = conn.execute(
                "SELECT response_gzip,response_ref FROM x_processing_attempt_events "
                "WHERE event_type='response_received' ORDER BY id",
            ).fetchall()
        assert [row[0] for row in source_responses] == [None, None]
        assert all(row[1] for row in source_responses)
        assert [store.load_evidence(row[1])["raw_model_response"] for row in source_responses] == [
            "not valid JSON", '{"title":"稿件","content":"正文"}',
        ]
        projector = MainTaskProjector(database, store)
        projector.run_once()
        detail = store.detail("task:x:post-1")
        assert detail is not None
        attempt_events = [event for event in detail["timeline"] if event["event_type"] == "stage_attempt"]
        assert [event["status"] for event in attempt_events] == ["running", "running", "failed", "running", "running", "succeeded"]
        responses = [event["evidence"]["raw_model_response"] for event in attempt_events if "raw_model_response" in event["evidence"]]
        assert responses == ["not valid JSON", '{"title":"稿件","content":"正文"}']
        with store.connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM system_information_evidence_pins").fetchone()[0] == 0


def test_source_sqlite_outbox_preserves_intermediate_status_and_excludes_large_or_secret_fields() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE tasks(id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,title TEXT,content TEXT,status TEXT,created_at TEXT,updated_at TEXT);
                INSERT INTO tasks VALUES(1,'x','source-1','标题','正文','pending','2026-11-03T00:00:00Z','2026-11-03T00:00:00Z');
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,content TEXT,api_key TEXT,updated_at TEXT);
            """)
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        installed = SourceAuditInstaller().install(source, (spec,))
        assert installed == {"installed": 1, "skipped": 0}

        with sqlite3.connect(source) as conn:
            conn.executescript("""
                INSERT INTO jobs VALUES(7,'Meme候选','queued',NULL,'超长原文','secret-value','2026-11-03T00:00:00Z');
                UPDATE jobs SET status='processing',updated_at='2026-11-03T00:00:01Z' WHERE id=7;
                UPDATE jobs SET status='failed',last_error='上游超时',updated_at='2026-11-03T00:00:02Z' WHERE id=7;
            """)
            conn.execute("UPDATE jobs SET status='retry',updated_at='2026-11-03T00:00:03Z' WHERE id=7")
            conn.commit()

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, additional_databases={"meme": source}, table_specs=(spec,))
        projector.run_once()
        detail = store.detail("meme:jobs:7")
        assert detail is not None
        transitions = [event for event in detail["timeline"] if event["event_type"] == "source_state_change"]
        assert [event["status"] for event in transitions] == ["queued", "processing", "failed", "retry"]
        assert [event["reason"] for event in transitions if event["reason"]] == ["上游超时", "上游超时"]
        serialized = json.dumps([event["evidence"] for event in transitions], ensure_ascii=False)
        assert "超长原文" not in serialized
        assert "secret-value" not in serialized

        projector.run_once()
        assert len([event for event in store.detail("meme:jobs:7")["timeline"] if event["event_type"] == "source_state_change"]) == 4


def test_source_outbox_ignores_updates_that_only_refresh_clock_fields() -> None:
    with TemporaryDirectory() as directory:
        source = Path(directory) / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE jobs(
                    id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,
                    updated_at TEXT,last_seen_at TEXT
                );
                INSERT INTO jobs VALUES(1,'candidate','queued',NULL,'old','old');
            """)
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        SourceAuditInstaller().install(source, (spec,))

        with sqlite3.connect(source) as conn:
            conn.execute(
                "UPDATE odaily_observability_meta SET meta_value='1' "
                "WHERE meta_key='source_audit_trigger_version:meme'"
            )
        SourceAuditInstaller().install(source, (spec,))

        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE jobs SET updated_at='new-time',last_seen_at='new-seen' WHERE id=1")
            assert conn.execute("SELECT COUNT(*) FROM odaily_observability_outbox").fetchone()[0] == 0
            conn.execute("UPDATE jobs SET status='processing',updated_at='state-change-time' WHERE id=1")
            event = conn.execute(
                "SELECT payload_json FROM odaily_observability_outbox ORDER BY seq DESC LIMIT 1"
            ).fetchone()[0]

        payload = json.loads(event)
        assert payload["status"] == "processing"
        assert payload["updated_at"] == "state-change-time"


def test_source_outbox_coalesces_snapshots_and_keeps_each_timeline_event() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,updated_at TEXT);
                INSERT INTO jobs VALUES(7,'candidate','queued',NULL,'2026-11-03T00:00:00Z');
            """)
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE jobs SET status='processing',updated_at='2026-11-03T00:00:01Z' WHERE id=7")
            conn.execute("UPDATE jobs SET status='failed',last_error='timeout',updated_at='2026-11-03T00:00:02Z' WHERE id=7")
            conn.execute("UPDATE jobs SET status='retry',updated_at='2026-11-03T00:00:03Z' WHERE id=7")
            conn.execute("UPDATE odaily_observability_outbox SET happened_at_ms=1000+seq")

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, table_specs=(spec,))
        original_upsert = store._upsert_snapshot_in_connection
        upserted_items: list[str] = []

        def counted_upsert(conn: sqlite3.Connection, snapshot: dict[str, Any], evidence: Any, observed: int) -> bool:
            upserted_items.append(str(snapshot["item_id"]))
            return original_upsert(conn, snapshot, evidence, observed)

        store._upsert_snapshot_in_connection = counted_upsert  # type: ignore[method-assign]
        imported, changed = projector._project_source_outbox(
            "meme", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        )

        assert (imported, changed) == (3, 1)
        assert upserted_items == ["meme:jobs:7"]
        detail = store.detail("meme:jobs:7")
        assert detail is not None and detail["status"] == "retry"
        transitions = [event for event in detail["timeline"] if event["event_type"] == "source_state_change"]
        assert [event["status"] for event in transitions] == ["processing", "failed", "retry"]


def test_task_source_outbox_advances_existing_snapshot() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE tasks(
                    id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,source_url TEXT,title TEXT,
                    content TEXT,status TEXT,created_at TEXT,updated_at TEXT,metadata TEXT,raw_payload TEXT
                );
                INSERT INTO tasks VALUES(
                    1,'x','tweet-1',NULL,'source title','source content','writing',
                    '2026-11-04T17:00:00+08:00','2026-11-04T17:00:01+08:00','{}','{}'
                );
            """)
        spec = TableSpec("main", "tasks", "unmapped_source", "main_pipeline")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.execute(
                "UPDATE tasks SET status='auto_published',updated_at='2026-11-04T17:00:02+08:00' WHERE id=1"
            )

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        snapshot = {
            "item_id": "task:x:tweet-1", "business_key": "x", "source": "x", "source_id": "tweet-1",
            "task_id": 1, "title": "source title", "summary": "写作中", "source_url": None,
            "stage": "写作", "status": "writing", "result": "processing", "created_at_ms": 1793782800000,
            "updated_at_ms": 1793782801000, "source_database": str(source), "source_table": "tasks",
            "source_row_id": "1", "legacy_history": False,
        }
        store.upsert_snapshot(snapshot, {"recorded_scope": "test"})

        projector = MainTaskProjector(source, store, table_specs=(spec,))
        projector._project_source_outbox("main", source, [spec], int(datetime.now(UTC).timestamp() * 1000))

        detail = store.detail("task:x:tweet-1")
        assert detail is not None and detail["status"] == "auto_published"
        assert detail["task_id"] == 1
        assert detail["admission"] == "accepted"
        assert detail["news_visible"] == 1
        transitions = [event for event in detail["timeline"] if event["event_type"] == "source_state_change"]
        assert [event["status"] for event in transitions] == ["auto_published"]

        with sqlite3.connect(source) as conn:
            conn.execute(
                "INSERT INTO odaily_observability_outbox "
                "(event_id,table_name,source_row_id,operation,happened_at_ms,payload_json) "
                "VALUES('stale-task-event','tasks','1','update',1,?)",
                (json.dumps({
                    "id": 1, "source": "x", "source_item_id": "tweet-1", "title": "source title",
                    "content": "source content", "status": "writing",
                    "updated_at": "2026-11-04T17:00:01+08:00",
                }),),
            )
        projector._project_source_outbox("main", source, [spec], int(datetime.now(UTC).timestamp() * 1000))
        assert store.detail("task:x:tweet-1")["status"] == "auto_published"


def test_duplicate_task_created_from_outbox_is_visible_in_news_view() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE tasks(
                    id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,source_url TEXT,title TEXT,
                    content TEXT,status TEXT,created_at TEXT,updated_at TEXT,metadata TEXT,raw_payload TEXT
                );
                INSERT INTO tasks VALUES(
                    2,'blockbeats','375499','https://example.test/story','WLFI新进展','正文','pending',
                    '2026-11-04T19:00:00+08:00','2026-11-04T19:00:00+08:00','{}','{}'
                );
            """)
        spec = TableSpec("main", "tasks", "unmapped_source", "main_pipeline")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.execute(
                "UPDATE tasks SET status='duplicate',updated_at='2026-11-04T19:00:01+08:00' WHERE id=2"
            )

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, table_specs=(spec,))
        projector._project_source_outbox("main", source, [spec], int(datetime.now(UTC).timestamp() * 1000))

        page = store.query({"view": "news"})
        assert len(page["items"]) == 1
        item = page["items"][0]
        assert item["task_id"] == 2
        assert item["business_key"] == "crypto_media"
        assert item["status"] == "duplicate"
        assert item["admission"] == "duplicate"


def test_x_capture_attempt_retention_deletes_do_not_create_or_project_events() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.execute("""
                CREATE TABLE x_capture_attempts(
                    id INTEGER PRIMARY KEY,username_lower TEXT,status TEXT,source TEXT,
                    candidate_count INTEGER,new_count INTEGER,saved_count INTEGER,error TEXT,
                    started_at TEXT,finished_at TEXT
                )
            """)
        spec = TableSpec("main", "x_capture_attempts", "x", "x_capture")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.execute(
                "UPDATE odaily_observability_meta SET meta_value='3' "
                "WHERE meta_key='source_audit_trigger_version:main'"
            )
            conn.execute("""
                CREATE TRIGGER odaily_obs_x_capture_attempts_delete
                AFTER DELETE ON x_capture_attempts
                BEGIN
                    INSERT INTO odaily_observability_outbox(
                        event_id,table_name,source_row_id,operation,happened_at_ms,payload_json
                    ) VALUES('old-delete-trigger', 'x_capture_attempts', CAST(OLD.rowid AS TEXT), 'delete', 1, '{}');
                END
            """)
        SourceAuditInstaller().install(source, (spec,))

        with sqlite3.connect(source) as conn:
            triggers = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'odaily_obs_x_capture_attempts_%'"
            )}
            assert "odaily_obs_x_capture_attempts_delete" not in triggers
            conn.execute("""
                INSERT INTO x_capture_attempts VALUES(
                    1,'account','success','fxtwitter',12,0,0,NULL,'2026-11-03T00:00:00Z','2026-11-03T00:00:01Z'
                )
            """)
            insert_event = conn.execute(
                "SELECT event_id,happened_at_ms,payload_json FROM odaily_observability_outbox"
            ).fetchone()
            conn.execute("DELETE FROM x_capture_attempts WHERE id=1")
            assert conn.execute("SELECT COUNT(*) FROM odaily_observability_outbox").fetchone()[0] == 1
            conn.execute(
                "INSERT INTO odaily_observability_outbox(event_id,table_name,source_row_id,operation,happened_at_ms,payload_json) "
                "VALUES('historical-retention-delete','x_capture_attempts','1','delete',?,?)",
                (insert_event[1] + 1, insert_event[2]),
            )

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, table_specs=(spec,))
        assert projector._project_source_outbox(
            "main", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        ) == (1, 1)
        detail = store.detail("main:x_capture_attempts:1")
        assert detail is not None
        assert [event["event_type"] for event in detail["timeline"]] == ["legacy_snapshot", "source_state_change"]
        assert store.get_state("source_audit_main_watermark") == "2"


def test_source_outbox_batch_uses_bounded_observability_transactions() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,updated_at TEXT)"
            )
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.executemany(
                "INSERT INTO jobs(id,title,status,last_error,updated_at) VALUES(?,?,?,NULL,?)",
                ((index, f"candidate-{index}", "queued", "2026-11-03T00:00:00Z") for index in range(1, 101)),
            )

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(source, store, additional_databases={"meme": source}, table_specs=(spec,))
        original_connect = store.connect
        connection_count = 0

        def counted_connect() -> sqlite3.Connection:
            nonlocal connection_count
            connection_count += 1
            return original_connect()

        store.connect = counted_connect  # type: ignore[method-assign]
        imported, changed = projector._project_source_outbox(
            "meme", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        )
        store.connect = original_connect  # type: ignore[method-assign]

        assert imported == changed == 100
        assert connection_count <= 5
        assert store.get_state("source_audit_meme_watermark") == "100"
        assert projector._project_source_outbox(
            "meme", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        ) == (0, 0)
        assert len([
            event for event in store.detail("meme:jobs:50")["timeline"]
            if event["event_type"] == "source_state_change"
        ]) == 1


def test_source_outbox_batch_size_is_independent_and_watermark_bounded() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,updated_at TEXT);
                INSERT INTO jobs VALUES(1,'candidate','queued',NULL,'2026-11-03T00:00:00Z');
            """)
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.executemany(
                "UPDATE jobs SET status=?,updated_at=? WHERE id=1",
                ((f"state-{index}", f"2026-11-03T00:00:{index:02d}Z") for index in range(1, 61)),
            )
            conn.execute("UPDATE odaily_observability_outbox SET happened_at_ms=1000+seq")

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(
            source, store, batch_size=25, outbox_batch_size=50, table_specs=(spec,),
        )

        assert projector._project_source_outbox(
            "meme", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        ) == (50, 1)
        assert store.get_state("source_audit_meme_watermark") == "50"
        assert projector._project_source_outbox(
            "meme", source, [spec], int(datetime.now(UTC).timestamp() * 1000),
        ) == (10, 1)
        assert store.get_state("source_audit_meme_watermark") == "60"
        detail = store.detail("meme:jobs:1")
        assert detail is not None and detail["status"] == "state-60"
        assert len([event for event in detail["timeline"] if event["event_type"] == "source_state_change"]) == 60


def test_source_outbox_cleanup_only_removes_projected_events_past_retention() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        with sqlite3.connect(source) as conn:
            conn.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,updated_at TEXT)"
            )
        spec = TableSpec("meme", "jobs", "meme", "meme_scanner")
        SourceAuditInstaller().install(source, (spec,))
        with sqlite3.connect(source) as conn:
            conn.executemany(
                "INSERT INTO jobs(id,title,status,last_error,updated_at) VALUES(?,?,?,NULL,?)",
                ((index, f"candidate-{index}", "queued", "2026-11-03T00:00:00Z") for index in range(1, 27)),
            )
            conn.execute(
                "UPDATE odaily_observability_outbox SET happened_at_ms=? WHERE seq IN (1,2)",
                (int(datetime.now(UTC).timestamp() * 1000) - 25 * 60 * 60 * 1000,),
            )

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(
            source, store, batch_size=25, outbox_batch_size=25,
            additional_databases={"meme": source}, table_specs=(spec,),
        )
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        assert projector._project_source_outbox("meme", source, [spec], now_ms) == (25, 25)

        with sqlite3.connect(source) as conn:
            remaining = conn.execute(
                "SELECT seq FROM odaily_observability_outbox ORDER BY seq"
            ).fetchall()
        assert [row[0] for row in remaining] == list(range(3, 27))
        assert len([
            event for event in store.detail("meme:jobs:1")["timeline"]
            if event["event_type"] == "source_state_change"
        ]) == 1
        assert store.detail("meme:jobs:26") is None


def test_additional_projection_throttles_tail_checks_and_keeps_snapshot_recovery() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source.sqlite"
        _source_db(source)
        with sqlite3.connect(source) as conn:
            conn.execute(
                "CREATE TABLE jobs(id INTEGER PRIMARY KEY,title TEXT,status TEXT,last_error TEXT,content TEXT,updated_at TEXT)"
            )
            conn.execute("INSERT INTO jobs VALUES(1,'候选','pending',NULL,'旧正文','2026-11-03T00:00:00Z')")

        store = SystemInformationStore(root / "system_information.sqlite")
        store.initialize()
        projector = MainTaskProjector(
            source, store, additional_databases={"main": source},
            table_specs=(TableSpec("main", "jobs", "meme", "meme_scanner"),),
        )
        projector.run_once()
        item_id = "main:jobs:1"
        assert store.detail(item_id)["evidence"]["row"]["content"] == "旧正文"

        store.set_state("additional_tail_recheck_last_ms", int(datetime.now(UTC).timestamp() * 1000))
        store.set_state("additional_projection_last_ms", 0)
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE jobs SET content='尾部复核更新' WHERE id=1")
        projector.run_once()

        detail = store.detail(item_id)
        assert detail["evidence"]["row"]["content"] == "旧正文"

        store.set_state("additional_tail_recheck_last_ms", 0)
        store.set_state("additional_projection_last_ms", 0)
        projector.run_once()
        assert store.detail(item_id)["evidence"]["row"]["content"] == "尾部复核更新"


def test_activity_sort_uses_index_and_keeps_null_timestamps_on_later_pages() -> None:
    with TemporaryDirectory() as directory:
        store = SystemInformationStore(Path(directory) / "system_information.sqlite")
        store.initialize()
        base = {
            "business_key": "crypto_media", "source": "competitor_event", "stage": "采集",
            "status": "pending", "result": "processing", "source_database": "fixture",
            "source_table": "items", "legacy_history": False,
        }
        for item_id, created, updated in (
            ("old-source-event", 2_000, 2_000),
            ("recent-competitor-event", None, 3_000),
            ("unknown-time", None, None),
        ):
            store.upsert_snapshot(
                {**base, "item_id": item_id, "source_row_id": item_id,
                 "created_at_ms": created, "updated_at_ms": updated}, None,
            )

        first = store.query({"sort": "activity", "business": "crypto_media", "limit": 2})
        second = store.query({"sort": "activity", "business": "crypto_media", "limit": 2, "cursor": first["next_cursor"]})
        assert [item["item_id"] for item in first["items"]] == ["recent-competitor-event", "old-source-event"]
        assert [item["item_id"] for item in second["items"]] == ["unknown-time"]
        with store.connect() as conn:
            plan = [row["detail"] for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT item_id,updated_at_ms FROM system_information_items "
                "WHERE business_key='crypto_media' ORDER BY updated_at_ms DESC,item_id DESC LIMIT 51"
            )]
        assert any("idx_system_information_business_activity" in detail for detail in plan)
        assert not any("TEMP B-TREE FOR ORDER BY" in detail for detail in plan)


def test_configuration_outbox_records_effective_changes_without_polling_noise() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "main.sqlite"
        with sqlite3.connect(source) as conn:
            conn.executescript("""
                CREATE TABLE tasks(id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,source_url TEXT,title TEXT,content TEXT,status TEXT,created_at TEXT,updated_at TEXT,metadata TEXT);
                CREATE TABLE x_capture_accounts(
                    id INTEGER PRIMARY KEY,username TEXT,username_lower TEXT,enabled INTEGER,
                    interval_seconds INTEGER,last_polled_at TEXT,updated_at TEXT
                );
                INSERT INTO x_capture_accounts VALUES(1,'source-account','source-account',1,60,NULL,'2026-11-03T00:00:00Z');
            """)

        store = SystemInformationStore(root / "observability" / "system_information.sqlite")
        store.initialize()
        spec = TableSpec("main", "x_capture_accounts", "x", "x_capture", "configuration")
        projector = MainTaskProjector(source, store, additional_databases={"main": source}, table_specs=(spec,))
        projector.run_once()
        with sqlite3.connect(source) as conn:
            conn.execute("UPDATE x_capture_accounts SET last_polled_at='2026-11-03T00:01:00Z' WHERE id=1")
            conn.execute("UPDATE x_capture_accounts SET enabled=0,updated_at='2026-11-03T00:02:00Z' WHERE id=1")
            conn.commit()
        store.set_state("additional_projection_last_ms", "0")
        projector.run_once()

        detail = store.detail("main:x_capture_accounts:source-account")
        assert detail is not None
        assert detail["stage"] == "覆盖配置"
        assert detail["status"] == "disabled"
        changes = [event for event in detail["timeline"] if event["event_type"] == "source_state_change"]
        assert len(changes) == 1
        assert changes[0]["status"] == "disabled"
        assert changes[0]["result"] == "configuration_change"


def test_new_evidence_reopens_review_item_without_erasing_decision_history() -> None:
    with TemporaryDirectory() as directory:
        repository = SystemReviewRepository(Path(directory) / "review.sqlite")
        issue = {"task_kind": "failure_recovery", "category": "cluster", "title": "错误聚合", "identity": {"source": "x"}, "evidence": [{"attempt": 1}]}
        key = repository.upsert_issue(issue)
        repository.decide(key, "rejected", "reviewer@example.com", "已核对")
        repository.upsert_issue(issue)
        assert repository.detail(key)["status"] == "rejected"

        repository.upsert_issue({**issue, "evidence": [{"attempt": 1}, {"attempt": 2}]})
        detail = repository.detail(key)
        assert detail["status"] == "pending"
        assert detail["analysis_status"] == "queued"
        assert len(detail["decisions"]) == 1


def test_budget_paused_review_retries_on_next_available_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    with TemporaryDirectory() as directory:
        repository = SystemReviewRepository(Path(directory) / "review.sqlite")
        key = repository.upsert_issue({"task_kind": "rule_conflict", "category": "conflict", "title": "规则需核对", "identity": {"template": "x"}})
        today = datetime.now(UTC).astimezone().date().isoformat()
        with repository.connect() as conn:
            conn.execute("INSERT INTO system_review_usage(usage_date,calls,input_tokens,updated_at) VALUES(?,100,0,'now')", (today,))
        service = SystemReviewService(Path(directory) / "main.sqlite", repository)
        assert service.analyze_pending()["budget_paused"] == 1
        assert repository.detail(key)["analysis_status"] == "budget_paused"
        with repository.connect() as conn:
            conn.execute("DELETE FROM system_review_usage WHERE usage_date=?", (today,))
        monkeypatch.setattr(service, "_analyze", lambda _item, **_kwargs: {"summary": "已分析", "severity": "low", "certainty": "high", "suggestion": "核对", "validation": "通过测试", "input_tokens": 10, "output_tokens": 4})
        assert service.analyze_pending()["analyzed"] == 1
        assert repository.detail(key)["analysis_status"] == "completed"


def test_rule_conflict_checks_only_active_template_versions() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "main.sqlite"
        with sqlite3.connect(database) as conn:
            conn.executescript("""
                CREATE TABLE prompt_templates(template_key TEXT PRIMARY KEY,active_version_id INTEGER,feature_mode_enabled INTEGER,feature_mode_text TEXT);
                CREATE TABLE prompt_template_versions(id INTEGER PRIMARY KEY,template_key TEXT,version_number INTEGER,content TEXT,deleted_at TEXT);
                CREATE TABLE publisher_rule_config(singleton_key TEXT,config_json TEXT,prompt_text TEXT,updated_at TEXT);
                INSERT INTO prompt_templates VALUES('active',2,0,'');
                INSERT INTO prompt_template_versions VALUES(1,'active',1,'必须挂后台',NULL);
                INSERT INTO prompt_template_versions VALUES(2,'active',2,'常规快讯',NULL);
                INSERT INTO prompt_templates VALUES('conflict',4,0,'');
                INSERT INTO prompt_template_versions VALUES(3,'conflict',1,'必须挂后台',NULL);
                INSERT INTO prompt_template_versions VALUES(4,'conflict',2,'必须挂后台' || char(10) || '禁止挂后台',NULL);
            """)
        service = SystemReviewService(database, SystemReviewRepository(root / "review.sqlite"))
        issues = service._rule_conflicts()
        assert len(issues) == 1
        assert issues[0]["scope"]["template_key"] == "conflict"


def test_rule_execution_checks_existing_onchain_opener_hyperliquid_backend_and_format_rules() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        database = root / "main.sqlite"
        with sqlite3.connect(database) as conn:
            conn.executescript("""
                CREATE TABLE tasks(
                    id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT, title TEXT,
                    status TEXT, metadata TEXT, raw_payload TEXT, updated_at TEXT
                );
                CREATE TABLE x_task_pipeline(
                    task_id INTEGER PRIMARY KEY, prompt_template_key TEXT, prompt_version_id INTEGER,
                    draft_title TEXT, draft_content TEXT, final_title TEXT, final_content TEXT
                );
            """)
            now = datetime.now(UTC).isoformat()
            conn.execute("INSERT INTO tasks VALUES(1,'x','x-1','https://x.test/1','链上消息','written',?,?,?)", (
                json.dumps({"author_display_name": "Alice"}), "{}", now,
            ))
            conn.execute("INSERT INTO x_task_pipeline VALUES(1,'x_onchain_writer',7,'标题','Alice在X平台发文称事实','标题','Alice在X平台发文称事实')")
            conn.execute("INSERT INTO tasks VALUES(2,'hyperliquid_news','hl-1','https://hl.test/1','账户变化','published',?,?,?)", (
                json.dumps({"force_backend_only": True}), "{}", now,
            ))
            conn.execute("INSERT INTO x_task_pipeline VALUES(2,'hyperliquid_writer',9,'账户盈利','金额12345万美元，样本时间：2026-11-03','账户盈利','金额12345万美元，样本时间：2026-11-03')")
            conn.execute("INSERT INTO tasks VALUES(3,'hyperliquid_news','hl-preview','https://hl.test/preview','预览','published',?,?,?)", (
                json.dumps({"mode": "preview_only"}), "{}", now,
            ))
            conn.execute("INSERT INTO x_task_pipeline VALUES(3,'hyperliquid_writer',9,'预览','样本时间：2026-11-03','预览','样本时间：2026-11-03')")

        service = SystemReviewService(database, SystemReviewRepository(root / "review.sqlite"))
        issues = service._rule_conflicts()
        execution = [item for item in issues if item["category"] == "rule_execution_mismatch"]

        assert len(execution) == 2
        onchain = next(item for item in execution if item["scope"]["source"] == "x")
        hyperliquid = next(item for item in execution if item["scope"]["source"] == "hyperliquid_news")
        assert "据Alice监测" in onchain["actual"]
        assert "强制进入后台" in hyperliquid["actual"]
        assert "金额/比例" in hyperliquid["actual"]
        assert "样本时间" in hyperliquid["actual"]


def test_failure_clusters_only_report_multiday_internal_errors_and_count_recovery() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        main = root / "main.sqlite"
        now = datetime.now(UTC)
        failure_times = [(now - timedelta(days=day)).isoformat() for day in (3, 2, 1)]
        recovered_at = (now - timedelta(days=3) + timedelta(minutes=1)).isoformat()
        with sqlite3.connect(main) as conn:
            conn.executescript("""
                CREATE TABLE tasks(id INTEGER PRIMARY KEY,source TEXT,source_item_id TEXT,status TEXT,updated_at TEXT);
                CREATE TABLE x_task_pipeline(task_id INTEGER PRIMARY KEY,last_error TEXT,prompt_template_key TEXT,prompt_version_id INTEGER);
                CREATE TABLE x_processing_attempts(
                    id INTEGER PRIMARY KEY,task_id INTEGER,stage TEXT,attempt_no INTEGER,executor TEXT,status TEXT,
                    error_code TEXT,error_message TEXT,started_at TEXT,finished_at TEXT,rule_json TEXT
                );
                CREATE TABLE x_capture_attempts(
                    id INTEGER PRIMARY KEY,account_id INTEGER,username_lower TEXT,status TEXT,source TEXT,error TEXT,
                    started_at TEXT,finished_at TEXT,candidate_count INTEGER,new_count INTEGER,saved_count INTEGER
                );
                CREATE TABLE binance_square_attempts(
                    id INTEGER PRIMARY KEY,account_id INTEGER,slug_lower TEXT,status TEXT,error TEXT,
                    started_at TEXT,finished_at TEXT,candidate_count INTEGER,new_count INTEGER,saved_count INTEGER
                );
            """)
            conn.executemany("INSERT INTO tasks VALUES(?,?,?,?,?)", [
                (1, "x", "x-1", "written", recovered_at),
                (2, "x", "x-2", "write_failed", failure_times[1]),
                (3, "panews", "panews-3", "write_failed", failure_times[2]),
            ])
            conn.executemany("INSERT INTO x_task_pipeline VALUES(?,?,?,?)", [
                (1, None, "x_onchain_writer", 4),
                (2, "writer output repeats title in content", "x_onchain_writer", 4),
                (3, "writer output repeats title in content", "x_regular_writer", 40),
            ])
            conn.executemany("INSERT INTO x_processing_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (1, 1, "write", 1, "model", "failed", "EVALIDATION", "writer output repeats title in content", failure_times[0], failure_times[0], '{"prompt_version_id":4}'),
                (2, 1, "write", 2, "model", "succeeded", None, None, recovered_at, recovered_at, '{"prompt_version_id":4}'),
                (3, 2, "write", 1, "model", "failed", "EVALIDATION", "writer output repeats title in content", failure_times[1], failure_times[1], '{"prompt_version_id":4}'),
            ])
            conn.executemany("INSERT INTO x_capture_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)", [
                (1, 10, "account_a", "failed", "fxtwitter", "invalid profile JSON", failure_times[0], failure_times[0], 0, 0, 0),
                (2, 10, "account_a", "failed", "fxtwitter", "invalid profile JSON", failure_times[1], failure_times[1], 0, 0, 0),
                (3, 10, "account_a", "failed", "fxtwitter", "invalid profile JSON", failure_times[2], failure_times[2], 0, 0, 0),
                (4, 10, "account_a", "success", "fxtwitter", None, now.isoformat(), now.isoformat(), 2, 2, 2),
            ])
            conn.execute("INSERT INTO binance_square_attempts VALUES(1,20,'square_a','failed','HTTP 429',?,?,0,0,0)", (failure_times[2], failure_times[2]))

        service = SystemReviewService(main, SystemReviewRepository(root / "review.sqlite"))
        issues = service._failure_clusters()
        processing = next(item for item in issues if item["category"] == "persistent_internal_failure")

        assert len(issues) == 1
        assert processing["title"] == "写作校验持续失败：标题重复在正文"
        assert processing["affected_count"] == 3
        assert "3 个自然日" in processing["summary"]
        assert "当前仍有 2 个对象未恢复" in processing["summary"]
        assert len(processing["counter_evidence"]) == 1
