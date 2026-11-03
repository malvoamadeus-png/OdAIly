from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from packages.system_observability import MainTaskProjector, SystemInformationStore
from packages.system_observability.projector import SourceAuditInstaller, TableSpec
from packages.system_observability.reviews import SystemReviewRepository, SystemReviewService
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
        assert result["estimated_growth_next_30d_bytes"] >= 0


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
