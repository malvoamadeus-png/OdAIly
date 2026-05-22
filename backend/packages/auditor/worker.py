from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from packages.common.config import AuditorSettings
from packages.common.heartbeat import HeartbeatThrottle
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient
from packages.x_processing.telegram import TelegramClient

from .models import AuditorResult, AuditorTask
from .prompts import AUDITOR_PROMPT_VERSION, AUDITOR_SCHEMA, auditor_result_to_dict, build_auditor_prompt, parse_auditor_output
from .repository import AuditorRepository


@dataclass(frozen=True, slots=True)
class AuditorRunResult:
    processed: int
    passed: int
    flagged: int
    failed: int
    message: str
    exit_code: int = 0


class AuditorWorker:
    def __init__(
        self,
        *,
        repository: AuditorRepository,
        settings: AuditorSettings,
        ai_client: TextGenerationClient | None = None,
        telegram_client: TelegramClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.worker_id = worker_id or f"auditor-{os.getpid()}"
        self.ai_client = ai_client or self._build_ai_client()
        self.telegram_client = telegram_client or self._build_telegram_client()
        self._heartbeat_throttle = HeartbeatThrottle(
            component="auditor",
            worker_id=self.worker_id,
            writer=lambda component, worker_id, status, success, error, metadata: self.repository.record_worker_heartbeat(
                component=component,
                worker_id=worker_id,
                status=status,
                success=success,
                error=error,
                metadata=metadata,
            ),
        )

    def run_once(self) -> AuditorRunResult:
        task = self.repository.claim_task(
            worker_id=self.worker_id,
            prompt_version=AUDITOR_PROMPT_VERSION,
            lookback_minutes=self.settings.lookback_minutes,
        )
        if task is None:
            self._heartbeat(success=True, metadata={"processed": 0})
            return AuditorRunResult(processed=0, passed=0, flagged=0, failed=0, message="no ready auditor task")
        try:
            result = self._process_task(task)
            self._heartbeat(success=True, metadata={"processed": 1, "source_item_id": task.source_item_id, "result": result.message})
            return result
        except Exception as exc:
            self.repository.complete_failed(task, error=str(exc))
            self._heartbeat(success=False, error=str(exc), metadata={"processed": 1, "source_item_id": task.source_item_id})
            return AuditorRunResult(processed=1, passed=0, flagged=0, failed=1, message=str(exc), exit_code=1)

    def run_forever(self) -> None:
        print("[odaily] auditor worker started")
        while True:
            processed = 0
            passed = 0
            flagged = 0
            failed = 0
            message = "idle"
            for _ in range(self.settings.max_items_per_run):
                result = self.run_once()
                processed += result.processed
                passed += result.passed
                flagged += result.flagged
                failed += result.failed
                message = result.message
                if result.processed == 0:
                    break
            if processed:
                print(
                    "[odaily] auditor round "
                    f"processed={processed} passed={passed} flagged={flagged} failed={failed} message={message}"
                )
            time.sleep(self.settings.worker_idle_sleep_seconds)

    def _process_task(self, task: AuditorTask) -> AuditorRunResult:
        raw_output = self.ai_client.generate_text(
            model=self.settings.model,
            prompt=build_auditor_prompt(task),
            text_format=AUDITOR_SCHEMA,
        )
        audit = parse_auditor_output(raw_output, task)
        result_payload = auditor_result_to_dict(audit)
        if not audit.has_issue:
            self.repository.complete_passed(
                task,
                model=self.settings.model,
                prompt_version=AUDITOR_PROMPT_VERSION,
                raw_output=raw_output,
                result=result_payload,
            )
            return AuditorRunResult(processed=1, passed=1, flagged=0, failed=0, message="passed")

        telegram_text = build_telegram_text(task, audit)
        telegram_result = self.telegram_client.send_message(
            telegram_text,
            message_thread_id=self.settings.telegram_message_thread_id,
        )
        self.repository.complete_flagged(
            task,
            model=self.settings.model,
            prompt_version=AUDITOR_PROMPT_VERSION,
            raw_output=raw_output,
            result=result_payload,
            telegram_text=telegram_text,
            telegram_result=telegram_result.model_dump(mode="json"),
        )
        return AuditorRunResult(
            processed=1,
            passed=0,
            flagged=1,
            failed=0,
            message="flagged" if telegram_result.ok else f"flagged telegram_failed={telegram_result.error}",
        )

    def _build_ai_client(self) -> TextGenerationClient:
        if not self.settings.openai_api_key:
            raise RuntimeError("Missing OPENAI_API_KEY")
        return OpenAIResponsesClient(
            api_key=self.settings.openai_api_key,
            base_url=str(self.settings.openai_base_url),
            api_style=self.settings.openai_api_style,
            timeout_seconds=self.settings.request_timeout_seconds,
            max_attempts=self.settings.retry.max_attempts,
            backoff_seconds=self.settings.retry.backoff_seconds,
        )

    def _build_telegram_client(self) -> TelegramClient:
        if self.settings.telegram_message_thread_id is None:
            raise RuntimeError("Missing AUDITOR_TELEGRAM_MESSAGE_THREAD_ID")
        return TelegramClient(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            timeout_seconds=self.settings.telegram_timeout_seconds,
            max_attempts=self.settings.retry.max_attempts,
            backoff_seconds=self.settings.retry.backoff_seconds,
        )

    def _heartbeat(self, *, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        try:
            self._heartbeat_throttle.send(
                status="ok" if success else "failed",
                success=success,
                error=error,
                metadata=metadata,
            )
        except Exception as exc:
            print(f"[odaily] auditor heartbeat failed: {exc}")


def build_telegram_text(task: AuditorTask, audit: AuditorResult) -> str:
    lines = [
        f"审核者发现疑似问题（{audit.severity}）",
        f"标题：{task.title or '(无标题)'}",
    ]
    if task.published_at:
        lines.append(f"发布时间：{task.published_at.isoformat()}")
    lines.append(f"链接：{odaily_newsflash_url(task)}")
    if audit.summary:
        lines.append(f"摘要：{audit.summary}")
    for index, issue in enumerate(audit.issues, start=1):
        lines.extend(
            [
                "",
                f"{index}. {issue.issue_type} / {issue.location}",
                f"原文：{issue.original}",
                f"建议：{issue.suggested}",
            ]
        )
        if issue.reason:
            lines.append(f"原因：{issue.reason}")
    return "\n".join(lines).strip()


def odaily_newsflash_url(task: AuditorTask) -> str:
    if task.source_item_id:
        return f"https://www.odaily.news/zh-CN/newsflash/{task.source_item_id}"
    return task.source_url or ""
