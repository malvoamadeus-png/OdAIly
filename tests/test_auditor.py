from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from packages.auditor.models import AuditorTask
from packages.auditor.prompts import AUDITOR_PROMPT_VERSION, build_auditor_prompt, parse_auditor_output
from packages.auditor.repository import calculate_content_hash
from packages.auditor.worker import AuditorWorker
from packages.common.config import AuditorSettings, RetrySettings
from packages.x_processing.telegram import TelegramResult


class FakeAiClient:
    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def generate_text(self, *, model: str, prompt: str, text_format: dict[str, Any] | None = None, reasoning_effort: str | None = None) -> str:
        self.calls.append({"model": model, "prompt": prompt, "text_format": text_format, "reasoning_effort": reasoning_effort})
        return json.dumps(self.outputs.pop(0), ensure_ascii=False)


class FakeTelegramClient:
    def __init__(self, result: TelegramResult | None = None) -> None:
        self.result = result or TelegramResult(ok=True, status_code=200, response_json={"ok": True})
        self.calls: list[dict[str, Any]] = []

    def send_message(self, text: str, *, message_thread_id: int | str | None = None, reply_markup: dict[str, Any] | None = None) -> TelegramResult:
        self.calls.append({"text": text, "message_thread_id": message_thread_id, "reply_markup": reply_markup})
        return self.result


class FakeAuditorRepository:
    def __init__(self, tasks: list[AuditorTask]) -> None:
        self.tasks = tasks
        self.done_keys: set[tuple[str, str, str]] = set()
        self.passed: list[dict[str, Any]] = []
        self.flagged: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []
        self.heartbeats: list[dict[str, Any]] = []

    def init_schema(self) -> None:
        return None

    def claim_task(self, *, worker_id: str, prompt_version: str, lookback_minutes: int, lock_seconds: int = 300) -> AuditorTask | None:
        for task in self.tasks:
            key = (task.source_item_id, task.content_hash, prompt_version)
            if key not in self.done_keys:
                return task
        return None

    def complete_passed(self, task: AuditorTask, *, model: str, prompt_version: str, raw_output: str, result: dict[str, Any]) -> None:
        self.done_keys.add((task.source_item_id, task.content_hash, prompt_version))
        self.passed.append({"task": task, "model": model, "prompt_version": prompt_version, "raw_output": raw_output, "result": result})

    def complete_flagged(
        self,
        task: AuditorTask,
        *,
        model: str,
        prompt_version: str,
        raw_output: str,
        result: dict[str, Any],
        telegram_text: str,
        telegram_result: dict[str, Any],
    ) -> None:
        self.done_keys.add((task.source_item_id, task.content_hash, prompt_version))
        self.flagged.append(
            {
                "task": task,
                "model": model,
                "prompt_version": prompt_version,
                "raw_output": raw_output,
                "result": result,
                "telegram_text": telegram_text,
                "telegram_result": telegram_result,
            }
        )

    def complete_failed(self, task: AuditorTask, *, error: str) -> None:
        self.done_keys.add((task.source_item_id, task.content_hash, AUDITOR_PROMPT_VERSION))
        self.failed.append({"task": task, "error": error})

    def record_worker_heartbeat(
        self,
        *,
        component: str,
        worker_id: str,
        status: str,
        success: bool,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.heartbeats.append(
            {"component": component, "worker_id": worker_id, "status": status, "success": success, "error": error, "metadata": metadata}
        )


def settings() -> AuditorSettings:
    return AuditorSettings(
        openai_api_key="test",
        retry=RetrySettings(max_attempts=1, backoff_seconds=0),
        telegram_bot_token="token",
        telegram_chat_id="-100",
        telegram_message_thread_id=77,
    )


def task(source_item_id: str = "481469", *, title: str = "标题", content: str = "Odaily星球日报讯 内容。") -> AuditorTask:
    return AuditorTask(
        id=1,
        source_item_id=source_item_id,
        source_url=None,
        title=title,
        content=content,
        content_hash=calculate_content_hash(title, content),
        published_at=datetime.now(UTC),
    )


def test_auditor_prompt_excludes_odaily_fixed_expression_checks() -> None:
    prompt = build_auditor_prompt(task())

    assert "不要检查" in prompt
    assert "Odaily 固定前缀" in prompt
    assert "媒体称谓" in prompt
    assert "空格风格" in prompt
    assert "在定价之前，看见变化" in prompt
    assert "结构助词“的”" in prompt
    assert "一新创建地址" in prompt
    assert "提出 / 转出 / 转入 / 提取 / 存入" in prompt
    assert "交易开放时间：" in prompt


def test_auditor_prompt_version_is_v5() -> None:
    assert AUDITOR_PROMPT_VERSION == "auditor_zh_quality_v5"


def test_auditor_passed_does_not_send_telegram() -> None:
    repo = FakeAuditorRepository([task()])
    ai = FakeAiClient([{"has_issue": False, "severity": "low", "issues": [], "summary": ""}])
    telegram = FakeTelegramClient()
    worker = AuditorWorker(repository=repo, settings=settings(), ai_client=ai, telegram_client=telegram, worker_id="auditor-test")

    result = worker.run_once()

    assert result.passed == 1
    assert repo.passed[0]["prompt_version"] == AUDITOR_PROMPT_VERSION
    assert telegram.calls == []


def test_auditor_flagged_sends_to_dedicated_topic() -> None:
    current = task(content="Odaily星球日报讯 项目完成融资。。")
    repo = FakeAuditorRepository([current])
    ai = FakeAiClient(
        [
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "punctuation",
                        "location": "content",
                        "original": "融资。。",
                        "suggested": "融资。",
                        "reason": "连续句号。",
                    }
                ],
                "summary": "正文存在连续句号。",
            }
        ]
    )
    telegram = FakeTelegramClient()
    worker = AuditorWorker(repository=repo, settings=settings(), ai_client=ai, telegram_client=telegram)

    result = worker.run_once()

    assert result.flagged == 1
    assert telegram.calls[0]["message_thread_id"] == 77
    assert "审核者发现疑似问题" in telegram.calls[0]["text"]
    assert repo.flagged[0]["telegram_result"]["ok"] is True


def test_auditor_telegram_failure_records_flagged_result() -> None:
    current = task(content="Odaily星球日报讯 项目完成融资。。")
    repo = FakeAuditorRepository([current])
    ai = FakeAiClient(
        [
            {
                "has_issue": True,
                "severity": "high",
                "issues": [
                    {
                        "type": "punctuation",
                        "location": "content",
                        "original": "融资。。",
                        "suggested": "融资。",
                        "reason": "连续句号。",
                    }
                ],
                "summary": "正文存在连续句号。",
            }
        ]
    )
    telegram = FakeTelegramClient(TelegramResult(ok=False, error="telegram failed"))
    worker = AuditorWorker(repository=repo, settings=settings(), ai_client=ai, telegram_client=telegram)

    result = worker.run_once()

    assert result.failed == 0
    assert result.flagged == 1
    assert repo.flagged[0]["telegram_result"]["ok"] is False
    assert repo.flagged[0]["telegram_result"]["error"] == "telegram failed"


def test_auditor_skips_unchanged_hash_and_reaudits_changed_content() -> None:
    original = task(content="Odaily星球日报讯 内容。")
    changed = replace(original, content="Odaily星球日报讯 内容。。")
    changed = replace(changed, content_hash=calculate_content_hash(changed.title, changed.content))
    repo = FakeAuditorRepository([original, changed])
    repo.done_keys.add((original.source_item_id, original.content_hash, AUDITOR_PROMPT_VERSION))
    ai = FakeAiClient([{"has_issue": False, "severity": "low", "issues": [], "summary": ""}])
    telegram = FakeTelegramClient()
    worker = AuditorWorker(repository=repo, settings=settings(), ai_client=ai, telegram_client=telegram)

    result = worker.run_once()

    assert result.processed == 1
    assert ai.calls
    assert repo.passed[0]["task"].content_hash == changed.content_hash


def test_parse_auditor_output_drops_invalid_issue_original() -> None:
    current = task(content="Odaily星球日报讯 项目完成融资。")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "punctuation",
                        "location": "content",
                        "original": "不存在片段",
                        "suggested": "替换",
                        "reason": "模型误报。",
                    }
                ],
                "summary": "误报",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_spacing_issue_type() -> None:
    current = task(content="a16z 关联钱包 (0xb5E4...c24e)")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "low",
                "issues": [
                    {
                        "type": "spacing",
                        "location": "content",
                        "original": "a16z 关联钱包 (0xb5E4...c24e)",
                        "suggested": "a16z关联钱包（0xb5E4...c24e）",
                        "reason": "空格风格不统一。",
                    }
                ],
                "summary": "空格问题。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_fixed_trailing_slogan_issue() -> None:
    current = task(content="Odaily星球日报讯 项目完成融资。\n在定价之前，看见变化")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "low",
                "issues": [
                    {
                        "type": "other",
                        "location": "content",
                        "original": "在定价之前，看见变化",
                        "suggested": "在定价之前，先看见变化",
                        "reason": "正文末句语序不通。",
                    }
                ],
                "summary": "固定标语被误报。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_punctuation_inserted_before_existing_line_break() -> None:
    current = task(
        content=(
            "Odaily星球日报讯 据官方消息，MGBX 将上线 REUSDT、ARXUSDT、ALABUSDT 永续合约交易对\n"
            "交易开放时间：2026 年 6 月 26 日 18:00（SGT）"
        )
    )
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "punctuation",
                        "location": "content",
                        "original": "永续合约交易对\n交易开放时间：2026 年 6 月 26 日 18:00（SGT）",
                        "suggested": "永续合约交易对。\n交易开放时间：2026 年 6 月 26 日 18:00（SGT）",
                        "reason": "上一句句末缺少标点。",
                    }
                ],
                "summary": "跨行误报缺少句号。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_missing_de_issue() -> None:
    current = task(content="Odaily星球日报讯 应 Shielded Labs 请求，项目更新参数。")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "grammar",
                        "location": "content",
                        "original": "应 Shielded Labs 请求",
                        "suggested": "应 Shielded Labs 的请求",
                        "reason": "缺少结构助词“的”。",
                    }
                ],
                "summary": "误报缺少的。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_missing_de_reason_even_with_extra_suggestion() -> None:
    current = task(title="Mag8中25%已在资产负债表持有比特币")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "grammar",
                        "location": "title",
                        "original": "Mag8中25%已在资产负债表持有比特币",
                        "suggested": "Mag8中25%的公司已在资产负债表上持有比特币",
                        "reason": "“25%”后缺少“的公司”等修饰成分，且“在资产负债表持有”语序不通。",
                    }
                ],
                "summary": "误报缺少的。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_keeps_non_de_grammar_issue() -> None:
    current = task(content="Odaily星球日报讯 宣布完成融资。")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "grammar",
                        "location": "content",
                        "original": "宣布完成融资。",
                        "suggested": "该项目宣布完成融资。",
                        "reason": "缺少必要的主语。",
                    }
                ],
                "summary": "正文存在成分缺失。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is True
    assert len(parsed.issues) == 1


def test_parse_auditor_output_ignores_headline_quantifier_expansion_issue() -> None:
    current = task(title="一新创建地址从币安提取1683枚BTC，价值1.05亿美元")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "grammar",
                        "location": "title",
                        "original": "一新创建地址",
                        "suggested": "一个新创建的地址",
                        "reason": "缺少量词，且语序不完整。",
                    }
                ],
                "summary": "标题疑似缺少量词。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []


def test_parse_auditor_output_ignores_chain_transfer_action_issue() -> None:
    current = task(content="某地址提出 50 WBTC，并提出 822.51 枚 ETH 至新地址。")
    parsed = parse_auditor_output(
        json.dumps(
            {
                "has_issue": True,
                "severity": "medium",
                "issues": [
                    {
                        "type": "typo",
                        "location": "content",
                        "original": "提出 50 WBTC",
                        "suggested": "购入 50 WBTC",
                        "reason": "“提出”在此语境不通，应为买入。",
                    },
                    {
                        "type": "typo",
                        "location": "content",
                        "original": "提出 822.51 枚 ETH",
                        "suggested": "购入 822.51 枚 ETH",
                        "reason": "“提出”在此语境不通，应为买入。",
                    }
                ],
                "summary": "正文动作词疑似错误。",
            },
            ensure_ascii=False,
        ),
        current,
    )

    assert parsed.has_issue is False
    assert parsed.issues == []
