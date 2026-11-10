from __future__ import annotations

import json
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
from packages.ai_text_self_upgrade.prompts import parse_output
from packages.ai_text_self_upgrade.worker import current_run_key


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
                delivery_key TEXT PRIMARY KEY, odaily_id TEXT, id_match_status TEXT
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
            "INSERT INTO newsflash_production_metadata VALUES('odaily-task-9','news-7',?)",
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
        assert cases == []
        assert unlinked == 1

        with sqlite3.connect(primary) as conn:
            conn.execute("UPDATE newsflash_production_metadata SET id_match_status='matched'")
            conn.execute("UPDATE x_task_pipeline SET final_content='编辑改过的版本。'")
        cases, unlinked = service._discover_cases()
        assert cases == []
        assert unlinked == 1

        with sqlite3.connect(primary) as conn:
            conn.execute("UPDATE x_task_pipeline SET final_content='项目公布融资。'")
            conn.execute("UPDATE newsflash_production_metadata SET delivery_key='odaily-task-9unexpected'")
        cases, unlinked = service._discover_cases()
        assert cases == []
        assert unlinked == 1


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

        repository.decide(key, decision="accepted", actor="editor@example.com", note="同意")
        assert repository.list_items({"status": "accepted"})["accepted_count"] == 1
        repository.decide(key, decision="rejected", actor="editor@example.com", note="复核后不认同")
        repository.decide(key, decision="ignored", actor="editor@example.com", note="特殊例外")
        detail = repository.detail(key)
        assert detail["human_decision"] == "ignored"
        assert [decision["decision"] for decision in detail["decisions"]] == ["accepted", "rejected", "ignored"]
        assert repository.list_items({"status": "accepted"})["total"] == 0
        assert repository.list_items({"status": "ignored"})["total"] == 1


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
