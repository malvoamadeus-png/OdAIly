from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from zoneinfo import ZoneInfo

from packages.auditor.repository import calculate_content_hash
from packages.fact_auditor.prompts import POLICY_VERSION
from packages.ai_text_self_upgrade import (
    AITextSelfUpgradeRepository,
    AITextSelfUpgradeService,
    AITextSelfUpgradeWorker,
)
from packages.ai_text_self_upgrade.prompts import build_prompt, parse_output
from packages.ai_text_self_upgrade.worker import current_run_key
from packages.ai_text_self_upgrade.service import _fetch_postgres_pages


class FakeAIClient:
    def __init__(self, outputs: list[str | Exception] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.calls: list[dict[str, Any]] = []

    def generate_text(self, *, model: str, prompt: str, text_format: dict[str, Any] | None = None,
                      reasoning_effort: str | None = None) -> str:
        self.calls.append({"model": model, "prompt": prompt, "schema": text_format, "reasoning_effort": reasoning_effort})
        output = self.outputs.pop(0) if self.outputs else json.dumps({
            "verdict": "writer1_error",
            "confidence": "high",
            "rationale": "事实审核者指出的内容与原始材料冲突，且草稿已有该表述。",
            "evidence": [{"location": "published_text", "quote": "项目公布融资。", "explanation": "已发布稿件包含该主张。"}],
        }, ensure_ascii=False)
        if isinstance(output, Exception):
            raise output
        return output


def test_postgres_source_paging_releases_each_checkout_and_advances_keyset() -> None:
    class Connection:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows
            self.closed = False
            self.cursor = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def execute(self, query: str, params: tuple[Any, ...]):
            self.query = query
            self.params = params
            return self

        def fetchall(self):
            if "WHERE (reviewed_at, auditor_check_id) < (%s, %s)" not in self.query:
                return self.rows[: self.params[-1]]
            last_key = self.params[-3:-1]
            return [row for row in self.rows if (row["reviewed_at"], row["auditor_check_id"]) < last_key][: self.params[-1]]

    all_rows = [
        {"reviewed_at": 3, "auditor_check_id": 3},
        {"reviewed_at": 3, "auditor_check_id": 2},
        {"reviewed_at": 2, "auditor_check_id": 9},
    ]
    connections: list[Connection] = []

    def connect():
        connection = Connection(all_rows)
        connections.append(connection)
        return connection

    rows = _fetch_postgres_pages(
        connect, "SELECT reviewed_at,auditor_check_id FROM audits", (),
        key_columns=("reviewed_at", "auditor_check_id"), page_size=2,
    )
    assert rows == all_rows
    assert len(connections) == 3
    assert all(connection.closed for connection in connections)


def _create_primary_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE fact_checks (
                id INTEGER PRIMARY KEY, source_item_id TEXT, content_hash TEXT, policy_version TEXT,
                title TEXT, content TEXT, source_url TEXT, source_locator TEXT, published_at TEXT,
                status TEXT, model TEXT, reason TEXT, summary TEXT, original_text TEXT,
                original_url TEXT, result_json TEXT
            );
            CREATE TABLE fact_check_runs (
                id INTEGER PRIMARY KEY, check_id INTEGER, status TEXT, summary TEXT,
                result_json TEXT, completed_at TEXT
            );
            CREATE TABLE newsflash_production_metadata (
                delivery_key TEXT PRIMARY KEY, odaily_id TEXT, id_match_status TEXT, title TEXT
            );
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY, source TEXT, source_item_id TEXT, source_url TEXT,
                title TEXT, content TEXT, metadata TEXT
            );
            CREATE TABLE x_task_pipeline (
                task_id INTEGER PRIMARY KEY, writer_model TEXT, writer_output TEXT,
                prompt_template_key TEXT, prompt_version_id INTEGER, draft_title TEXT,
                draft_content TEXT, final_title TEXT, final_content TEXT,
                write_completed_at TEXT, publish_completed_at TEXT
            );
            """
        )


def _insert_candidate(path: Path, *, flagged_at: datetime, mapped: bool = True,
                      published_content: str = "项目公布融资。") -> None:
    title = "项目公布融资"
    digest = calculate_content_hash(title, published_content)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO fact_checks VALUES(1,'news-7',?,?, '项目公布融资',?,'https://source.example/news','https://source.example/news',?,'flagged','gpt-6-luna','与原文金额不符','融资金额表述有误',?,'https://source.example/news',?)",
            (digest, POLICY_VERSION, published_content, flagged_at.isoformat(), "项目方今日公布融资消息。", json.dumps({
                "status": "flagged", "summary": "融资金额表述有误", "questions": [],
                "stages": {"comparison": {"result": {"status": "flagged", "summary": "金额与原文不符",
                    "issues": [{"original": "项目公布融资。", "reason": "金额表述与来源材料冲突。",
                                "evidence": "项目方今日公布融资消息。"}]}}},
            }, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO fact_check_runs VALUES(1,1,'flagged','融资金额表述有误',?,?)",
            (json.dumps({"status": "flagged"}, ensure_ascii=False), flagged_at.isoformat()),
        )
        conn.execute(
            "INSERT INTO newsflash_production_metadata VALUES('odaily-task-9','news-7',?,'项目公布融资')",
            ("matched" if mapped else "ambiguous",),
        )
        conn.execute(
            "INSERT INTO tasks VALUES(9,'x','post-9','https://source.example/news','源稿标题','项目方今日公布融资消息。','{}')"
        )
        conn.execute(
            "INSERT INTO x_task_pipeline VALUES(9,'gpt-6-luna','{}','x_regular_writer',4,'项目公布融资','项目公布融资。',?,?,?,?)",
            (title, published_content, flagged_at.isoformat(), flagged_at.isoformat()),
        )


def _make_service(root: Path, client: FakeAIClient) -> tuple[AITextSelfUpgradeRepository, AITextSelfUpgradeService]:
    repository = AITextSelfUpgradeRepository(root / "runtime" / "ai_text_self_upgrade.sqlite")
    service = AITextSelfUpgradeService(root / "main.sqlite", repository, client)
    return repository, service


def test_discovery_requires_new_flag_exact_publication_link_and_matching_version() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        repository = AITextSelfUpgradeRepository(root / "review.sqlite")
        enabled_at = datetime.fromisoformat(repository.fact_enabled_at())
        flag_time = enabled_at + timedelta(seconds=1)
        _insert_candidate(primary, flagged_at=flag_time)
        service = AITextSelfUpgradeService(primary, repository, FakeAIClient())

        cases, unlinked = service._discover_cases()
        assert len(cases) == 1
        assert unlinked == 0
        assert cases[0]["task_id"] == 9
        assert cases[0]["source_content"] == "项目方今日公布融资消息。"
        assert cases[0]["audited_content"] == cases[0]["final_content"]

        with sqlite3.connect(primary) as conn:
            conn.execute("UPDATE newsflash_production_metadata SET id_match_status='ambiguous'")
        cases, unlinked = service._discover_cases()
        assert len(cases) == 1
        assert unlinked == 0

        with sqlite3.connect(primary) as conn:
            conn.execute("UPDATE newsflash_production_metadata SET id_match_status='matched'")
            conn.execute("UPDATE x_task_pipeline SET final_content='编辑改过的版本。'")
        cases, unlinked = service._discover_cases()
        assert len(cases) == 1
        assert unlinked == 0

        with sqlite3.connect(primary) as conn:
            conn.execute("UPDATE x_task_pipeline SET final_content='项目公布融资。'")
            conn.execute("UPDATE newsflash_production_metadata SET delivery_key='odaily-task-9unexpected'")
        cases, unlinked = service._discover_cases()
        assert len(cases) == 1
        assert unlinked == 0


def test_discovery_does_not_backfill_pre_activation_audits() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        repository = AITextSelfUpgradeRepository(root / "review.sqlite")
        before_enabled = datetime.fromisoformat(repository.fact_enabled_at()) - timedelta(seconds=1)
        _insert_candidate(primary, flagged_at=before_enabled)
        service = AITextSelfUpgradeService(primary, repository, FakeAIClient())

        assert service._discover_cases() == ([], 0)


def test_empty_slot_skips_model_and_slot_is_idempotent() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        _create_primary_database(root / "main.sqlite")
        client = FakeAIClient()
        repository, service = _make_service(root, client)
        worker = AITextSelfUpgradeWorker(service)
        slot = datetime(2026, 11, 9, 4, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = worker.run_due(now=slot)
        assert result == {"cases_found": 0, "cases_completed": 0, "cases_failed": 0, "cases_unlinked": 0}
        assert client.calls == []
        assert worker.run_due(now=slot + timedelta(minutes=10)) is None
        assert repository.recent_runs()[0]["cases_found"] == 0


def test_schedule_uses_beijing_four_hour_slots():
    beijing = ZoneInfo("Asia/Shanghai")
    assert [
        current_run_key(datetime(2026, 11, 9, hour, 7, tzinfo=beijing))
        for hour in (0, 4, 8, 12, 16, 20)
    ] == [
        "2026-11-09T00:00+08:00", "2026-11-09T04:00+08:00", "2026-11-09T08:00+08:00",
        "2026-11-09T12:00+08:00", "2026-11-09T16:00+08:00", "2026-11-09T20:00+08:00",
    ]


def test_batch_uses_gpt6luna_and_records_decisions_as_history() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        client = FakeAIClient()
        repository = AITextSelfUpgradeRepository(root / "review.sqlite")
        enabled_at = datetime.fromisoformat(repository.fact_enabled_at())
        _insert_candidate(primary, flagged_at=enabled_at + timedelta(seconds=2))
        service = AITextSelfUpgradeService(primary, repository, client)
        worker = AITextSelfUpgradeWorker(service)

        result = worker.run_due(now=datetime(2026, 11, 9, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
        assert result == {"cases_found": 1, "cases_completed": 1, "cases_failed": 0, "cases_unlinked": 0}
        assert len(client.calls) == 1
        assert client.calls[0]["model"] == "gpt-6-luna"
        assert client.calls[0]["reasoning_effort"] == "medium"
        item = repository.list_items({"status": "pending"})["items"][0]
        assert item["verdict"] == "writer1_error"
        assert item["evidence"][0]["quote"] == "项目公布融资。"
        key = item["case_key"]

        with repository.connect() as conn:
            conn.execute("UPDATE ai_text_self_upgrade_items SET human_decision='accepted' WHERE case_key=?", (key,))
            conn.execute("INSERT INTO ai_text_self_upgrade_decisions(case_key,decision,actor,note,decided_at) VALUES(?,?,?,?,?)", (key, "accepted", "editor@example.com", "旧决定", "2026-01-01"))
        assert repository.list_items({"status": "legacy"})["total"] == 1
        repository.decide(key, decision="reviewer_correct", actor="editor@example.com", note="同意")
        assert repository.list_items({"status": "reviewer_correct"})["confirmed_count"] == 1
        repository.decide(key, decision="writer_correct", actor="editor@example.com", note="复核后不认同")
        repository.decide(key, decision="ignored", actor="editor@example.com", note="特殊例外")
        detail = repository.detail(key)
        assert detail["human_resolution"] == "ignored"
        assert [decision["decision"] for decision in detail["decisions"]] == ["accepted", "reviewer_correct", "writer_correct", "ignored"]
        assert detail["decisions"][0]["legacy"] == 1
        assert repository.list_items({"status": "reviewer_correct"})["confirmed_count"] == 0
        assert repository.list_items({"status": "ignored"})["total"] == 1


def test_deleted_case_is_not_recreated_by_later_discovery() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        repository = AITextSelfUpgradeRepository(root / "review.sqlite")
        enabled_at = datetime.fromisoformat(repository.fact_enabled_at())
        _insert_candidate(primary, flagged_at=enabled_at + timedelta(seconds=2))
        service = AITextSelfUpgradeService(primary, repository, FakeAIClient())
        first = service.run_batch(run_key="first")
        assert first["cases_found"] == 1
        case_key = repository.list_items({"status": "all"})["items"][0]["case_key"]
        assert repository.delete_case(case_key, actor="editor@example.com")
        assert not repository.case_exists(case_key)
        rediscovered, _ = service._discover_cases()
        candidate = next(item for item in rediscovered if item["case_key"] == case_key)
        assert not repository.insert_case(candidate)
        assert repository.list_items({"status": "all"})["total"] == 0


def test_sqlite_manual_rejection_requires_exact_writer_snapshot() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        repository = AITextSelfUpgradeRepository(root / "review.sqlite", source_database_path=primary)
        stamp = datetime.now(UTC).isoformat()
        title, content = "Published title", "Published body."
        digest = calculate_content_hash(title, content)
        with sqlite3.connect(primary) as conn:
            conn.execute("""CREATE TABLE auditor_checks(
                id INTEGER PRIMARY KEY,source_item_id TEXT,content_hash TEXT,title TEXT,content TEXT,source_url TEXT,
                published_at TEXT,prompt_version TEXT,model TEXT,audit_result TEXT,status TEXT)""")
            conn.execute("INSERT INTO tasks VALUES(12,'x','post-12','https://source.test/item','Source title','Source body','{}')")
            conn.execute("INSERT INTO newsflash_production_metadata VALUES('odaily-task-12','odaily-12','matched','Published title')")
            conn.execute("INSERT INTO x_task_pipeline VALUES(12,'writer','{}','brief',3,'Draft title','Draft body',?,?,?,?)",
                         (title, content, stamp, stamp))
            conn.execute("INSERT INTO auditor_checks VALUES(41,'odaily-12',?,?,?,'https://source.test/item',?,'audit-v1','reviewer','{}','flagged')",
                         (digest, title, content, stamp))
        queue_id = repository.queue_manual_rejection(41, actor="editor@example.com")
        assert queue_id == "manual-rejection:41"
        assert repository.list_items({"status": "all"})["total"] == 0
        assert repository.process_manual_rejection_queue() == {
            "processed": 1, "completed": 1, "retrying": 0, "deleted": 0,
        }
        key = repository.list_items({"status": "all"})["items"][0]["case_key"]
        case = repository.detail(key)
        assert case["task_id"] == 12
        assert case["source_content"] == "Source body"
        assert case["draft_content"] == "Draft body"
        assert case["final_content"] == content


def test_sqlite_manual_publication_rejection_creates_independent_inconclusive_case() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        stamp = datetime.now(UTC).isoformat()
        title, content = "Manual published title", "Manual published body."
        digest = calculate_content_hash(title, content)
        with sqlite3.connect(primary) as conn:
            conn.execute("ALTER TABLE newsflash_production_metadata ADD COLUMN outlet_category TEXT")
            conn.execute(
                "INSERT INTO newsflash_production_metadata(delivery_key,odaily_id,id_match_status,title,outlet_category) VALUES(?,?,?,?,?)",
                ("manual:post-77", "odaily-manual-77", "matched", title, "manual"),
            )
            conn.execute("""CREATE TABLE auditor_checks(
                id INTEGER PRIMARY KEY,source_item_id TEXT,content_hash TEXT,title TEXT,content TEXT,source_url TEXT,
                published_at TEXT,prompt_version TEXT,model TEXT,audit_result TEXT,status TEXT)""")
            conn.execute(
                "INSERT INTO auditor_checks VALUES(77,'odaily-manual-77',?,?,?,'https://source.test/manual',?,'audit-v1','reviewer','{}','flagged')",
                (digest, title, content, stamp),
            )

        repository = AITextSelfUpgradeRepository(root / "review.sqlite", source_database_path=primary)
        repository.queue_manual_rejection(77, actor="editor@example.com")
        assert repository.process_manual_rejection_queue() == {
            "processed": 1, "completed": 1, "retrying": 0, "deleted": 0,
        }
        item = repository.list_items({})["items"][0]
        case = repository.detail(item["case_key"])
        assert case["task_id"] is None
        assert case["writer_model"] is None
        assert case["draft_content"] is None
        assert case["final_content"] == content
        assert case["audit_result"]["manual_rejection"]["authoring_path"] == "manual_publication"

        inconclusive = {
            "verdict": "inconclusive", "confidence": "low", "rationale": "缺少 Writer1 草稿，无法归因。",
            "evidence": [{"location": "published_text", "quote": content, "explanation": "仅有已发布稿件。"}],
        }
        prompt = build_prompt(case)
        assert "manual_publication_without_writer1_snapshot" in prompt
        result = parse_output(json.dumps(inconclusive, ensure_ascii=False), case)
        repository.complete_analysis(case["case_key"], model="gpt-6-luna", prompt_version="test", result=result)
        completed = repository.detail(case["case_key"])
        assert completed["analysis_status"] == "completed"
        assert completed["verdict"] == "inconclusive"
        assert repository.manual_rejection_status(77) == "completed"


def test_sqlite_rejection_without_writer_snapshot_defaults_to_manual_publication() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        stamp = datetime.now(UTC).isoformat()
        title, content = "Unknown source title", "Published text contains typoo."
        digest = calculate_content_hash(title, content)
        with sqlite3.connect(primary) as conn:
            conn.execute("DROP TABLE newsflash_production_metadata")
            conn.execute("""CREATE TABLE auditor_checks(
                id INTEGER PRIMARY KEY,source_item_id TEXT,content_hash TEXT,title TEXT,content TEXT,source_url TEXT,
                published_at TEXT,prompt_version TEXT,model TEXT,audit_result TEXT,status TEXT)""")
            conn.execute(
                "INSERT INTO auditor_checks VALUES(78,'unknown-78',?,?,?,'https://source.test/unknown',?,'audit-v1','reviewer','{}','flagged')",
                (digest, title, content, stamp),
            )

        repository = AITextSelfUpgradeRepository(root / "review.sqlite", source_database_path=primary)
        repository.queue_manual_rejection(78, actor="editor@example.com")
        assert repository.process_manual_rejection_queue() == {
            "processed": 1, "completed": 1, "retrying": 0, "deleted": 0,
        }
        item = repository.list_items({})["items"][0]
        case = repository.detail(item["case_key"])
        assert case["task_id"] is None
        assert case["audit_result"]["manual_rejection"]["authoring_path"] == "manual_publication"
        assert "manual_publication_without_writer1_snapshot" in build_prompt(case)

        result = parse_output(json.dumps({
            "verdict": "reviewer_correct_unattributed", "confidence": "medium",
            "rationale": "审核意见可由已发布文本确认；人工稿不属于 Writer1 写作。",
            "evidence": [{"location": "published_text", "quote": "typoo", "explanation": "正文中存在被指出的拼写错误。"}],
        }, ensure_ascii=False), case)
        repository.complete_analysis(case["case_key"], model="gpt-6-luna", prompt_version="test", result=result)
        completed = repository.detail(case["case_key"])
        assert completed["analysis_status"] == "completed"
        assert completed["verdict"] == "reviewer_correct_unattributed"
        assert repository.manual_rejection_status(78) == "completed"


def test_sqlite_existing_review_schema_migrates_task_id_to_nullable() -> None:
    with TemporaryDirectory() as directory:
        path = Path(directory) / "review.sqlite"
        from packages.ai_text_self_upgrade import repository as repository_module

        old_schema = repository_module.SCHEMA_SQL.replace("task_id INTEGER,", "task_id INTEGER NOT NULL,").replace(
            "'writer1_error','auditor_false_positive','reviewer_correct_unattributed'",
            "'writer1_error','auditor_false_positive'",
        )
        with sqlite3.connect(path) as conn:
            conn.executescript(old_schema)
        repository = AITextSelfUpgradeRepository(path)
        with repository.connect() as conn:
            task_column = next(row for row in conn.execute("PRAGMA table_info(ai_text_self_upgrade_items)") if row["name"] == "task_id")
            assert task_column["notnull"] == 0


def test_manual_rejection_snapshots_ambiguous_link_and_stops_at_tombstone() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        repository = AITextSelfUpgradeRepository(root / "review.sqlite", source_database_path=primary)
        stamp = datetime.now(UTC).isoformat()
        title, content = "Published title", "Published body."
        digest = calculate_content_hash(title, content)
        with sqlite3.connect(primary) as conn:
            conn.execute("""CREATE TABLE auditor_checks(
                id INTEGER PRIMARY KEY,source_item_id TEXT,content_hash TEXT,title TEXT,content TEXT,source_url TEXT,
                published_at TEXT,prompt_version TEXT,model TEXT,audit_result TEXT,status TEXT)""")
            conn.execute("INSERT INTO tasks VALUES(12,'x','post-12','https://source.test/item','Source title','Source body','{}')")
            conn.execute("INSERT INTO newsflash_production_metadata VALUES('odaily-task-12','odaily-12','ambiguous','Published title')")
            conn.execute("INSERT INTO x_task_pipeline VALUES(12,'writer','{}','brief',3,'Draft title','Draft body',?,?,?,?)",
                         (title, content, stamp, stamp))
            conn.execute("INSERT INTO auditor_checks VALUES(41,'odaily-12',?,?,?,'https://source.test/item',?,'audit-v1','reviewer','{}','flagged')",
                         (digest, title, content, stamp))
        repository.queue_manual_rejection(41, actor="editor@example.com")
        first = repository.process_manual_rejection_queue()
        assert first == {"processed": 1, "completed": 1, "retrying": 0, "deleted": 0}
        item = repository.list_items({})["items"][0]
        case = repository.detail(item["case_key"])
        assert case["task_id"] is None
        assert case["audit_result"]["manual_rejection"]["authoring_path"] == "unknown_publication"
        with repository.connect() as conn:
            queued = conn.execute("SELECT status,attempt_count,last_error FROM ai_text_self_upgrade_manual_rejections").fetchone()
            assert tuple(queued) == ("completed", 1, None)
        with sqlite3.connect(primary) as conn:
            conn.execute("INSERT INTO auditor_checks VALUES(42,'odaily-12',?,?,?,'https://source.test/item',?,'audit-v1','reviewer','{}','flagged')",
                         (digest, title, content, stamp))
        repository.queue_manual_rejection(42, actor="editor@example.com")
        with repository.connect() as conn:
            conn.execute("INSERT INTO ai_text_self_upgrade_deleted_cases(case_key,deleted_by,deleted_at) VALUES(?,?,?)",
                         (hashlib.sha256(f"odaily-12\n{digest}".encode()).hexdigest(), "admin", stamp))
        retried = repository.process_manual_rejection_queue()
        assert retried["deleted"] == 1
        assert repository.list_items({"status": "all"})["total"] == 1
        with repository.connect() as conn:
            assert conn.execute("SELECT status,attempt_count FROM ai_text_self_upgrade_manual_rejections WHERE auditor_check_id=42").fetchone()["status"] == "deleted"


def _now_for_test() -> str:
    return datetime.now(UTC).isoformat()


def test_failed_case_is_retried_in_the_next_four_hour_slot() -> None:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        primary = root / "main.sqlite"
        _create_primary_database(primary)
        client = FakeAIClient([RuntimeError("upstream timeout")])
        repository = AITextSelfUpgradeRepository(root / "review.sqlite")
        enabled_at = datetime.fromisoformat(repository.fact_enabled_at())
        _insert_candidate(primary, flagged_at=enabled_at + timedelta(seconds=3))
        service = AITextSelfUpgradeService(primary, repository, client)
        worker = AITextSelfUpgradeWorker(service)

        first = datetime(2026, 11, 9, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert worker.run_due(now=first)["cases_failed"] == 1
        failed = repository.list_items({"status": "pending"})["items"][0]
        assert failed["analysis_status"] == "failed"
        assert failed["attempt_count"] == 1

        client.outputs.append(json.dumps({
            "verdict": "fact_auditor_error", "confidence": "medium", "rationale": "原始材料支持稿件主张，事实疑点没有证据。",
            "evidence": [{"location": "published_text", "quote": "项目公布融资。", "explanation": "已发布主张可在原文中核对。"}],
        }, ensure_ascii=False))
        second = first + timedelta(hours=4)
        assert worker.run_due(now=second)["cases_completed"] == 1
        completed = repository.detail(failed["case_key"])
        assert completed["analysis_status"] == "completed"
        assert completed["attempt_count"] == 2
        assert completed["verdict"] == "fact_auditor_error"


def test_worker_processes_manual_rejections_even_when_analysis_slot_is_done() -> None:
    class Repository:
        def process_manual_rejection_queue(self):
            return {"processed": 1, "completed": 1, "retrying": 0, "deleted": 0}

        def has_run(self, run_key):
            return True

        def start_run(self, run_key):
            raise AssertionError("existing analysis slot must not start again")

    service = type("Service", (), {"repository": Repository()})()
    result = AITextSelfUpgradeWorker(service).run_due(now=datetime(2026, 11, 9, 12, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert result == {
        "manual_rejections_processed": 1,
        "manual_rejections_completed": 1,
        "manual_rejections_retrying": 0,
        "manual_rejections_deleted": 0,
    }


def test_manual_rejection_processing_rows_are_recovered_after_restart() -> None:
    with TemporaryDirectory() as directory:
        repository = AITextSelfUpgradeRepository(Path(directory) / "review.sqlite")
        repository.queue_manual_rejection(41, actor="editor@example.com")
        with repository.connect() as conn:
            conn.execute("UPDATE ai_text_self_upgrade_manual_rejections SET status='processing'")
        assert repository.recover_manual_rejections() == 1
        with repository.connect() as conn:
            row = conn.execute("SELECT status,next_attempt_at FROM ai_text_self_upgrade_manual_rejections").fetchone()
            assert row["status"] == "pending"
            assert datetime.fromisoformat(row["next_attempt_at"]) <= datetime.now(UTC)


def test_daily_retention_runs_from_always_on_worker_once_and_retries_failures(monkeypatch) -> None:
    from types import SimpleNamespace

    from packages import maintenance

    with TemporaryDirectory() as directory:
        repository = AITextSelfUpgradeRepository(Path(directory) / "review.sqlite")
        worker = AITextSelfUpgradeWorker(type("Service", (), {"repository": repository})())
        calls = []
        outcome = SimpleNamespace(deleted={"runtime": 2}, cleared={"tasks_payloads": 3}, has_more=False)

        class Maintenance:
            def cleanup_main_pipeline_fields(self, **kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    raise RuntimeError("database temporarily unavailable")
                return outcome

        monkeypatch.setattr(maintenance, "create_maintenance_repository", lambda: Maintenance())
        morning = datetime(2026, 11, 20, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert worker.run_storage_retention_due(now=morning.replace(hour=5, minute=59)) is None
        assert worker.run_storage_retention_due(now=morning) == {"failed": 1}
        assert repository.run_status("storage_retention:2026-11-20:v1") == "failed"
        assert worker.run_storage_retention_due(now=morning) is None
        with repository.connect() as conn:
            conn.execute(
                "UPDATE ai_text_self_upgrade_runs SET completed_at=? WHERE run_key=?",
                ((datetime.now(UTC) - timedelta(minutes=6)).isoformat(), "storage_retention:2026-11-20:v1"),
            )
        assert worker.run_storage_retention_due(now=morning) == {"runtime": 2, "tasks_payloads": 3}
        assert worker.run_storage_retention_due(now=morning) is None
        assert len(calls) == 2
        assert calls[-1] == {"dry_run": False, "completed_field_retention_days": 5}
        assert repository.run_status("storage_retention:2026-11-20:v1") == "completed"


def test_daily_retention_run_stays_resumable_until_all_batches_are_drained(monkeypatch) -> None:
    from types import SimpleNamespace

    from packages import maintenance

    with TemporaryDirectory() as directory:
        repository = AITextSelfUpgradeRepository(Path(directory) / "review.sqlite")
        worker = AITextSelfUpgradeWorker(type("Service", (), {"repository": repository})())
        batches = [True, False]
        calls = []

        class Maintenance:
            def cleanup_main_pipeline_fields(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(deleted={}, cleared={"tasks_payloads": 500}, has_more=batches.pop(0))

        monkeypatch.setattr(maintenance, "create_maintenance_repository", lambda: Maintenance())
        morning = datetime(2026, 11, 20, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert worker.run_storage_retention_due(now=morning) == {"tasks_payloads": 500}
        assert repository.run_status("storage_retention:2026-11-20:v1") == "running"
        assert worker.run_storage_retention_due(now=morning) == {"tasks_payloads": 500}
        assert repository.run_status("storage_retention:2026-11-20:v1") == "completed"
        assert worker.run_storage_retention_due(now=morning) is None
        assert len(calls) == 2


def test_daily_retention_resumes_previous_day_batch(monkeypatch) -> None:
    from types import SimpleNamespace

    from packages import maintenance

    with TemporaryDirectory() as directory:
        repository = AITextSelfUpgradeRepository(Path(directory) / "review.sqlite")
        worker = AITextSelfUpgradeWorker(type("Service", (), {"repository": repository})())
        prior_run_key = "storage_retention:2026-11-19:v1"
        assert repository.start_run(prior_run_key)
        calls = []

        class Maintenance:
            def cleanup_main_pipeline_fields(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(deleted={}, cleared={"tasks_payloads": 1}, has_more=False)

        monkeypatch.setattr(maintenance, "create_maintenance_repository", lambda: Maintenance())
        next_day = datetime(2026, 11, 20, 7, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        assert worker.run_storage_retention_due(now=next_day) == {"tasks_payloads": 1}
        assert repository.run_status(prior_run_key) == "completed"
        assert repository.run_status("storage_retention:2026-11-20:v1") is None
        assert len(calls) == 1


def test_retention_exception_does_not_skip_the_worker_analysis_round() -> None:
    worker = AITextSelfUpgradeWorker(object())
    calls = []

    def broken_retention():
        calls.append("retention")
        raise RuntimeError("cleanup failed")

    def analysis_round():
        calls.append("analysis")
        worker._stop.set()
        return None

    worker.run_storage_retention_due = broken_retention
    worker.run_due = analysis_round
    worker._run()

    assert calls == ["retention", "analysis"]


def test_worker_compensates_feedback_committed_before_manual_queue_insert() -> None:
    from types import SimpleNamespace

    class Outbox:
        status = "pending"

        def pending_manual_rejections(self, *, limit):
            assert limit == 100
            return [SimpleNamespace(id=9, auditor_check_id=41, actor_email="editor@example.com", status=self.status)] if self.status in {"pending", "queued"} else []

        def mark_manual_rejection_queued(self, feedback_id):
            assert feedback_id == 9
            self.status = "queued"

        def mark_manual_rejection_complete(self, feedback_id, *, deleted=False):
            assert feedback_id == 9 and not deleted
            self.status = "completed"

        def mark_manual_rejection_failed(self, feedback_id, *, error):
            raise AssertionError(error)

    class Repository:
        status = None

        def queue_manual_rejection(self, auditor_check_id, *, actor):
            assert auditor_check_id == 41 and actor == "editor@example.com"
            self.status = "pending"
            return "manual-rejection:41"

        def process_manual_rejection_queue(self):
            self.status = "completed"
            return {"processed": 1, "completed": 1, "retrying": 0, "deleted": 0}

        def manual_rejection_status(self, auditor_check_id):
            assert auditor_check_id == 41
            return self.status

        def has_run(self, _run_key):
            return True

        def start_run(self, _run_key):
            raise AssertionError("analysis slot is already complete")

    outbox, repository = Outbox(), Repository()
    service = type("Service", (), {"repository": repository, "manual_rejection_outbox": outbox})()
    result = AITextSelfUpgradeWorker(service).run_due(
        now=datetime(2026, 11, 20, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert repository.status == "completed"
    assert outbox.status == "completed"
    assert result["manual_rejections_outbox_queued"] == 1


def test_parser_rejects_evidence_not_found_in_snapshot() -> None:
    raw = json.dumps({
        "verdict": "writer1_error", "confidence": "high", "rationale": "有问题。",
        "evidence": [{"location": "published_text", "quote": "不存在的原句", "explanation": "引用证据。"}],
    }, ensure_ascii=False)
    case = {"audited_title": "标题", "audited_content": "正文", "source_title": "", "source_content": "",
            "draft_title": "", "draft_content": ""}
    try:
        parse_output(raw, case)
    except ValueError as exc:
        assert "证据无法" in str(exc)
    else:
        raise AssertionError("invalid evidence was accepted")
