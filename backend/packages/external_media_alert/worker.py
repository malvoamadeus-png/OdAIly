from __future__ import annotations

import json
import os
import random
import re
import select
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from packages.common.config import ExternalMediaAlertSettings
from packages.common.paths import get_paths
from packages.x_processing.ai_client import OpenAIResponsesClient, TextGenerationClient
from packages.x_processing.models import PromptTemplateVersion, render_prompt_content
from packages.x_processing.searcher import CachedEmbeddingService, DashScopeEmbeddingClient, SearchCache, SearchDecision, SearchDocument, top_match
from packages.x_processing.telegram import TelegramClient

from .models import ALERT_PROMPT_KEY, ALERT_TASK_SOURCE, AlertStage, DomainJudgeRoute, StageRunResult
from .repository import PROMPT_NOTIFY_CHANNEL, TASK_NOTIFY_CHANNEL, ExternalMediaAlertRepository, PostgresExternalMediaAlertRepository, utc_now


class HandledStageError(RuntimeError):
    pass


DOMAIN_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "external_media_alert_domain_judge",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route": {
                "type": "string",
                "enum": ["crypto", "discard"],
            }
        },
        "required": ["route"],
    },
    "strict": True,
}


ALERT_AI_REVIEW_SCHEMA = {
    "type": "json_schema",
    "name": "external_media_alert_search_review",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_duplicate": {"type": "boolean"},
            "duplicate_target_type": {
                "type": "string",
                "enum": ["odaily_published", "external_media_alert_history", "none"],
            },
            "duplicate_target_id": {"type": "string"},
            "reason": {
                "type": "string",
                "enum": ["same_event", "same_topic_different_event", "update_of_existing_event", "unrelated"],
            },
        },
        "required": ["is_duplicate", "duplicate_target_type", "duplicate_target_id", "reason"],
    },
    "strict": True,
}


class ExternalMediaAlertWorker:
    def __init__(
        self,
        *,
        stage: AlertStage,
        repository: ExternalMediaAlertRepository,
        settings: ExternalMediaAlertSettings,
        ai_client: TextGenerationClient | None = None,
        search_embedding_service: CachedEmbeddingService | None = None,
        search_ai_client: TextGenerationClient | None = None,
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

        self.ai_client = (ai_client or self._build_ai_client()) if stage in {"domain_judge", "search"} else ai_client
        self.search_embedding_service = search_embedding_service or (self._build_embedding_service() if stage == "search" else None)
        self.search_ai_client = search_ai_client or (self.ai_client if stage == "search" else None)
        self.telegram_client = telegram_client or TelegramClient(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            message_thread_id=settings.telegram_message_thread_id,
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

    def _build_embedding_service(self) -> CachedEmbeddingService:
        if not self.settings.dashscope_api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY")
        client = DashScopeEmbeddingClient(
            api_key=self.settings.dashscope_api_key,
            base_url=str(self.settings.search_embedding_base_url),
            model=self.settings.search_embedding_model,
            timeout_seconds=self.settings.request_timeout_seconds,
            max_attempts=self.settings.retry.max_attempts,
            backoff_seconds=self.settings.retry.backoff_seconds,
        )
        return CachedEmbeddingService(client=client, cache=SearchCache(get_paths().searcher_cache_path))

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run_once(self) -> StageRunResult:
        task = None
        try:
            task = self.repository.claim_task(self.stage, worker_id=self.worker_id)
            if task is None:
                result = StageRunResult(0, self.stage, 0, 0, "no task")
                self._record_heartbeat(result)
                return result
            self._process_task(task)
            result = StageRunResult(0, self.stage, 1, 0, f"processed task {task.id}")
        except HandledStageError as exc:
            task_label = str(task.id) if task is not None else "unknown"
            result = StageRunResult(1, self.stage, 0, 1, f"failed task {task_label}: {exc}")
        except Exception as exc:
            if task is not None:
                self.repository.fail_task(task.id, stage=self.stage, error=str(exc))
                result = StageRunResult(1, self.stage, 0, 1, f"failed task {task.id}: {exc}")
            else:
                result = StageRunResult(1, self.stage, 0, 1, f"worker failed before claim: {exc}")
        self._record_heartbeat(result)
        return result

    def run_forever(self) -> None:
        notify_thread = self._start_notify_listener()
        print(f"[odaily] external media alert worker started. stage={self.stage} worker_id={self.worker_id}")
        try:
            while not self._stop_event.is_set():
                result = self.run_once()
                if result.processed or result.failed:
                    print(f"[odaily] external media alert stage={self.stage} {result.message}")
                    if result.processed:
                        continue
                self._wake_event.wait(self.idle_sleep_seconds + random.uniform(0, 0.5))
                self._wake_event.clear()
        finally:
            self.stop()
            if notify_thread:
                notify_thread.join(timeout=2)

    def _record_heartbeat(self, result: StageRunResult) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self.repository.record_worker_heartbeat(
                component=f"external_media_alert_{self.stage}",
                worker_id=self.worker_id,
                status="ok" if not result.failed else "failed",
                success=not bool(result.failed),
                error=result.message if result.failed else None,
                metadata={
                    "stage": self.stage,
                    "processed": result.processed,
                    "failed": result.failed,
                    "message": result.message,
                },
            )
        except Exception as exc:
            print(f"[odaily] external media alert heartbeat failed stage={self.stage}: {exc}")

    def _process_task(self, task) -> None:
        if self.stage == "domain_judge":
            self._run_domain_judge(task)
            return
        if self.stage == "search":
            self._run_search(task)
            return
        if self.stage == "notify":
            self._run_notify(task)
            return
        raise ValueError(f"unknown stage: {self.stage}")

    def _run_domain_judge(self, task) -> None:
        prompt = self._get_prompt(ALERT_PROMPT_KEY)
        raw_output = self.ai_client.generate_text(
            model=self.settings.domain_judge_model,
            prompt=build_domain_prompt(task=task, prompt=prompt),
            text_format=DOMAIN_JSON_SCHEMA,
        )
        route = parse_domain_route(raw_output)
        if route == "discard":
            self.repository.complete_domain_discard(
                task.id,
                prompt=prompt,
                model=self.settings.domain_judge_model,
                raw_output=raw_output,
            )
            return
        self.repository.complete_domain(
            task.id,
            route="crypto",
            prompt=prompt,
            model=self.settings.domain_judge_model,
            raw_output=raw_output,
        )

    def _run_search(self, task) -> None:
        if self.search_embedding_service is None:
            raise RuntimeError("search embedding service is not configured")
        excerpt = str(task.metadata.get("excerpt") or task.content or "").strip()
        query = SearchDocument(
            doc_type="external_media_alert",
            doc_id=task.source_item_id,
            title=task.title,
            content=excerpt or (task.title or ""),
            source=task.source,
            source_url=task.source_url,
            task_id=task.id,
            published_at=task.published_at,
            metadata=task.metadata,
        )
        cache = getattr(self.search_embedding_service, "cache", None)
        if cache is not None and hasattr(cache, "upsert_document"):
            cache.upsert_document(query)
        recent_since = utc_since_hours(self.settings.search_window_hours)
        odaily_documents = self.repository.list_odaily_reference_documents(since=recent_since)
        history_documents_all = self.repository.list_notified_alert_documents(since=None)
        exact = exact_duplicate_decision(query=query, documents=odaily_documents, target_type="odaily_published")
        if exact is None:
            exact = exact_duplicate_decision(
                query=query,
                documents=history_documents_all,
                target_type="external_media_alert_history",
            )
        if exact is not None:
            self.repository.complete_search_duplicate(task.id, result=exact.to_result())
            return

        history_documents_recent = self.repository.list_notified_alert_documents(since=recent_since)
        query_vector = self.search_embedding_service.embed_one(
            cache_key=f"external_media_alert_task:{task.id}",
            text=query.embedding_text,
        )
        match = top_match(
            query_vector,
            self.search_embedding_service.embed_documents(odaily_documents + history_documents_recent),
        )
        decision = self._semantic_duplicate_decision(query=query, match=match)
        if decision is not None and decision.is_duplicate:
            self.repository.complete_search_duplicate(task.id, result=decision.to_result())
            return
        self.repository.complete_search_ready(
            task.id,
            result={
                "is_duplicate": False,
                "duplicate_target_type": "none",
                "duplicate_target_id": None,
                "reason": "no_match",
            },
        )

    def _semantic_duplicate_decision(self, *, query: SearchDocument, match) -> SearchDecision | None:
        if match is None:
            return None
        target_type = "odaily_published" if match.document.source == "odaily" else "external_media_alert_history"
        if match.similarity >= self.settings.search_duplicate_threshold:
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=match.document.doc_id,
                reason="same_event",
                similarity=match.similarity,
            )
        if match.similarity < self.settings.search_ai_review_threshold or self.search_ai_client is None:
            return None
        raw_output = self.search_ai_client.generate_text(
            model=self.settings.domain_judge_model,
            prompt=build_alert_ai_review_prompt(query=query, match=match, target_type=target_type),
            text_format=ALERT_AI_REVIEW_SCHEMA,
        )
        payload = parse_alert_ai_review_output(raw_output)
        is_duplicate = bool(payload.get("is_duplicate"))
        duplicate_target_type = str(payload.get("duplicate_target_type") or "none")
        duplicate_target_id = str(payload.get("duplicate_target_id") or match.document.doc_id)
        return SearchDecision(
            is_duplicate=is_duplicate,
            duplicate_target_type=duplicate_target_type if is_duplicate else "none",
            duplicate_target_id=duplicate_target_id if is_duplicate else None,
            reason=str(payload.get("reason") or "unrelated"),
            similarity=match.similarity,
            raw_ai_output=raw_output,
        )

    def _run_notify(self, task) -> None:
        notice = build_alert_notice(
            site_display_name=str(task.metadata.get("site_display_name") or "外媒"),
            title=task.title or task.source_item_id,
            source_url=task.source_url,
        )
        result = self.telegram_client.send_message(notice)
        if not result.ok:
            error = result.error or "telegram notify failed"
            self.repository.fail_task(task.id, stage=self.stage, error=error, status="notify_failed")
            raise HandledStageError(error)
        self.repository.complete_notify(task.id, telegram_result=result.model_dump(mode="json"))

    def _get_prompt(self, template_key: str):
        prompt = self._prompt_cache.get(template_key)
        if prompt is None:
            prompt = self.repository.get_active_prompt(template_key)
            self._prompt_cache[template_key] = prompt
        return prompt

    def _start_notify_listener(self) -> threading.Thread | None:
        if not isinstance(self.repository, PostgresExternalMediaAlertRepository):
            return None
        thread = threading.Thread(
            target=self._listen_for_changes,
            name=f"external-media-alert-{self.stage}-listener",
            daemon=True,
        )
        thread.start()
        return thread

    def _listen_for_changes(self) -> None:
        repository = self.repository
        if not isinstance(repository, PostgresExternalMediaAlertRepository):
            return
        channels = [TASK_NOTIFY_CHANNEL]
        if self.stage == "domain_judge":
            channels.append(PROMPT_NOTIFY_CHANNEL)
        while not self._stop_event.is_set():
            try:
                with repository._connect() as conn:
                    conn.autocommit = True
                    for channel in channels:
                        conn.execute(f"LISTEN {channel}")
                    self._wake_event.set()
                    print(f"[odaily] external media alert stage={self.stage} listening for {','.join(channels)}")
                    while not self._stop_event.is_set():
                        if select.select([conn], [], [], self.notify_wait_seconds)[0]:
                            for notify in conn.notifies(timeout=0, stop_after=100):
                                if getattr(notify, "channel", "") == PROMPT_NOTIFY_CHANNEL:
                                    self._prompt_cache.clear()
                                self._wake_event.set()
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                print(f"[odaily] external media alert listener reconnecting stage={self.stage}: {exc}")
                self._wake_event.set()
                self._stop_event.wait(self.notify_wait_seconds)


def utc_since_hours(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def build_domain_prompt(*, task, prompt: PromptTemplateVersion) -> str:
    excerpt = str(task.metadata.get("excerpt") or task.content or "").strip()
    return (
        f"{render_prompt_content(prompt)}\n\n"
        "【待判断标题提醒】\n"
        f"来源媒体：{task.metadata.get('site_display_name') or '外媒'}\n"
        f"站点标识：{task.metadata.get('site_key') or ''}\n"
        f"标题：{task.title or ''}\n"
        f"摘要：{excerpt}\n"
        f"原链接：{task.source_url or ''}\n"
    )


def parse_domain_route(value: str) -> DomainJudgeRoute:
    text = strip_code_fence(value)
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("domain judge output must be a JSON object")
    route = payload.get("route")
    if route not in {"crypto", "discard"}:
        raise ValueError(f"invalid domain route: {route}")
    return route


def exact_duplicate_decision(
    *,
    query: SearchDocument,
    documents: list[SearchDocument],
    target_type: str,
) -> SearchDecision | None:
    query_url = normalize_compare_url(query.source_url)
    query_title = normalize_title_key(query.title)
    for document in documents:
        if query_url and normalize_compare_url(document.source_url) == query_url:
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=document.doc_id,
                reason="same_event",
                similarity=1.0,
            )
        if query.doc_id and document.doc_id == query.doc_id:
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=document.doc_id,
                reason="same_event",
                similarity=1.0,
            )
        if query_title and query_title == normalize_title_key(document.title):
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=document.doc_id,
                reason="same_event",
                similarity=1.0,
            )
    return None


def normalize_compare_url(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip().lower()
    text = re.sub(r"[?#].*$", "", text)
    return text.rstrip("/")


def normalize_title_key(value: str | None) -> str:
    if not value:
        return ""
    lowered = value.strip().lower()
    normalized = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", lowered)
    return normalized or re.sub(r"\s+", " ", lowered)


def build_alert_ai_review_prompt(*, query: SearchDocument, match, target_type: str) -> str:
    return (
        "你是 Odaily 外媒标题提醒搜索者。判断两条标题提醒是否是同一个新闻事件。\n"
        "只输出 JSON，不输出解释。\n\n"
        "【新标题提醒】\n"
        f"标题：{query.title or ''}\n"
        f"摘要：{query.content}\n"
        f"原链接：{query.source_url or ''}\n\n"
        "【候选材料】\n"
        f"类型：{target_type}\n"
        f"ID：{match.document.doc_id}\n"
        f"标题：{match.document.title or ''}\n"
        f"摘要：{match.document.content}\n"
        f"相似度：{match.similarity:.4f}\n\n"
        'JSON格式：{"is_duplicate":true|false,"duplicate_target_type":"odaily_published|external_media_alert_history|none",'
        '"duplicate_target_id":"string","reason":"same_event|same_topic_different_event|update_of_existing_event|unrelated"}'
    )


def parse_alert_ai_review_output(value: str) -> dict[str, Any]:
    payload = json.loads(strip_code_fence(value))
    if not isinstance(payload, dict):
        raise ValueError("external media alert search AI output must be a JSON object")
    return payload


def build_alert_notice(*, site_display_name: str, title: str, source_url: str | None) -> str:
    text = f"外媒标题提醒：{site_display_name}｜{title}"
    if source_url and source_url.strip():
        text += f"\n{source_url.strip()}"
    return text


def strip_code_fence(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
