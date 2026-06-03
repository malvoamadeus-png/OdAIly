from __future__ import annotations

import json
import os
import re
import select
import threading
import time
from datetime import UTC, datetime, time as dt_time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from packages.common.config import XProcessingSettings
from packages.common.freshness import evaluate_source_freshness, freshness_error
from packages.common.heartbeat import HeartbeatThrottle
from packages.common.paths import get_paths
from packages.publisher import PushClient

from .ai_client import OpenAIResponsesClient, TextGenerationClient
from .formatter import format_brief, parse_draft_output
from .models import (
    ACTIVE_CANDIDATE_TTL,
    AI_SOURCE,
    COMPETITOR_SOURCES,
    DISCARD_TYPES,
    JUDGE_ROUTES,
    MAINSTREAM_MEDIA_SOURCE,
    NEWS_TYPES,
    NON_MAINSTREAM_MEDIA_SOURCE,
    PUBLISHER_CATEGORIES,
    PROMPT_KEY_BY_NEWS_TYPE,
    DiscardType,
    JudgeRoute,
    NewsType,
    PipelineRecord,
    ProcessingStage,
    PromptTemplateVersion,
    StageRunResult,
    STAGE_SPECS,
    TaskRecord,
    render_prompt_content,
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
    exact_duplicate_decision,
    parse_ai_review_output,
    top_match,
)
from .telegram import TelegramClient


class HandledStageError(RuntimeError):
    pass


X_JUDGE_JSON_SCHEMA = {
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


NON_MAINSTREAM_JUDGE_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "non_mainstream_media_judge_route",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route": {
                "type": "string",
                "enum": ["non_mainstream_media", "discard"],
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


X_JUDGE_PROMPT_TEMPLATE = """你是 Odaily 快讯判断者。你的任务是对一条候选内容做第一道轻量判断。

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

NON_MAINSTREAM_JUDGE_PROMPT_TEMPLATE = """你是 Odaily 快讯判断者。你的任务是判断一条外媒原文，是否值得进入后续快讯写作。

请严格分两步判断：
1. 先判断它是否属于可丢弃内容。
2. 如果不属于可丢弃内容，统一输出 non_mainstream_media。

可丢弃内容只有四类：
- pure_emotion：纯情绪表达，没有可报道事实。
- baseless_trading_call：无逻辑的纯粹喊单、价格口号或无事实支撑的涨跌判断。
- daily_chatter：寒暄、日常状态、meme、节日祝福、无新闻事实的社区闲聊。
- non_crypto_ai：AI 泛科技内容，且不属于当前自动快讯流水线需要处理的 Crypto 新闻。

不要因为内容有主观语气、营销语气或表达不规范就直接丢弃；只要有明确主体、明确动作和可报道结果，就保留。

只允许两种 route：
- non_mainstream_media：保留并进入统一外媒写作模板。
- discard：只用于上面四类可丢弃内容。

只输出 JSON，不输出解释文本。格式必须为：
{{"route":"non_mainstream_media|discard","discard_type":"none|pure_emotion|baseless_trading_call|daily_chatter|non_crypto_ai"}}

如果 route 不是 discard，discard_type 必须是 none。

来源媒体：{site_display_name}
作者：{author_names}
标题：{title}
来源链接：{source_url}
正文：{content}
"""


AI_SOURCE_JUDGE_PROMPT_TEMPLATE = """你是 Odaily 快讯判断者。你的任务是判断一条AI信源原文，是否值得进入后续快讯写作。

请严格分两步判断：
1. 先判断它是否属于可丢弃内容。
2. 如果不属于可丢弃内容，统一输出 ai_source。

可丢弃内容只有四类：
- pure_emotion：纯情绪表达，没有可报道事实。
- baseless_trading_call：无逻辑的纯粹喊单、价格口号或无事实支撑的涨跌判断。
- daily_chatter：寒暄、日常状态、meme、节日祝福、无新闻事实的社区闲聊。
- non_crypto_ai：明显无新闻事实、纯闲聊或营销化的 AI 泛科技内容；不要仅因内容不属于 Crypto 就丢弃 AI信源。

不要因为内容有主观语气、营销语气或表达不规范就直接丢弃；只要有明确主体、明确动作和可报道结果，就保留。

只允许两种 route：
- ai_source：保留并进入AI信源写作模板。
- discard：只用于上面四类可丢弃内容。

只输出 JSON，不输出解释文本。格式必须为：
{{"route":"ai_source|discard","discard_type":"none|pure_emotion|baseless_trading_call|daily_chatter|non_crypto_ai"}}

如果 route 不是 discard，discard_type 必须是 none。

来源媒体：{site_display_name}
作者：{author_names}
标题：{title}
来源链接：{source_url}
正文：{content}
"""


AI_TOPIC_PATTERN = re.compile(
    r"人工智能|生成式\s*AI|AIGC|大模型|机器学习|深度学习|智能体|"
    r"OpenAI|ChatGPT|DeepSeek|Claude|Gemini|Sora|"
    r"(?<![A-Za-z0-9.])AI(?![A-Za-z0-9])",
    re.IGNORECASE,
)

CRYPTO_CONTEXT_PATTERN = re.compile(
    r"Web3|Crypto|DeFi|NFT|DAO|DApp|"
    r"加密|区块链|链上|公链|主网|测试网|智能合约|"
    r"比特币|以太坊|稳定币|代币|空投|质押|挖矿|矿工|矿企|"
    r"交易所|钱包|预言机|跨链|Layer\s*2|L2|"
    r"BTC|ETH|USDT|USDC|SOL|BNB|"
    r"Bitcoin|Ethereum|Solana|Binance|Coinbase|OKX|Tether|Circle|"
    r"币安|欧易|CZ|Vitalik|Worldcoin|"
    r"Polymarket|Base|Arbitrum|Optimism|Polygon|Avalanche|"
    r"Uniswap|Aave|Curve|Chainlink|Bittensor",
    re.IGNORECASE,
)

DETERMINISTIC_JUDGE_MODEL = "deterministic-precheck"
PUBLISHER_CATEGORY_ALLOWLIST = {
    "policy_regulation",
    "people_view",
    "major_project_progress",
    "funding",
}
PUBLISHER_CHANNEL_BY_SOURCE = {
    NON_MAINSTREAM_MEDIA_SOURCE: "external_media",
    AI_SOURCE: "external_media",
    "x": "x",
    "blockbeats": "competitor",
    "panews": "competitor",
    "jinse": "competitor",
}
PUBLISHER_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "publisher_category",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "category": {
                "type": "string",
                "enum": [
                    "policy_regulation",
                    "people_view",
                    "major_project_progress",
                    "funding",
                    "other",
                ],
            },
            "reason": {"type": "string"},
        },
        "required": ["category", "reason"],
    },
    "strict": True,
}


AI_SOURCE_JUDGE_JSON_SCHEMA = {
    "type": "json_schema",
    "name": "ai_source_judge_route",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "route": {
                "type": "string",
                "enum": ["ai_source", "discard"],
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
PUBLISHER_PROMPT_TEMPLATE = """你是 Odaily 的自动发布分类器。

请只根据这条稿件内容判断，它是否属于以下固定分类之一：
- policy_regulation：政策法规
- people_view：人物观点
- major_project_progress：项目重大进展
- funding：融资
- other：不属于以上四类，或信息不够明确

要求：
1. 只输出 JSON，不要输出解释性正文。
2. 如果不确定，必须输出 other。
3. reason 只写一句简短中文原因，便于后台记录。

原始来源：{source}
原始标题：{source_title}
原始正文：{source_content}

定稿标题：{final_title}
定稿正文：{final_content}
"""
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
        self._search_cache_store = SearchCache(_search_cache_path_for_repository(self.repository))

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
            message_thread_id=settings.telegram_message_thread_id,
            timeout_seconds=settings.telegram_timeout_seconds,
            max_attempts=settings.retry.max_attempts,
            backoff_seconds=settings.retry.backoff_seconds,
        )
        self._heartbeat = HeartbeatThrottle(
            component=f"x_process_{self.stage}",
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

    def _get_ai_client(self) -> TextGenerationClient:
        if self.ai_client is None:
            self.ai_client = self._build_ai_client()
        return self.ai_client

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
        return CachedEmbeddingService(client=client, cache=self._search_cache_store)

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
            if self._expire_task_if_stale(task):
                result = StageRunResult(0, self.stage, 1, 0, f"expired task {task.id}")
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
                self._release_local_candidate_for_task(task.id, release_reason=STAGE_SPECS[self.stage].failure_status)
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
            self._heartbeat.send(
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
        elif self.stage == "publish":
            self._run_publish(task)
        else:
            raise ValueError(f"unknown stage: {self.stage}")

    def _expire_task_if_stale(self, task: TaskRecord) -> bool:
        check = evaluate_source_freshness(
            task.published_at,
            window_seconds=self.settings.processing_freshness_window_seconds,
        )
        if check.is_fresh:
            return False
        error = freshness_error(check)
        self.repository.fail_task(task.id, stage=self.stage, error=error, status="expired")
        self._release_local_candidate_for_task(task.id, release_reason="expired")
        delay = int(check.delay_seconds) if check.delay_seconds is not None else "-"
        published = check.published_at.isoformat() if check.published_at else "-"
        print(
            "[odaily] x-processing freshness expired "
            f"stage={self.stage} task_id={task.id} source={task.source} source_item_id={task.source_item_id} "
            f"published_at={published} delay_seconds={delay} "
            f"window_seconds={check.window_seconds} action=expire_task"
        )
        return True

    def _run_judge(self, task: TaskRecord) -> None:
        if is_ai_source_task(task):
            self.repository.complete_judge(
                task.id,
                news_type=AI_SOURCE,
                model=DETERMINISTIC_JUDGE_MODEL,
                raw_output=json.dumps(
                    {
                        "route": AI_SOURCE,
                        "discard_type": "none",
                        "auto_pass": "ai_source",
                    },
                    ensure_ascii=False,
                ),
            )
            return
        deterministic_discard_type = deterministic_judge_discard_type(task)
        if deterministic_discard_type is not None:
            self._release_local_candidate_for_task(task.id, release_reason="discarded")
            raw_output = json.dumps(
                {
                    "route": "discard",
                    "discard_type": deterministic_discard_type,
                },
                ensure_ascii=False,
            )
            self.repository.complete_judge_discard(
                task.id,
                discard_type=deterministic_discard_type,
                model=DETERMINISTIC_JUDGE_MODEL,
                raw_output=raw_output,
            )
            return
        prompt = X_JUDGE_PROMPT_TEMPLATE.format(
            author=task.metadata.get("author_display_name") or task.metadata.get("author_username") or "",
            source_kind="信源" if is_competitor_task(task) else "X",
            content=task.content,
        )
        schema = X_JUDGE_JSON_SCHEMA
        if is_non_mainstream_media_task(task) or is_ai_source_task(task):
            is_ai_source = is_ai_source_task(task)
            prompt_template = AI_SOURCE_JUDGE_PROMPT_TEMPLATE if is_ai_source else NON_MAINSTREAM_JUDGE_PROMPT_TEMPLATE
            prompt = prompt_template.format(
                site_display_name=task.metadata.get("site_display_name") or ("AI信源" if is_ai_source else "外媒"),
                author_names="、".join(task.metadata.get("author_names") or []),
                title=task.title or "",
                source_url=task.source_url or "",
                content=task.content,
            )
            schema = AI_SOURCE_JUDGE_JSON_SCHEMA if is_ai_source else NON_MAINSTREAM_JUDGE_JSON_SCHEMA
        try:
            raw_output = self._get_ai_client().generate_text(
                model=self.settings.judge_model,
                prompt=prompt,
                text_format=schema,
            )
            route, discard_type = parse_judge_route(raw_output)
        except Exception as exc:
            fallback_route = competitor_judge_fallback_route(task) if is_competitor_task(task) else None
            if fallback_route is None:
                raise
            route = fallback_route
            discard_type = "none"
            raw_output = json.dumps(
                {
                    "route": route,
                    "discard_type": discard_type,
                    "fallback": "competitor_judge_ai_failed",
                    "error": str(exc)[:500],
                },
                ensure_ascii=False,
            )
            print(
                "[odaily] x-processing judge fallback "
                f"task_id={task.id} source={task.source} source_item_id={task.source_item_id} "
                f"route={route} error={exc}"
            )
        if route == "discard":
            self._release_local_candidate_for_task(task.id, release_reason="discarded")
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
        since = utc_since_hours(self.settings.search_window_hours)
        odaily_documents = self._load_odaily_reference_documents(since=since)
        decision = exact_duplicate_decision(
            query=query,
            documents=odaily_documents,
            target_type="odaily_published",
        )
        query_vector: list[float] | None = None
        if decision is None:
            candidate_documents = self._load_active_candidate_documents(exclude_task_id=task.id)
            decision = exact_duplicate_decision(
                query=query,
                documents=candidate_documents,
                target_type="inflight_candidate",
            )
        if decision is None:
            query_vector = self.search_embedding_service.embed_one(cache_key=f"task:{task.id}", text=query.embedding_text)
            odaily_match = top_match(
                query_vector,
                self.search_embedding_service.embed_documents(odaily_documents),
            )
            decision = self._decide_match(query=query, match=odaily_match, target_type="odaily_published")
        if decision is None:
            candidate_documents = self._load_active_candidate_documents(exclude_task_id=task.id)
            if query_vector is None:
                query_vector = self.search_embedding_service.embed_one(cache_key=f"task:{task.id}", text=query.embedding_text)
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
        if is_primary:
            self._mirror_active_candidate(task=task, candidate_id=candidate_id)
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
        template_key = None
        if is_mainstream_media_task(task):
            template_key = "mainstream_media_writer"
        elif pipeline.news_type is not None:
            template_key = PROMPT_KEY_BY_NEWS_TYPE[pipeline.news_type]
        if template_key is None:
            raise ValueError("missing news_type")
        prompt = self._get_prompt(template_key)
        input_prompt = build_writer_prompt(task=task, prompt=prompt)
        raw_output = self._get_ai_client().generate_text(
            model=self.settings.writer_model,
            prompt=input_prompt,
            reasoning_effort=self.settings.writer_reasoning_effort,
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
            self._release_local_candidate_for_task(task.id, release_reason="format_failed")
            raise HandledStageError("missing draft title or content")
        try:
            final = format_brief(parse_draft_output(f"{pipeline.draft_title}\n\n{pipeline.draft_content}"))
        except Exception as exc:
            self.repository.fail_task(task.id, stage=self.stage, error=str(exc), status="format_failed")
            self._release_local_candidate_for_task(task.id, release_reason="format_failed")
            raise HandledStageError(str(exc)) from exc
        self.repository.complete_format_publish(
            task.id,
            final_title=final.title,
            final_content=final.content,
        )

    def _run_publish(self, task: TaskRecord) -> None:
        pipeline = self.repository.get_pipeline(task.id)
        decided_at = datetime.now(UTC)
        publisher_channel = resolve_publisher_channel(task)
        if not pipeline.final_title or not pipeline.final_content:
            error = "missing final title or content"
            self.repository.complete_publish(
                task.id,
                publisher_channel=publisher_channel,
                publisher_model=None,
                publisher_category=None,
                publisher_decision="failed",
                publisher_reason_code="format_missing",
                publisher_output={"source": task.source},
                push_result={},
                telegram_result={},
                decided_at=decided_at,
                status="publisher_failed",
                last_error=error,
            )
            self._release_local_candidate_for_task(task.id, release_reason="publisher_failed")
            raise HandledStageError(error)

        publisher_settings = self.repository.get_publisher_settings()
        publisher_channels = {item.channel_key: item for item in self.repository.list_publisher_channels()}
        context_output = {
            "source": task.source,
            "publisher_channel": publisher_channel,
            "timezone": publisher_settings.timezone,
            "window_start_local": normalize_local_time_text(publisher_settings.window_start_local),
            "window_end_local": normalize_local_time_text(publisher_settings.window_end_local),
        }
        now_local = decided_at.astimezone(resolve_publisher_timezone(publisher_settings.timezone))
        context_output["decision_local_time"] = now_local.strftime("%Y-%m-%d %H:%M")

        if not publisher_settings.enabled:
            self._complete_manual_review_publish(
                task=task,
                pipeline=pipeline,
                publisher_channel=publisher_channel,
                reason_code="publisher_disabled",
                output=context_output,
                decided_at=decided_at,
            )
            return

        if publisher_channel is None:
            self._complete_manual_review_publish(
                task=task,
                pipeline=pipeline,
                publisher_channel=None,
                reason_code="source_not_eligible",
                output=context_output,
                decided_at=decided_at,
            )
            return

        channel_config = publisher_channels.get(publisher_channel)
        if channel_config is None or not channel_config.enabled:
            self._complete_manual_review_publish(
                task=task,
                pipeline=pipeline,
                publisher_channel=publisher_channel,
                reason_code="channel_disabled",
                output={**context_output, "channel_enabled": False},
                decided_at=decided_at,
            )
            return

        if not is_within_publish_window(
            now_local=now_local,
            window_start_local=publisher_settings.window_start_local,
            window_end_local=publisher_settings.window_end_local,
        ):
            self._complete_manual_review_publish(
                task=task,
                pipeline=pipeline,
                publisher_channel=publisher_channel,
                reason_code="outside_publish_window",
                output=context_output,
                decided_at=decided_at,
            )
            return

        try:
            raw_output = self._get_ai_client().generate_text(
                model=self.settings.publisher_model,
                prompt=build_publisher_prompt(task=task, pipeline=pipeline),
                text_format=PUBLISHER_JSON_SCHEMA,
                reasoning_effort=self.settings.publisher_reasoning_effort,
            )
            category, reason = parse_publisher_output(raw_output)
        except Exception as exc:
            error = str(exc)
            self.repository.complete_publish(
                task.id,
                publisher_channel=publisher_channel,
                publisher_model=self.settings.publisher_model,
                publisher_category=None,
                publisher_decision="failed",
                publisher_reason_code="model_failed",
                publisher_output={**context_output, "error": error},
                push_result={},
                telegram_result={},
                decided_at=decided_at,
                status="publisher_failed",
                last_error=error,
            )
            self._release_local_candidate_for_task(task.id, release_reason="publisher_failed")
            raise HandledStageError(error) from exc

        should_publish = category in PUBLISHER_CATEGORY_ALLOWLIST
        push_result = self.push_client.push(
            title=pipeline.final_title,
            content=pipeline.final_content,
            dry_run=self.settings.dry_run,
            source_url=None if hide_source_url(task) else task.source_url,
            is_publish=should_publish,
            is_push=False,
        )
        push_payload = push_result.model_dump(mode="json")
        publisher_output = {
            **context_output,
            "category": category,
            "reason": reason,
            "raw_output": raw_output,
        }
        if not push_result.ok:
            error = push_result.error or "push failed"
            self.repository.complete_publish(
                task.id,
                publisher_channel=publisher_channel,
                publisher_model=self.settings.publisher_model,
                publisher_category=category,
                publisher_decision="failed",
                publisher_reason_code="push_failed",
                publisher_output=publisher_output,
                push_result=push_payload,
                telegram_result={},
                decided_at=decided_at,
                status="publisher_failed",
                last_error=error,
            )
            self._release_local_candidate_for_task(task.id, release_reason="publisher_failed")
            raise HandledStageError(error)

        telegram_result = self._send_publish_notice(task=task, pipeline=pipeline)
        self.repository.complete_publish(
            task.id,
            publisher_channel=publisher_channel,
            publisher_model=self.settings.publisher_model,
            publisher_category=category,
            publisher_decision="auto_publish" if should_publish else "manual_review",
            publisher_reason_code="category_allowed" if should_publish else "category_other",
            publisher_output=publisher_output,
            push_result=push_payload,
            telegram_result=telegram_result.model_dump(mode="json"),
            decided_at=decided_at,
            status="auto_published" if should_publish else "ready_review",
        )

    def _complete_manual_review_publish(
        self,
        *,
        task: TaskRecord,
        pipeline: PipelineRecord,
        publisher_channel: str | None,
        reason_code: str,
        output: dict[str, Any],
        decided_at: datetime,
    ) -> None:
        push_result = self.push_client.push(
            title=pipeline.final_title or task.title or "",
            content=pipeline.final_content or task.content,
            dry_run=self.settings.dry_run,
            source_url=None if hide_source_url(task) else task.source_url,
            is_publish=False,
            is_push=False,
        )
        if not push_result.ok:
            error = push_result.error or "push failed"
            self.repository.complete_publish(
                task.id,
                publisher_channel=publisher_channel,
                publisher_model=None,
                publisher_category=None,
                publisher_decision="failed",
                publisher_reason_code="push_failed",
                publisher_output=output,
                push_result=push_result.model_dump(mode="json"),
                telegram_result={},
                decided_at=decided_at,
                status="publisher_failed",
                last_error=error,
            )
            self._release_local_candidate_for_task(task.id, release_reason="publisher_failed")
            raise HandledStageError(error)
        telegram_result = self._send_publish_notice(task=task, pipeline=pipeline)
        self.repository.complete_publish(
            task.id,
            publisher_channel=publisher_channel,
            publisher_model=None,
            publisher_category=None,
            publisher_decision="manual_review",
            publisher_reason_code=reason_code,
            publisher_output=output,
            push_result=push_result.model_dump(mode="json"),
            telegram_result=telegram_result.model_dump(mode="json"),
            decided_at=decided_at,
            status="ready_review",
        )

    def _send_publish_notice(self, *, task: TaskRecord, pipeline: PipelineRecord):
        return self.telegram_client.send_message(
            build_telegram_notice(
                source=task.source,
                title=pipeline.final_title or task.title or "",
                source_url=task.source_url,
                route_label=resolve_notice_route_label(task=task, pipeline=pipeline),
                feature_mode_enabled=pipeline.writer_feature_mode_enabled,
                site_display_name=(
                    task.metadata.get("site_display_name")
                    if is_mainstream_media_task(task) or is_non_mainstream_media_task(task) or is_ai_source_task(task)
                    else None
                ),
            )
        )

    def _load_odaily_reference_documents(self, *, since: datetime) -> list[SearchDocument]:
        cache = self._search_cache()
        if cache is None:
            return self.repository.list_odaily_reference_documents(since=since)
        local_documents = cache.list_odaily_reference_documents(since=since)
        if local_documents:
            return local_documents
        remote_documents = self.repository.list_odaily_reference_documents(since=since)
        cache.upsert_documents(remote_documents)
        return remote_documents

    def _load_active_candidate_documents(self, *, exclude_task_id: int) -> list[SearchDocument]:
        cache = self._search_cache()
        if cache is None:
            documents = self.repository.list_active_candidate_documents()
        else:
            documents = cache.list_active_candidate_documents()
            if not documents:
                documents = self.repository.list_active_candidate_documents()
                cache.upsert_documents(documents)
        return [document for document in documents if document.task_id != exclude_task_id]

    def _mirror_active_candidate(self, *, task: TaskRecord, candidate_id: int) -> None:
        cache = self._search_cache()
        if cache is None:
            return
        now = datetime.now(UTC)
        cache.upsert_document(
            SearchDocument(
                doc_type="candidate",
                doc_id=str(candidate_id),
                title=task.title,
                content=task.content,
                source="candidate",
                task_id=task.id,
                candidate_id=candidate_id,
                status="active",
                created_at=now,
                updated_at=now,
                expires_at=now + ACTIVE_CANDIDATE_TTL,
                metadata={"source": task.source, "source_item_id": task.source_item_id, **task.metadata},
            )
        )

    def _release_local_candidate_for_task(self, task_id: int, *, release_reason: str) -> None:
        cache = self._search_cache()
        if cache is None:
            return
        try:
            pipeline = self.repository.get_pipeline(task_id)
        except Exception:
            return
        if pipeline.candidate_id is None:
            return
        cache.mark_document_status(
            cache_key=f"candidate:{pipeline.candidate_id}",
            status="inactive",
            expires_at=datetime.now(UTC),
            metadata_updates={
                "released_by_task_id": task_id,
                "released_by_task_status": release_reason,
            },
        )

    def _search_cache(self) -> SearchCache | None:
        return self._search_cache_store

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


def is_non_mainstream_media_task(task: TaskRecord) -> bool:
    return task.source == NON_MAINSTREAM_MEDIA_SOURCE


def is_ai_source_task(task: TaskRecord) -> bool:
    return task.source == AI_SOURCE


def is_mainstream_media_task(task: TaskRecord) -> bool:
    return task.source == MAINSTREAM_MEDIA_SOURCE


def hide_source_url(task: TaskRecord) -> bool:
    return is_competitor_task(task)


def utc_since_hours(hours: int) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def deterministic_judge_discard_type(task: TaskRecord) -> DiscardType | None:
    if is_ai_source_task(task):
        return None
    text = f"{task.title or ''}\n{task.content}"
    if AI_TOPIC_PATTERN.search(text) and not CRYPTO_CONTEXT_PATTERN.search(text):
        return "non_crypto_ai"
    return None


def competitor_judge_fallback_route(task: TaskRecord) -> JudgeRoute:
    text = f"{task.title or ''}\n{task.content}"
    if re.search(r"融资|募资|投资|领投|参投|估值|收购|并购|基金|战略轮|种子轮|A\s*轮|B\s*轮", text, re.IGNORECASE):
        return "funding"
    if re.search(
        r"链上|地址|钱包|巨鲸|转入|转出|转账|被盗|攻击|漏洞|合约|冻结|黑客|助记词|跨链桥|清算|爆仓|"
        r"USDT|USDC|BTC|ETH|SOL|BNB|Bitcoin|Ethereum",
        text,
        re.IGNORECASE,
    ):
        return "onchain"
    return "regular"


def build_writer_prompt(*, task: TaskRecord, prompt: PromptTemplateVersion) -> str:
    if is_non_mainstream_media_task(task) or is_ai_source_task(task):
        author_names = "、".join(task.metadata.get("author_names") or []) or "未知"
        is_ai_source = is_ai_source_task(task)
        site_display_name = task.metadata.get("site_display_name") or ("AI信源" if is_ai_source else "外媒")
        block_label = "待处理AI信源原文" if is_ai_source else "待处理外媒原文"
        return (
            f"{render_prompt_content(prompt)}\n\n"
            f"【{block_label}】\n"
            f"来源媒体：{site_display_name}\n"
            f"作者：{author_names}\n"
            f"原标题：{task.title or ''}\n"
            f"来源链接：{task.source_url or ''}\n"
            f"原文正文：{task.content}\n\n"
            "请严格输出一行标题、空一行、正文。不要输出解释。"
        )
    if is_mainstream_media_task(task):
        site_display_name = task.metadata.get("site_display_name") or "外媒"
        original_title = task.metadata.get("original_title") or task.title or ""
        return (
            f"{render_prompt_content(prompt)}\n\n"
            "【待处理外媒快讯】\n"
            f"来源媒体：{site_display_name}\n"
            f"原标题：{original_title}\n"
            f"来源链接：{task.source_url or ''}\n"
            f"原始快讯：{task.content}\n\n"
            "请严格输出一行标题、空一行、正文。不要输出解释。"
        )
    if is_competitor_task(task):
        return (
            f"{render_prompt_content(prompt)}\n\n"
            "【信源材料】\n"
            "来源类型：信源\n"
            f"标题：{task.title or ''}\n"
            f"正文：{task.content}\n\n"
            "禁止提及采集媒体名称，禁止提及来源平台，禁止输出解释。\n"
            "请严格输出一行标题、空一行、正文。"
        )
    author = task.metadata.get("author_display_name") or task.metadata.get("author_username") or task.title or "Odaily"
    return (
        f"{render_prompt_content(prompt)}\n\n"
        "【待处理原文】\n"
        f"发布人：{author}\n"
        f"来源链接：{task.source_url or ''}\n"
        f"原文内容：{task.content}\n\n"
        "请严格输出一行标题、空一行、正文。不要输出解释。"
    )


def build_publisher_prompt(*, task: TaskRecord, pipeline: PipelineRecord) -> str:
    return PUBLISHER_PROMPT_TEMPLATE.format(
        source=task.source,
        source_title=task.title or "",
        source_content=task.content,
        final_title=pipeline.final_title or "",
        final_content=pipeline.final_content or "",
    )


def parse_publisher_output(value: str) -> tuple[str, str]:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("publisher output must be a JSON object")
    category = str(payload.get("category") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if category not in PUBLISHER_CATEGORIES:
        raise ValueError(f"invalid publisher category: {category}")
    if not reason:
        raise ValueError("publisher reason is required")
    return category, reason


def resolve_publisher_channel(task: TaskRecord) -> str | None:
    return PUBLISHER_CHANNEL_BY_SOURCE.get(task.source)


def resolve_publisher_timezone(value: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(value or "Asia/Shanghai")
    except Exception:
        return SHANGHAI_TZ


def normalize_local_time_text(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        return "00:00"
    return text[:5] if len(text) >= 5 else text


def parse_local_time(value: str | None) -> dt_time:
    text = normalize_local_time_text(value)
    hour_text, minute_text = text.split(":", maxsplit=1)
    return dt_time(hour=int(hour_text), minute=int(minute_text))


def is_within_publish_window(*, now_local: datetime, window_start_local: str, window_end_local: str) -> bool:
    current = now_local.time().replace(second=0, microsecond=0)
    start = parse_local_time(window_start_local)
    end = parse_local_time(window_end_local)
    if start == end:
        return True
    if start < end:
        return start <= current <= end
    return current >= start or current <= end


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
    "non_mainstream_media": "外媒",
    "ai_source": "AI信源",
    "mainstream_media": "外媒",
}


def source_display_name(source: str) -> str:
    return SOURCE_DISPLAY_NAMES.get(source, source)


ROUTE_DISPLAY_NAMES = {
    "regular": "常规",
    "onchain": "链上",
    "funding": "融资",
    "non_mainstream_media": "外媒",
    "ai_source": "AI信源",
    "mainstream_media": "外媒",
}


def resolve_notice_route_label(*, task: TaskRecord, pipeline: PipelineRecord) -> str:
    if is_mainstream_media_task(task):
        return ROUTE_DISPLAY_NAMES["mainstream_media"]
    if is_ai_source_task(task):
        return ROUTE_DISPLAY_NAMES["ai_source"]
    if pipeline.news_type is None:
        return "未分类"
    return ROUTE_DISPLAY_NAMES.get(pipeline.news_type, "未分类")


def build_telegram_notice(
    *,
    source: str = "x",
    title: str,
    source_url: str | None,
    route_label: str | None = None,
    feature_mode_enabled: bool | None = None,
    site_display_name: str | None = None,
) -> str:
    display_name = site_display_name.strip() if site_display_name and site_display_name.strip() else source_display_name(source)
    normalized_route_label = route_label.strip() if route_label and route_label.strip() else "未分类"
    mode_label = "特色模式" if bool(feature_mode_enabled) else "标准模式"
    text = f"{display_name}有新快讯-{normalized_route_label}-{mode_label}：{title}"
    if source_url and source_url.strip():
        text += f"\n{source_url.strip()}"
    return text


def _search_cache_path_for_repository(repository: Any):
    paths = get_paths()
    if type(repository).__name__.startswith("Postgres"):
        return paths.searcher_cache_path
    return paths.processed_dir / "searcher" / f"test-searcher-{uuid4().hex}.sqlite"
