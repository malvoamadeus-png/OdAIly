from __future__ import annotations

import json
import os
import select
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from packages.common.config import XProcessingSettings
from packages.publisher import PushClient

from .ai_client import OpenAIResponsesClient, TextGenerationClient
from .formatter import format_brief, parse_draft_output
from .models import (
    COMPETITOR_SOURCES,
    DISCARD_TYPES,
    JUDGE_ROUTES,
    NEWS_TYPES,
    PROMPT_KEY_BY_NEWS_TYPE,
    DiscardType,
    JudgeRoute,
    NewsType,
    ProcessingStage,
    StageRunResult,
    TaskRecord,
)
from .repository import (
    PROMPT_NOTIFY_CHANNEL,
    TASK_NOTIFY_CHANNEL,
    PostgresXProcessingRepository,
    XProcessingRepository,
)
from .searcher import (
    AI_REVIEW_SCHEMA,
    CachedEmbeddingService,
    DashScopeEmbeddingClient,
    SearchCache,
    SearchDecision,
    SearchDocument,
    build_ai_review_prompt,
    parse_ai_review_output,
    top_match,
)
from .telegram import TelegramClient


class HandledStageError(RuntimeError):
    pass


JUDGE_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "x_judge_route",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route": {
                "type": "string",
                "enum": ["regular", "onchain", "funding", "discard"],
            },
            "discard_type": {
                "type": "string",
                "enum": ["none", "pure_emotion", "baseless_trading_call", "daily_chatter", "non_crypto_ai"],
            },
        },
        "required": ["route", "discard_type"],
    },
    "strict": True,
}


JUDGE_PROMPT_TEMPLATE = """你是 Odaily 快讯判断者。你的任务是对一条候选内容做第一道轻量判断。

请严格分两步判断：
1. 先判断它是否属于可丢弃内容。
2. 如果不属于可丢弃内容，再判断它应该进入 regular、onchain、funding 哪类快讯模板。

可丢弃内容只有四类：
- pure_emotion：纯情绪表达，没有可报道事实。例如“你应该购买更多的 BTC”“ETH 要起飞了”“太牛了”。
- baseless_trading_call：无逻辑的纯粹喊单、价格口号或无事实支撑的涨跌判断。例如“买入 SOL”“BTC 很快到 20 万美元”。
- daily_chatter：寒暄、日常状态、meme、节日祝福、无新闻事实的社区闲聊。
- non_crypto_ai：AI 泛科技内容，且不属于当前自动快讯流水线需要处理的 Crypto 新闻。

不要因为内容有主观语气、营销语气或表达不规范就直接丢弃；只要有明确主体、明确动作和可报道结果，就进入后续新闻流水线。

路由规则：
- regular：常规加密行业快讯，包括项目、交易所、监管、公司、产品、合作、公告、市场事件等。
- onchain：链上快讯，包括链上交易、地址、合约、资金流、清算、攻击、安全事件、链上数据变化等。
- funding：融资快讯，包括融资、投资、收购、基金、估值、投资机构参与等。
- discard：只用于上面四类可丢弃内容。

只输出 JSON，不输出解释文本。格式必须为：
{{"route":"regular|onchain|funding|discard","discard_type":"none|pure_emotion|baseless_trading_call|daily_chatter|non_crypto_ai"}}

如果 route 不是 discard，discard_type 必须是 none。

作者：{author}
来源类型：{source_kind}
内容：{content}
"""


class XProcessingWorker:
    def __init__(
        self,
        *,
        stage: ProcessingStage,
        repository: XProcessingRepository,
        settings: XProcessingSettings,
        ai_client: TextGenerationClient | None = None,
        search_embedding_service: CachedEmbeddingService | None = None,
        search_ai_client: TextGenerationClient | None = None,
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

        self.ai_client = (ai_client or self._build_ai_client()) if stage in {"judge", "write"} else ai_client
        self.search_embedding_service = search_embedding_service or (self._build_embedding_service() if stage == "search" else None)
        self.search_ai_client = (
            search_ai_client
            or ((ai_client or self._build_ai_client()) if stage == "search" and settings.openai_api_key else ai_client)
        )
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

    def _build_embedding_service(self) -> CachedEmbeddingService:
        if not self.settings.dashscope_api_key:
            raise RuntimeError("Missing DASHSCOPE_API_KEY")
        from packages.common.paths import get_paths

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
        task: TaskRecord | None = None
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

    def _record_heartbeat(self, result: StageRunResult) -> None:
        if not hasattr(self.repository, "record_worker_heartbeat"):
            return
        try:
            self.repository.record_worker_heartbeat(
                component=f"x_process_{self.stage}",
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
            print(f"[odaily] x-processing heartbeat failed stage={self.stage}: {exc}")

    def _process_task(self, task: TaskRecord) -> None:
        if self.stage == "judge":
            self._run_judge(task)
        elif self.stage == "search":
            self._run_search(task)
        elif self.stage == "write":
            self._run_write(task)
        elif self.stage == "format_publish":
            self._run_format_publish(task)
        else:
            raise ValueError(f"unknown stage: {self.stage}")

    def _run_judge(self, task: TaskRecord) -> None:
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            author=task.metadata.get("author_display_name") or task.metadata.get("author_username") or "",
            source_kind="信源" if is_competitor_task(task) else "X",
            content=task.content,
        )
        raw_output = self.ai_client.generate_text(
            model=self.settings.judge_model,
            prompt=prompt,
            text_format=JUDGE_JSON_SCHEMA,
        )
        route, discard_type = parse_judge_route(raw_output)
        if route == "discard":
            self.repository.complete_judge_discard(
                task.id,
                discard_type=discard_type,
                model=self.settings.judge_model,
                raw_output=raw_output,
            )
        else:
            self.repository.complete_judge(
                task.id,
                news_type=route,
                model=self.settings.judge_model,
                raw_output=raw_output,
            )

    def _run_search(self, task: TaskRecord) -> None:
        if self.search_embedding_service is None:
            raise RuntimeError("search embedding service is not configured")
        query = SearchDocument(
            doc_type="task",
            doc_id=str(task.id),
            title=task.title,
            content=task.content,
            source=task.source,
            source_url=task.source_url,
            task_id=task.id,
            published_at=task.published_at,
            metadata=task.metadata,
        )
        cache = getattr(self.search_embedding_service, "cache", None)
        if cache is not None and hasattr(cache, "upsert_document"):
            cache.upsert_document(query)
        query_vector = self.search_embedding_service.embed_one(cache_key=f"task:{task.id}", text=query.embedding_text)
        since = utc_since_hours(self.settings.search_window_hours)
        odaily_match = top_match(
            query_vector,
            self.search_embedding_service.embed_documents(self.repository.list_odaily_reference_documents(since=since)),
        )
        decision = self._decide_match(query=query, match=odaily_match, target_type="odaily_published")
        if decision is None:
            candidate_documents = [
                document
                for document in self.repository.list_active_candidate_documents()
                if document.task_id != task.id
            ]
            candidate_match = top_match(
                query_vector,
                self.search_embedding_service.embed_documents(candidate_documents),
            )
            decision = self._decide_match(query=query, match=candidate_match, target_type="inflight_candidate")
        if decision and decision.is_duplicate:
            result = decision.to_result()
            if decision.candidate_id is not None:
                self.repository.link_task_to_candidate(task, candidate_id=decision.candidate_id, search_result=result)
            self.repository.complete_search_duplicate(task.id, result=result)
            return
        result = {
            "is_duplicate": False,
            "duplicate_target_type": "none",
            "duplicate_target_id": None,
            "reason": "no_match",
        }
        candidate_id, is_primary = self.repository.create_candidate_for_task(task, search_result=result)
        if not is_primary:
            duplicate_result = {
                **result,
                "is_duplicate": True,
                "duplicate_target_type": "inflight_candidate",
                "duplicate_target_id": str(candidate_id),
                "reason": "same_event",
                "candidate_id": candidate_id,
            }
            self.repository.complete_search_duplicate(task.id, result=duplicate_result)
            return
        self.repository.complete_search_ready(task.id, candidate_id=candidate_id, result={**result, "candidate_id": candidate_id})

    def _decide_match(self, *, query: SearchDocument, match, target_type: str) -> SearchDecision | None:
        if match is None:
            return None
        candidate_id = match.document.candidate_id if target_type == "inflight_candidate" else None
        if match.similarity >= self.settings.search_duplicate_threshold:
            return SearchDecision(
                is_duplicate=True,
                duplicate_target_type=target_type,
                duplicate_target_id=match.document.doc_id,
                reason="same_event",
                similarity=match.similarity,
                candidate_id=candidate_id,
            )
        if match.similarity < self.settings.search_ai_review_threshold or self.search_ai_client is None:
            return None
        raw_output = self.search_ai_client.generate_text(
            model=self.settings.judge_model,
            prompt=build_ai_review_prompt(query=query, match=match),
            text_format=AI_REVIEW_SCHEMA,
        )
        payload = parse_ai_review_output(raw_output)
        is_duplicate = bool(payload.get("is_duplicate"))
        duplicate_type = str(payload.get("duplicate_target_type") or "none")
        return SearchDecision(
            is_duplicate=is_duplicate,
            duplicate_target_type=duplicate_type if is_duplicate else "none",
            duplicate_target_id=str((payload.get("duplicate_target_id") or match.document.doc_id) if is_duplicate else ""),
            reason=str(payload.get("reason") or "unrelated"),
            similarity=match.similarity,
            candidate_id=candidate_id if is_duplicate and duplicate_type == "inflight_candidate" else None,
            raw_ai_output=raw_output,
        )

    def _run_write(self, task: TaskRecord) -> None:
        pipeline = self.repository.get_pipeline(task.id)
        if pipeline.news_type is None:
            raise ValueError("missing news_type")
        template_key = PROMPT_KEY_BY_NEWS_TYPE[pipeline.news_type]
        prompt = self._get_prompt(template_key)
        input_prompt = build_writer_prompt(task=task, prompt_content=prompt.content)
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
            source_url=None if is_competitor_task(task) else task.source_url,
        )
        if not push_result.ok:
            error = push_result.error or "push failed"
            self.repository.fail_task(task.id, stage=self.stage, error=error, status="publish_failed")
            raise HandledStageError(error)
        telegram_result = self.telegram_client.send_message(
            build_telegram_notice(source=task.source, title=final.title, source_url=task.source_url)
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
    route, _discard_type = parse_judge_route(value)
    if route == "discard":
        raise ValueError("discard route does not have a news_type")
    return route


def is_competitor_task(task: TaskRecord) -> bool:
    return task.source in COMPETITOR_SOURCES


def utc_since_hours(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def build_writer_prompt(*, task: TaskRecord, prompt_content: str) -> str:
    if is_competitor_task(task):
        return (
            f"{prompt_content}\n\n"
            "【信源材料】\n"
            "来源类型：信源\n"
            f"标题：{task.title or ''}\n"
            f"正文：{task.content}\n\n"
            "禁止提及采集媒体名称，禁止提及来源平台，禁止输出解释。\n"
            "请严格输出一行标题、空一行、正文。"
        )
    author = task.metadata.get("author_display_name") or task.metadata.get("author_username") or task.title or "Odaily"
    return (
        f"{prompt_content}\n\n"
        "【待处理原文】\n"
        f"发布人：{author}\n"
        f"来源链接：{task.source_url or ''}\n"
        f"原文内容：{task.content}\n\n"
        "请严格输出一行标题、空一行、正文。不要输出解释。"
    )


def parse_judge_route(value: str) -> tuple[JudgeRoute, DiscardType]:
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
    if not isinstance(payload, dict):
        raise ValueError("judge output must be a JSON object")
    if "route" in payload:
        route = payload.get("route")
        discard_type = payload.get("discard_type")
    else:
        route = payload.get("news_type")
        discard_type = "none"
    if route not in JUDGE_ROUTES:
        raise ValueError(f"invalid route: {route}")
    if discard_type not in DISCARD_TYPES:
        raise ValueError(f"invalid discard_type: {discard_type}")
    if route == "discard" and discard_type == "none":
        raise ValueError("discard route requires a discard_type")
    if route != "discard" and discard_type != "none":
        raise ValueError("non-discard route requires discard_type none")
    return route, discard_type


SOURCE_DISPLAY_NAMES = {
    "x": "X平台",
    "blockbeats": "律动",
    "panews": "PANews",
    "jinse": "金色财经",
}


def source_display_name(source: str) -> str:
    return SOURCE_DISPLAY_NAMES.get(source, source)


def build_telegram_notice(*, source: str = "x", title: str, source_url: str | None) -> str:
    text = f"{source_display_name(source)}有新快讯：{title}"
    if source_url and source_url.strip():
        text += f"\n{source_url.strip()}"
    return text
