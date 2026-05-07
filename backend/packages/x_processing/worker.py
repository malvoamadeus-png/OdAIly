from __future__ import annotations

import json
import os
import select
import threading
import time
from typing import Any
from uuid import uuid4

from packages.common.config import XProcessingSettings
from packages.publisher import PushClient

from .ai_client import OpenAIResponsesClient, TextGenerationClient
from .formatter import format_brief, parse_draft_output
from .models import NEWS_TYPES, PROMPT_KEY_BY_NEWS_TYPE, NewsType, ProcessingStage, StageRunResult, TaskRecord
from .repository import (
    PROMPT_NOTIFY_CHANNEL,
    TASK_NOTIFY_CHANNEL,
    PostgresXProcessingRepository,
    XProcessingRepository,
)
from .telegram import TelegramClient


class HandledStageError(RuntimeError):
    pass


JUDGE_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "x_news_type",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "news_type": {
                "type": "string",
                "enum": ["regular", "onchain", "funding"],
            }
        },
        "required": ["news_type"],
    },
    "strict": True,
}


class XProcessingWorker:
    def __init__(
        self,
        *,
        stage: ProcessingStage,
        repository: XProcessingRepository,
        settings: XProcessingSettings,
        ai_client: TextGenerationClient | None = None,
        push_client: PushClient | None = None,
        telegram_client: TelegramClient | None = None,
        worker_id: str | None = None,
        idle_sleep_seconds: float = 5.0,
        notify_wait_seconds: float = 5.0,
    ) -> None:
        self.stage = stage
        self.repository = repository
        self.settings = settings
        self.worker_id = worker_id or f"{stage}-{os.getpid()}-{uuid4().hex[:8]}"
        self.idle_sleep_seconds = idle_sleep_seconds
        self.notify_wait_seconds = notify_wait_seconds
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._prompt_cache: dict[str, Any] = {}

        self.ai_client = ai_client or self._build_ai_client() if stage in {"judge", "write"} else ai_client
        self.push_client = push_client or PushClient(
            endpoint=str(settings.push_endpoint),
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self.telegram_client = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
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

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run_once(self) -> StageRunResult:
        task = self.repository.claim_task(self.stage, worker_id=self.worker_id)
        if task is None:
            return StageRunResult(0, self.stage, 0, 0, "no task")
        try:
            self._process_task(task)
            return StageRunResult(0, self.stage, 1, 0, f"processed task {task.id}")
        except HandledStageError as exc:
            return StageRunResult(1, self.stage, 0, 1, f"failed task {task.id}: {exc}")
        except Exception as exc:
            self.repository.fail_task(task.id, stage=self.stage, error=str(exc))
            return StageRunResult(1, self.stage, 0, 1, f"failed task {task.id}: {exc}")

    def run_forever(self) -> None:
        notify_thread = self._start_notify_listener()
        print(f"[odaily] x-processing worker started. stage={self.stage} worker_id={self.worker_id}")
        try:
            while not self._stop_event.is_set():
                result = self.run_once()
                if result.processed:
                    print(f"[odaily] x-processing stage={self.stage} {result.message}")
                    continue
                if result.failed:
                    print(f"[odaily] x-processing stage={self.stage} {result.message}")
                self._wake_event.wait(self.idle_sleep_seconds)
                self._wake_event.clear()
        finally:
            self.stop()
            if notify_thread:
                notify_thread.join(timeout=2)

    def _process_task(self, task: TaskRecord) -> None:
        if self.stage == "judge":
            self._run_judge(task)
        elif self.stage == "search":
            self.repository.complete_search(task.id)
        elif self.stage == "write":
            self._run_write(task)
        elif self.stage == "format_publish":
            self._run_format_publish(task)
        else:
            raise ValueError(f"unknown stage: {self.stage}")

    def _run_judge(self, task: TaskRecord) -> None:
        prompt = (
            "你是 Odaily 快讯分类器。只判断这条 X 内容应该进入哪个快讯模板。\n"
            "只能输出 JSON：{\"news_type\":\"regular|onchain|funding\"}。\n\n"
            f"作者：{task.metadata.get('author_display_name') or task.metadata.get('author_username') or ''}\n"
            f"内容：{task.content}\n"
        )
        raw_output = self.ai_client.generate_text(
            model=self.settings.judge_model,
            prompt=prompt,
            text_format=JUDGE_JSON_SCHEMA,
        )
        news_type = parse_news_type(raw_output)
        self.repository.complete_judge(
            task.id,
            news_type=news_type,
            model=self.settings.judge_model,
            raw_output=raw_output,
        )

    def _run_write(self, task: TaskRecord) -> None:
        pipeline = self.repository.get_pipeline(task.id)
        if pipeline.news_type is None:
            raise ValueError("missing news_type")
        template_key = PROMPT_KEY_BY_NEWS_TYPE[pipeline.news_type]
        prompt = self._get_prompt(template_key)
        author = task.metadata.get("author_display_name") or task.metadata.get("author_username") or task.title or "Odaily"
        input_prompt = (
            f"{prompt.content}\n\n"
            "【待处理原文】\n"
            f"发布人：{author}\n"
            f"来源链接：{task.source_url or ''}\n"
            f"原文内容：{task.content}\n\n"
            "请严格输出一行标题、空一行、正文。不要输出解释。"
        )
        raw_output = self.ai_client.generate_text(
            model=self.settings.writer_model,
            prompt=input_prompt,
        )
        draft = parse_draft_output(raw_output)
        self.repository.complete_write(
            task.id,
            prompt=prompt,
            model=self.settings.writer_model,
            draft_title=draft.title,
            draft_content=draft.content,
            raw_output=raw_output,
        )

    def _run_format_publish(self, task: TaskRecord) -> None:
        pipeline = self.repository.get_pipeline(task.id)
        if not pipeline.draft_title or not pipeline.draft_content:
            self.repository.fail_task(task.id, stage=self.stage, error="missing draft title or content", status="format_failed")
            raise HandledStageError("missing draft title or content")
        try:
            final = format_brief(parse_draft_output(f"{pipeline.draft_title}\n\n{pipeline.draft_content}"))
        except Exception as exc:
            self.repository.fail_task(task.id, stage=self.stage, error=str(exc), status="format_failed")
            raise HandledStageError(str(exc)) from exc
        push_result = self.push_client.push(
            title=final.title,
            content=final.content,
            dry_run=self.settings.dry_run,
        )
        if not push_result.ok:
            error = push_result.error or "push failed"
            self.repository.fail_task(task.id, stage=self.stage, error=error, status="publish_failed")
            raise HandledStageError(error)
        telegram_result = self.telegram_client.send_message(
            build_telegram_notice(title=final.title, source_url=task.source_url)
        )
        self.repository.complete_format_publish(
            task.id,
            final_title=final.title,
            final_content=final.content,
            push_result=push_result.model_dump(mode="json"),
            telegram_result=telegram_result.model_dump(mode="json"),
        )

    def _get_prompt(self, template_key: str):
        prompt = self._prompt_cache.get(template_key)
        if prompt is None:
            prompt = self.repository.get_active_prompt(template_key)
            self._prompt_cache[template_key] = prompt
        return prompt

    def _start_notify_listener(self) -> threading.Thread | None:
        if not isinstance(self.repository, PostgresXProcessingRepository):
            return None
        thread = threading.Thread(target=self._listen_for_changes, name=f"x-processing-{self.stage}-listener", daemon=True)
        thread.start()
        return thread

    def _listen_for_changes(self) -> None:
        repository = self.repository
        if not isinstance(repository, PostgresXProcessingRepository):
            return
        channels = (TASK_NOTIFY_CHANNEL, PROMPT_NOTIFY_CHANNEL)
        while not self._stop_event.is_set():
            try:
                with repository._connect() as conn:
                    conn.autocommit = True
                    for channel in channels:
                        conn.execute(f"LISTEN {channel}")
                    self._wake_event.set()
                    print(f"[odaily] x-processing stage={self.stage} listening for {','.join(channels)}")
                    while not self._stop_event.is_set():
                        if select.select([conn], [], [], self.notify_wait_seconds)[0]:
                            for notify in conn.notifies(timeout=0, stop_after=100):
                                if getattr(notify, "channel", "") == PROMPT_NOTIFY_CHANNEL:
                                    self._prompt_cache.clear()
                                self._wake_event.set()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                print(f"[odaily] x-processing listener reconnecting stage={self.stage}: {exc}")
                self._wake_event.set()
                self._stop_event.wait(self.notify_wait_seconds)


def parse_news_type(value: str) -> NewsType:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("judge output must be valid JSON") from exc
    news_type = payload.get("news_type") if isinstance(payload, dict) else None
    if news_type not in NEWS_TYPES:
        raise ValueError(f"invalid news_type: {news_type}")
    return news_type


def build_telegram_notice(*, title: str, source_url: str | None) -> str:
    text = f"有新快讯：{title}"
    if source_url and source_url.strip():
        text += f"\n原文链接：{source_url.strip()}"
    return text
