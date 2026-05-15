from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from packages.common.config import Writer3Settings
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient
from packages.x_processing.telegram import TelegramClient

from .index import Writer3Index
from .matching import exclusion_reason
from .models import AnalysisResult, ContextResult, Writer3Candidate, Writer3Task
from .prompts import (
    ANALYSIS_SCHEMA,
    CONTEXT_SCHEMA,
    build_analysis_prompt,
    build_context_prompt,
    parse_analysis_output,
    parse_context_output,
)
from .repository import Writer3Repository


@dataclass(frozen=True, slots=True)
class Writer3RunResult:
    processed: int
    sent: int
    skipped: int
    failed: int
    message: str
    exit_code: int = 0


class Writer3Worker:
    def __init__(
        self,
        *,
        repository: Writer3Repository,
        index: Writer3Index,
        settings: Writer3Settings,
        ai_client: TextGenerationClient | None = None,
        telegram_client: TelegramClient | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.repository = repository
        self.index = index
        self.settings = settings
        self.worker_id = worker_id or f"writer3-{os.getpid()}"
        self.start_after = parse_start_after(self.settings.start_after)
        self.ai_client = ai_client or self._build_ai_client()
        self.telegram_client = telegram_client or self._build_telegram_client()

    def run_once(self) -> Writer3RunResult:
        task = self.repository.claim_task(
            worker_id=self.worker_id,
            start_after=self.start_after,
            freshness_window_seconds=self.settings.current_freshness_window_seconds,
        )
        if task is None:
            self._heartbeat(success=True, metadata={"processed": 0})
            return Writer3RunResult(processed=0, sent=0, skipped=0, failed=0, message="no ready writer3 task")
        try:
            result = self._process_task(task)
            self._heartbeat(success=True, metadata={"processed": 1, "current_source_item_id": task.source_item_id, "result": result.message})
            return result
        except Exception as exc:
            self.repository.complete_failed(task, error=str(exc))
            self._heartbeat(success=False, error=str(exc), metadata={"processed": 1, "current_source_item_id": task.source_item_id})
            return Writer3RunResult(processed=1, sent=0, skipped=0, failed=1, message=str(exc), exit_code=1)

    def run_forever(self) -> None:
        print("[odaily] writer3 worker started")
        while True:
            result = self.run_once()
            if result.processed:
                print(
                    "[odaily] writer3 round "
                    f"processed={result.processed} sent={result.sent} skipped={result.skipped} failed={result.failed} "
                    f"message={result.message}"
                )
            time.sleep(self.settings.worker_idle_sleep_seconds)

    def _process_task(self, task: Writer3Task) -> Writer3RunResult:
        terms = self.repository.list_enabled_filter_keywords()
        reason = exclusion_reason(task.title, task.final_content, terms)
        if reason:
            self.repository.complete_skipped(task, reason="excluded_keyword", metadata={"term": reason})
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message=f"excluded term={reason}")

        if task.published_at is None:
            self.repository.complete_skipped(task, reason="missing_published_at")
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message="missing published_at")
        age_seconds = (datetime.now(UTC) - task.published_at.astimezone(UTC)).total_seconds()
        if age_seconds > self.settings.current_freshness_window_seconds:
            self.repository.complete_skipped(
                task,
                reason="expired_current_news",
                metadata={
                    "age_seconds": int(age_seconds),
                    "window_seconds": self.settings.current_freshness_window_seconds,
                },
            )
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message="expired current news")

        analysis_raw = self.ai_client.generate_text(
            model=self.settings.analysis_model,
            prompt=build_analysis_prompt(task),
            text_format=ANALYSIS_SCHEMA,
        )
        analysis = parse_analysis_output(analysis_raw)
        if not analysis.should_run_writer3:
            self.repository.complete_skipped(task, reason="analysis_not_triggered", metadata={"analysis": analysis_to_dict(analysis)})
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message="analysis not triggered")

        candidates = self.index.search(
            analysis=analysis,
            current_time=task.published_at,
            history_days=self.settings.history_days,
            candidate_limit=self.settings.candidate_limit,
            exclude_source_item_id=task.source_item_id,
        )
        if not candidates:
            self.repository.complete_skipped(task, reason="no_candidates", metadata={"analysis": analysis_to_dict(analysis)})
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message="no candidates")

        context_candidates = candidates[: self.settings.context_candidates]
        context_raw = self.ai_client.generate_text(
            model=self.settings.writer_model,
            prompt=build_context_prompt(task, analysis, context_candidates),
            text_format=CONTEXT_SCHEMA,
            reasoning_effort=self.settings.writer_reasoning_effort,
        )
        context = parse_context_output(context_raw)
        evidence_url = evidence_source_url(context=context, candidates=context_candidates)
        if not context.should_write or not context.context_text or not evidence_url:
            self.repository.complete_skipped(
                task,
                reason="context_not_writable",
                metadata={"analysis": analysis_to_dict(analysis), "candidate_count": len(candidates)},
            )
            return Writer3RunResult(processed=1, sent=0, skipped=1, failed=0, message="context not writable")

        telegram_text = build_telegram_text(task=task, context=context, evidence_source_url=evidence_url)
        telegram_result = self.telegram_client.send_message(
            telegram_text,
            message_thread_id=self.settings.telegram_message_thread_id,
            reply_markup=writer3_confirm_reply_markup(task),
        )
        if not telegram_result.ok:
            raise RuntimeError(telegram_result.error or "writer3 telegram send failed")
        self._record_confirmation_target(task=task, telegram_text=telegram_text, telegram_result=telegram_result.model_dump(mode="json"))
        self.repository.complete_sent(
            task,
            analysis=analysis_to_dict(analysis),
            candidates=context_candidates,
            context=context,
            telegram_text=telegram_text,
            telegram_result=telegram_result.model_dump(mode="json"),
            analysis_model=self.settings.analysis_model,
            writer_model=self.settings.writer_model,
            writer_reasoning_effort=self.settings.writer_reasoning_effort,
        )
        return Writer3RunResult(processed=1, sent=1, skipped=0, failed=0, message="sent")

    def _record_confirmation_target(self, *, task: Writer3Task, telegram_text: str, telegram_result: dict[str, Any]) -> None:
        message = (telegram_result.get("response_json") or {}).get("result") if isinstance(telegram_result.get("response_json"), dict) else None
        if not isinstance(message, dict):
            return
        message_id = message.get("message_id")
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = chat.get("id")
        if message_id is None or chat_id is None or task.context_id is None:
            return
        self.index.upsert_telegram_confirmation(
            message_id=int(message_id),
            chat_id=str(chat_id),
            context_id=int(task.context_id),
            current_source=task.source,
            current_source_item_id=task.source_item_id,
            current_message_text=telegram_text,
            sent_at=datetime.now(UTC),
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
            raise RuntimeError("Missing WRITER3_TELEGRAM_MESSAGE_THREAD_ID")
        return TelegramClient(
            bot_token=self.settings.telegram_bot_token,
            chat_id=self.settings.telegram_chat_id,
            timeout_seconds=self.settings.telegram_timeout_seconds,
            max_attempts=self.settings.retry.max_attempts,
            backoff_seconds=self.settings.retry.backoff_seconds,
        )

    def _heartbeat(self, *, success: bool, error: str | None = None, metadata: dict[str, Any] | None = None) -> None:
        try:
            self.repository.record_worker_heartbeat(
                component="writer3",
                worker_id=self.worker_id,
                status="ok" if success else "failed",
                success=success,
                error=error,
                metadata=metadata,
            )
        except Exception as exc:
            print(f"[odaily] writer3 heartbeat failed: {exc}")


def parse_start_after(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def build_telegram_text(
    *,
    task: Writer3Task,
    context: ContextResult,
    evidence_source_url: str | None = None,
    candidates: list[Writer3Candidate] | None = None,
) -> str:
    if evidence_source_url is None and candidates is not None:
        evidence_source_url = evidence_source_url_for_context(context=context, candidates=candidates)
    parts = [task.final_content.strip(), context.context_text.strip()]
    current_url = current_news_url(task)
    if current_url:
        parts.append(f"当前快讯链接：{current_url}")
    if evidence_source_url:
        parts.append(f"此前消息来源：{evidence_source_url}")
    return "\n".join(part for part in parts if part).strip()


def writer3_confirm_reply_markup(task: Writer3Task) -> dict[str, Any]:
    message_key = task.context_id if task.context_id is not None else task.source_item_id
    return {
        "inline_keyboard": [
            [
                {
                    "text": "确认已读",
                    "callback_data": f"w3_confirm:{message_key}",
                }
            ]
        ]
    }


def current_news_url(task: Writer3Task) -> str:
    if task.source == "odaily_reference" and task.source_item_id:
        return f"https://www.odaily.news/zh-CN/newsflash/{task.source_item_id}"
    return task.source_url or ""


def evidence_source_url(*, context: ContextResult, candidates: list[Writer3Candidate]) -> str:
    return evidence_source_url_for_context(context=context, candidates=candidates)


def evidence_source_url_for_context(*, context: ContextResult, candidates: list[Writer3Candidate]) -> str:
    by_id = {candidate.source_item_id: candidate for candidate in candidates}
    for source_id in context.evidence_source_item_ids:
        candidate = by_id.get(source_id)
        if not candidate:
            continue
        link = odaily_newsflash_url(candidate)
        if link:
            return link
    return ""


def odaily_newsflash_url(candidate: Writer3Candidate) -> str:
    if candidate.source_item_id:
        return f"https://www.odaily.news/zh-CN/newsflash/{candidate.source_item_id}"
    return candidate.source_url or ""


def analysis_to_dict(analysis: AnalysisResult) -> dict[str, Any]:
    payload = asdict(analysis)
    payload["focus_subject"] = asdict(analysis.focus_subject)
    return payload
